/**
 * Shared recipe plumbing: the connected-client type, a no-credential stub-backed runner, and
 * the `ToolExecutionOptions` stand-in for direct (non-loop) tool invocations.
 */
import type { ToolExecutionOptions } from "ai";
import { connectAlgentaMCPClient } from "algenta-tools";

import { startStubAlgentaServer, type StubServerHandle } from "./stub-server.js";

/** The connected `@ai-sdk/mcp` client `connectAlgentaMCPClient` returns -- typed via the
 * factory's own return type so this package doesn't take a direct dependency on `@ai-sdk/mcp`
 * just for an annotation. */
export type AlgentaMcpClient = Awaited<ReturnType<typeof connectAlgentaMCPClient>>;

export interface StubAlgentaDeps {
  client: AlgentaMcpClient;
  stub: StubServerHandle;
}

/** Starts a fresh stub Algenta MCP server + connected client, runs `fn`, and always tears both
 * down -- the shape every recipe's standalone runner and most tests share. */
export async function withStubAlgenta<T>(fn: (deps: StubAlgentaDeps) => Promise<T>): Promise<T> {
  const stub = await startStubAlgentaServer();
  const client = await connectAlgentaMCPClient({ baseUrl: stub.baseUrl });
  try {
    return await fn({ client, stub });
  } finally {
    await client.close();
    await stub.close();
  }
}

/** The `ToolExecutionOptions` the AI SDK's own loop would pass to a tool's `execute()`, for when
 * a recipe calls a governed tool directly (route-handler / server-action style) instead of from
 * inside a model loop. `toolCallId` is required by the real type; `messages` is the conversation
 * so far, empty here because a direct call has none. */
export function directToolCallOptions(toolCallId: string): ToolExecutionOptions<unknown> {
  return { toolCallId, messages: [], context: undefined };
}
