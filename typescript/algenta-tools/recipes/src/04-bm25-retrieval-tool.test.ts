import { generateText, stepCountIs } from "ai";
import { describe, expect, it } from "vitest";
import type { LanguageModelV4CallOptions } from "@ai-sdk/provider";
import { createAlgentaTools, EXECUTE_DECISION } from "algenta-tools";

import {
  buildBm25Index,
  createGovernedBm25SearchTool,
  scoreBm25,
  tokenize,
  type Bm25Document,
  type GovernedSearchResult,
} from "./04-bm25-retrieval-tool.js";
import { withStubAlgenta } from "./support/algenta.js";
import { scriptedModel, textResponse, toolCallResponse } from "./support/scripted-model.js";
import { directToolCallOptions } from "./support/algenta.js";

/** The pinned corpus for the exact-score tests. Its expected scores were computed independently
 * by hand from the Okapi BM25 formula (k1=1.5, b=0.75, idf = ln(1 + (N-df+0.5)/(df+0.5))):
 * N=3, avgdl=7/3; e.g. score(d1, "beta") = ln(8/3) * (2*2.5)/(2 + 1.5*(0.25 + 0.75*3/(7/3)))
 * = 1.2833279945947824. */
const PINNED_CORPUS: Bm25Document[] = [
  { id: "d1", text: "alpha beta beta" },
  { id: "d2", text: "alpha" },
  { id: "d3", text: "gamma gamma gamma" },
];

const RAG_CORPUS: Bm25Document[] = [
  { id: "pricing-v3", text: "Pricing v3: the growth tier costs 49 dollars per seat per month with annual billing." },
  { id: "pricing-v2", text: "Pricing v2 (deprecated): the growth tier used to cost 59 dollars per seat." },
  { id: "refunds", text: "Refund policy: annual plans are refundable within 30 days of purchase." },
  { id: "onboarding", text: "Onboarding: new workspaces get a guided tour and a sample dataset." },
];

describe("recipe 04 -- BM25 scorer (pure, deterministic)", () => {
  it("tokenizes lowercase alphanumerics only", () => {
    expect(tokenize("Growth-Tier, 49 Dollars!")).toEqual(["growth", "tier", "49", "dollars"]);
    expect(tokenize("")).toEqual([]);
  });

  it("matches the hand-computed Okapi scores exactly", () => {
    const index = buildBm25Index(PINNED_CORPUS);
    const hits = scoreBm25(index, "beta");
    expect(hits[0]).toEqual({ id: "d1", score: 1.2833279945947824 });
    expect(hits[1]!.score).toBe(0);
    expect(hits[2]!.score).toBe(0);
  });

  it("normalizes by document length: same tf, shorter doc ranks higher", () => {
    const index = buildBm25Index(PINNED_CORPUS);
    const hits = scoreBm25(index, "alpha");
    expect(hits.map(h => h.id)).toEqual(["d2", "d1", "d3"]);
    expect(hits[0]!.score).toBeCloseTo(0.6326971932154132, 10);
    expect(hits[1]!.score).toBeCloseTo(0.4164589119898923, 10);
  });

  it("weights rarer terms higher (idf): d1 beats d2 on a two-term query via beta", () => {
    const index = buildBm25Index(PINNED_CORPUS);
    const hits = scoreBm25(index, "alpha beta");
    expect(hits.map(h => h.id)).toEqual(["d1", "d2", "d3"]);
    expect(hits[0]!.score).toBeCloseTo(1.6997869065846747, 10);
  });

  it("is deterministic: same corpus and query, identical hit order across runs", () => {
    const a = scoreBm25(buildBm25Index(PINNED_CORPUS), "alpha beta");
    const b = scoreBm25(buildBm25Index(PINNED_CORPUS), "alpha beta");
    expect(a).toEqual(b);
  });

  it("handles an empty corpus and a no-match query without NaN or invention", () => {
    expect(scoreBm25(buildBm25Index([]), "anything")).toEqual([]);
    const hits = scoreBm25(buildBm25Index(PINNED_CORPUS), "zzz");
    expect(hits.every(hit => hit.score === 0)).toBe(true);
  });
});

describe("recipe 04 -- governed BM25 search tool", () => {
  it("returns ranked hits AND records the retrieval in Algenta decision memory", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const governTools = await createAlgentaTools({ client, profile: "govern" });
      const search = createGovernedBm25SearchTool({ corpus: RAG_CORPUS, tools: governTools });

      const result = (await search.execute!(
        { query: "annual growth tier", topK: 2 },
        directToolCallOptions("test-search"),
      )) as GovernedSearchResult;

      expect(result.hits).toHaveLength(2);
      // The current pricing doc outranks the deprecated one for a price query.
      expect(result.hits[0]!.id).toBe("pricing-v3");
      expect(result.hits[0]!.score).toBeGreaterThan(result.hits[1]!.score);
      expect(result.hits[0]!.text).toContain("49 dollars");

      // The audit trail: exactly one decision, naming the retrieval, its id carried in the
      // tool result.
      expect(stub.loggedDecisions).toHaveLength(1);
      expect(stub.loggedDecisions[0]!.chosen_action).toBe("retrieve:annual growth tier");
      expect(result.decisionId).toBe(stub.loggedDecisions[0]!.decision_id);
    });
  });

  it("honors the default topK and returns no hits (but still logs) for a no-match query", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const governTools = await createAlgentaTools({ client, profile: "govern" });
      const search = createGovernedBm25SearchTool({ corpus: RAG_CORPUS, tools: governTools, defaultTopK: 1 });

      const topHeavy = (await search.execute!(
        { query: "refund" },
        directToolCallOptions("test-search-2"),
      )) as GovernedSearchResult;
      expect(topHeavy.hits).toHaveLength(1);
      expect(topHeavy.hits[0]!.id).toBe("refunds");

      const noMatch = (await search.execute!(
        { query: "zzzz" },
        directToolCallOptions("test-search-3"),
      )) as GovernedSearchResult;
      expect(noMatch.hits).toEqual([]);
      expect(stub.loggedDecisions.map(d => d.chosen_action)).toEqual(["retrieve:refund", "retrieve:zzzz"]);
    });
  });

  it("drives a real agentic-RAG loop: the model calls corpus_search, then answers", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const governTools = await createAlgentaTools({ client, profile: "govern" });
      const tools = { corpus_search: createGovernedBm25SearchTool({ corpus: RAG_CORPUS, tools: governTools }) };
      const offered: string[][] = [];

      const result = await generateText({
        model: scriptedModel({
          generate: [
            toolCallResponse("corpus_search", { query: "annual growth tier" }),
            textResponse("49 dollars per seat per month."),
          ],
          onCall: (options: LanguageModelV4CallOptions) =>
            offered.push((options.tools ?? []).map(tool => tool.name)),
        }),
        tools,
        stopWhen: stepCountIs(4),
        prompt: "How much does the growth tier cost?",
      });

      expect(result.text).toBe("49 dollars per seat per month.");
      // The retrieval really happened, once, against the engine's decision memory.
      expect(stub.loggedDecisions).toHaveLength(1);
      // The tool result the model was fed carries both the hits and the decision id.
      const step = result.steps[0]!;
      const toolResult = step.content.find(part => part.type === "tool-result");
      expect(toolResult).toBeDefined();
      if (toolResult?.type === "tool-result") {
        const output = toolResult.output as GovernedSearchResult;
        expect(output.decisionId).toBe(stub.loggedDecisions[0]!.decision_id);
        expect(output.hits[0]!.id).toBe("pricing-v3");
      }
      // This merged toolset is the app's own -- no Algenta execute-tier tool leaks in.
      expect(offered[0]).toEqual(["corpus_search"]);
      expect(offered[0]).not.toContain(EXECUTE_DECISION);
    });
  });
});
