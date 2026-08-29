# n8n demo workflow

`log-and-execute-decision.json` is a real, importable n8n workflow built on
[`n8n-nodes-algenta`](../../typescript/n8n-nodes-algenta/packages/n8n-nodes-algenta): it logs a
decision, attempts to execute it, and branches on whether the connected engine's policy blocked
that execution — the same success/denial shapes `n8n-nodes-algenta`'s own test suite exercises
against a real MCP server, not an invented example.

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
