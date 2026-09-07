/**
 * Try it locally -- no live Algenta Engine, no model provider, no API key.
 *
 * Runs `createAlgentaTools` against this package's own stub Algenta MCP server (the same
 * one its test suite uses -- see `../src/test-support/stub-server.ts`) to show real tool
 * listing, real tool-profile filtering, a real scrubbed `execute_decision` schema, and the
 * real `ExecutionReceipt` / `ExecutionBlockedError` shapes -- everything except an actual
 * model turn, which needs a real model provider and isn't exercised here.
 *
 * Run from a clone of this repository (see "Install from source" in the README):
 *
 *   cd typescript/algenta-tools
 *   pnpm install
 *   pnpm --filter algenta-tools example
 */
import type { ToolExecutionOptions } from "ai";

import {
  connectAlgentaMCPClient,
  createAlgentaTools,
  EXECUTE_DECISION,
  ExecutionBlockedError,
  LOG_DECISION,
} from "../src/index.js";
import { startStubAlgentaServer } from "../src/test-support/stub-server.js";

const execOptions: ToolExecutionOptions<unknown> = {
  toolCallId: "try-it-locally",
  messages: [],
  context: undefined,
};

const stub = await startStubAlgentaServer();
// One connected client, reused across profiles -- see "Advanced: bringing your own MCP
// client" in the README. The stub server (like a real MCP session) only completes one
// `initialize` handshake, so this is also the one right way to fetch more than one profile
// against a single connection.
const client = await connectAlgentaMCPClient({ baseUrl: stub.baseUrl });

try {
  const observeTools = await createAlgentaTools({ client, profile: "observe" });
  console.log('\n1. Tools in the default "observe" profile:');
  console.log("  ", Object.keys(observeTools));

  const executeTools = await createAlgentaTools({ client, profile: "execute" });
  console.log('\n2. Tools in the "execute" profile:');
  console.log("  ", Object.keys(executeTools));

  const executeDecisionSchema = executeTools[EXECUTE_DECISION]!.inputSchema;
  console.log("\n3. execute_decision's advertised schema (force/override_safety are stripped):");
  console.log("  ", JSON.stringify(executeDecisionSchema, null, 2).split("\n").join("\n   "));

  const logged = (await executeTools[LOG_DECISION]!.execute!(
    { chosen_action: "ship_it", confidence: 0.92, risk_p5: 10, expected_value: 500 },
    execOptions,
  )) as { decision_id: string };
  console.log("\n4. log_decision's real result:");
  console.log("  ", logged);

  const receipt = await executeTools[EXECUTE_DECISION]!.execute!(
    { decision_id: logged.decision_id, webhook_url: "https://example.com/hook" },
    execOptions,
  );
  console.log("\n5. execute_decision's real ExecutionReceipt:");
  console.log("  ", receipt);

  console.log("\n6. Calling execute_decision again on the same decision_id (blocked -- idempotency):");
  try {
    await executeTools[EXECUTE_DECISION]!.execute!(
      { decision_id: logged.decision_id, webhook_url: "https://example.com/hook" },
      execOptions,
    );
  } catch (error) {
    if (error instanceof ExecutionBlockedError) {
      console.log(`   Blocked on gate "${error.gate}" (code: ${error.code})`);
    } else {
      throw error;
    }
  }

  console.log("\nAll of the above came from a real MCP round trip to a real (stub) server --");
  console.log("swap `stub.baseUrl` for your own engine's URL and this is the real thing.\n");
} finally {
  await client.close();
  await stub.close();
}
