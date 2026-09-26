/**
 * Standalone-runner smoke tests: every recipe's `main()` -- the exact code path its documented
 * `pnpm recipe:NN` run command takes -- must run end-to-end against a fresh stub engine,
 * zero credentials. The behavioral assertions live in each recipe's own suite; these pin
 * runnability.
 */
import { describe, expect, it } from "vitest";

import { main as recipe01 } from "./01-governed-agent.js";
import { main as recipe02 } from "./02-structured-denial-as-typed-error.js";
import { main as recipe03 } from "./03-receipt-typed-results.js";
import { main as recipe04 } from "./04-bm25-retrieval-tool.js";
import { main as recipe05 } from "./05-decision-simulation-tool.js";
import { main as recipe06 } from "./06-profile-filtered-toolset.js";
import { main as recipe07 } from "./07-streaming-governed-turns.js";
import { main as recipe08 } from "./08-approval-flow-with-receipts.js";
import { main as recipe09 } from "./09-generate-object-decision-schema.js";
import { main as recipe10 } from "./10-audit-logging-middleware.js";

const MAINS: Array<[string, () => Promise<void>]> = [
  ["recipe:01 governed agent", recipe01],
  ["recipe:02 structured denial", recipe02],
  ["recipe:03 receipt-typed results", recipe03],
  ["recipe:04 BM25 retrieval tool", recipe04],
  ["recipe:05 decision simulation tool", recipe05],
  ["recipe:06 profile-filtered toolset", recipe06],
  ["recipe:07 streaming governed turns", recipe07],
  ["recipe:08 approval flow", recipe08],
  ["recipe:09 generateObject decision schema", recipe09],
  ["recipe:10 audit logging middleware", recipe10],
];

describe("standalone runners (pnpm recipe:NN)", () => {
  for (const [name, main] of MAINS) {
    it(`${name} runs end-to-end`, async () => {
      await expect(main()).resolves.toBeUndefined();
    });
  }
});
