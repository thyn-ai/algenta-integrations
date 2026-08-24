import { describe, expect, it } from "vitest";

import {
  ExecutionBlockedError,
  executionReceiptSchema,
  parseExecutionBlockedBody,
  parseExecutionReceipt,
} from "./receipts.js";

const FULL_RECEIPT = {
  decision_id: "decision-1",
  webhook_url: "https://example.com/hooks/decision",
  execution_status: "delivered",
  response_code: 200,
  executed_at: "2026-08-23T00:00:00.000Z",
  policy_snapshot_id: "policy-snap-1",
  schema_snapshot_id: "schema-snap-1",
  manifest_version: 1,
  payload_summary: { delivered: true },
  safety_overridden: false,
};

describe("parseExecutionReceipt", () => {
  it("round-trips every documented field", () => {
    const receipt = parseExecutionReceipt(FULL_RECEIPT);
    expect(receipt).not.toBeNull();
    expect(receipt).toMatchObject(FULL_RECEIPT);
  });

  it("tolerates unknown future fields", () => {
    const receipt = parseExecutionReceipt({ ...FULL_RECEIPT, engine_build: "2026.08.1" });
    expect(receipt).not.toBeNull();
    expect((receipt as Record<string, unknown>).engine_build).toBe("2026.08.1");
  });

  it("returns null for a non-envelope object (missing required fields)", () => {
    expect(parseExecutionReceipt({ capabilities: ["query"], engine_version: "1.4.0" })).toBeNull();
  });

  it("returns null for non-object values", () => {
    expect(parseExecutionReceipt("plain string result")).toBeNull();
    expect(parseExecutionReceipt(null)).toBeNull();
    expect(parseExecutionReceipt(undefined)).toBeNull();
    expect(parseExecutionReceipt(["a", "list"])).toBeNull();
    expect(parseExecutionReceipt(42)).toBeNull();
  });

  it("returns a typed object for a real receipt, including execution_status: failed", () => {
    const receipt = parseExecutionReceipt({ ...FULL_RECEIPT, execution_status: "failed" });
    expect(receipt?.execution_status).toBe("failed");
  });

  it("rejects an unknown execution_status", () => {
    const result = executionReceiptSchema.safeParse({ ...FULL_RECEIPT, execution_status: "pending" });
    expect(result.success).toBe(false);
  });

  it("applies documented defaults for a minimal receipt", () => {
    const receipt = parseExecutionReceipt({
      decision_id: "decision-1",
      webhook_url: "https://example.com/hooks/decision",
      execution_status: "delivered",
      executed_at: "2026-08-23T00:00:00.000Z",
    });
    expect(receipt).toMatchObject({
      response_code: null,
      policy_snapshot_id: null,
      schema_snapshot_id: null,
      manifest_version: null,
      payload_summary: null,
      safety_overridden: false,
    });
  });
});

describe("parseExecutionBlockedBody", () => {
  it("parses the real three-field denial shape", () => {
    const body = parseExecutionBlockedBody({
      error: {
        code: "execution_blocked_confidence",
        gate: "confidence",
        message: "confidence 0.2 is below the policy minimum of 0.5.",
        override_hint: "Set override_safety=true to bypass the confidence gate.",
      },
    });
    expect(body).toEqual({
      code: "execution_blocked_confidence",
      gate: "confidence",
      message: "confidence 0.2 is below the policy minimum of 0.5.",
      overrideHint: "Set override_safety=true to bypass the confidence gate.",
    });
  });

  it("defaults a missing override_hint to null", () => {
    const body = parseExecutionBlockedBody({
      error: { code: "execution_blocked_idempotency", gate: "idempotency", message: "already delivered." },
    });
    expect(body?.overrideHint).toBeNull();
  });

  it("returns null for a body with no error object", () => {
    expect(parseExecutionBlockedBody({ message: "generic failure" })).toBeNull();
  });

  it("returns null for non-object values", () => {
    expect(parseExecutionBlockedBody("plain string")).toBeNull();
    expect(parseExecutionBlockedBody(null)).toBeNull();
    expect(parseExecutionBlockedBody(undefined)).toBeNull();
  });

  it("preserves a gate name this package doesn't yet know about", () => {
    const body = parseExecutionBlockedBody({
      error: { code: "execution_blocked_future_gate", gate: "future_gate", message: "not yet known." },
    });
    expect(body?.gate).toBe("future_gate");
  });
});

describe("ExecutionBlockedError", () => {
  it("carries the real gate/code/overrideHint verbatim and is a real Error", () => {
    const error = new ExecutionBlockedError("execute_decision", {
      code: "execution_blocked_risk_floor",
      gate: "risk_floor",
      message: "risk_p5 -80 is below the policy risk floor of -50.",
      overrideHint: "Set override_safety=true to bypass the risk floor gate.",
    });
    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe("ExecutionBlockedError");
    expect(error.gate).toBe("risk_floor");
    expect(error.code).toBe("execution_blocked_risk_floor");
    expect(error.overrideHint).toBe("Set override_safety=true to bypass the risk floor gate.");
    expect(error.message).toContain("risk_floor");
    expect(error.message).toContain("risk_p5 -80 is below the policy risk floor of -50.");
  });
});
