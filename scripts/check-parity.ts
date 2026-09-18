/**
 * check-parity.ts — thin wrapper that delegates to scripts/check-parity.py.
 *
 * Run via `npx --yes tsx@4 scripts/check-parity.ts` (that's what
 * .github/workflows/ci.yml does) — this file is plain TypeScript with no
 * build step of its own, and `tsx` is what strips/transpiles it on the fly
 * without requiring a Node version new enough for native type-stripping.
 *
 * Why a wrapper instead of a second implementation: the parity rules (the
 * contract's four profiles, exact per-profile tool sets, default profile,
 * never-model-facing fields) were originally meant to be checked by two
 * hand-mirrored scripts, one per language. Two implementations of one rule
 * is how the checks themselves drift. scripts/check-parity.py is the single
 * implementation — it already checks both the Python AND the TypeScript
 * packages' embedded contract modules statically — and this file exists only
 * so the TypeScript CI job keeps a stable, tsx-runnable entry point. Both CI
 * invocations therefore run exactly the same checks and can never disagree.
 *
 * The repository root is resolved inside the Python script from its own
 * location, so this wrapper works from any cwd (ci.yml runs it from the repo
 * root; sync-on-sdk-release.yml runs it from typescript/algenta-tools). Any
 * arguments (e.g. --root) are forwarded verbatim.
 *
 * Exit codes are passed through from the Python script:
 *   0  clean · 1  parity violations found · 2  usage/internal error
 * (this wrapper itself returns 2 when no Python interpreter can be found).
 */

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const checker = path.join(path.dirname(fileURLToPath(import.meta.url)), "check-parity.py");
const forwardedArgs = process.argv.slice(2);

function main(): number {
  // GitHub-hosted runners and macOS both ship `python3`; `python` is the
  // fallback for environments that only provide the unversioned name.
  for (const interpreter of ["python3", "python"]) {
    const result = spawnSync(interpreter, [checker, ...forwardedArgs], { stdio: "inherit" });
    if (result.error) {
      if ((result.error as { code?: string }).code === "ENOENT") {
        continue;
      }
      console.error(`check-parity.ts: failed to launch ${interpreter}: ${result.error.message}`);
      return 2;
    }
    if (result.status === null) {
      console.error(`check-parity.ts: ${interpreter} was killed by signal ${result.signal}`);
      return 2;
    }
    return result.status;
  }
  console.error(
    "check-parity.ts: no Python interpreter found on PATH (tried python3, python). " +
      "Install Python 3.10+ or run `python3 scripts/check-parity.py` directly.",
  );
  return 2;
}

process.exit(main());
