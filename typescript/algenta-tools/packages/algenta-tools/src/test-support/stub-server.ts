/**
 * A minimal, deterministic, fake self-hosted Algenta MCP server for tests.
 *
 * This is a real `@modelcontextprotocol/sdk` `McpServer`, served over a real
 * `node:http` socket via `StreamableHTTPServerTransport` -- not an in-memory transport shortcut
 * and not a mock of `createAlgentaTools`'s internals. Tests exercise the real
 * `createAlgentaTools` -> real `@ai-sdk/mcp` `MCPClient` -> real wire -> this server round trip,
 * so a wire-shape regression (e.g. a receipt not surviving `structuredContent` round-tripping)
 * would actually be caught here, unlike a test that mocks `callTool` directly.
 *
 * Nothing here talks to any real Algenta Engine -- none is reachable in this test environment.
 * `execute_decision` below is a hand-built fake shaped like the real, documented
 * `ExecutionReceipt` success shape and the real three-named-gate denial shape (see
 * `../receipts.ts`); every other tool is a freeform passthrough to its own plain fake payload,
 * matching the real engine's contract that only `execute_decision` has this particular
 * success/denial shape. `admin_only_diagnostic_tool` is a test-only administrative tool (not part
 * of the real contract) representing part of a real server's wider registry that only the
 * `"full"` profile should ever see.
 */
import { randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type Server as HttpServer, type ServerResponse } from "node:http";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

/** A tool whose result is intentionally *not* an `ExecutionReceipt` (or any other invented
 * envelope), to exercise the passthrough path for tools that don't return one -- which, per the
 * real contract, is every tool except `execute_decision`. */
export const NON_ENVELOPE_RESULT = {
  capabilities: ["query", "simulate", "recommend"],
  engine_version: "1.4.0",
};

/** The fake policy thresholds this stub's `execute_decision` gates on, mirroring the real
 * engine's `policy.min_confidence` / `policy.risk_floor`. A `log_decision` call whose `confidence`
 * is below {@link POLICY_MIN_CONFIDENCE}, or whose `risk_p5` is below the negative of
 * {@link POLICY_RISK_FLOOR}, produces a decision that `execute_decision` blocks on the matching
 * named gate. */
export const POLICY_MIN_CONFIDENCE = 0.5;
export const POLICY_RISK_FLOOR = 50;

/** Wraps a JSON-serializable payload as a real MCP `CallToolResult`: both a `structuredContent`
 * field (the modern, outputSchema-driven shape) and a text content block carrying the same JSON
 * (the shape a server without a declared output schema uses) -- so tests exercise both
 * extraction paths a real Algenta MCP server might use. */
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
  confidence?: number;
  risk_p5?: number;
  delivered: boolean;
}

export interface StubAlgentaServer {
  server: McpServer;
  /** Every `execute_decision` call's actually-received arguments, in call order -- lets tests
   * assert on what the server *received* (not just what the client's schema advertised), per
   * the project brief's "verify this by inspecting what your stub server actually received" ask. */
  executeDecisionCalls: ExecuteDecisionCall[];
}

/** Builds a fresh stub server instance with its own isolated decision/gate state. */
export function buildStubAlgentaServer(): StubAlgentaServer {
  const server = new McpServer({ name: "algenta-stub", version: "0.0.0-test" });
  const decisions = new Map<string, StoredDecision>();
  const executeDecisionCalls: ExecuteDecisionCall[] = [];

  server.registerTool(
    "get_contract",
    { description: "Fake discovery payload." },
    async () => jsonResult(NON_ENVELOPE_RESULT),
  );

  server.registerTool(
    "query_data",
    { description: "Fake query_data.", inputSchema: { dataset: z.string() } },
    async ({ dataset }) => jsonResult({ dataset, rows: [{ value: 1 }, { value: 2 }] }),
  );

  server.registerTool(
    "simulate",
    { description: "Fake simulate.", inputSchema: { scenario: z.string() } },
    async ({ scenario }) => jsonResult({ scenario, expected_value: 42.0 }),
  );

  server.registerTool(
    "recommend",
    { description: "Fake recommend.", inputSchema: { scenario: z.string() } },
    async ({ scenario }) => jsonResult({ scenario, recommended_action: "hold", confidence: 0.87 }),
  );

  server.registerTool(
    "plan_decision",
    { description: "Fake plan_decision -- freeform DecisionPlan summary.", inputSchema: { scenario: z.string() } },
    async ({ scenario }) => jsonResult({ plan_id: `plan-${scenario}`, scenario, summary: "looks fine" }),
  );

  server.registerTool(
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
      const decision_id = `decision-${randomUUID()}`;
      decisions.set(decision_id, { confidence, risk_p5, delivered: false });
      return jsonResult({
        decision_id,
        chosen_action,
        expected_value: expected_value ?? null,
        confidence: confidence ?? null,
        created_at: new Date().toISOString(),
        note: null,
      });
    },
  );

  server.registerTool(
    "execute_decision",
    {
      description: "Dispatch an already-logged decision for real-world execution (webhook delivery).",
      // `force`/`override_safety` are declared on this fake tool's schema on purpose, mirroring
      // the real contract's note that `execute_decision` carries operator-only fields on its
      // real schema -- `createAlgentaTools` is the thing under test for stripping them, not this
      // server.
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
  server.registerTool(
    "admin_only_diagnostic_tool",
    { description: "Not in the contract -- full-profile-only diagnostic tool." },
    async () => jsonResult({ ok: true }),
  );

  return { server, executeDecisionCalls };
}

export interface StubServerHandle {
  baseUrl: string;
  executeDecisionCalls: ExecuteDecisionCall[];
  close: () => Promise<void>;
}

/** Starts `buildStubAlgentaServer()` over a real HTTP socket on an ephemeral local port. */
export async function startStubAlgentaServer(): Promise<StubServerHandle> {
  const { server: mcpServer, executeDecisionCalls } = buildStubAlgentaServer();
  // Stateful mode (a real session id generator): the AI SDK MCP HTTP transport opens a
  // standalone GET notification stream alongside its POST requests even in its legacy
  // (non-`server/discover`) initialization path, and the MCP SDK's *stateless* transport mode
  // (`sessionIdGenerator: undefined`) doesn't tolerate a concurrent GET+POST pair against one
  // shared transport instance (a real 500, verified directly against the installed SDK, not a
  // hypothetical) -- stateful mode's per-session bookkeeping is what actually supports this,
  // and is the officially documented, common configuration besides.
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
    close: async () => {
      // `transport.close()` alone waits on the client's still-open standalone GET
      // (notification-stream) socket to end on its own, which -- even after the client has
      // already called its own `.close()` -- takes several seconds to be noticed. Closing every
      // live connection on the HTTP server first makes that socket teardown immediate.
      httpServer.closeAllConnections();
      await transport.close();
      await new Promise<void>(resolve => httpServer.close(() => resolve()));
    },
  };
}
