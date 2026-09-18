import { defineConfig } from "vitest/config";

// Coverage runs on every `pnpm test` (the script passes --coverage), locally and in CI
// alike -- one code path, so a locally-green suite can never fail CI on coverage. The
// thresholds are the measured baseline (2026-09-18, vitest 5, v8 provider) minus 2 points
// of headroom; raise them as coverage grows. Only shipped source is measured -- the
// suites themselves and their stub server are test infrastructure, not product surface.
// CI uploads coverage/lcov.info to Codecov (dashboard only; this gate is the enforcement).
export default defineConfig({
  test: {
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.ts"],
      exclude: ["src/**/*.test.ts", "src/test-support/**"],
      thresholds: {
        lines: 92,
        functions: 98,
        branches: 82,
        statements: 92,
      },
    },
  },
});
