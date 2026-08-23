/**
 * check-parity.ts — verify every TypeScript integration package exposes the
 * tool-profile contract in contracts/integration-tool-contract.json.
 *
 * Run via `npx --yes tsx@4 scripts/check-parity.ts` (that's what
 * .github/workflows/ci.yml does) — this file is plain TypeScript with no
 * build step of its own, and `tsx` is what strips/transpiles it on the fly
 * without requiring a Node version new enough for native type-stripping.
 *
 * STATUS: stub. There is nothing to check yet — typescript/algenta-tools is
 * a placeholder package with no tool-exposing code (see its README.md).
 * This script exists now so:
 *
 *   1. CI has a stable command to run from day one, rather than a new
 *      workflow step being added later alongside the first real package.
 *   2. The shape of what D1+ needs to implement is written down here, not
 *      just described in prose (see check-parity.py for the Python-side
 *      twin of this same TODO).
 *
 * TODO(D1+): once typescript/algenta-tools (or a later package) actually
 * registers tools with a framework (Vercel AI SDK, LangChain.js, etc.), this
 * script should:
 *   - import each package's tool-registration entrypoint
 *   - ask it, for each profile in the contract ("observe", "govern",
 *     "execute", "full"), which tool names it would expose
 *   - diff that set against contracts/integration-tool-contract.json's
 *     "tools"/"adds_tools" for that profile (inherited via "extends")
 *   - fail if there's any addition, omission, or mismatched default profile
 *   - fail if 'execute' is reachable without the package demonstrating that
 *     every entry in "execute".requires_all_of is enforced before the call
 *     reaches the engine
 *   - fail if 'force' / 'overrideSafety' (or any equivalent) appear anywhere
 *     in a model-facing tool schema, for any profile
 *   - cross-check its findings against check-parity.py's findings so the two
 *     languages can never silently drift apart
 *
 * Until then, this script only validates that the contract file itself is
 * present and well-formed JSON, so a syntax error in the contract still
 * fails CI rather than being silently ignored by a script nobody wired up
 * yet.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const contractPath = path.join(here, "..", "contracts", "integration-tool-contract.json");

function main(): number {
  let raw: string;
  try {
    raw = readFileSync(contractPath, "utf-8");
  } catch (err) {
    console.error(`check-parity.ts: contract file missing at ${contractPath}`);
    return 1;
  }

  let contract: Record<string, unknown>;
  try {
    contract = JSON.parse(raw);
  } catch (err) {
    console.error(`check-parity.ts: contract file is not valid JSON: ${(err as Error).message}`);
    return 1;
  }

  const profiles = contract["profiles"] as Record<string, unknown> | undefined;
  if (!profiles) {
    console.error("check-parity.ts: contract file has no 'profiles' key");
    return 1;
  }

  console.log(
    "check-parity.ts: STUB — contract file is present and well-formed " +
      `(${Object.keys(profiles).length} profiles declared). No TypeScript ` +
      "integration package implements tool exposure yet, so there is nothing " +
      "further to diff against. See this file's header comment for what D1+ " +
      "must add.",
  );
  return 0;
}

process.exit(main());
