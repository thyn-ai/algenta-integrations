/**
 * `createAlgentaTools` -- builds an Algenta-aware Vercel AI SDK `ToolSet` from a self-hosted
 * Algenta engine's MCP tool surface.
 *
 * Layers three things on top of the wrapped MCP tools' real calls:
 *
 * 1. Tool-profile filtering (`profile`): only the tool names
 *    `contracts/integration-tool-contract.json` assigns to the requested profile are exposed to
 *    the model. Defaults to `"observe"` (read-only).
 * 2. Never-model-facing scrubbing: `force`/`override_safety` are stripped from every tool's
 *    advertised JSON schema *and* from the arguments object actually forwarded to the wrapped
 *    MCP call, in every profile, for every tool -- not just `execute_decision`.
 * 3. `execute_decision`'s typed success/denial contract: a successful call is parsed into a
 *    typed `ExecutionReceipt`; a blocked call's named gate is surfaced as a typed
 *    `ExecutionBlockedError` thrown from `execute()` -- see `wrapAlgentaTool` below for the exact
 *    mapping and why. Every other tool's result is returned unchanged (they're freeform
 *    passthroughs to their own real response bodies, not this same shape).
 */
import { asSchema, jsonSchema, tool, type Tool, type ToolSet } from "ai";
import type { MCPClient } from "@ai-sdk/mcp";

import {
  DEFAULT_PROFILE,
  EXECUTE_DECISION,
  NEVER_MODEL_FACING_FIELDS,
  isToolProfile,
  resolveProfileToolNames,
  type ToolProfile,
} from "./contract.js";
import { ExecutionBlockedError, parseExecutionBlockedBody, parseExecutionReceipt } from "./receipts.js";
import { connectAlgentaMCPClient, type ConnectAlgentaMCPClientOptions } from "./mcp-client.js";

/** An Algenta-aware `ToolSet`, ready to pass as `tools:` to `generateText` / `streamText` /
 * `Agent`. */
export type AlgentaToolSet = ToolSet;

export interface CreateAlgentaToolsOptions extends ConnectAlgentaMCPClientOptions {
  /** Which tool profile to expose. Defaults to `"observe"` (read-only) -- see the contract's
   * `profiles` and the package README. */
  profile?: ToolProfile;
  /** An already-connected `@ai-sdk/mcp` `MCPClient` to wrap, instead of connecting to
   * `baseUrl`/`ALGENTA_BASE_URL` internally. The caller owns this client's lifecycle. Mutually
   * exclusive with `tools` and with the connection options (`baseUrl`/`headers`/`mcpClientConfig`). */
  client?: MCPClient;
  /** A pre-built `ToolSet` to filter/wrap instead of connecting over MCP at all -- e.g. an
   * in-memory fake registry in a test. Mirrors the sibling Python package's `wrapped=` escape
   * hatch. Mutually exclusive with `client` and with the connection options. */
  tools?: Record<string, Tool>;
}

/** Loosely-typed JSON Schema object -- just enough structure to scrub `properties`/`required`
 * without depending on a `json-schema` type package this repository doesn't otherwise need. */
interface JsonSchemaObject {
  properties?: Record<string, unknown>;
  required?: string[];
  [key: string]: unknown;
}

/**
 * Strips `force`/`override_safety` from a tool's JSON schema, if present.
 *
 * Returns the exact same object reference when neither field is present, so a tool whose schema
 * never had them to begin with isn't needlessly rebuilt.
 */
export function stripNeverModelFacingSchema(schema: JsonSchemaObject): JsonSchemaObject {
  const properties = schema.properties;
  if (!properties || !Object.keys(properties).some(key => NEVER_MODEL_FACING_FIELDS.has(key))) {
    return schema;
  }
  const newProperties: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(properties)) {
    if (!NEVER_MODEL_FACING_FIELDS.has(key)) {
      newProperties[key] = value;
    }
  }
  const newSchema: JsonSchemaObject = { ...schema, properties: newProperties };
  if (Array.isArray(schema.required)) {
    newSchema.required = schema.required.filter(name => !NEVER_MODEL_FACING_FIELDS.has(name));
  }
  return newSchema;
}

/** Removes `force`/`override_safety` from an arguments object actually being sent to the
 * wrapped MCP tool call -- the second, defense-in-depth layer behind the schema-level scrub, in
 * case a model (or a hand-rolled caller) still supplied one despite the schema not advertising
 * it. */
export function scrubNeverModelFacingArgs(args: Record<string, unknown>): Record<string, unknown> {
  if (!Object.keys(args).some(key => NEVER_MODEL_FACING_FIELDS.has(key))) {
    return args;
  }
  const scrubbed: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(args)) {
    if (!NEVER_MODEL_FACING_FIELDS.has(key)) {
      scrubbed[key] = value;
    }
  }
  return scrubbed;
}

interface McpCallToolResultLike {
  content?: Array<{ type: string; text?: string } & Record<string, unknown>>;
  structuredContent?: unknown;
  toolResult?: unknown;
  isError?: boolean;
}

/** Unwraps the actual JSON payload out of a raw MCP `CallToolResult` envelope: prefers
 * `structuredContent` (the modern, outputSchema-driven shape), falls back to parsing the first
 * text content block as JSON (the shape an MCP server without a declared output schema uses),
 * and otherwise returns the raw result unchanged. */
function extractToolPayload(callResult: unknown): unknown {
  if (callResult === null || typeof callResult !== "object") {
    return callResult;
  }
  const result = callResult as McpCallToolResultLike;
  if (Object.hasOwn(result, "toolResult") && result.toolResult !== undefined) {
    return result.toolResult;
  }
  if (result.structuredContent !== undefined) {
    return result.structuredContent;
  }
  const first = result.content?.[0];
  if (first && first.type === "text" && typeof first.text === "string") {
    try {
      return JSON.parse(first.text);
    } catch {
      return first.text;
    }
  }
  return callResult;
}

function isMcpErrorResult(callResult: unknown): boolean {
  return Boolean(
    callResult && typeof callResult === "object" && (callResult as McpCallToolResultLike).isError,
  );
}

/**
 * Wraps one source `Tool` (an MCP tool exposed by `@ai-sdk/mcp`'s `client.tools()`, or a fake in
 * a test) with never-model-facing scrubbing and `execute_decision`'s success/denial mapping.
 *
 * `execute_decision` either succeeds synchronously (a real `ExecutionReceipt`) or is blocked
 * synchronously, in the same call, naming exactly one of its three real safety gates
 * (`"idempotency"` | `"confidence"` | `"risk_floor"`) -- there is no separate pending/approval
 * state to model at all, so there's nothing here for AI SDK's pre-call `needsApproval` gate (or
 * any pause/resume primitive) to gate on; this package sets neither for `execute_decision`, same
 * as every other tool.
 *
 * A blocked call comes back over MCP as a tool-error result. That's the same native idiom every
 * other tool-level failure in this package already goes through (a thrown error, surfaced by AI
 * SDK as a `tool-error` part) -- so a denial for `execute_decision` throws an `ExecutionBlockedError`
 * carrying the engine's real `gate`/`code`/`overrideHint`, instead of a plain `Error`, letting a
 * caller branch on `error.gate` without re-parsing the message. Any other tool-error result (for
 * `execute_decision` without the named-gate body, or for any other tool) throws a plain `Error`.
 * A successful `execute_decision` call is parsed into a typed `ExecutionReceipt`; every other
 * tool's result -- including `execute_decision`'s only if it doesn't validate against the receipt
 * shape -- is returned unchanged.
 */
async function wrapAlgentaTool(name: string, sourceTool: Tool): Promise<Tool> {
  const execute = sourceTool.execute;

  const rawSchema = (await asSchema(sourceTool.inputSchema).jsonSchema) as JsonSchemaObject;
  const scrubbedSchema = stripNeverModelFacingSchema(rawSchema);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- JSONSchema7 vs our loosely
  // typed JsonSchemaObject: this repo doesn't otherwise depend on a `json-schema` type package.
  const inputSchema = jsonSchema(scrubbedSchema as any);

  if (!execute) {
    // No execute function on the source tool (schema-only / provider-executed) -- expose the
    // scrubbed schema but don't invent an execute function that doesn't exist upstream.
    return tool({
      description: sourceTool.description,
      inputSchema,
    }) as Tool;
  }

  return tool({
    description: sourceTool.description,
    inputSchema,
    execute: async (input, options) => {
      const scrubbedInput = scrubNeverModelFacingArgs((input ?? {}) as Record<string, unknown>);
      const rawResult = await execute(scrubbedInput, options);
      const payload = extractToolPayload(rawResult);

      if (isMcpErrorResult(rawResult)) {
        if (name === EXECUTE_DECISION) {
          const blocked = parseExecutionBlockedBody(payload);
          if (blocked !== null) {
            throw new ExecutionBlockedError(name, blocked);
          }
        }
        throw new Error(
          `Algenta tool '${name}' failed: ${typeof payload === "string" ? payload : JSON.stringify(payload)}`,
        );
      }

      if (name === EXECUTE_DECISION) {
        const receipt = parseExecutionReceipt(payload);
        if (receipt !== null) {
          return receipt;
        }
      }

      // Not `execute_decision`, or `execute_decision`'s result didn't validate as a receipt --
      // either way, return the tool's real payload unchanged rather than inventing a shape for
      // it (e.g. get_contract's discovery payload, or plan_decision's freeform DecisionPlan).
      return payload;
    },
  }) as Tool;
}

function connectionOptionsGiven(options: CreateAlgentaToolsOptions): boolean {
  return (
    options.client !== undefined ||
    options.baseUrl !== undefined ||
    options.headers !== undefined ||
    options.mcpClientConfig !== undefined
  );
}

/**
 * Builds an Algenta-aware `ToolSet` from a self-hosted Algenta engine's MCP tool surface,
 * filtered to `profile` (default `"observe"`) and scrubbed of never-model-facing fields.
 *
 * Connects to the caller's own self-hosted engine (`baseUrl`, `ALGENTA_BASE_URL`, or
 * `http://localhost:8000/mcp`) unless an already-connected `client` or a pre-built `tools` map is
 * supplied instead.
 */
export async function createAlgentaTools(
  options: CreateAlgentaToolsOptions = {},
): Promise<AlgentaToolSet> {
  const profile = options.profile ?? DEFAULT_PROFILE;
  if (!isToolProfile(profile)) {
    throw new Error(
      `Unknown Algenta tool profile ${JSON.stringify(profile)}. Expected one of: observe, govern, execute, full.`,
    );
  }

  if (options.tools !== undefined && connectionOptionsGiven(options)) {
    throw new Error(
      "Pass either `tools` or the MCP-connection options (`client`/`baseUrl`/`headers`/`mcpClientConfig`), not both.",
    );
  }

  let sourceTools: Record<string, Tool>;
  if (options.tools !== undefined) {
    sourceTools = options.tools;
  } else {
    const client = options.client ?? (await connectAlgentaMCPClient(options));
    sourceTools = (await client.tools()) as unknown as Record<string, Tool>;
  }

  const allowedNames = resolveProfileToolNames(profile, Object.keys(sourceTools));
  const entries = await Promise.all(
    Object.entries(sourceTools)
      .filter(([name]) => allowedNames.has(name))
      .map(async ([name, sourceTool]) => [name, await wrapAlgentaTool(name, sourceTool)] as const),
  );
  const result: AlgentaToolSet = {};
  for (const [name, wrapped] of entries) {
    result[name] = wrapped;
  }
  return result;
}
