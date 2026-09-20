/**
 * A minimal, deterministic, fake self-hosted Algenta MCP server for tests.
 *
 * Duplicated (not imported) from `algenta-tools/src/test-support/stub-server.ts` -- same
 * reasoning as `../../src/receipts.ts` duplicating that package's schema: this package has no
 * dependency on `@ai-sdk/mcp`/`ai` and shouldn't gain one just to reuse a test fixture.
 *
 * This is a real `@modelcontextprotocol/sdk` `McpServer`, served over a real `node:http` socket
 * via `StreamableHTTPServerTransport` -- not an in-memory transport shortcut and not a mock of
 * `connectAlgentaMcp`'s internals. Tests exercise the real `connectAlgentaMcp` -> real
 * `@modelcontextprotocol/sdk` `Client` -> real wire -> this server round trip, so a wire-shape
 * regression (e.g. a receipt not surviving `structuredContent` round-tripping) would actually be
 * caught here, unlike a test that mocks `callTool` directly.
 *
 * Nothing here talks to any real Algenta engine -- none is reachable in this test environment.
 * `execute_decision` below is a hand-built fake shaped like the real, documented
 * `ExecutionReceipt` success shape and the real three-named-gate denial shape (see
 * `../../src/receipts.ts`); every other tool is a freeform passthrough to its own plain fake
 * payload, matching the real engine's contract that only `execute_decision` has this particular
 * success/denial shape. `admin_only_diagnostic_tool` is a test-only administrative tool (not part
 * of the real contract) representing part of a real server's wider registry that only the
 * `"full"` profile should ever see; its three `admin_only_*` siblings answer in the result shapes
 * the contract tools never use (JSON in a bare text block, a non-JSON string, no content), so the
 * client's documented fallbacks for those shapes are exercised over the real wire too.
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

/** Wraps a plain string as ONLY a text content block, with no `structuredContent` -- the shape a
 * server tool without a declared `outputSchema` uses, whether or not the text happens to be JSON
 * (the client has to find out). */
function textOnlyResult(text: string): CallToolResult {
  return { content: [{ type: "text", text }] };
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

/** Builds a fresh McpServer instance wired to the given (or a fresh) decision/gate state. Split
 * from `startStubAlgentaServer` below so a multi-session HTTP server can build one `McpServer` +
 * `StreamableHTTPServerTransport` pair per session while every pair shares the SAME `decisions`
 * map and `executeDecisionCalls` list by reference -- a `log_decision` on one session's connection
 * and the matching `execute_decision` on a LATER, separate connection (this package's node
 * reconnects per `execute()` call, same as two separate n8n nodes in one workflow would) need to
 * see the same decision, not two independent stub instances. */
export function buildStubAlgentaServer(
  decisions: Map<string, StoredDecision> = new Map(),
  executeDecisionCalls: ExecuteDecisionCall[] = [],
): StubAlgentaServer {
  const server = new McpServer({ name: "algenta-stub", version: "0.0.0-test" });

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

  // Three more wider-registry tools (full-profile-only, not in the contract), each answering in a
  // result shape the contract tools above never use, so `../../src/mcp-client.ts`'s
  // `extractStructuredOrTextContent` fallbacks are exercised over the real wire: JSON carried only
  // in a text block (a server tool with no declared `outputSchema`), a plain non-JSON string, and
  // no content blocks at all.
  server.registerTool(
    "admin_only_text_json_tool",
    { description: "Not in the contract -- JSON in a bare text block, no structuredContent." },
    async () => textOnlyResult(JSON.stringify({ ok: true, source: "text-block" })),
  );

  server.registerTool(
    "admin_only_plain_text_tool",
    { description: "Not in the contract -- a human-readable string, not JSON." },
    async () => textOnlyResult("pong"),
  );

  server.registerTool(
    "admin_only_empty_result_tool",
    { description: "Not in the contract -- a result with no content blocks." },
    async (): Promise<CallToolResult> => ({ content: [] }),
  );

  return { server, executeDecisionCalls };
}

export interface StubServerHandle {
  baseUrl: string;
  executeDecisionCalls: ExecuteDecisionCall[];
  close: () => Promise<void>;
}

/** Starts `buildStubAlgentaServer()` over a real HTTP socket on an ephemeral local port, routing
 * MULTIPLE SEQUENTIAL client sessions to their own `McpServer`/transport pair.
 *
 * `StreamableHTTPServerTransport` rejects a second `initialize` against the same instance once
 * one session has claimed it (`this._initialized && this.sessionId !== undefined` in the SDK's
 * own request handler) -- by design, since one transport instance IS one session in this SDK.
 * This package's node opens a fresh connection per `execute()` call rather than holding one open
 * across a whole workflow (the same shape two separate n8n nodes -- Log Decision, then a later
 * Execute Decision -- would produce), so a test exercising that sequence needs the server side to
 * actually support more than one session, the way a real long-running Algenta engine does. Each
 * session gets its own `McpServer` + transport pair (`buildStubAlgentaServer` accepts the SAME
 * `decisions`/`executeDecisionCalls` state for every pair, by reference), keyed by the
 * `mcp-session-id` header once the SDK assigns one via `onsessioninitialized`. */
export async function startStubAlgentaServer(): Promise<StubServerHandle> {
  const decisions = new Map<string, StoredDecision>();
  const executeDecisionCalls: ExecuteDecisionCall[] = [];
  const sessions = new Map<string, StreamableHTTPServerTransport>();

  async function createSessionTransport(): Promise<StreamableHTTPServerTransport> {
    const { server: mcpServer } = buildStubAlgentaServer(decisions, executeDecisionCalls);
    const transport: StreamableHTTPServerTransport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      onsessioninitialized: (sessionId: string) => {
        sessions.set(sessionId, transport);
      },
      onsessionclosed: (sessionId: string) => {
        sessions.delete(sessionId);
      },
    });
    await mcpServer.connect(transport);
    return transport;
  }

  const httpServer: HttpServer = createServer((req: IncomingMessage, res: ServerResponse) => {
    const headerSessionId = req.headers["mcp-session-id"];
    const sessionId = Array.isArray(headerSessionId) ? headerSessionId[0] : headerSessionId;
    const existing = sessionId ? sessions.get(sessionId) : undefined;

    const routed = existing ? Promise.resolve(existing) : createSessionTransport();
    routed
      .then(transport => transport.handleRequest(req, res))
      .catch(error => {
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
      // Closing every live connection on the HTTP server first makes each session transport's
      // standalone GET (notification-stream) socket teardown immediate, rather than waiting
      // several seconds for the client side to notice on its own -- same reasoning as the
      // single-session version of this helper, just applied to every session still open.
      httpServer.closeAllConnections();
      await Promise.all([...sessions.values()].map(transport => transport.close()));
      await new Promise<void>(resolve => httpServer.close(() => resolve()));
    },
  };
}
