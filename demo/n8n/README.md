# n8n demo workflow

`log-and-execute-decision.json` is a real, importable n8n workflow built on
[`n8n-nodes-algenta`](../../typescript/n8n-nodes-algenta/packages/n8n-nodes-algenta): it logs a
decision, attempts to execute it, and branches on whether the connected engine's policy blocked
that execution — the same success/denial shapes `n8n-nodes-algenta`'s own test suite exercises
against a real MCP server, not an invented example.

## What you need before importing this

- **n8n**, running locally or wherever you already run it. Don't have it yet? The node package's
  own [Quickstart](../../typescript/n8n-nodes-algenta/packages/n8n-nodes-algenta/README.md#quickstart)
  gets you a local n8n editor with the Algenta node already installed, in one `pnpm` command.
- **A running self-hosted Algenta Engine**, reachable over HTTP from wherever n8n runs (e.g.
  `http://localhost:8000` for a local n8n talking to a local engine). This workflow calls that
  engine directly — there is no hosted-by-Algenta alternative to point it at instead. See
  [Self-host the engine](https://docs.algenta.ai/deploy-and-operate/self-hosting) if you don't have
  one running yet.
- An **API key** for that engine, only if it enforces authentication.

Without a reachable engine, importing still works, but running the workflow fails at
`Log Decision` with a plain connection error — expected, and not a sign anything here is broken.

## Import it

1. In n8n: **Workflows → Import from File** (or **Import from URL** if hosting this file), and
   select `log-and-execute-decision.json`.
2. Create an **Algenta API** credential pointing at your own self-hosted Algenta Engine (see the
   node's README for the Base URL / API Key fields), and attach it to both `Log Decision` and
   `Execute Decision`.
3. Replace the `Execute Decision` node's **Webhook URL** placeholder
   (`https://your-automation.example.com/hooks/renewal`) with your own downstream endpoint.
4. Run it. A successful execution flows into **Format success receipt**; a policy-blocked one
   (named gate: `idempotency`, `confidence`, or `risk_floor`) flows into
   **Format policy-denial alert** instead — nothing in between silently swallows the difference.

The full `meta.instructions` block inside the workflow JSON repeats this for anyone opening the
file directly in n8n's editor.
