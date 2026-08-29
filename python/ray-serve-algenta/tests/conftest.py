"""Test-collection-time setup for this package's conformance suite.

`RAY_ENABLE_UV_RUN_RUNTIME_ENV` must be disabled here, before `ray` is imported anywhere in this
process -- a real, reproduced-live gotcha specific to running Ray inside this repository's `uv`
workspace layout, not a hypothetical:

Ray >=2.58 defaults `RAY_ENABLE_UV_RUN_RUNTIME_ENV` to `True`
(`ray._private.ray_constants.RAY_ENABLE_UV_RUN_RUNTIME_ENV`). When the driver process itself was
launched via `uv run` -- exactly how this repository's CI, and this package's own documented
`uv run pytest ray-serve-algenta -v`, invoke every test -- Ray auto-detects that and reconstructs
the *same* `uv run` invocation for every worker/replica process it spawns, resolved against
whatever `pyproject.toml` governs the driver's current working directory. In this repository, that
directory is `python/` -- the **uv workspace root**, whose own `[project] dependencies = []`
(`python/pyproject.toml`) is deliberately empty; each real package (including this one) declares
its own dependencies independently. The result, reproduced directly while building this test
suite: every Ray worker Ray spawns fails to start with `ModuleNotFoundError: No module named
'ray'`, because the environment Ray silently rebuilt for it has nothing installed in it at all --
and because Ray's raylet keeps retrying worker startup indefinitely rather than surfacing that
import error to the driver, the whole test suite appears to simply hang forever instead of failing
fast with a legible error.

Setting this to `"0"` makes Ray fall back to its pre-`uv run`-aware behavior: workers inherit the
driver's own already-fully-installed environment directly (the same interpreter running this test
process, `ray[serve]` and everything else already resolved onto it by `uv sync`) instead of trying
to reconstruct one. That is what this package's tests actually want -- there is nothing project-
scoped about a worker for this test suite's purposes, and the workspace root has no relevant
dependencies to reconstruct in the first place.
"""

from __future__ import annotations

import os

os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
