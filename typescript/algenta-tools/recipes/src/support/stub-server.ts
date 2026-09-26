/**
 * A minimal, deterministic, fake self-hosted Algenta MCP server for the recipes.
 *
 * This is the same local-stub-server pattern every integration package in this repository uses:
 * a real `@modelcontextprotocol/sdk` `McpServer`, served over a real `node:http` socket via
 * `StreamableHTTPServerTransport` -- not an in-memory transport shortcut and not a mock of
 * `createAlgentaTools`'s internals. The recipes exercise the real `createAlgentaTools` -> real
 * `@ai-sdk/mcp` `MCPClient` -> real wire -> this server round trip, exactly like they would
 * against a self-hosted Algenta Engine; swap `stub.baseUrl` for a real engine URL and the same
 * recipe code is the real thing. The recipe test suites and their standalone runners
 * (`pnpm recipe:NN`) both use this, so every recipe runs with zero credentials.
 *
 * Nothing here talks to any real Algenta Engine -- none is reachable in this test environment.
 * `execute_decision` below is a hand-built fake shaped like the real, documented
 * `ExecutionReceipt` success shape and the real three-named-gate denial shape (see
 * `algenta-tools`'s `receipts.ts`); every other tool is a freeform passthrough to its own plain
 * fake payload, matching the real engine's contract that only `execute_decision` has that
 * success/denial shape.
 */
import { randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type Server as HttpServer, type ServerResponse } from "node:http";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

/** The fake policy thresholds this stub's `execute_decision` gates on, mirroring the real
 * engine's `policy.min_confidence` / `policy.risk_floor`. */
export const POLICY_MIN_CONFIDENCE = 0.5;
export const POLICY_RISK_FLOOR = 50;

/** Wraps a JSON-serializable payload as a real MCP `CallToolResult`: both a `structuredContent`
 * field and a text content block carrying the same JSON. */
function jsonResult(payload: unknown): CallToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify(payload) }],
    structuredContent: payload as Record<string, unknown>,
  };
}

/** Builds the real, synchronous denial shape: a tool-error result whose payload is
 * `{"error": {"code", "gate", "message", "override_hint"}}`. */
function blockedResult(gate: string, message: string, overrideHint: string): CallToolResult {
  return {
    isError: true,
    content: [
      {
        type: "text",
        text: JSON.stringify({
          error: { code: `execution_blocked_${gate}`, gate, message, override_hint: overrideHint },
        }),
      },
    ],
  };
}

/** Builds an object shaped exactly like the real `ExecutionReceipt`'s field list. */
function buildExecutionReceipt(options: {
  decisionId: string;
  webhookUrl: string;
  safetyOverridden?: boolean;
}): Record<string, unknown> {
  return {
    decision_id: options.decisionId,
    webhook_url: options.webhookUrl,
    execution_status: "delivered",
    response_code: 200,
    executed_at: new Date().toISOString(),
    policy_snapshot_id: "policy-snap-1",
    schema_snapshot_id: "schema-snap-1",
    manifest_version: 1,
    payload_summary: { delivered: true },
    safety_overridden: options.safetyOverridden ?? false,
  };
}

export interface ExecuteDecisionCall {
  decision_id: string;
  webhook_url: string;
  force: boolean;
  override_safety: boolean;
}

interface StoredDecision {
  chosen_action: string;
  confidence?: number;
  risk_p5?: number;
  expected_value?: number;
  delivered: boolean;
}

/** A logged decision record, as this stub's `log_decision` returns it -- the shape the real
 * engine's `log_decision` responds with (a freeform passthrough body, not a receipt). */
export interface LoggedDecision {
  decision_id: string;
  chosen_action: string;
  expected_value: number | null;
  confidence: number | null;
  created_at: string;
  note: string | null;
}

export interface StubServerHandle {
  baseUrl: string;
  /** Every `execute_decision` call's actually-received arguments, in call order -- lets tests
   * assert on what the server *received* (e.g. that a recipe's approval gate really did stop an
   * `execute_decision` call from ever being made). */
  executeDecisionCalls: ExecuteDecisionCall[];
  /** Every decision record `log_decision` persisted, in call order. */
  loggedDecisions: LoggedDecision[];
  close: () => Promise<void>;
}

/** Starts a fresh stub Algenta MCP server (its own isolated decision/gate state) over a real
 * HTTP socket on an ephemeral local port. */
export async function startStubAlgentaServer(): Promise<StubServerHandle> {
  const mcpServer = new McpServer({ name: "algenta-recipes-stub", version: "0.0.0-test" });
  const decisions = new Map<string, StoredDecision>();
  const executeDecisionCalls: ExecuteDecisionCall[] = [];
  const loggedDecisions: LoggedDecision[] = [];

  mcpServer.registerTool(
    "get_contract",
    { description: "Fake discovery payload." },
    async () =>
      jsonResult({
        capabilities: ["query", "simulate", "recommend"],
        engine_version: "1.4.0",
      }),
  );

  mcpServer.registerTool(
    "query_data",
    { description: "Fake query_data.", inputSchema: { dataset: z.string() } },
    async ({ dataset }) => jsonResult({ dataset, rows: [{ value: 1 }, { value: 2 }] }),
  );

  mcpServer.registerTool(
    "simulate",
    { description: "Fake simulate.", inputSchema: { scenario: z.string() } },
    async ({ scenario }) => jsonResult({ scenario, expected_value: 42.0 }),
  );

  mcpServer.registerTool(
    "recommend",
    { description: "Fake recommend.", inputSchema: { scenario: z.string() } },
    async ({ scenario }) => jsonResult({ scenario, recommended_action: "hold", confidence: 0.87 }),
  );

  mcpServer.registerTool(
    "plan_decision",
    {
      description: "Fake plan_decision -- freeform DecisionPlan summary.",
      inputSchema: { scenario: z.string() },
    },
    async ({ scenario }) => jsonResult({ plan_id: `plan-${scenario}`, scenario, summary: "looks fine" }),
  );

  mcpServer.registerTool(
    "log_decision",
    {
      description: "Fake log_decision -- persists a decision record, returns its decision_id.",
      inputSchema: {
        chosen_action: z.string(),
        confidence: z.number().optional(),
        risk_p5: z.number().optional(),
        expected_value: z.number().optional(),
      },
    },
    async ({ chosen_action, confidence, risk_p5, expected_value }) => {
      const record: LoggedDecision = {
        decision_id: `decision-${randomUUID()}`,
        chosen_action,
        expected_value: expected_value ?? null,
        confidence: confidence ?? null,
        created_at: new Date().toISOString(),
        note: null,
      };
      decisions.set(record.decision_id, {
        chosen_action,
        confidence,
        risk_p5,
        expected_value,
        delivered: false,
      });
      loggedDecisions.push(record);
      return jsonResult(record);
    },
  );

  mcpServer.registerTool(
    "execute_decision",
    {
      description: "Dispatch an already-logged decision for real-world execution (webhook delivery).",
      // `force`/`override_safety` are declared on this fake tool's schema on purpose, mirroring
      // the real contract's note that `execute_decision` carries operator-only fields on its
      // real schema -- `createAlgentaTools` is the thing stripping them, not this server.
      inputSchema: {
        decision_id: z.string(),
        webhook_url: z.string(),
        timeout_seconds: z.number().optional(),
        force: z.boolean().default(false),
        override_safety: z.boolean().default(false),
      },
    },
    async ({ decision_id, webhook_url, force, override_safety }) => {
      executeDecisionCalls.push({ decision_id, webhook_url, force, override_safety });
      const decision = decisions.get(decision_id);

      if (decision?.delivered && !force) {
        return blockedResult(
          "idempotency",
          "This decision has already been delivered.",
          "Override the idempotency gate for one re-execution.",
        );
      }
      if (
        decision?.confidence !== undefined &&
        decision.confidence < POLICY_MIN_CONFIDENCE &&
        !override_safety
      ) {
        return blockedResult(
          "confidence",
          `confidence ${decision.confidence} is below the policy minimum of ${POLICY_MIN_CONFIDENCE}.`,
          "Set override_safety=true to bypass the confidence gate.",
        );
      }
      if (
        decision?.risk_p5 !== undefined &&
        decision.risk_p5 < -POLICY_RISK_FLOOR &&
        !override_safety
      ) {
        return blockedResult(
          "risk_floor",
          `risk_p5 ${decision.risk_p5} is below the policy risk floor of ${-POLICY_RISK_FLOOR}.`,
          "Set override_safety=true to bypass the risk floor gate.",
        );
      }

      if (decision) {
        decision.delivered = true;
      }
      return jsonResult(
        buildExecutionReceipt({
          decisionId: decision_id,
          webhookUrl: webhook_url,
          safetyOverridden: Boolean(override_safety),
        }),
      );
    },
  );

  // Represents part of a real server's wider registry that only the "full" profile should ever
  // see -- not in the contract at all.
  mcpServer.registerTool(
    "admin_only_diagnostic_tool",
    { description: "Not in the contract -- full-profile-only diagnostic tool." },
    async () => jsonResult({ ok: true }),
  );

  // Stateful mode (a real session id generator), the officially documented common
  // configuration: the AI SDK MCP HTTP transport opens a standalone GET notification stream
  // alongside its POST requests, which the MCP SDK's stateless mode does not tolerate against
  // one shared transport instance.
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: () => randomUUID(),
  });
  await mcpServer.connect(transport);

  const httpServer: HttpServer = createServer((req: IncomingMessage, res: ServerResponse) => {
    transport.handleRequest(req, res).catch(error => {
      if (!res.headersSent) {
        res.writeHead(500, { "content-type": "application/json" }).end(
          JSON.stringify({ error: String(error) }),
        );
      }
    });
  });

  await new Promise<void>((resolve, reject) => {
    httpServer.once("error", reject);
    httpServer.listen(0, "127.0.0.1", () => resolve());
  });

  const address = httpServer.address();
  if (address === null || typeof address === "string") {
    throw new Error("Expected the stub Algenta MCP server to bind to a TCP port.");
  }
  const baseUrl = `http://127.0.0.1:${address.port}/mcp`;

  return {
    baseUrl,
    executeDecisionCalls,
    loggedDecisions,
    close: async () => {
      // Closing every live connection first makes the client's standalone GET
      // (notification-stream) socket teardown immediate instead of multi-second.
      httpServer.closeAllConnections();
      await transport.close();
      await new Promise<void>(resolve => httpServer.close(() => resolve()));
    },
  };
}
