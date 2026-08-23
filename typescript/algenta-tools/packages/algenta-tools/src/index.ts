/**
 * algenta-tools -- Vercel AI SDK ("ai") tool integration for Algenta.
 *
 * `createAlgentaTools` builds a governed-execution-aware `ToolSet` from a self-hosted Algenta
 * Engine's MCP tool surface: tool-profile filtering, never-model-facing field scrubbing, typed
 * governed-execution receipts, and a mapping of the receipt's approval/denial/failure states
 * onto AI SDK's own `needsApproval` / thrown-error primitives.
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
  APPROVAL_STATES,
  NAMED_POLICY_GATE_CODES,
  denialReason,
  governedExecutionReceiptSchema,
  isDenied,
  isPendingApproval,
  isSuccess,
  parseReceipt,
  type ApprovalState,
  type GovernedExecutionReceipt,
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
