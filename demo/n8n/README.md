# n8n demo workflows

This directory holds real, importable n8n workflows built on
[`n8n-nodes-algenta`](../../typescript/n8n-nodes-algenta/packages/n8n-nodes-algenta).

- `log-and-execute-decision.json` logs a decision, attempts to execute it, and branches on whether
  the connected engine's policy blocked that execution.
- `observe-weekly-report.json` uses only the read-only `observe` tool profile to build a weekly
  report from the engine's contract, a governed query, a simulation, and a recommendation.

Both workflows exercise the same success/denial and profile shapes `n8n-nodes-algenta`'s own test
suite exercises against a real MCP server, not invented examples.

## What you need before importing these

- **n8n**, running locally or wherever you already run it. Don't have it yet? The node package's
  own [Quickstart](../../typescript/n8n-nodes-algenta/packages/n8n-nodes-algenta/README.md#quickstart)
  gets you a local n8n editor with the Algenta node already installed, in one `pnpm` command.
- **A running self-hosted Algenta engine**, reachable over HTTP from wherever n8n runs (e.g.
  `http://localhost:8000` for a local n8n talking to a local engine). These workflows call that
  engine directly — there is no hosted-by-Algenta alternative to point them at instead. See
  [Self-host the engine](https://docs.algenta.ai/deploy-and-operate/self-hosting) if you don't have
  one running yet.
- An **API key** for that engine, only if it enforces authentication.

Without a reachable engine, importing still works, but running a workflow fails at the first
Algenta node with a plain connection error — expected, and not a sign anything here is broken.

## `log-and-execute-decision.json` — governed execution

1. In n8n: **Workflows → Import from File** (or **Import from URL** if hosting this file), and
   select `log-and-execute-decision.json`.
2. Create an **Algenta API** credential pointing at your own self-hosted Algenta engine (see the
   node's README for the Base URL / API Key fields), and attach it to both `Log Decision` and
   `Execute Decision`.
3. Replace the `Execute Decision` node's **Webhook URL** placeholder
   (`https://your-automation.example.com/hooks/renewal`) with your own downstream endpoint.
4. Run it. A successful execution flows into **Format success receipt**; a policy-blocked one
   (named gate: `idempotency`, `confidence`, or `risk_floor`) flows into
   **Format policy-denial alert** instead — nothing in between silently swallows the difference.

The full `meta.instructions` block inside the workflow JSON repeats this for anyone opening the
file directly in n8n's editor.

## `observe-weekly-report.json` — read-only weekly report

1. In n8n: **Workflows → Import from File**, and select `observe-weekly-report.json`.
2. Create an **Algenta API** credential pointing at your own self-hosted Algenta engine, and attach
   it to every Algenta node (`Get Contract`, `Fetch Weekly Metrics`, `Forecast Next Week`,
   `Recommend Action`).
3. If your engine does not have a dataset named `weekly_renewals`, update the `arguments` in
   **Fetch Weekly Metrics** to a dataset that exists in your deployment.
4. Run it. The workflow chains four observe-profile tools and renders their outputs into a single
   Markdown `report` item in **Render Weekly Report**. See
   `observe-weekly-report.sample.md` for the rendered shape against the package's stub server.

Every Algenta node in this workflow is pinned to the `observe` profile, so it cannot log or execute
a decision. The full `meta.instructions` block inside the workflow JSON repeats this for anyone
opening the file directly in n8n's editor.
