/**
 * algenta-tools -- Vercel AI SDK ("ai") tool integration for Algenta.
 *
 * `createAlgentaTools` builds an Algenta-aware `ToolSet` from a self-hosted Algenta engine's MCP
 * tool surface: tool-profile filtering, never-model-facing field scrubbing, and `execute_decision`'s
 * typed success/denial contract -- a typed `ExecutionReceipt` on success, or a typed
 * `ExecutionBlockedError` (carrying the engine's real named gate) thrown from `execute()` when
 * the engine's synchronous safety gate blocks the call.
 *
 * See the package README for a runnable example, and
 * `contracts/integration-tool-contract.json` in the `algenta-integrations` repository root for
 * the tool-profile contract this package conforms to.
 */

export {
  DEFAULT_PROFILE,
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

export {
  EXECUTION_GATES,
  ExecutionBlockedError,
  executionReceiptSchema,
  parseExecutionBlockedBody,
  parseExecutionReceipt,
  type ExecutionBlockedBody,
  type ExecutionGate,
  type ExecutionReceipt,
} from "./receipts.js";

export {
  ALGENTA_BASE_URL_ENV_VAR,
  DEFAULT_ALGENTA_BASE_URL,
  connectAlgentaMCPClient,
  resolveAlgentaBaseUrl,
  type ConnectAlgentaMCPClientOptions,
} from "./mcp-client.js";

export {
  createAlgentaTools,
  scrubNeverModelFacingArgs,
  stripNeverModelFacingSchema,
  type AlgentaToolSet,
  type CreateAlgentaToolsOptions,
} from "./toolset.js";
