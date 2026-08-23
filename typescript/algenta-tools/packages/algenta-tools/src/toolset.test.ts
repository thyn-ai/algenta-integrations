import { tool, type Tool, type ToolExecutionOptions } from "ai";
import { z } from "zod";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { MCPClient } from "@ai-sdk/mcp";

import {
  EXECUTE_DECISION,
  GET_CONTRACT,
  LOG_DECISION,
  PLAN_DECISION,
  QUERY_DATA,
  RECOMMEND,
  SIMULATE,
} from "./contract.js";
import { connectAlgentaMCPClient } from "./mcp-client.js";
import {
  createAlgentaTools,
  scrubNeverModelFacingArgs,
  stripNeverModelFacingSchema,
} from "./toolset.js";
import type { GovernedExecutionReceipt } from "./receipts.js";
import {
  PENDING_PLAN_HASH,
  REJECTED_PLAN_CODE,
  REJECTED_PLAN_HASH,
  startStubAlgentaServer,
  type StubServerHandle,
} from "./test-support/stub-server.js";

const noopExecOptions: ToolExecutionOptions<unknown> = {
  toolCallId: "test-call",
  messages: [],
  context: undefined,
};

// ── Profile filtering against an in-memory fake tool set (no network) ─────────────────────────

function fakeReceipt(overrides: Partial<Record<string, unknown>> = {}) {
  return { status: "ok", code: "ok", approval_state: "none", result: {}, ...overrides };
}

function fakeToolSet(): Record<string, Tool> {
  return {
    [GET_CONTRACT]: tool({
      description: "fake get_contract",
      inputSchema: z.object({}),
      execute: async () => ({ capabilities: [] }),
    }),
    [QUERY_DATA]: tool({
      description: "fake query_data",
      inputSchema: z.object({ dataset: z.string() }),
      execute: async ({ dataset }) => fakeReceipt({ result: { dataset } }),
    }),
    [SIMULATE]: tool({
      description: "fake simulate",
      inputSchema: z.object({ scenario: z.string() }),
      execute: async ({ scenario }) => fakeReceipt({ result: { scenario } }),
    }),
    [RECOMMEND]: tool({
      description: "fake recommend",
      inputSchema: z.object({ scenario: z.string() }),
      execute: async ({ scenario }) => fakeReceipt({ result: { scenario } }),
    }),
    [PLAN_DECISION]: tool({
      description: "fake plan_decision",
      inputSchema: z.object({ scenario: z.string() }),
      execute: async () => fakeReceipt({ plan_hash: "p1" }),
    }),
    [LOG_DECISION]: tool({
      description: "fake log_decision",
      inputSchema: z.object({ plan_hash: z.string() }),
      execute: async () => fakeReceipt(),
    }),
    [EXECUTE_DECISION]: tool({
      description: "fake execute_decision",
      inputSchema: z.object({
        plan_hash: z.string(),
        force: z.boolean().optional(),
        override_safety: z.boolean().optional(),
      }),
      execute: async () => fakeReceipt({ approval_state: "approved" }),
    }),
    admin_only_diagnostic_tool: tool({
      description: "not in the contract at all",
      inputSchema: z.object({}),
      execute: async () => ({ ok: true }),
    }),
  };
}

describe("profile filtering (in-memory tool set)", () => {
  it("observe (the default) exposes exactly the contract's observe tools", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet() });
    expect(Object.keys(tools).sort()).toEqual([GET_CONTRACT, QUERY_DATA, RECOMMEND, SIMULATE].sort());
  });

  it("observe never lists execute_decision, plan_decision, or log_decision", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet(), profile: "observe" });
    expect(tools[EXECUTE_DECISION]).toBeUndefined();
    expect(tools[PLAN_DECISION]).toBeUndefined();
    expect(tools[LOG_DECISION]).toBeUndefined();
  });

  it("govern adds plan_decision and log_decision but not execute_decision", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet(), profile: "govern" });
    expect(Object.keys(tools).sort()).toEqual(
      [GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION].sort(),
    );
    expect(tools[EXECUTE_DECISION]).toBeUndefined();
  });

  it("execute adds execute_decision but not the admin-only tool", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet(), profile: "execute" });
    expect(Object.keys(tools).sort()).toEqual(
      [GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION, EXECUTE_DECISION].sort(),
    );
    expect(tools.admin_only_diagnostic_tool).toBeUndefined();
  });

  it("full exposes everything the source tool set advertises", async () => {
    const source = fakeToolSet();
    const tools = await createAlgentaTools({ tools: source, profile: "full" });
    expect(Object.keys(tools).sort()).toEqual(Object.keys(source).sort());
    expect(tools.admin_only_diagnostic_tool).toBeDefined();
  });

  it("rejects an unknown profile", async () => {
    await expect(
      createAlgentaTools({ tools: fakeToolSet(), profile: "admin" as never }),
    ).rejects.toThrow(/Unknown Algenta tool profile/);
  });

  it("rejects passing both tools and connection options", async () => {
    await expect(
      createAlgentaTools({ tools: fakeToolSet(), baseUrl: "http://localhost:1/mcp" }),
    ).rejects.toThrow(/Pass either `tools` or the MCP-connection options/);
  });

  it("only exposes execute_decision with needsApproval set (never observe/govern-tier tools)", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet(), profile: "execute" });
    expect(tools[EXECUTE_DECISION]?.needsApproval).toBe(true);
    expect(tools[GET_CONTRACT]?.needsApproval).toBeUndefined();
    expect(tools[QUERY_DATA]?.needsApproval).toBeUndefined();
  });
});

// ── stripNeverModelFacingSchema / scrubNeverModelFacingArgs unit tests ─────────────────────────

describe("stripNeverModelFacingSchema", () => {
  it("removes force/override_safety from properties and required", () => {
    const schema = {
      type: "object",
      properties: {
        plan_hash: { type: "string" },
        force: { type: "boolean" },
        override_safety: { type: "boolean" },
      },
      required: ["plan_hash", "force"],
    };
    const stripped = stripNeverModelFacingSchema(schema);
    expect(stripped.properties).toEqual({ plan_hash: { type: "string" } });
    expect(stripped.required).toEqual(["plan_hash"]);
  });

  it("returns the exact same object reference when nothing needs stripping", () => {
    const schema = { type: "object", properties: { scenario: { type: "string" } } };
    expect(stripNeverModelFacingSchema(schema)).toBe(schema);
  });
});

describe("scrubNeverModelFacingArgs", () => {
  it("removes force/override_safety from an arguments object", () => {
    const scrubbed = scrubNeverModelFacingArgs({ plan_hash: "p1", force: true, override_safety: true });
    expect(scrubbed).toEqual({ plan_hash: "p1" });
  });

  it("returns the exact same object reference when there's nothing to scrub", () => {
    const args = { plan_hash: "p1" };
    expect(scrubNeverModelFacingArgs(args)).toBe(args);
  });
});

// ── End-to-end against a real stub Algenta MCP server over real HTTP ───────────────────────────

describe("createAlgentaTools against a real stub MCP server", () => {
  let stub: StubServerHandle;
  let client: MCPClient;

  beforeEach(async () => {
    stub = await startStubAlgentaServer();
    client = await connectAlgentaMCPClient({ baseUrl: stub.baseUrl });
  });

  afterEach(async () => {
    await client.close();
    await stub.close();
  });

  it("full profile exposes exactly what the stub server advertises", async () => {
    const tools = await createAlgentaTools({ client, profile: "full" });
    expect(Object.keys(tools).sort()).toEqual(
      [
        GET_CONTRACT,
        QUERY_DATA,
        SIMULATE,
        RECOMMEND,
        PLAN_DECISION,
        LOG_DECISION,
        EXECUTE_DECISION,
        "_test_approve_plan",
        "admin_only_diagnostic_tool",
      ].sort(),
    );
  });

  it("observe profile exposes exactly get_contract/query_data/simulate/recommend over the wire", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    expect(Object.keys(tools).sort()).toEqual([GET_CONTRACT, QUERY_DATA, RECOMMEND, SIMULATE].sort());
  });

  it("get_contract's non-envelope discovery payload passes through unchanged", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    const result = await tools[GET_CONTRACT]!.execute!({}, noopExecOptions);
    expect(result).toEqual({
      capabilities: ["query", "simulate", "recommend"],
      engine_version: "1.4.0",
    });
  });

  it("a governed tool's receipt comes back as a parsed, typed object on success", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    const result = (await tools[QUERY_DATA]!.execute!(
      { dataset: "orders" },
      noopExecOptions,
    )) as GovernedExecutionReceipt;
    expect(result.status).toBe("ok");
    expect(result.approval_state).toBe("none");
    expect(result.result).toEqual({ dataset: "orders", rows: [{ value: 1 }, { value: 2 }] });
  });

  it("force/override_safety are absent from execute_decision's advertised schema", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const jsonSchemaValue = (tools[EXECUTE_DECISION]!.inputSchema as { jsonSchema: unknown }).jsonSchema;
    const schema = (await jsonSchemaValue) as { properties?: Record<string, unknown>; required?: string[] };
    expect(schema.properties).not.toHaveProperty("force");
    expect(schema.properties).not.toHaveProperty("override_safety");
    expect(schema.required ?? []).not.toContain("force");
    // and the field that IS supposed to be model-facing survives untouched
    expect(schema.properties).toHaveProperty("plan_hash");
  });

  it("execute_decision has needsApproval set unconditionally", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    expect(tools[EXECUTE_DECISION]!.needsApproval).toBe(true);
  });

  it("a smuggled force/override_safety argument never reaches the wrapped MCP call", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    await tools[EXECUTE_DECISION]!.execute!(
      { plan_hash: PENDING_PLAN_HASH, idempotency_key: "idem-x", force: true, override_safety: true },
      noopExecOptions,
    ).catch(() => {
      // expected to throw (pending approval) -- we only care about what the server received
    });
    expect(stub.executeDecisionCalls).toHaveLength(1);
    expect(stub.executeDecisionCalls[0]).toEqual({
      plan_hash: PENDING_PLAN_HASH,
      idempotency_key: "idem-x",
      force: false,
      override_safety: false,
    });
  });

  it("throws a clear pending-approval error when the engine reports approval_state pending", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    await expect(
      tools[EXECUTE_DECISION]!.execute!({ plan_hash: PENDING_PLAN_HASH }, noopExecOptions),
    ).rejects.toThrow(/still pending server-side policy approval/);
  });

  it("throws a policy-denial error for a named policy-gate code", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    await expect(
      tools[EXECUTE_DECISION]!.execute!({ plan_hash: REJECTED_PLAN_HASH }, noopExecOptions),
    ).rejects.toThrow(new RegExp(`denied by policy.*${REJECTED_PLAN_CODE}`));
  });

  it("returns the receipt normally once the engine has genuinely approved the plan", async () => {
    // Approve out-of-band via the full profile's test-only tool (stands in for a human/policy
    // engine approval against the engine's real HTTP endpoint).
    const fullTools = await createAlgentaTools({ client, profile: "full" });
    await fullTools._test_approve_plan!.execute!({ plan_hash: PENDING_PLAN_HASH }, noopExecOptions);

    const tools = await createAlgentaTools({ client, profile: "execute" });
    const result = (await tools[EXECUTE_DECISION]!.execute!(
      { plan_hash: PENDING_PLAN_HASH },
      noopExecOptions,
    )) as GovernedExecutionReceipt;
    expect(result.approval_state).toBe("approved");
    expect(result.result).toEqual({ executed: true, plan_hash: PENDING_PLAN_HASH });
  });

  it("a tool the contract names but the server doesn't advertise is simply absent", async () => {
    // Sanity check on resolveProfileToolNames' documented behavior end-to-end: every contract
    // tool this stub declares is present, and nothing beyond the profile's set leaks through.
    const tools = await createAlgentaTools({ client, profile: "govern" });
    expect(Object.keys(tools).sort()).toEqual(
      [GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION].sort(),
    );
  });
});
