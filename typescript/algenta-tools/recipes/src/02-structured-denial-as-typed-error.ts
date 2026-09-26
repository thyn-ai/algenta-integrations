/**
 * Recipe 02 -- Structured denial as a typed tool error.
 *
 * One of the most-asked AI SDK questions: "how do I handle a failing tool call in my route
 * handler / server action?" For Algenta's `execute_decision` the answer is typed, not stringly:
 * the engine's synchronous policy denial surfaces as an `ExecutionBlockedError` carrying the
 * real named gate (`"idempotency"` | `"confidence"` | `"risk_floor"`), the engine's own code,
 * and its operator-facing override hint. This recipe wraps the call in a discriminated-union
 * result so a caller branches on `outcome.gate` -- no message parsing, no guessing.
 *
 * Run it (zero credentials -- stub engine):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:02
 */
import { pathToFileURL } from "node:url";
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

/** The outcome of one `execute_decision` attempt: either a typed receipt, or a typed denial
 * naming the real gate that blocked it. */
export type ExecuteDecisionOutcome =
  | { ok: true; receipt: ExecutionReceipt }
  | { ok: false; gate: string; code: string; message: string; overrideHint: string | null };

/** Calls `execute_decision` directly (route-handler style, outside any model loop) and folds the
 * engine's success/denial contract into a typed union. Any failure that is *not* an Algenta
 * policy denial (network, protocol, an upstream error without a named gate) is rethrown -- it
 * must not be silently re-labeled as a policy decision. */
export async function executeDecisionSafely(options: {
  tools: AlgentaToolSet;
  decisionId: string;
  webhookUrl: string;
}): Promise<ExecuteDecisionOutcome> {
  const executeDecision = options.tools[EXECUTE_DECISION];
  if (!executeDecision?.execute) {
    throw new Error(
      "execute_decision is not in this ToolSet -- build it with profile 'execute' (or 'full') first.",
    );
  }
  try {
    const payload = await executeDecision.execute(
      { decision_id: options.decisionId, webhook_url: options.webhookUrl },
      directToolCallOptions(`direct-${options.decisionId}`),
    );
    // `createAlgentaTools` already parsed this into a receipt; parse again here so this
    // function's return type is honest even if a caller hand-built the ToolSet.
    return { ok: true, receipt: executionReceiptSchema.parse(payload) };
  } catch (error) {
    if (error instanceof ExecutionBlockedError) {
      return {
        ok: false,
        gate: error.gate,
        code: error.code,
        message: error.message,
        overrideHint: error.overrideHint,
      };
    }
    throw error;
  }
}

/** Logs a decision through the govern-tier tools and returns its `decision_id`. */
export async function logDecision(options: {
  tools: AlgentaToolSet;
  chosenAction: string;
  confidence?: number;
  riskP5?: number;
}): Promise<string> {
  const logDecisionTool = options.tools[LOG_DECISION];
  if (!logDecisionTool?.execute) {
    throw new Error("log_decision is not in this ToolSet -- build it with profile 'govern' (or higher) first.");
  }
  const result = (await logDecisionTool.execute(
    {
      chosen_action: options.chosenAction,
      ...(options.confidence !== undefined ? { confidence: options.confidence } : {}),
      ...(options.riskP5 !== undefined ? { risk_p5: options.riskP5 } : {}),
    },
    directToolCallOptions("direct-log_decision"),
  )) as { decision_id: string };
  return result.decision_id;
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:02`. Walks all three named
 * gates plus the success path against the stub engine. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client }) => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const webhookUrl = "https://example.com/hooks/orders";

    const goodId = await logDecision({ tools, chosenAction: "ship_it", confidence: 0.92, riskP5: 10 });
    const lowConfidenceId = await logDecision({ tools, chosenAction: "ship_it", confidence: 0.1 });
    const highRiskId = await logDecision({ tools, chosenAction: "ship_it", riskP5: -500 });

    console.log("1. Success path:");
    console.log("  ", await executeDecisionSafely({ tools, decisionId: goodId, webhookUrl }));

    console.log("2. Idempotency gate (same decision_id executed twice):");
    console.log("  ", await executeDecisionSafely({ tools, decisionId: goodId, webhookUrl }));

    console.log("3. Confidence gate (confidence 0.1 < policy minimum):");
    console.log("  ", await executeDecisionSafely({ tools, decisionId: lowConfidenceId, webhookUrl }));

    console.log("4. Risk-floor gate (risk_p5 -500 below the floor):");
    console.log("  ", await executeDecisionSafely({ tools, decisionId: highRiskId, webhookUrl }));
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:02`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
