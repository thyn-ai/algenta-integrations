# Python integration examples (placeholder)

Empty for now. Once `pydantic-ai-algenta`, `langchain-algenta`, and
`litellm-algenta` have real implementations (D1/D3/D4), this directory will
hold runnable example scripts showing each package used against a
self-hosted Algenta Engine — never against a hosted cloud endpoint by
default, matching the rest of this repository's self-hosted-only
positioning.

This is a package only so it can be a valid `uv` workspace member (see
`../pyproject.toml`'s `[tool.uv.workspace]`); it declares no dependencies
and is never published.
