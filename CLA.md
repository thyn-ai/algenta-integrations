# Algenta Contributor License Agreement — DRAFT, NOT FOR USE UNTIL COUNSEL-REVIEWED

> **This file is a placeholder, not a legal document.** It mirrors, word for
> word in substance, the placeholder CLA already carried by the sibling
> repository [`thyn-ai/algenta-sdk`](https://github.com/thyn-ai/algenta-sdk/blob/main/CLA.md).
> Algenta wants a formal CLA (rather than a lightweight DCO) across every
> open governance surface in the `thyn-ai` org — including this repository
> — so that community contributions to any Apache-2.0 package can later be
> folded into other Algenta products/editions with maximum flexibility. The
> text below sketches the *shape* of that agreement, modeled loosely on the
> publicly-available structure of the Apache Software Foundation's
> Individual CLA and Google's CLA (both of which Apache's own guidance says
> are reasonable starting points to adapt) — **it is not drafted or
> reviewed by a lawyer and must not be put in front of a real contributor
> until Algenta's counsel has drafted or reviewed the actual text.** Do not
> wire up a CLA-assistant bot against this placeholder in production.

This repository should adopt whatever text counsel finalizes for
`thyn-ai/algenta-sdk`'s `CLA.md` — the two should read as one CLA, not two
diverging ones, since a contributor working across both repos should sign
the same agreement. Do not draft separate CLA language for this repo.

## What the real CLA needs to cover (for counsel's scoping)

- **Grant**: contributor grants Algenta, Inc. a perpetual, worldwide,
  non-exclusive, royalty-free, irrevocable license to reproduce, prepare
  derivative works of, publicly display, publicly perform, sublicense, and
  distribute the contribution and derivative works, under any license terms
  Algenta chooses — including proprietary/closed licenses for other Algenta
  products or editions, not only the Apache-2.0 terms this repository ships
  under.
- **Patent grant**: a patent license from the contributor for any patent
  claims necessarily infringed by their contribution alone or in combination
  with the project, terminating if the contributor initiates patent
  litigation against Algenta or the project over the contribution.
- **Contributor representations**: the contributor has the right to grant
  this license (either it's their own original work, or their employer has
  authorized it — an employer/Corporate CLA variant may be needed
  separately for contributors submitting on behalf of a company).
- **No warranty**: the contribution is provided "as is," consistent with the
  underlying Apache-2.0 license's own disclaimer.
- **Mechanism**: signed electronically via an automated CLA-assistant-style
  GitHub Action on a contributor's first pull request; the signature record
  should be retained (not just the bot's in-PR comment) in case it's ever
  needed as evidence.

## Scope note for counsel

This repository (`thyn-ai/algenta-integrations`) contains framework
integration packages (pydantic-ai, LangChain, LiteLLM, TypeScript/JS
tool-calling helpers) that wrap the published `algenta-sdk` client. It does
not contain the Algenta Engine, which is closed-source and lives in a
separate private repository, and it does not contain the SDK's own source
either (that is `thyn-ai/algenta-sdk`). The CLA's grant should be broad
enough to let Algenta relicense or fold contributions from this repo, from
`algenta-sdk`, or from any other Apache-2.0 `thyn-ai` repository into
Algenta's closed product line, since that's the explicit business reason
for wanting a CLA over a plain DCO org-wide.

---

*Replace this entire file with counsel-approved text — the same text used
in `thyn-ai/algenta-sdk` — before this repository is made public. Track
this as a hard blocker alongside the other visibility-flip gates in the
approved plan.*
