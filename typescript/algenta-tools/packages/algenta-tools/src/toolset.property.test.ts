/**
 * Property-based tests (fast-check) for the model-facing tool surface: which tool NAMES a profile
 * lets through (`contract.ts`), which FIELDS never reach the model (`toolset.ts`'s two scrubbing
 * layers), and `createAlgentaTools` itself over an in-memory `tools` map -- so the end-to-end
 * filter is checked against the resolver for arbitrary registries, schemas and argument objects.
 * No network: every registry here is built in memory.
 *
 * These complement the example-based suites in `contract.test.ts` / `toolset.test.ts`. Those pin
 * down the concrete contract values; these check the invariants that must hold for EVERY input,
 * not just the hand-picked ones -- and shrink any violation to a minimal counterexample.
 */
import { asSchema, jsonSchema, tool, type JSONSchema7, type Tool, type ToolExecutionOptions } from "ai";
import * as fc from "fast-check";
import { describe, expect, it } from "vitest";

import {
  EXECUTE_DECISION,
  FULL_PROFILE_SENTINEL,
  GET_CONTRACT,
  LOG_DECISION,
  NEVER_MODEL_FACING_FIELDS,
  PLAN_DECISION,
  QUERY_DATA,
  RECOMMEND,
  SIMULATE,
  TOOL_PROFILES,
  isToolProfile,
  resolveProfileToolNames,
  type ToolProfile,
} from "./contract.js";
import { createAlgentaTools, scrubNeverModelFacingArgs, stripNeverModelFacingSchema } from "./toolset.js";

// ── Fixtures ──────────────────────────────────────────────────────────────────────────────────

/** Every profile the embedded contract declares, read off the module rather than re-typed here,
 * so a fifth profile automatically joins every property below. */
const PROFILES = Object.keys(TOOL_PROFILES) as ToolProfile[];

/** The contract's escalation ladder, lowest to highest: each rung must expose a superset of the
 * rung below it. Pinned against {@link PROFILES} in the first test so the two can't drift. */
const LADDER: readonly ToolProfile[] = ["observe", "govern", "execute", "full"];

const CONTRACT_TOOLS: readonly string[] = [
  GET_CONTRACT,
  QUERY_DATA,
  SIMULATE,
  RECOMMEND,
  PLAN_DECISION,
  LOG_DECISION,
  EXECUTE_DECISION,
];

const FORBIDDEN: readonly string[] = [...NEVER_MODEL_FACING_FIELDS];

const noopExecOptions: ToolExecutionOptions<unknown> = {
  toolCallId: "property-call",
  messages: [],
  context: undefined,
};

/** The contract's fixed tool set for a non-`full` profile. */
function fixedSetOf(profile: ToolProfile): ReadonlySet<string> {
  const allowed = TOOL_PROFILES[profile];
  if (allowed === FULL_PROFILE_SENTINEL) {
    throw new Error(`profile ${profile} has no fixed tool set`);
  }
  return allowed;
}

const sorted = (names: Iterable<string>): string[] => [...names].sort();

const hasForbiddenKey = (obj: Record<string, unknown> | undefined): boolean =>
  Object.keys(obj ?? {}).some(key => NEVER_MODEL_FACING_FIELDS.has(key));

// ── Arbitraries ───────────────────────────────────────────────────────────────────────────────

/** snake_case identifiers -- the shape of real MCP tool names and JSON-schema property names.
 * `__proto__` is excluded on purpose: assigning it as a plain-object key sets the prototype
 * instead of a property, a JavaScript hazard unrelated to anything under test here. */
const identArb = fc
  .string({ unit: fc.constantFrom(..."abcdefghijklmnopqrstuvwxyz0123456789_"), minLength: 1, maxLength: 20 })
  .filter(name => name !== "__proto__");

const profileArb = fc.constantFrom(...PROFILES);

/** Tool names biased toward the contract's own, so a random registry regularly contains them
 * (a registry of only unknown names would exercise nothing but the `full` passthrough). */
const toolNameArb = fc.oneof(fc.constantFrom(...CONTRACT_TOOLS), identArb);

/** What a connected engine might advertise: any mix of contract and unknown names, duplicates
 * allowed -- `resolveProfileToolNames` takes an `Iterable`, not a `Set`. */
const registryArb = fc.array(toolNameArb, { maxLength: 12 });

const forbiddenKeyArb = fc.constantFrom(...FORBIDDEN);

/** Property names weighted 3:1 toward model-facing ones, so most schemas mix both kinds. */
const propertyKeyArb = fc.oneof({ arbitrary: identArb, weight: 3 }, { arbitrary: forbiddenKeyArb, weight: 1 });

/** A small, realistic JSON-schema fragment for one property. Fresh object per draw, so
 * reference-identity assertions below are meaningful. */
const propertySchemaArb = fc.record(
  {
    type: fc.constantFrom("string", "boolean", "number", "integer", "object", "array"),
    description: fc.string(),
  },
  { requiredKeys: ["type"], noNullPrototype: true },
);

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

/** The loosely-typed JSON-schema object `stripNeverModelFacingSchema` operates on. */
interface SchemaLike {
  properties?: Record<string, unknown>;
  required?: string[];
  [key: string]: unknown;
}

/** An `execute_decision`-shaped tool schema: an object schema whose properties may include the
 * never-model-facing fields, and whose `required` -- when present -- is a subset of the declared
 * properties. That subset relation is what a well-formed JSON schema looks like (and what
 * `asSchema()` produces from a zod schema); a `required` entry with no matching property would
 * be a malformed schema, not a scrub case. */
const schemaArb: fc.Arbitrary<SchemaLike> = fc
  .record(
    {
      type: fc.constant("object"),
      properties: fc.dictionary(propertyKeyArb, propertySchemaArb, { maxKeys: 8, noNullPrototype: true }),
      description: fc.string(),
      additionalProperties: fc.boolean(),
    },
    { requiredKeys: ["type"], noNullPrototype: true },
  )
  .chain(base => {
    const declared = Object.keys(base.properties ?? {});
    return fc
      .tuple(fc.subarray(declared), fc.boolean())
      .map(([required, withRequired]) => (withRequired ? { ...base, required } : base));
  });

/** An arguments object as a model (or a hand-rolled caller) might emit it, forbidden keys included. */
const argsArb = fc.dictionary(propertyKeyArb, jsonArb, { maxKeys: 8, noNullPrototype: true });

// ── resolveProfileToolNames ───────────────────────────────────────────────────────────────────

describe("resolveProfileToolNames (properties)", () => {
  it("the ladder used below names exactly the contract's profiles", () => {
    expect(sorted(LADDER)).toEqual(sorted(PROFILES));
  });

  it("never resolves a tool the engine does not advertise", () => {
    fc.assert(
      fc.property(profileArb, registryArb, (profile, registry) => {
        const advertised = new Set(registry);
        for (const name of resolveProfileToolNames(profile, registry)) {
          expect(advertised.has(name)).toBe(true);
        }
      }),
    );
  });

  it("a non-full profile resolves exactly its fixed contract set intersected with what is advertised", () => {
    fc.assert(
      fc.property(
        profileArb.filter(profile => profile !== "full"),
        registryArb,
        (profile, registry) => {
          const advertised = new Set(registry);
          const expected = [...fixedSetOf(profile)].filter(name => advertised.has(name));
          expect(sorted(resolveProfileToolNames(profile, registry))).toEqual(sorted(expected));
        },
      ),
    );
  });

  it("full resolves exactly what the engine advertises -- no more, no less", () => {
    fc.assert(
      fc.property(registryArb, registry => {
        expect(sorted(resolveProfileToolNames("full", registry))).toEqual(sorted(new Set(registry)));
      }),
    );
  });

  it("profiles form a ladder: observe ⊆ govern ⊆ execute ⊆ full, for any registry", () => {
    fc.assert(
      fc.property(registryArb, registry => {
        const rungs = LADDER.map(profile => resolveProfileToolNames(profile, registry));
        for (let i = 1; i < rungs.length; i++) {
          const lower = rungs[i - 1];
          const higher = rungs[i];
          for (const name of lower) {
            expect(higher.has(name)).toBe(true);
          }
        }
      }),
    );
  });

  it("execute_decision reaches the model only through the execute and full profiles", () => {
    fc.assert(
      fc.property(profileArb, registryArb, (profile, registry) => {
        const resolved = resolveProfileToolNames(profile, [...registry, EXECUTE_DECISION]);
        expect(resolved.has(EXECUTE_DECISION)).toBe(profile === "execute" || profile === "full");
      }),
    );
  });

  it("observe never resolves a govern- or execute-tier tool, whatever the engine advertises", () => {
    fc.assert(
      fc.property(registryArb, registry => {
        const resolved = resolveProfileToolNames("observe", [
          ...registry,
          PLAN_DECISION,
          LOG_DECISION,
          EXECUTE_DECISION,
        ]);
        expect(resolved.has(PLAN_DECISION)).toBe(false);
        expect(resolved.has(LOG_DECISION)).toBe(false);
        expect(resolved.has(EXECUTE_DECISION)).toBe(false);
      }),
    );
  });

  it("is idempotent: resolving an already-resolved set changes nothing", () => {
    fc.assert(
      fc.property(profileArb, registryArb, (profile, registry) => {
        const once = resolveProfileToolNames(profile, registry);
        expect(sorted(resolveProfileToolNames(profile, once))).toEqual(sorted(once));
      }),
    );
  });

  it("ignores the order and multiplicity of the advertised names", () => {
    fc.assert(
      fc.property(
        profileArb,
        registryArb.chain(registry =>
          fc.tuple(
            fc.constant(registry),
            fc.shuffledSubarray(registry, { minLength: registry.length }),
            fc.subarray(registry),
          ),
        ),
        (profile, [registry, permuted, duplicates]) => {
          expect(sorted(resolveProfileToolNames(profile, [...permuted, ...duplicates]))).toEqual(
            sorted(resolveProfileToolNames(profile, registry)),
          );
        },
      ),
    );
  });
});

// ── isToolProfile ─────────────────────────────────────────────────────────────────────────────

describe("isToolProfile (properties)", () => {
  /** Arbitrary strings plus near misses: a real profile with its case, whitespace or last
   * character perturbed. Each must be rejected -- the contract is matched byte for byte. */
  const candidateArb = fc.oneof(
    fc.string(),
    profileArb,
    profileArb.map(profile => profile.toUpperCase()),
    profileArb.map(profile => ` ${profile}`),
    profileArb.map(profile => `${profile} `),
    profileArb.map(profile => profile.slice(0, -1)),
  );

  it("is total and accepts exactly the contract's profiles", () => {
    fc.assert(
      fc.property(candidateArb, value => {
        expect(isToolProfile(value)).toBe((PROFILES as string[]).includes(value));
      }),
    );
  });
});

// ── stripNeverModelFacingSchema ───────────────────────────────────────────────────────────────

describe("stripNeverModelFacingSchema (properties)", () => {
  it("the advertised schema never names a never-model-facing field, in properties or in required", () => {
    fc.assert(
      fc.property(schemaArb, schema => {
        const stripped = stripNeverModelFacingSchema(schema);
        for (const forbidden of NEVER_MODEL_FACING_FIELDS) {
          expect(stripped.properties ?? {}).not.toHaveProperty(forbidden);
          expect(stripped.required ?? []).not.toContain(forbidden);
        }
      }),
    );
  });

  it("keeps every model-facing property by reference, required in order, and every other key untouched", () => {
    fc.assert(
      fc.property(schemaArb, schema => {
        const stripped = stripNeverModelFacingSchema(schema);
        const inputProperties = schema.properties ?? {};
        const kept = Object.keys(inputProperties).filter(key => !NEVER_MODEL_FACING_FIELDS.has(key));

        expect(Object.keys(stripped.properties ?? {})).toEqual(kept);
        for (const key of kept) {
          expect((stripped.properties ?? {})[key]).toBe(inputProperties[key]);
        }

        if (schema.required === undefined) {
          expect(stripped.required).toBeUndefined();
        } else {
          expect(stripped.required).toEqual(schema.required.filter(name => !NEVER_MODEL_FACING_FIELDS.has(name)));
        }

        for (const key of Object.keys(schema)) {
          if (key !== "properties" && key !== "required") {
            expect(stripped[key]).toBe(schema[key]);
          }
        }
      }),
    );
  });

  it("is idempotent, and the second pass is a no-op by reference", () => {
    fc.assert(
      fc.property(schemaArb, schema => {
        const once = stripNeverModelFacingSchema(schema);
        expect(stripNeverModelFacingSchema(once)).toBe(once);
      }),
    );
  });

  it("returns the very same object when there is nothing to strip", () => {
    fc.assert(
      fc.property(
        schemaArb.filter(schema => !hasForbiddenKey(schema.properties)),
        schema => {
          expect(stripNeverModelFacingSchema(schema)).toBe(schema);
        },
      ),
    );
  });

  it("stripping a schema that smuggled the fields in yields the schema that never declared them", () => {
    fc.assert(
      fc.property(
        schemaArb.filter(schema => !hasForbiddenKey(schema.properties)),
        fc.dictionary(forbiddenKeyArb, propertySchemaArb, {
          minKeys: 1,
          maxKeys: FORBIDDEN.length,
          noNullPrototype: true,
        }),
        fc.boolean(),
        (clean, smuggled, alsoRequired) => {
          const polluted: SchemaLike = { ...clean, properties: { ...clean.properties, ...smuggled } };
          if (alsoRequired) {
            polluted.required = [...(clean.required ?? []), ...Object.keys(smuggled)];
          }
          // Stripping rebuilds `properties` (and `required`, when present), so a clean schema that
          // never had either key comes back with the empty container rather than no key at all.
          const expected: SchemaLike = { ...clean, properties: clean.properties ?? {} };
          if (alsoRequired) {
            expected.required = clean.required ?? [];
          }
          expect(stripNeverModelFacingSchema(polluted)).toEqual(expected);
        },
      ),
    );
  });

  it("never mutates its input", () => {
    fc.assert(
      fc.property(schemaArb, schema => {
        const before = structuredClone(schema);
        stripNeverModelFacingSchema(schema);
        expect(schema).toEqual(before);
      }),
    );
  });
});

// ── scrubNeverModelFacingArgs ─────────────────────────────────────────────────────────────────

describe("scrubNeverModelFacingArgs (properties)", () => {
  it("the forwarded arguments never carry a never-model-facing key; every other entry survives by reference", () => {
    fc.assert(
      fc.property(argsArb, args => {
        const scrubbed = scrubNeverModelFacingArgs(args);
        for (const forbidden of NEVER_MODEL_FACING_FIELDS) {
          expect(scrubbed).not.toHaveProperty(forbidden);
        }
        const kept = Object.keys(args).filter(key => !NEVER_MODEL_FACING_FIELDS.has(key));
        expect(Object.keys(scrubbed)).toEqual(kept);
        for (const key of kept) {
          expect(scrubbed[key]).toBe(args[key]);
        }
      }),
    );
  });

  it("is idempotent, and the second pass is a no-op by reference", () => {
    fc.assert(
      fc.property(argsArb, args => {
        const once = scrubNeverModelFacingArgs(args);
        expect(scrubNeverModelFacingArgs(once)).toBe(once);
      }),
    );
  });

  it("returns the very same object when there is nothing to scrub", () => {
    fc.assert(
      fc.property(
        argsArb.filter(args => !hasForbiddenKey(args)),
        args => {
          expect(scrubNeverModelFacingArgs(args)).toBe(args);
        },
      ),
    );
  });

  it("scrubbing a smuggled force/override_safety restores exactly the clean arguments", () => {
    fc.assert(
      fc.property(
        argsArb.filter(args => !hasForbiddenKey(args)),
        fc.dictionary(forbiddenKeyArb, jsonArb, { minKeys: 1, maxKeys: FORBIDDEN.length, noNullPrototype: true }),
        (clean, smuggled) => {
          expect(scrubNeverModelFacingArgs({ ...clean, ...smuggled })).toEqual(clean);
        },
      ),
    );
  });

  it("never mutates its input", () => {
    fc.assert(
      fc.property(argsArb, args => {
        const before = structuredClone(args);
        scrubNeverModelFacingArgs(args);
        expect(args).toEqual(before);
      }),
    );
  });
});

// ── createAlgentaTools over an in-memory registry ─────────────────────────────────────────────

/** A source tool that records every arguments object it is actually called with. */
function recordingTool(schema: SchemaLike, seen: Array<Record<string, unknown>>): Tool {
  return tool({
    description: "in-memory fake",
    inputSchema: jsonSchema(schema as unknown as JSONSchema7),
    execute: async input => {
      seen.push(input as Record<string, unknown>);
      return { ok: true };
    },
  });
}

/** A registry of `[name, schema]` entries with distinct names, as `client.tools()` would key it. */
const registryEntriesArb = fc.uniqueArray(fc.tuple(toolNameArb, schemaArb), {
  selector: ([name]) => name,
  maxLength: 8,
});

describe("createAlgentaTools over an in-memory registry (properties)", () => {
  it("exposes exactly the names resolveProfileToolNames allows, for any registry and profile", async () => {
    await fc.assert(
      fc.asyncProperty(profileArb, registryEntriesArb, async (profile, entries) => {
        const source: Record<string, Tool> = {};
        for (const [name, schema] of entries) {
          source[name] = recordingTool(schema, []);
        }
        const tools = await createAlgentaTools({ tools: source, profile });
        const names = entries.map(([name]) => name);
        expect(sorted(Object.keys(tools))).toEqual(sorted(resolveProfileToolNames(profile, names)));
      }),
      { numRuns: 60 },
    );
  });

  it("no exposed tool advertises a never-model-facing field, in any profile", async () => {
    await fc.assert(
      fc.asyncProperty(profileArb, registryEntriesArb, async (profile, entries) => {
        const source: Record<string, Tool> = {};
        for (const [name, schema] of entries) {
          source[name] = recordingTool(schema, []);
        }
        const tools = await createAlgentaTools({ tools: source, profile });
        for (const exposed of Object.values(tools)) {
          const advertised = (await asSchema(exposed.inputSchema).jsonSchema) as SchemaLike;
          for (const forbidden of NEVER_MODEL_FACING_FIELDS) {
            expect(advertised.properties ?? {}).not.toHaveProperty(forbidden);
            expect(advertised.required ?? []).not.toContain(forbidden);
          }
        }
      }),
      { numRuns: 60 },
    );
  });

  it("a smuggled force/override_safety never reaches the wrapped call, for any tool in any profile", async () => {
    await fc.assert(
      fc.asyncProperty(profileArb, argsArb, async (profile, args) => {
        const seen: Array<Record<string, unknown>> = [];
        const source: Record<string, Tool> = {};
        for (const name of CONTRACT_TOOLS) {
          source[name] = recordingTool({ type: "object", properties: {} }, seen);
        }
        const tools = await createAlgentaTools({ tools: source, profile });
        const exposed = Object.values(tools);
        for (const exposedTool of exposed) {
          await exposedTool.execute!(args, noopExecOptions);
        }
        expect(seen).toHaveLength(exposed.length);
        for (const forwarded of seen) {
          expect(forwarded).toEqual(scrubNeverModelFacingArgs(args));
          for (const forbidden of NEVER_MODEL_FACING_FIELDS) {
            expect(forwarded).not.toHaveProperty(forbidden);
          }
        }
      }),
      { numRuns: 60 },
    );
  });
});
