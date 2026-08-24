/**
 * `execute_decision` is the only Algenta MCP tool with a typed success/denial contract:
 *
 * - Success (200) returns an `ExecutionReceipt`.
 * - A blocked call returns a *synchronous* denial (surfaced over MCP as a tool-error result)
 *   naming exactly one of three real, literal safety gates: `"idempotency"`, `"confidence"`, or
 *   `"risk_floor"`. `ExecutionBlockedError` carries that gate (plus the engine's own code,
 *   message, and override hint) so a caller can branch on `error.gate` directly.
 *
 * There is no third, "pending" outcome -- a call either succeeds or is blocked, synchronously,
 * in the same call. Every other Algenta MCP tool (`plan_decision`, `log_decision`, `query_data`,
 * `simulate`, `recommend`, ...) is a freeform passthrough to its own real response body; none of
 * them share this shape, so `createAlgentaTools` doesn't try to parse their results into it.
 */
import { z } from "zod";

/** The real engine's three named, literal safety-gate codes on `execute_decision`'s synchronous
 * denial. `"idempotency"` is bypassable only via `force` (for one re-execution); `"confidence"`
 * and `"risk_floor"` only via `override_safety` -- both fields are operator/break-glass-only and
 * never model-facing (see `contract.ts`'s `NEVER_MODEL_FACING_FIELDS`). */
export const EXECUTION_GATES = ["idempotency", "confidence", "risk_floor"] as const;
export type ExecutionGate = (typeof EXECUTION_GATES)[number];

/**
 * `.passthrough()` on purpose: the engine may add fields to the receipt over time; a newer
 * engine talking to an older client should not fail to parse just because it sent one more field
 * than known about.
 */
export const executionReceiptSchema = z
  .object({
    decision_id: z.string(),
    webhook_url: z.string(),
    execution_status: z.enum(["delivered", "failed"]),
    response_code: z.number().nullable().default(null),
    executed_at: z.string(),
    policy_snapshot_id: z.string().nullable().default(null),
    schema_snapshot_id: z.string().nullable().default(null),
    manifest_version: z.union([z.number(), z.string()]).nullable().default(null),
    payload_summary: z.unknown().default(null),
    safety_overridden: z.boolean().default(false),
  })
  .passthrough();

export type ExecutionReceipt = z.infer<typeof executionReceiptSchema>;

/** Returns `null` (rather than throwing) when `raw` doesn't validate as a real `execute_decision`
 * success receipt -- the signal `createAlgentaTools` uses to fall back to returning the payload
 * unchanged. */
export function parseExecutionReceipt(raw: unknown): ExecutionReceipt | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const parsed = executionReceiptSchema.safeParse(raw);
  return parsed.success ? parsed.data : null;
}

const executionBlockedBodySchema = z.object({
  error: z
    .object({
      code: z.string(),
      // A `string`, not `z.enum(EXECUTION_GATES)`: a future fourth gate this package doesn't
      // know about yet should still surface with its real name, not fail to parse.
      gate: z.string(),
      message: z.string(),
      override_hint: z.string().nullable().default(null),
    })
    .passthrough(),
});

export interface ExecutionBlockedBody {
  code: string;
  gate: string;
  message: string;
  overrideHint: string | null;
}

/** Parses an MCP tool-error result's payload as the real `execute_decision` denial body
 * (`{"error": {"code", "gate", "message", "override_hint"}}`). Returns `null` for anything else
 * (a generic upstream failure with no named gate, or a non-object payload), so the caller can
 * fall back to a plain tool-failure error instead of inventing a gate that wasn't named. */
export function parseExecutionBlockedBody(raw: unknown): ExecutionBlockedBody | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const parsed = executionBlockedBodySchema.safeParse(raw);
  if (!parsed.success) {
    return null;
  }
  const { code, gate, message, override_hint } = parsed.data.error;
  return { code, gate, message, overrideHint: override_hint };
}

/**
 * Thrown by a wrapped `execute_decision` call when the real engine's synchronous denial blocks
 * it on one of its three named gates. AI SDK surfaces a thrown error from a tool's `execute()` as
 * a `tool-error` part -- the same native idiom every other tool-level failure in this package
 * already goes through -- so this is a normal `instanceof Error`, just one that also carries the
 * engine's own `gate`/`code`/`overrideHint` for a caller that wants to branch on them instead of
 * re-parsing `message`.
 */
export class ExecutionBlockedError extends Error {
  /** The real engine's literal gate name, e.g. `"idempotency"`. See {@link ExecutionGate} for
   * the three currently known values -- typed as `string` here (not `ExecutionGate`) so a future
   * gate this package doesn't know about yet still comes through instead of being dropped. */
  readonly gate: string;
  /** The engine's own error code, e.g. `"execution_blocked_idempotency"`. */
  readonly code: string;
  /** The engine's operator-facing hint for how a human could bypass this gate (e.g. "pass
   * force=true"). Never something this package or a model surfaces back into the model-facing
   * arguments -- see `NEVER_MODEL_FACING_FIELDS`. */
  readonly overrideHint: string | null;

  constructor(toolName: string, body: ExecutionBlockedBody) {
    super(`Algenta tool '${toolName}' was blocked by policy (gate: ${body.gate}) -- ${body.message}`);
    this.name = "ExecutionBlockedError";
    this.code = body.code;
    this.gate = body.gate;
    this.overrideHint = body.overrideHint;
  }
}
