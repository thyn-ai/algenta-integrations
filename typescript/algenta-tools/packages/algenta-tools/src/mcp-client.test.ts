import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, resolveAlgentaBaseUrl } from "./mcp-client.js";

describe("resolveAlgentaBaseUrl", () => {
  const originalEnvValue = process.env[ALGENTA_BASE_URL_ENV_VAR];

  beforeEach(() => {
    delete process.env[ALGENTA_BASE_URL_ENV_VAR];
  });

  afterEach(() => {
    if (originalEnvValue === undefined) {
      delete process.env[ALGENTA_BASE_URL_ENV_VAR];
    } else {
      process.env[ALGENTA_BASE_URL_ENV_VAR] = originalEnvValue;
    }
  });

  it("defaults to the local self-hosted endpoint -- never a hosted-by-Algenta cloud default", () => {
    expect(resolveAlgentaBaseUrl()).toBe(DEFAULT_ALGENTA_BASE_URL);
    expect(DEFAULT_ALGENTA_BASE_URL).toBe("http://localhost:8000/mcp");
  });

  it("prefers ALGENTA_BASE_URL over the default", () => {
    process.env[ALGENTA_BASE_URL_ENV_VAR] = "https://engine.internal.example.com/mcp";
    expect(resolveAlgentaBaseUrl()).toBe("https://engine.internal.example.com/mcp");
  });

  it("prefers an explicit argument over ALGENTA_BASE_URL and the default", () => {
    process.env[ALGENTA_BASE_URL_ENV_VAR] = "https://engine.internal.example.com/mcp";
    expect(resolveAlgentaBaseUrl("https://explicit.example.com/mcp")).toBe(
      "https://explicit.example.com/mcp",
    );
  });
});
