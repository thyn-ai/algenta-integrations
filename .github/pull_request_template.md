## What does this change?

<!-- One or two sentences. -->

## Checklist

- [ ] Tests added or updated for this change
- [ ] Docs updated if needed
- [ ] `python scripts/check-no-engine-dependency.py --root .` passes (CI
      enforces this, but check locally too — see CONTRIBUTING.md)
- [ ] Any new model-facing tool is in the correct profile per
      `contracts/integration-tool-contract.json` (no `execute`-tier tool
      leaking into `observe`/`govern`; no `force`/`override_safety` reachable
      by a model)
- [ ] No hardcoded credentials, secrets, or hosted-cloud base URLs (every
      example defaults to the customer's own self-hosted `ALGENTA_BASE_URL`)
- [ ] I've signed the CLA, or the CLA-assistant bot will prompt me to on
      this PR
