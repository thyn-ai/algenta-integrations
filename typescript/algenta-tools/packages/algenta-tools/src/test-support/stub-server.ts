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
 * Every tool below is a hand-built fake shaped like the real, documented governed-execution
 * envelope (see `../receipts.ts`), plus one test-only administrative tool
 * (`admin_only_diagnostic_tool`, not part of the real contract) that represents part of a real
 * server's wider registry that only the `"full"` profile should ever see.
 */
import { randomUUID } from "node:crypto";
import { createServer, type IncomingMessage, type Server as HttpServer, type ServerResponse } from "node:http";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

/** A plan whose first `execute_decision` call always comes back pending, and which becomes
 * `approval_state: "approved"` only after `_test_approve_plan` has been called for it. */
export const PENDING_PLAN_HASH = "plan-needs-approval";

/** A plan `execute_decision` always rejects outright with a named policy-gate code, regardless
 * of approval state -- simulates a stale/mismatched plan the engine refuses to run at all. */
export const REJECTED_PLAN_HASH = "plan-stale-hash";
export const REJECTED_PLAN_CODE = "stale_plan";

/** A tool whose result is intentionally *not* a governed-execution envelope, to exercise the
 * passthrough path for tools that don't return one (e.g. a real `get_contract` discovery blob). */
export const NON_ENVELOPE_RESULT = {
  capabilities: ["query", "simulate", "recommend"],
  engine_version: "1.4.0",
};

interface ReceiptOptions {
  status?: string;
  code?: string;
  retryable?: boolean;
  approvalState?: "none" | "pending" | "approved" | "rejected" | "expired";
  planHash?: string | null;
  executionId?: string | null;
  idempotencyKey?: string | null;
  result?: unknown;
}

/** Builds an object shaped exactly like `GovernedExecutionReceipt`'s real field list. */
function buildReceipt(options: ReceiptOptions = {}): Record<string, unknown> {
  const {
    status = "ok",
    code = "ok",
    retryable = false,
    approvalState = "none",
    planHash = null,
    executionId = null,
    idempotencyKey = null,
    result = null,
  } = options;
  return {
    status,
    code,
    retryable,
    request_id: `req-${code}`,
    trace_id: `trace-${code}`,
    policy_snapshot_hash: "snap-1",
    receipt_version: 1,
    plan_hash: planHash,
    approval_state: approvalState,
    execution_id: executionId,
    idempotency_key: idempotencyKey,
    result,
  };
}

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

export interface ExecuteDecisionCall {
  plan_hash: string;
  idempotency_key: string;
  force: boolean;
  override_safety: boolean;
}

export interface StubAlgentaServer {
  server: McpServer;
  /** Every `execute_decision` call's actually-received arguments, in call order -- lets tests
   * assert on what the server *received* (not just what the client's schema advertised), per
   * the project brief's "verify this by inspecting what your stub server actually received" ask. */
  executeDecisionCalls: ExecuteDecisionCall[];
}

/** Builds a fresh stub server instance with its own isolated approval state. */
export function buildStubAlgentaServer(): StubAlgentaServer {
  const server = new McpServer({ name: "algenta-stub", version: "0.0.0-test" });
  const approvedPlans = new Set<string>();
  const executeDecisionCalls: ExecuteDecisionCall[] = [];

  server.registerTool(
    "get_contract",
    { description: "Fake discovery payload -- deliberately not a governed-execution envelope." },
    async () => jsonResult(NON_ENVELOPE_RESULT),
  );

  server.registerTool(
    "query_data",
    { description: "Fake query_data.", inputSchema: { dataset: z.string() } },
    async ({ dataset }) =>
      jsonResult(buildReceipt({ result: { dataset, rows: [{ value: 1 }, { value: 2 }] } })),
  );

  server.registerTool(
    "simulate",
    { description: "Fake simulate.", inputSchema: { scenario: z.string() } },
    async ({ scenario }) =>
      jsonResult(buildReceipt({ result: { scenario, expected_value: 42.0 } })),
  );

  server.registerTool(
    "recommend",
    { description: "Fake recommend.", inputSchema: { scenario: z.string() } },
    async ({ scenario }) =>
      jsonResult(
        buildReceipt({ result: { scenario, recommended_action: "hold", confidence: 0.87 } }),
      ),
  );

  server.registerTool(
    "plan_decision",
    { description: "Fake plan_decision.", inputSchema: { scenario: z.string() } },
    async ({ scenario }) => {
      const planHash = `plan-${scenario}`;
      return jsonResult(
        buildReceipt({ planHash, result: { plan_hash: planHash, rationale: "looks fine" } }),
      );
    },
  );

  server.registerTool(
    "log_decision",
    { description: "Fake log_decision.", inputSchema: { plan_hash: z.string() } },
    async ({ plan_hash }) => jsonResult(buildReceipt({ planHash: plan_hash, result: { logged: true } })),
  );

  server.registerTool(
    "execute_decision",
    {
      description: "The safety-critical, approval-gated tool.",
      // `force`/`override_safety` are declared on this fake tool's schema on purpose, mirroring
      // the real contract's note that `execute_decision` carries operator-only fields on its
      // real schema -- `createAlgentaTools` is the thing under test for stripping them, not this
      // server.
      inputSchema: {
        plan_hash: z.string(),
        idempotency_key: z.string().default("idem-1"),
        force: z.boolean().default(false),
        override_safety: z.boolean().default(false),
      },
    },
    async ({ plan_hash, idempotency_key, force, override_safety }) => {
      executeDecisionCalls.push({ plan_hash, idempotency_key, force, override_safety });
      const executionId = `exec-${plan_hash}`;
      if (plan_hash === REJECTED_PLAN_HASH) {
        return jsonResult(
          buildReceipt({
            status: "error",
            code: REJECTED_PLAN_CODE,
            approvalState: "rejected",
            planHash: plan_hash,
            executionId,
            idempotencyKey: idempotency_key,
          }),
        );
      }
      if (approvedPlans.has(plan_hash)) {
        return jsonResult(
          buildReceipt({
            approvalState: "approved",
            planHash: plan_hash,
            executionId,
            idempotencyKey: idempotency_key,
            result: { executed: true, plan_hash },
          }),
        );
      }
      return jsonResult(
        buildReceipt({
          approvalState: "pending",
          planHash: plan_hash,
          executionId,
          idempotencyKey: idempotency_key,
        }),
      );
    },
  );

  // Test-only: stand-in for a human approving the plan via the engine's real HTTP endpoint. Not
  // part of the real Algenta MCP tool registry or the tool-profile contract.
  server.registerTool(
    "_test_approve_plan",
    { description: "Test-only approval stand-in.", inputSchema: { plan_hash: z.string() } },
    async ({ plan_hash }) => {
      approvedPlans.add(plan_hash);
      return jsonResult({ approved: true, plan_hash });
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
