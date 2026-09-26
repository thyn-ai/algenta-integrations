import { defineConfig } from "vitest/config";

// Same one-code-path coverage policy as the sibling `algenta-tools` package: coverage runs on
// every `pnpm test`, locally and in CI alike. The thresholds are the measured baseline minus
// headroom; raise them as coverage grows. Recipe modules are the product surface here; the
// suites themselves and the stub server are test infrastructure and are excluded.
export default defineConfig({
  test: {
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.ts"],
      exclude: ["src/**/*.test.ts", "src/support/**"],
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 75,
        statements: 80,
      },
    },
  },
});
