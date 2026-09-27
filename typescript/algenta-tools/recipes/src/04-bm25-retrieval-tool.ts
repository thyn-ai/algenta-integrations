/**
 * Recipe 04 -- BM25 retrieval tool with governed decision logging (agentic RAG).
 *
 * RAG is the most popular Vercel AI SDK use case, and "retrieval as a tool the agent calls" is
 * its canonical agentic form. This recipe wires a real Okapi BM25 retriever up as an AI SDK
 * `tool` and puts Algenta at the center of it: every retrieval the agent makes is recorded in
 * Algenta's decision memory via `log_decision` (govern tier), so "what did the agent look up,
 * and why" is an auditable decision record, not a lost log line. The BM25 scorer here is plain,
 * deterministic TypeScript that runs anywhere. On the Python side the same retrieval hot path
 * is served by `bm25-mojo` (Algenta's published Mojo kernel, live on PyPI -- see the sibling
 * `python/langchain-algenta` BM25 recipe, which uses it transparently when installed); a
 * TS-side kernel is a separate kernel-program track, so this recipe keeps the dependency-free
 * scorer.
 *
 * Run it (zero credentials -- stub engine + scripted stand-in model):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:04
 */
import { generateText, stepCountIs, tool, type LanguageModel, type Tool } from "ai";
import { pathToFileURL } from "node:url";
import { z } from "zod";
import { createAlgentaTools, LOG_DECISION, type AlgentaToolSet } from "algenta-tools";

import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";
import { scriptedModel, textResponse, toolCallResponse } from "./support/scripted-model.js";

// The published Okapi BM25 defaults (Robertson & Zaragoza, "The Probabilistic Relevance
// Framework: BM25 and Beyond", 2009): term-frequency saturation and document-length
// normalization. These are the standard, widely replicated constant values, not tunables
// invented for this corpus.
const OKAPI_K1 = 1.5;
const OKAPI_B = 0.75;
/** Robertson's "+1 inside the log" idf form, keeping idf non-negative for terms in >50% of
 * documents instead of letting it go negative. */
const IDF_LOG_OFFSET = 1;

/** Lowercase alphanumeric tokenizer -- deterministic, locale-independent. */
export function tokenize(text: string): string[] {
  return text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
}

export interface Bm25Document {
  id: string;
  text: string;
}

export interface Bm25Index {
  docCount: number;
  avgDocLength: number;
  /** term -> number of documents containing it. */
  docFrequency: Map<string, number>;
  /** docId -> (term -> raw term frequency). */
  termFrequencies: Map<string, Map<string, number>>;
  docLengths: Map<string, number>;
}

/** Builds the inverted index. Deterministic: same corpus in, same index out. */
export function buildBm25Index(documents: Bm25Document[]): Bm25Index {
  const docFrequency = new Map<string, number>();
  const termFrequencies = new Map<string, Map<string, number>>();
  const docLengths = new Map<string, number>();
  let totalLength = 0;

  for (const doc of documents) {
    const terms = tokenize(doc.text);
    docLengths.set(doc.id, terms.length);
    totalLength += terms.length;
    const tf = new Map<string, number>();
    for (const term of terms) {
      tf.set(term, (tf.get(term) ?? 0) + 1);
    }
    termFrequencies.set(doc.id, tf);
    for (const term of tf.keys()) {
      docFrequency.set(term, (docFrequency.get(term) ?? 0) + 1);
    }
  }

  return {
    docCount: documents.length,
    avgDocLength: documents.length === 0 ? 0 : totalLength / documents.length,
    docFrequency,
    termFrequencies,
    docLengths,
  };
}

export interface Bm25Hit {
  id: string;
  score: number;
}

/** Scores every document against `query` with Okapi BM25 and returns the hits sorted by score
 * (desc), ties broken by document id (asc) so the output order is fully deterministic.
 * Documents scoring 0 (no query term in common) are included -- ranking transparency beats
 * hiding them. */
export function scoreBm25(index: Bm25Index, query: string): Bm25Hit[] {
  const queryTerms = [...new Set(tokenize(query))];
  const hits: Bm25Hit[] = [];
  for (const [docId, tf] of index.termFrequencies) {
    const docLength = index.docLengths.get(docId) ?? 0;
    let score = 0;
    for (const term of queryTerms) {
      const termFreq = tf.get(term);
      if (termFreq === undefined) {
        continue;
      }
      const df = index.docFrequency.get(term) ?? 0;
      const idf = Math.log(
        IDF_LOG_OFFSET + (index.docCount - df + 0.5) / (df + 0.5),
      );
      const numerator = termFreq * (OKAPI_K1 + 1);
      const denominator =
        termFreq + OKAPI_K1 * (1 - OKAPI_B + (OKAPI_B * docLength) / index.avgDocLength);
      score += idf * (numerator / denominator);
    }
    hits.push({ id: docId, score });
  }
  hits.sort((a, b) => b.score - a.score || a.id.localeCompare(b.id));
  return hits;
}

export interface GovernedSearchHit extends Bm25Hit {
  text: string;
}

export interface GovernedSearchResult {
  /** The decision-memory record id for this retrieval -- the audit trail entry. */
  decisionId: string;
  query: string;
  hits: GovernedSearchHit[];
}

/**
 * Builds the `corpus_search` tool: BM25 over the caller's corpus, with every call recorded in
 * Algenta's decision memory (`log_decision`, `chosen_action: "retrieve:<query>"`) before the
 * hits are returned. The retrieval and its audit record travel together in the tool result, so
 * the model (and any downstream consumer of the transcript) can cite the decision id for what
 * it was shown.
 */
export function createGovernedBm25SearchTool(options: {
  corpus: Bm25Document[];
  /** A govern-tier (or higher) `AlgentaToolSet` -- must contain `log_decision`. */
  tools: AlgentaToolSet;
  /** Default hit cap when the model doesn't pass `topK`. */
  defaultTopK?: number;
}): Tool {
  const index = buildBm25Index(options.corpus);
  const textById = new Map(options.corpus.map(doc => [doc.id, doc.text]));
  const defaultTopK = options.defaultTopK ?? 3;

  return tool({
    description:
      "Search the local corpus with BM25 (Okapi) and return the top-ranked passages. Every " +
      "search is recorded in Algenta's decision memory; the result carries that record's id.",
    inputSchema: z.object({
      query: z.string().min(1),
      topK: z.number().int().min(1).max(20).optional(),
    }),
    execute: async ({ query, topK }): Promise<GovernedSearchResult> => {
      const limit = topK ?? defaultTopK;
      const ranked = scoreBm25(index, query).filter(hit => hit.score > 0).slice(0, limit);

      const logged = (await options.tools[LOG_DECISION]!.execute!(
        { chosen_action: `retrieve:${query}` },
        directToolCallOptions(`direct-log_decision-${query}`),
      )) as { decision_id: string };

      return {
        decisionId: logged.decision_id,
        query,
        hits: ranked.map(hit => ({ ...hit, text: textById.get(hit.id) ?? "" })),
      };
    },
  });
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:04`. Runs a real agent loop in
 * which the model answers a question by calling the governed `corpus_search` tool. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client, stub }) => {
    const corpus: Bm25Document[] = [
      { id: "pricing-v3", text: "Pricing v3: the growth tier costs 49 dollars per seat per month with annual billing." },
      { id: "pricing-v2", text: "Pricing v2 (deprecated): the growth tier used to cost 59 dollars per seat." },
      { id: "refunds", text: "Refund policy: annual plans are refundable within 30 days of purchase." },
      { id: "onboarding", text: "Onboarding: new workspaces get a guided tour and a sample dataset." },
    ];
    const governTools = await createAlgentaTools({ client, profile: "govern" });
    const tools = {
      corpus_search: createGovernedBm25SearchTool({ corpus, tools: governTools }),
    };

    const result = await generateText({
      model: scriptedModel({
        generate: [
          toolCallResponse("corpus_search", { query: "annual growth tier" }),
          textResponse("The growth tier costs 49 dollars per seat per month (annual billing)."),
        ],
      }),
      tools,
      stopWhen: stepCountIs(4),
      prompt: "How much does the growth tier cost with annual billing? Search the corpus before answering.",
    });

    console.log(`Agent answer: ${result.text}`);
    console.log("Retrievals recorded in Algenta decision memory:");
    for (const record of stub.loggedDecisions) {
      console.log(`  ${record.decision_id}  ${record.chosen_action}`);
    }
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:04`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
