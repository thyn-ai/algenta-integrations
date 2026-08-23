import { describe, expect, it } from "vitest";

import {
  denialReason,
  governedExecutionReceiptSchema,
  isDenied,
  isPendingApproval,
  isSuccess,
  parseReceipt,
} from "./receipts.js";

const FULL_ENVELOPE = {
  status: "ok",
  code: "ok",
  retryable: false,
  request_id: "req-1",
  trace_id: "trace-1",
  policy_snapshot_hash: "snap-1",
  receipt_version: 1,
  plan_hash: "plan-abc",
  approval_state: "approved",
  execution_id: "exec-1",
  idempotency_key: "idem-1",
  result: { executed: true },
};

describe("parseReceipt", () => {
  it("round-trips every documented field", () => {
    const receipt = parseReceipt(FULL_ENVELOPE);
    expect(receipt).not.toBeNull();
    expect(receipt).toMatchObject(FULL_ENVELOPE);
  });

  it("tolerates unknown future fields", () => {
    const receipt = parseReceipt({ ...FULL_ENVELOPE, engine_build: "2026.08.1" });
    expect(receipt).not.toBeNull();
    expect((receipt as Record<string, unknown>).engine_build).toBe("2026.08.1");
  });

  it("returns null for a non-envelope object (missing status/code)", () => {
    expect(parseReceipt({ capabilities: ["query"], engine_version: "1.4.0" })).toBeNull();
  });

  it("returns null for non-object values", () => {
    expect(parseReceipt("plain string result")).toBeNull();
    expect(parseReceipt(null)).toBeNull();
    expect(parseReceipt(undefined)).toBeNull();
    expect(parseReceipt(["a", "list"])).toBeNull();
    expect(parseReceipt(42)).toBeNull();
  });

  it("returns a typed object for a real envelope", () => {
    const receipt = parseReceipt(FULL_ENVELOPE);
    expect(receipt?.execution_id).toBe("exec-1");
  });

  it("applies documented defaults for a minimal envelope", () => {
    const receipt = parseReceipt({ status: "ok", code: "ok" });
    expect(receipt).toMatchObject({
      status: "ok",
      code: "ok",
      retryable: false,
      request_id: null,
      trace_id: null,
      policy_snapshot_hash: null,
      receipt_version: null,
      plan_hash: null,
      approval_state: "none",
      execution_id: null,
      idempotency_key: null,
      result: null,
    });
  });
});

describe("governedExecutionReceiptSchema", () => {
  it("rejects an unknown approval_state", () => {
    const result = governedExecutionReceiptSchema.safeParse({
      ...FULL_ENVELOPE,
      approval_state: "cancelled",
    });
    expect(result.success).toBe(false);
  });
});

describe("isPendingApproval / isDenied / isSuccess", () => {
  it("is pending when approval_state is pending", () => {
    const pending = parseReceipt({ ...FULL_ENVELOPE, approval_state: "pending" })!;
    expect(isPendingApproval(pending)).toBe(true);
    expect(isDenied(pending)).toBe(false);
    expect(isSuccess(pending)).toBe(false);
  });

  it("is denied via approval_state rejected", () => {
    const rejected = parseReceipt({
      ...FULL_ENVELOPE,
      approval_state: "rejected",
      status: "error",
      code: "policy_rejected",
    })!;
    expect(isDenied(rejected)).toBe(true);
    expect(isPendingApproval(rejected)).toBe(false);
    expect(isSuccess(rejected)).toBe(false);
    expect(denialReason(rejected)).toContain("the decision plan was rejected by policy");
  });

  it("is denied via a named policy-gate code, even without approval_state rejected", () => {
    // A named 409-style gate can fire with approval_state left at "none" -- e.g. it never got as
    // far as an approval decision, because the plan_hash itself didn't match.
    const receipt = parseReceipt({
      ...FULL_ENVELOPE,
      approval_state: "none",
      status: "error",
      code: "plan_hash_mismatch",
    })!;
    expect(isDenied(receipt)).toBe(true);
  });

  it("is denied via expired approval", () => {
    const expired = parseReceipt({ ...FULL_ENVELOPE, approval_state: "expired", status: "error" })!;
    expect(isDenied(expired)).toBe(true);
    expect(denialReason(expired)).toContain("approval window");
  });

  it("is success for approval_state none or approved with an ok status", () => {
    const none = parseReceipt({ ...FULL_ENVELOPE, approval_state: "none" })!;
    const approved = parseReceipt({ ...FULL_ENVELOPE, approval_state: "approved" })!;
    expect(isSuccess(none)).toBe(true);
    expect(isSuccess(approved)).toBe(true);
  });

  it("is success for status success (not only ok)", () => {
    const receipt = parseReceipt({ ...FULL_ENVELOPE, approval_state: "none", status: "success" })!;
    expect(isSuccess(receipt)).toBe(true);
  });

  it("is not success when status is error even without a named gate", () => {
    // A generic upstream failure: not a policy denial, not pending -- just failed.
    const receipt = parseReceipt({
      ...FULL_ENVELOPE,
      approval_state: "none",
      status: "error",
      code: "upstream_timeout",
    })!;
    expect(isSuccess(receipt)).toBe(false);
    expect(isDenied(receipt)).toBe(false);
    expect(isPendingApproval(receipt)).toBe(false);
  });

  it("denial reason prefers a message embedded in result", () => {
    const receipt = parseReceipt({
      ...FULL_ENVELOPE,
      approval_state: "rejected",
      code: "plan_hash_mismatch",
      result: { message: "the plan changed after this call was issued" },
    })!;
    expect(denialReason(receipt)).toBe(
      "plan_hash_mismatch: the plan changed after this call was issued",
    );
  });
});
