/**
 * `GovernedExecutionReceipt` -- the typed shape of a governed Algenta MCP tool call's result.
 */
import { z } from "zod";

export const APPROVAL_STATES = ["none", "pending", "approved", "rejected", "expired"] as const;
export type ApprovalState = (typeof APPROVAL_STATES)[number];

export const NAMED_POLICY_GATE_CODES: ReadonlySet<string> = new Set([
  "plan_not_approved",
  "stale_plan",
  "plan_hash_mismatch",
  "idempotency_key_conflict",
]);

const SUCCESS_STATUSES: ReadonlySet<string> = new Set(["ok", "success"]);

/**
 * `.passthrough()` on purpose: the engine may add fields over time; a newer engine talking to an
 * older client should not fail to parse just because it sent one more field than known about.
 */
export const governedExecutionReceiptSchema = z
  .object({
    status: z.string(),
    code: z.string(),
    retryable: z.boolean().default(false),
    request_id: z.string().nullable().default(null),
    trace_id: z.string().nullable().default(null),
    policy_snapshot_hash: z.string().nullable().default(null),
    receipt_version: z.union([z.number(), z.string()]).nullable().default(null),
    plan_hash: z.string().nullable().default(null),
    approval_state: z.enum(APPROVAL_STATES).default("none"),
    execution_id: z.string().nullable().default(null),
    idempotency_key: z.string().nullable().default(null),
    result: z.unknown().default(null),
  })
  .passthrough();

export type GovernedExecutionReceipt = z.infer<typeof governedExecutionReceiptSchema>;

/**
 * Returns `null` (rather than throwing) when `raw` doesn't validate as a governed execution
 * envelope -- e.g. an object missing `status`/`code`, or a non-object value entirely. This is
 * the deliberate signal callers use to pass a non-governed tool's result (e.g. `get_contract`'s
 * discovery payload) through unchanged.
 */
export function parseReceipt(raw: unknown): GovernedExecutionReceipt | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const parsed = governedExecutionReceiptSchema.safeParse(raw);
  return parsed.success ? parsed.data : null;
}

export function isPendingApproval(receipt: GovernedExecutionReceipt): boolean {
  return receipt.approval_state === "pending";
}

export function isDenied(receipt: GovernedExecutionReceipt): boolean {
  return (
    receipt.approval_state === "rejected" ||
    receipt.approval_state === "expired" ||
    NAMED_POLICY_GATE_CODES.has(receipt.code)
  );
}

export function isSuccess(receipt: GovernedExecutionReceipt): boolean {
  return (
    (receipt.approval_state === "none" || receipt.approval_state === "approved") &&
    SUCCESS_STATUSES.has(receipt.status)
  );
}

export function denialReason(receipt: GovernedExecutionReceipt): string {
  const result = receipt.result;
  const message =
    result !== null && typeof result === "object" && !Array.isArray(result) && "message" in result
      ? (result as Record<string, unknown>).message
      : undefined;
  if (typeof message === "string" && message.length > 0) {
    return `${receipt.code}: ${message}`;
  }
  if (receipt.approval_state === "rejected") {
    return `${receipt.code}: the decision plan was rejected by policy.`;
  }
  if (receipt.approval_state === "expired") {
    return `${receipt.code}: the approval window for this decision plan expired.`;
  }
  return receipt.code;
}
