import { describe, expect, it } from "vitest";
import { createAlgentaTools, executionReceiptSchema, QUERY_DATA } from "algenta-tools";

import {
  asExecutionReceipt,
  buildExecutionAuditRecord,
  logAndExecuteWithReceipt,
} from "./03-receipt-typed-results.js";
import { directToolCallOptions, withStubAlgenta } from "./support/algenta.js";

describe("recipe 03 -- receipt-typed results", () => {
  it("returns a fully typed receipt and a traceable audit record", async () => {
    await withStubAlgenta(async ({ client }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });

      const { decisionId, receipt, auditRecord } = await logAndExecuteWithReceipt({
        tools,
        chosenAction: "ship_it",
        confidence: 0.92,
        riskP5: 10,
        webhookUrl: "https://example.com/hooks/orders",
      });

      expect(receipt.decision_id).toBe(decisionId);
      expect(receipt.execution_status).toBe("delivered");
      expect(receipt.response_code).toBe(200);
      expect(receipt.safety_overridden).toBe(false);

      // Every audit field is a direct projection of a receipt field -- traceable, no inference.
      expect(auditRecord).toEqual({
        decisionId,
        webhookUrl: "https://example.com/hooks/orders",
        status: "delivered",
        responseCode: 200,
        executedAt: receipt.executed_at,
        policySnapshotId: "policy-snap-1",
        schemaSnapshotId: "schema-snap-1",
        manifestVersion: 1,
        safetyOverridden: false,
      });
    });
  });

  it("keeps unknown future receipt fields (the schema is passthrough on purpose)", () => {
    const parsed = executionReceiptSchema.parse({
      decision_id: "decision-1",
      webhook_url: "https://example.com/h",
      execution_status: "delivered",
      executed_at: "2026-09-26T00:00:00.000Z",
      future_engine_field: { nested: true },
    });
    expect((parsed as Record<string, unknown>).future_engine_field).toEqual({ nested: true });
  });

  it("does not invent a receipt shape for other tools' freeform payloads", async () => {
    await withStubAlgenta(async ({ client }) => {
      const tools = await createAlgentaTools({ client, profile: "observe" });
      const queryPayload = await tools[QUERY_DATA]!.execute!(
        { dataset: "orders" },
        directToolCallOptions("direct-query_data"),
      );

      expect(asExecutionReceipt(queryPayload)).toBeNull();
      expect(asExecutionReceipt("not an object")).toBeNull();
      expect(asExecutionReceipt(null)).toBeNull();
      expect(asExecutionReceipt([{ decision_id: "x" }])).toBeNull();
    });
  });

  it("buildExecutionAuditRecord maps a failed-delivery status verbatim", () => {
    const record = buildExecutionAuditRecord(
      executionReceiptSchema.parse({
        decision_id: "decision-2",
        webhook_url: "https://example.com/h",
        execution_status: "failed",
        response_code: 503,
        executed_at: "2026-09-26T00:00:00.000Z",
        safety_overridden: true,
      }),
    );
    expect(record.status).toBe("failed");
    expect(record.responseCode).toBe(503);
    expect(record.safetyOverridden).toBe(true);
    expect(record.policySnapshotId).toBeNull();
    expect(record.manifestVersion).toBeNull();
  });
});
