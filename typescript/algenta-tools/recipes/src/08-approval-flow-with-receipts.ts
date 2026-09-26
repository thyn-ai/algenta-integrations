/**
 * Recipe 08 -- Human-in-the-loop approval flow with receipts.
 *
 * Human approval before a consequential tool call is one of the AI SDK's most-used agent
 * patterns (chat UIs render an approve/deny prompt on the tool call). Algenta's take: a
 * decision is proposed and logged first (`log_decision`), a human reviewer approves or rejects
 * it at the application layer, and only then does `execute_decision` run -- returning the typed
 * `ExecutionReceipt` that proves what happened. Two properties make this safe rather than
 * decorative:
 *
 *   1. Human approval does NOT bypass engine policy. A low-confidence decision the human
 *      approved anyway still comes back blocked on the engine's `confidence` gate -- the only
 *      bypass is the operator-only `override_safety` field, which `createAlgentaTools` never
 *      exposes to a model.
 *   2. A rejection means the engine call is never made -- verifiable by inspecting what the
 *      engine received.
 *
 * (`createAlgentaTools` deliberately sets no AI SDK `needsApproval` flag on `execute_decision`:
 * the engine's gates are synchronous, so there is no pending state for the SDK to gate on. The
 * approval pause lives at the application layer, where the human is -- which is what this
 * recipe models.)
 *
 * Run it (zero credentials -- stub engine):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:08
 */
import { pathToFileURL } from "node:url";
import { z } from "zod";
import {
  createAlgentaTools,
  EXECUTE_DECISION,
  ExecutionBlockedError,
  executionReceiptSchema,
  LOG_DECISION,
  type AlgentaToolSet,
  type ExecutionReceipt,
} from "algenta-tools";

import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";

/** The review card a human approver sees -- the logged decision's own fields, schema-validated
 * rather than assumed. */
export interface DecisionReview {
  decisionId: string;
  chosenAction: string;
  confidence: number | null;
  expectedValue: number | null;
}

const loggedDecisionSchema = z
  .object({
    decision_id: z.string(),
    chosen_action: z.string(),
    confidence: z.number().nullable(),
    expected_value: z.number().nullable(),
  })
  .passthrough();

/** Projects a raw `log_decision` payload into a review card. Throws on a malformed payload --
 * a human must never approve a record the app couldn't read. */
export function buildDecisionReview(loggedDecision: unknown): DecisionReview {
  const parsed = loggedDecisionSchema.parse(loggedDecision);
  return {
    decisionId: parsed.decision_id,
    chosenAction: parsed.chosen_action,
    confidence: parsed.confidence,
    expectedValue: parsed.expected_value,
  };
}

/** The application's approval decision for one review card -- a UI prompt, a policy check, or
 * (in tests) a plain function. Returning false rejects; throwing rejects AND aborts. */
export type ApprovalDecision = (review: DecisionReview) => boolean | Promise<boolean>;

export type ApprovalOutcome =
  | { status: "executed"; receipt: ExecutionReceipt }
  | { status: "rejected"; review: DecisionReview }
  | { status: "blocked"; review: DecisionReview; gate: string; code: string; overrideHint: string | null };

/** The full human-in-the-loop flow: review -> human approve/reject -> (approved only) engine
 * execution -> typed receipt, or the engine's own denial if policy still blocks the call. */
export async function approveAndExecute(options: {
  tools: AlgentaToolSet;
  loggedDecision: unknown;
  webhookUrl: string;
  approve: ApprovalDecision;
}): Promise<ApprovalOutcome> {
  const review = buildDecisionReview(options.loggedDecision);

  if (!(await options.approve(review))) {
    return { status: "rejected", review };
  }

  try {
    const payload = await options.tools[EXECUTE_DECISION]!.execute!(
      { decision_id: review.decisionId, webhook_url: options.webhookUrl },
      directToolCallOptions(`direct-${review.decisionId}`),
    );
    return { status: "executed", receipt: executionReceiptSchema.parse(payload) };
  } catch (error) {
    if (error instanceof ExecutionBlockedError) {
      return {
        status: "blocked",
        review,
        gate: error.gate,
        code: error.code,
        overrideHint: error.overrideHint,
      };
    }
    throw error;
  }
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:08`. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client, stub }) => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const log = async (confidence: number) =>
      (await tools[LOG_DECISION]!.execute!(
        { chosen_action: "ship_it", confidence },
        directToolCallOptions(`direct-log-${confidence}`),
      )) as { decision_id: string };

    const goodDecision = await log(0.92);
    const riskyDecision = await log(0.1);
    const webhookUrl = "https://example.com/hooks/orders";

    // A scripted "human": approve only decisions at or above 0.8 confidence.
    const human = (review: DecisionReview) => (review.confidence ?? 0) >= 0.8;

    console.log("1. Human approves a solid decision:");
    console.log("  ", await approveAndExecute({ tools, loggedDecision: goodDecision, webhookUrl, approve: human }));

    console.log("2. Human rejects the risky one -- engine call never happens:");
    console.log("  ", await approveAndExecute({ tools, loggedDecision: riskyDecision, webhookUrl, approve: human }));

    console.log("3. Human approves the risky one anyway -- engine policy still blocks it:");
    console.log(
      "  ",
      await approveAndExecute({ tools, loggedDecision: riskyDecision, webhookUrl, approve: () => true }),
    );

    console.log(`\nEngine saw ${stub.executeDecisionCalls.length} execute_decision call(s) total (not 3).`);
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:08`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
