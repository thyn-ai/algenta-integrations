/**
 * Property-based tests (fast-check) for `execute_decision`'s typed success/denial contract:
 * `parseExecutionReceipt` / `parseExecutionBlockedBody` over arbitrary payloads, the
 * `ExecutionBlockedError` they feed, and -- through `createAlgentaTools` over an in-memory `tools`
 * map, no network -- the mapping from every MCP result envelope a server can hand back to the
 * typed receipt, or typed throw, the model ends up seeing.
 *
 * These complement the example-based `receipts.test.ts` / `toolset.test.ts`: those pin down the
 * concrete shapes, these check what must hold for EVERY payload -- totality (never throw),
 * round-trip fidelity, defaults, forward-compatible passthrough, and that corrupting any single
 * required field is enough to reject.
 */
import { jsonSchema, tool, type JSONSchema7, type Tool, type ToolExecutionOptions } from "ai";
import * as fc from "fast-check";
import { describe, expect, it } from "vitest";

import { EXECUTE_DECISION, GET_CONTRACT, LOG_DECISION, PLAN_DECISION, QUERY_DATA, RECOMMEND, SIMULATE } from "./contract.js";
import {
  EXECUTION_GATES,
  ExecutionBlockedError,
  executionReceiptSchema,
  parseExecutionBlockedBody,
  parseExecutionReceipt,
  type ExecutionBlockedBody,
  type ExecutionReceipt,
} from "./receipts.js";
import { createAlgentaTools } from "./toolset.js";

// ── Fixtures ──────────────────────────────────────────────────────────────────────────────────

const CONTRACT_TOOLS: readonly string[] = [
  GET_CONTRACT,
  QUERY_DATA,
  SIMULATE,
  RECOMMEND,
  PLAN_DECISION,
  LOG_DECISION,
  EXECUTE_DECISION,
];

/** The receipt fields the schema itself declares -- read off the zod object, not re-typed. */
const RECEIPT_KEYS: readonly string[] = Object.keys(executionReceiptSchema.shape);

/** The four fields with no default: dropping any one of them must reject the receipt. */
const REQUIRED_RECEIPT_KEYS = ["decision_id", "webhook_url", "executed_at", "execution_status"] as const;

const DENIAL_KEYS: readonly string[] = ["code", "gate", "message", "override_hint"];

/** Keys `toolset.ts`'s `extractToolPayload` reads to recognise an MCP result envelope. A bare
 * payload carrying one of these would be (correctly) treated as an envelope, so the extra
 * passthrough fields generated below never use them -- the ambiguity is the MCP shape's, not
 * the parser's. */
const ENVELOPE_KEYS: readonly string[] = ["content", "structuredContent", "toolResult", "isError"];

const noopExecOptions: ToolExecutionOptions<unknown> = {
  toolCallId: "property-call",
  messages: [],
  context: undefined,
};

// ── Arbitraries ───────────────────────────────────────────────────────────────────────────────

/** snake_case identifiers. `__proto__` is excluded on purpose: assigning it as a plain-object key
 * sets the prototype instead of a property, a JavaScript hazard unrelated to anything here. */
const identArb = fc
  .string({ unit: fc.constantFrom(..."abcdefghijklmnopqrstuvwxyz0123456789_"), minLength: 1, maxLength: 20 })
  .filter(name => name !== "__proto__");

/** JSON values with integer-only numbers and identifier keys: lossless through
 * `JSON.stringify`/`JSON.parse`, so a text-envelope round trip compares equal to the original. */
const jsonArb: fc.Arbitrary<unknown> = fc.letrec(tie => ({
  json: fc.oneof(
    { depthSize: "small" },
    fc.constant(null),
    fc.boolean(),
    fc.integer(),
    fc.string(),
    fc.array(tie("json"), { maxLength: 4 }),
    fc.dictionary(identArb, tie("json"), { maxKeys: 4, noNullPrototype: true }),
  ),
})).json;

const nullable = <T>(arb: fc.Arbitrary<T>): fc.Arbitrary<T | null> => fc.option(arb, { nil: null });

/** Values that are definitely not a string -- including `undefined`, i.e. "field missing". */
const nonStringArb: fc.Arbitrary<unknown> = fc.oneof(
  fc.constant(undefined),
  fc.constant(null),
  fc.integer(),
  fc.boolean(),
  fc.array(fc.string(), { maxLength: 2 }),
  fc.dictionary(identArb, fc.string(), { maxKeys: 2, noNullPrototype: true }),
);

/** Values that are neither an object nor an array: the parsers must reject all of them. */
const nonObjectArb: fc.Arbitrary<unknown> = fc.oneof(
  fc.string(),
  fc.double(),
  fc.boolean(),
  fc.constant(null),
  fc.constant(undefined),
  fc.bigInt(),
  fc.array(fc.anything(), { maxLength: 3 }),
);

/** A payload key that is neither a receipt field nor an envelope marker. */
const extraKeyArb = identArb.filter(key => !RECEIPT_KEYS.includes(key) && !ENVELOPE_KEYS.includes(key));

/** Fields the engine may add in a future version: they must pass through unchanged. */
const extrasArb = fc.dictionary(extraKeyArb, jsonArb, { maxKeys: 4, noNullPrototype: true });

interface RawReceipt {
  decision_id: string;
  webhook_url: string;
  executed_at: string;
  execution_status: "delivered" | "failed";
  response_code?: number | null;
  policy_snapshot_id?: string | null;
  schema_snapshot_id?: string | null;
  manifest_version?: number | string | null;
  payload_summary?: unknown;
  safety_overridden?: boolean;
}

/** A valid receipt: the four required fields always present, every defaulted field independently
 * present-or-absent, so the parser's defaults are exercised as often as its explicit values. */
const receiptArb: fc.Arbitrary<RawReceipt> = fc.record(
  {
    decision_id: fc.string(),
    webhook_url: fc.string(),
    executed_at: fc.string(),
    execution_status: fc.constantFrom("delivered", "failed"),
    response_code: nullable(fc.integer()),
    policy_snapshot_id: nullable(fc.string()),
    schema_snapshot_id: nullable(fc.string()),
    manifest_version: nullable(fc.oneof(fc.integer(), fc.string())),
    payload_summary: jsonArb,
    safety_overridden: fc.boolean(),
  },
  { requiredKeys: [...REQUIRED_RECEIPT_KEYS], noNullPrototype: true },
);

/** A valid receipt plus forward-compatible extras, as the engine would actually send it. */
const receiptWithExtrasArb = fc.tuple(receiptArb, extrasArb).map(([receipt, extras]) => ({
  raw: { ...extras, ...receipt } as Record<string, unknown>,
  receipt,
  extras,
}));

interface RawDenial {
  code: string;
  gate: string;
  message: string;
  override_hint?: string | null;
}

/** The real denial body. `gate` is drawn from the three known gates AND from arbitrary names: a
 * fourth gate this package has never heard of must surface with its real name, not fail to parse. */
const denialArb: fc.Arbitrary<RawDenial> = fc.record(
  {
    code: fc.string(),
    gate: fc.oneof(fc.constantFrom(...EXECUTION_GATES), identArb),
    message: fc.string(),
    override_hint: nullable(fc.string()),
  },
  { requiredKeys: ["code", "gate", "message"], noNullPrototype: true },
);

/** `{"error": {...}}` as it arrives over MCP, with unknown extra keys at both levels. */
const denialEnvelopeArb = fc
  .tuple(
    denialArb,
    fc.dictionary(identArb.filter(key => !DENIAL_KEYS.includes(key)), jsonArb, { maxKeys: 3, noNullPrototype: true }),
    fc.dictionary(identArb.filter(key => key !== "error"), jsonArb, { maxKeys: 2, noNullPrototype: true }),
  )
  .map(([error, errorExtras, topLevelExtras]) => ({
    envelope: { ...topLevelExtras, error: { ...errorExtras, ...error } } as Record<string, unknown>,
    error,
  }));

/** A plain JSON object that is not a receipt -- the shape of every other tool's freeform result. */
const otherPayloadArb = fc
  .dictionary(extraKeyArb, jsonArb, { maxKeys: 5, noNullPrototype: true })
  .filter(payload => parseExecutionReceipt(payload) === null && parseExecutionBlockedBody(payload) === null);

// ── parseExecutionReceipt ─────────────────────────────────────────────────────────────────────

describe("parseExecutionReceipt (properties)", () => {
  it("is total: never throws, and yields null or a plain object for anything at all", () => {
    fc.assert(
      fc.property(fc.anything(), raw => {
        const parsed = parseExecutionReceipt(raw);
        expect(parsed === null || (typeof parsed === "object" && !Array.isArray(parsed))).toBe(true);
      }),
    );
  });

  it("rejects every non-object payload", () => {
    fc.assert(
      fc.property(nonObjectArb, raw => {
        expect(parseExecutionReceipt(raw)).toBeNull();
      }),
    );
  });

  it("round-trips a valid receipt: required fields verbatim, absent fields defaulted, extras passed through", () => {
    fc.assert(
      fc.property(receiptWithExtrasArb, ({ raw, receipt, extras }) => {
        const parsed = parseExecutionReceipt(raw);
        expect(parsed).not.toBeNull();
        const out = parsed as ExecutionReceipt;

        expect(out.decision_id).toBe(receipt.decision_id);
        expect(out.webhook_url).toBe(receipt.webhook_url);
        expect(out.executed_at).toBe(receipt.executed_at);
        expect(out.execution_status).toBe(receipt.execution_status);
        expect(out.response_code).toBe(receipt.response_code ?? null);
        expect(out.policy_snapshot_id).toBe(receipt.policy_snapshot_id ?? null);
        expect(out.schema_snapshot_id).toBe(receipt.schema_snapshot_id ?? null);
        expect(out.manifest_version).toBe(receipt.manifest_version ?? null);
        expect(out.payload_summary).toEqual(receipt.payload_summary ?? null);
        expect(out.safety_overridden).toBe(receipt.safety_overridden ?? false);

        const passthrough = out as Record<string, unknown>;
        for (const [key, value] of Object.entries(extras)) {
          expect(passthrough[key]).toEqual(value);
        }
        // The parser adds exactly the schema's defaulted fields and invents nothing else.
        expect(Object.keys(out).sort()).toEqual([...new Set([...Object.keys(raw), ...RECEIPT_KEYS])].sort());
      }),
    );
  });

  it("is a fixed point: a parsed receipt re-parses to itself", () => {
    fc.assert(
      fc.property(receiptWithExtrasArb, ({ raw }) => {
        const once = parseExecutionReceipt(raw);
        expect(once).not.toBeNull();
        expect(parseExecutionReceipt(once)).toEqual(once);
      }),
    );
  });

  it("rejects a receipt missing any one of its required fields", () => {
    fc.assert(
      fc.property(receiptWithExtrasArb, fc.constantFrom(...REQUIRED_RECEIPT_KEYS), ({ raw }, field) => {
        const incomplete = { ...raw };
        delete incomplete[field];
        expect(parseExecutionReceipt(incomplete)).toBeNull();
      }),
    );
  });

  it("rejects a receipt with any one field of the wrong type", () => {
    const corruptions: ReadonlyArray<readonly [keyof RawReceipt, fc.Arbitrary<unknown>]> = [
      ["decision_id", nonStringArb],
      ["webhook_url", nonStringArb],
      ["executed_at", nonStringArb],
      [
        "execution_status",
        fc.oneof(fc.string().filter(s => s !== "delivered" && s !== "failed"), nonStringArb),
      ],
      ["response_code", fc.oneof(fc.string(), fc.boolean(), fc.constant({}), fc.constant(Number.NaN))],
      ["policy_snapshot_id", fc.oneof(fc.integer(), fc.boolean(), fc.constant({}))],
      ["schema_snapshot_id", fc.oneof(fc.integer(), fc.boolean(), fc.constant({}))],
      ["manifest_version", fc.oneof(fc.boolean(), fc.constant({}), fc.array(fc.integer(), { maxLength: 2 }))],
      ["safety_overridden", fc.oneof(fc.string(), fc.integer(), fc.constant(null), fc.constant({}))],
    ];
    const corruptionArb = fc
      .constantFrom(...corruptions)
      .chain(([field, badValueArb]) => fc.tuple(fc.constant(field), badValueArb));

    fc.assert(
      fc.property(receiptWithExtrasArb, corruptionArb, ({ raw }, [field, bad]) => {
        expect(parseExecutionReceipt({ ...raw, [field]: bad })).toBeNull();
      }),
    );
  });

  it("never mutates its input", () => {
    fc.assert(
      fc.property(receiptWithExtrasArb, ({ raw }) => {
        const before = structuredClone(raw);
        parseExecutionReceipt(raw);
        expect(raw).toEqual(before);
      }),
    );
  });
});

// ── parseExecutionBlockedBody ─────────────────────────────────────────────────────────────────

describe("parseExecutionBlockedBody (properties)", () => {
  it("is total, and only ever yields the four-field denial or null", () => {
    fc.assert(
      fc.property(fc.anything(), raw => {
        const parsed = parseExecutionBlockedBody(raw);
        if (parsed !== null) {
          expect(Object.keys(parsed).sort()).toEqual(["code", "gate", "message", "overrideHint"]);
          expect(typeof parsed.code).toBe("string");
          expect(typeof parsed.gate).toBe("string");
          expect(typeof parsed.message).toBe("string");
          expect(parsed.overrideHint === null || typeof parsed.overrideHint === "string").toBe(true);
        }
      }),
    );
  });

  it("maps a real denial envelope exactly, carrying any gate name verbatim", () => {
    fc.assert(
      fc.property(denialEnvelopeArb, ({ envelope, error }) => {
        expect(parseExecutionBlockedBody(envelope)).toEqual({
          code: error.code,
          gate: error.gate,
          message: error.message,
          overrideHint: error.override_hint ?? null,
        });
      }),
    );
  });

  it("rejects a denial with code/gate/message missing or non-string, or a non-string override_hint", () => {
    const corruptions: ReadonlyArray<readonly [keyof RawDenial, fc.Arbitrary<unknown>]> = [
      ["code", nonStringArb],
      ["gate", nonStringArb],
      ["message", nonStringArb],
      ["override_hint", fc.oneof(fc.integer(), fc.boolean(), fc.constant({}), fc.array(fc.string(), { maxLength: 2 }))],
    ];
    const corruptionArb = fc
      .constantFrom(...corruptions)
      .chain(([field, badValueArb]) => fc.tuple(fc.constant(field), badValueArb));

    fc.assert(
      fc.property(denialEnvelopeArb, corruptionArb, ({ envelope, error }, [field, bad]) => {
        expect(parseExecutionBlockedBody({ ...envelope, error: { ...error, [field]: bad } })).toBeNull();
      }),
    );
  });

  it("rejects a denial body not wrapped in `error`, and an `error` that is not an object", () => {
    fc.assert(
      fc.property(denialArb, nonObjectArb, (error, notAnObject) => {
        // The flattened body is what a generic upstream failure might look like -- not a denial.
        expect(parseExecutionBlockedBody(error)).toBeNull();
        expect(parseExecutionBlockedBody({ error: notAnObject })).toBeNull();
      }),
    );
  });

  it("never mutates its input", () => {
    fc.assert(
      fc.property(denialEnvelopeArb, ({ envelope }) => {
        const before = structuredClone(envelope);
        parseExecutionBlockedBody(envelope);
        expect(envelope).toEqual(before);
      }),
    );
  });
});

// ── ExecutionBlockedError ─────────────────────────────────────────────────────────────────────

describe("ExecutionBlockedError (properties)", () => {
  it("carries the engine's gate/code/hint verbatim and names the tool, gate and message", () => {
    fc.assert(
      fc.property(fc.string(), denialEnvelopeArb, (toolName, { envelope, error }) => {
        const body = parseExecutionBlockedBody(envelope) as ExecutionBlockedBody;
        const thrown = new ExecutionBlockedError(toolName, body);

        expect(thrown).toBeInstanceOf(Error);
        expect(thrown.name).toBe("ExecutionBlockedError");
        expect(thrown.gate).toBe(error.gate);
        expect(thrown.code).toBe(error.code);
        expect(thrown.overrideHint).toBe(error.override_hint ?? null);
        expect(thrown.message).toContain(toolName);
        expect(thrown.message).toContain(error.gate);
        expect(thrown.message).toContain(error.message);
      }),
    );
  });
});

// ── createAlgentaTools: from MCP result envelope to typed result / typed throw ────────────────

/** Every tool in the registry returns the same pre-baked MCP result. */
function registryReturning(result: unknown): Record<string, Tool> {
  const source: Record<string, Tool> = {};
  for (const name of CONTRACT_TOOLS) {
    source[name] = tool({
      description: "in-memory fake",
      inputSchema: jsonSchema({ type: "object", properties: {} } as JSONSchema7),
      execute: async () => result,
    });
  }
  return source;
}

/** The result envelopes a wrapped tool can hand back for one JSON payload: the bare payload (an
 * in-memory tool), `structuredContent` (outputSchema-driven MCP), `toolResult`, or a single text
 * content block holding the JSON (an MCP server with no output schema). All must unwrap alike. */
const successEnvelopeArb = (payload: unknown): fc.Arbitrary<unknown> =>
  fc.constantFrom(
    payload,
    { structuredContent: payload },
    { toolResult: payload },
    { content: [{ type: "text", text: JSON.stringify(payload) }] },
  );

/** The same envelopes flagged as an in-band MCP tool error. */
const errorEnvelopeArb = (payload: unknown): fc.Arbitrary<unknown> =>
  fc.constantFrom(
    { isError: true, structuredContent: payload },
    { isError: true, toolResult: payload },
    { isError: true, content: [{ type: "text", text: JSON.stringify(payload) }] },
  );

async function rejectionOf(run: () => Promise<unknown>): Promise<unknown> {
  try {
    await run();
  } catch (error) {
    return error;
  }
  throw new Error("expected the wrapped call to throw");
}

describe("createAlgentaTools maps execute_decision results (properties)", () => {
  it("returns exactly parseExecutionReceipt(payload) -- or the payload untouched -- for every success envelope", async () => {
    const payloadArb = fc.oneof(receiptWithExtrasArb.map(({ raw }) => raw as unknown), otherPayloadArb);
    await fc.assert(
      fc.asyncProperty(
        payloadArb.chain(payload => fc.tuple(fc.constant(payload), successEnvelopeArb(payload))),
        async ([payload, envelope]) => {
          const tools = await createAlgentaTools({ tools: registryReturning(envelope), profile: "execute" });
          const result = await tools[EXECUTE_DECISION]!.execute!({}, noopExecOptions);
          expect(result).toEqual(parseExecutionReceipt(payload) ?? payload);
        },
      ),
      { numRuns: 60 },
    );
  });

  it("every other tool returns its payload untouched, even one that happens to look like a receipt", async () => {
    await fc.assert(
      fc.asyncProperty(
        receiptWithExtrasArb.chain(({ raw }) => fc.tuple(fc.constant(raw), successEnvelopeArb(raw))),
        fc.constantFrom(...CONTRACT_TOOLS.filter(name => name !== EXECUTE_DECISION)),
        async ([payload, envelope], toolName) => {
          const tools = await createAlgentaTools({ tools: registryReturning(envelope), profile: "full" });
          const result = await tools[toolName]!.execute!({}, noopExecOptions);
          expect(result).toEqual(payload);
        },
      ),
      { numRuns: 60 },
    );
  });

  it("maps every denial envelope on execute_decision to an ExecutionBlockedError carrying the engine's gate/code/hint", async () => {
    await fc.assert(
      fc.asyncProperty(
        denialEnvelopeArb.chain(({ envelope, error }) => fc.tuple(fc.constant(error), errorEnvelopeArb(envelope))),
        async ([error, mcpResult]) => {
          const tools = await createAlgentaTools({ tools: registryReturning(mcpResult), profile: "execute" });
          const caught = await rejectionOf(() => tools[EXECUTE_DECISION]!.execute!({}, noopExecOptions) as Promise<unknown>);
          expect(caught).toBeInstanceOf(ExecutionBlockedError);
          const blocked = caught as ExecutionBlockedError;
          expect(blocked.gate).toBe(error.gate);
          expect(blocked.code).toBe(error.code);
          expect(blocked.overrideHint).toBe(error.override_hint ?? null);
        },
      ),
      { numRuns: 60 },
    );
  });

  it("a tool error that is not a named-gate denial throws a plain Error, never an ExecutionBlockedError", async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.oneof(fc.string(), otherPayloadArb).chain(payload => errorEnvelopeArb(payload)),
        async mcpResult => {
          const tools = await createAlgentaTools({ tools: registryReturning(mcpResult), profile: "execute" });
          const caught = await rejectionOf(() => tools[EXECUTE_DECISION]!.execute!({}, noopExecOptions) as Promise<unknown>);
          expect(caught).toBeInstanceOf(Error);
          expect(caught).not.toBeInstanceOf(ExecutionBlockedError);
          expect((caught as Error).message).toContain(`Algenta tool '${EXECUTE_DECISION}' failed`);
        },
      ),
      { numRuns: 60 },
    );
  });

  it("a named-gate denial on any OTHER tool is a plain Error: the typed mapping is execute_decision-only", async () => {
    await fc.assert(
      fc.asyncProperty(
        denialEnvelopeArb.chain(({ envelope }) => errorEnvelopeArb(envelope)),
        fc.constantFrom(...CONTRACT_TOOLS.filter(name => name !== EXECUTE_DECISION)),
        async (mcpResult, toolName) => {
          const tools = await createAlgentaTools({ tools: registryReturning(mcpResult), profile: "full" });
          const caught = await rejectionOf(() => tools[toolName]!.execute!({}, noopExecOptions) as Promise<unknown>);
          expect(caught).toBeInstanceOf(Error);
          expect(caught).not.toBeInstanceOf(ExecutionBlockedError);
          expect((caught as Error).message).toContain(`Algenta tool '${toolName}' failed`);
        },
      ),
      { numRuns: 60 },
    );
  });
});
