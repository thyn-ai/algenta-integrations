/**
 * Recipe 03 -- Receipt-typed tool results.
 *
 * End-to-end type safety is one of the AI SDK's biggest draws. This recipe shows the Algenta
 * side of that: `execute_decision`'s result isn't an opaque blob, it's a typed
 * `ExecutionReceipt` (validated against `executionReceiptSchema` from `algenta-tools`), and the
 * receipt is what downstream code -- audit records, UI, webhooks -- keys off. Non-receipt tools
 * stay freeform passthroughs; `parseExecutionReceipt` tells the two apart without inventing a
 * shape for payloads that don't have one.
 *
 * Run it (zero credentials -- stub engine):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:03
 */
import { pathToFileURL } from "node:url";
import {
  createAlgentaTools,
  EXECUTE_DECISION,
  LOG_DECISION,
  parseExecutionReceipt,
  QUERY_DATA,
  type AlgentaToolSet,
  type ExecutionReceipt,
} from "algenta-tools";

import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";

/** A flat, display/persistence-ready audit record derived from a typed receipt -- every field
 * traceable to a real receipt field, nothing inferred. */
export interface ExecutionAuditRecord {
  decisionId: string;
  webhookUrl: string;
  status: "delivered" | "failed";
  responseCode: number | null;
  executedAt: string;
  policySnapshotId: string | null;
  schemaSnapshotId: string | null;
  manifestVersion: number | string | null;
  safetyOverridden: boolean;
}

/** Projects a typed `ExecutionReceipt` into an audit record. Pure and deterministic. */
export function buildExecutionAuditRecord(receipt: ExecutionReceipt): ExecutionAuditRecord {
  return {
    decisionId: receipt.decision_id,
    webhookUrl: receipt.webhook_url,
    status: receipt.execution_status,
    responseCode: receipt.response_code,
    executedAt: receipt.executed_at,
    policySnapshotId: receipt.policy_snapshot_id,
    schemaSnapshotId: receipt.schema_snapshot_id,
    manifestVersion: receipt.manifest_version,
    safetyOverridden: receipt.safety_overridden,
  };
}

export interface LogAndExecuteResult {
  decisionId: string;
  receipt: ExecutionReceipt;
  auditRecord: ExecutionAuditRecord;
}

/** The full happy path: `log_decision` (govern tier) -> `execute_decision` (execute tier) ->
 * typed receipt -> audit record. Throws explicitly if `execute_decision`'s payload ever stops
 * validating as a receipt, rather than passing an unexpected shape downstream. */
export async function logAndExecuteWithReceipt(options: {
  tools: AlgentaToolSet;
  chosenAction: string;
  confidence?: number;
  riskP5?: number;
  webhookUrl: string;
}): Promise<LogAndExecuteResult> {
  const logged = (await options.tools[LOG_DECISION]!.execute!(
    {
      chosen_action: options.chosenAction,
      ...(options.confidence !== undefined ? { confidence: options.confidence } : {}),
      ...(options.riskP5 !== undefined ? { risk_p5: options.riskP5 } : {}),
    },
    directToolCallOptions("direct-log_decision"),
  )) as { decision_id: string };

  const payload = await options.tools[EXECUTE_DECISION]!.execute!(
    { decision_id: logged.decision_id, webhook_url: options.webhookUrl },
    directToolCallOptions(`direct-${logged.decision_id}`),
  );
  const receipt = parseExecutionReceipt(payload);
  if (receipt === null) {
    throw new Error(
      `execute_decision returned a payload that is not an ExecutionReceipt: ${JSON.stringify(payload)}`,
    );
  }
  return { decisionId: logged.decision_id, receipt, auditRecord: buildExecutionAuditRecord(receipt) };
}

/** Narrows an arbitrary tool payload to a receipt when it is one, `null` when it isn't -- the
 * honest way to tell receipt-shaped results from every other tool's freeform passthrough. */
export function asExecutionReceipt(payload: unknown): ExecutionReceipt | null {
  return parseExecutionReceipt(payload);
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:03`. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client }) => {
    const tools = await createAlgentaTools({ client, profile: "execute" });

    const { receipt, auditRecord } = await logAndExecuteWithReceipt({
      tools,
      chosenAction: "ship_it",
      confidence: 0.92,
      riskP5: 10,
      webhookUrl: "https://example.com/hooks/orders",
    });
    console.log("Typed ExecutionReceipt:");
    console.log("  ", receipt);
    console.log("Derived audit record:");
    console.log("  ", auditRecord);

    const queryPayload = await tools[QUERY_DATA]!.execute!(
      { dataset: "orders" },
      directToolCallOptions("direct-query_data"),
    );
    console.log("query_data's freeform payload is not a receipt -- asExecutionReceipt returns:");
    console.log("  ", asExecutionReceipt(queryPayload));
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:03`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
