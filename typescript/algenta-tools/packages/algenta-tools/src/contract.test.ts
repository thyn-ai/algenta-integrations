import { describe, expect, it } from "vitest";

import {
  DEFAULT_PROFILE,
  EXECUTE_DECISION,
  FULL_PROFILE_SENTINEL,
  GET_CONTRACT,
  LOG_DECISION,
  NEVER_MODEL_FACING_FIELDS,
  PLAN_DECISION,
  QUERY_DATA,
  RECOMMEND,
  SIMULATE,
  TOOL_PROFILES,
  isToolProfile,
  resolveProfileToolNames,
} from "./contract.js";

describe("DEFAULT_PROFILE", () => {
  it("is observe", () => {
    expect(DEFAULT_PROFILE).toBe("observe");
  });
});

describe("isToolProfile", () => {
  it("accepts the four known profiles", () => {
    expect(isToolProfile("observe")).toBe(true);
    expect(isToolProfile("govern")).toBe(true);
    expect(isToolProfile("execute")).toBe(true);
    expect(isToolProfile("full")).toBe(true);
  });

  it("rejects anything else", () => {
    expect(isToolProfile("admin")).toBe(false);
    expect(isToolProfile("")).toBe(false);
  });
});

describe("resolveProfileToolNames", () => {
  const registry = [
    GET_CONTRACT,
    QUERY_DATA,
    SIMULATE,
    RECOMMEND,
    PLAN_DECISION,
    LOG_DECISION,
    EXECUTE_DECISION,
    "admin_only_diagnostic_tool",
  ];

  it("observe resolves to exactly the contract's observe tools", () => {
    expect(resolveProfileToolNames("observe", registry)).toEqual(
      new Set([GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND]),
    );
  });

  it("observe never resolves execute-tier or govern-tier tools", () => {
    const resolved = resolveProfileToolNames("observe", registry);
    expect(resolved.has(EXECUTE_DECISION)).toBe(false);
    expect(resolved.has(PLAN_DECISION)).toBe(false);
    expect(resolved.has(LOG_DECISION)).toBe(false);
  });

  it("govern adds plan_decision and log_decision but not execute_decision", () => {
    const resolved = resolveProfileToolNames("govern", registry);
    expect(resolved).toEqual(
      new Set([GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION]),
    );
    expect(resolved.has(EXECUTE_DECISION)).toBe(false);
  });

  it("execute adds execute_decision but not the admin-only tool", () => {
    const resolved = resolveProfileToolNames("execute", registry);
    expect(resolved).toEqual(
      new Set([
        GET_CONTRACT,
        QUERY_DATA,
        SIMULATE,
        RECOMMEND,
        PLAN_DECISION,
        LOG_DECISION,
        EXECUTE_DECISION,
      ]),
    );
    expect(resolved.has("admin_only_diagnostic_tool")).toBe(false);
  });

  it("full resolves to everything the registry advertises", () => {
    expect(resolveProfileToolNames("full", registry)).toEqual(new Set(registry));
  });

  it("a contract-named tool the server doesn't currently advertise is simply absent, not an error", () => {
    const partialRegistry = [GET_CONTRACT, QUERY_DATA];
    expect(resolveProfileToolNames("observe", partialRegistry)).toEqual(
      new Set([GET_CONTRACT, QUERY_DATA]),
    );
  });
});

describe("TOOL_PROFILES", () => {
  it("full is the literal wildcard sentinel, matching the contract", () => {
    expect(TOOL_PROFILES.full).toBe(FULL_PROFILE_SENTINEL);
    expect(FULL_PROFILE_SENTINEL).toBe("*");
  });
});

describe("NEVER_MODEL_FACING_FIELDS", () => {
  it("is exactly force and override_safety", () => {
    expect(NEVER_MODEL_FACING_FIELDS).toEqual(new Set(["force", "override_safety"]));
  });
});
