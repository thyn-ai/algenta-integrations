/**
 * Connects to the caller's own self-hosted Algenta engine over its MCP endpoint.
 *
 * `ai`'s own MCP client support (`experimental_createMCPClient` / `createMCPClient`) moved out
 * of the `ai` package itself and into the dedicated `@ai-sdk/mcp` package (see that package's
 * CHANGELOG: "feat(ai): add OAuth for MCP clients + refactor to new package") -- `ai@7.0.77`'s
 * own `dist/index.d.ts` no longer exports an MCP client at all, verified directly against the
 * installed package rather than assumed. `@ai-sdk/mcp` is the real, current home for it.
 */
import { createMCPClient, type MCPClient, type MCPClientConfig } from "@ai-sdk/mcp";

/** The default endpoint for a local self-hosted Algenta engine. Never a hosted-by-Algenta cloud
 * default -- see the package README's "Self-hosted-first" section. */
export const DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp";

export const ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL";

export interface ConnectAlgentaMCPClientOptions {
  /** The self-hosted Algenta engine's MCP endpoint. Falls back to `ALGENTA_BASE_URL`, then
   * {@link DEFAULT_ALGENTA_BASE_URL}. */
  baseUrl?: string;
  /** Additional HTTP headers to send with every request (e.g. an `Authorization` header for the
   * caller's own engine deployment). */
  headers?: Record<string, string>;
  /** Advanced: any other `@ai-sdk/mcp` `createMCPClient` option (`maxRetries`, `clientName`,
   * `onUncaughtError`, an `authProvider`, etc.), passed through unchanged. */
  mcpClientConfig?: Omit<MCPClientConfig, "transport">;
}

/** Resolves the base URL in the same order documented for `AlgentaToolset` in the sibling Python
 * package: an explicit argument, then `ALGENTA_BASE_URL`, then the local self-hosted default. */
export function resolveAlgentaBaseUrl(baseUrl?: string): string {
  if (baseUrl) {
    return baseUrl;
  }
  const fromEnv = typeof process !== "undefined" ? process.env?.[ALGENTA_BASE_URL_ENV_VAR] : undefined;
  return fromEnv || DEFAULT_ALGENTA_BASE_URL;
}

/**
 * Connects to a self-hosted Algenta engine's MCP endpoint over Streamable HTTP.
 *
 * The caller owns the returned client's lifecycle (call `.close()` when done with it) -- this
 * function only performs the connection/handshake.
 */
export async function connectAlgentaMCPClient(
  options: ConnectAlgentaMCPClientOptions = {},
): Promise<MCPClient> {
  const url = resolveAlgentaBaseUrl(options.baseUrl);
  return createMCPClient({
    transport: {
      type: "http",
      url,
      ...(options.headers ? { headers: options.headers } : {}),
    },
    ...options.mcpClientConfig,
  });
}
