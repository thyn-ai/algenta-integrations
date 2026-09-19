import { defineConfig } from "vitest/config";

// Coverage runs on every `pnpm test` (the script passes --coverage), locally and in CI
// alike -- one code path, so a locally-green suite can never fail CI on coverage. The
// thresholds are the measured baseline (2026-09-19, vitest 5, v8 provider: 99.35% lines,
// 99.36% statements, 94.16% branches, 100% functions) minus 2 points of headroom; raise them
// as coverage grows, never lower them. Every shipped TypeScript source is measured,
// including the n8n node and credentials definitions -- only the tests themselves (and
// their stub server) are excluded. CI uploads coverage/lcov.info to Codecov (dashboard
// only; this gate is the enforcement).
export default defineConfig({
  test: {
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.ts", "nodes/**/*.ts", "credentials/**/*.ts", "index.ts"],
      exclude: ["test/**"],
      thresholds: {
        lines: 97,
        functions: 98,
        branches: 92,
        statements: 97,
      },
    },
  },
});
