#!/usr/bin/env python3
"""
check-parity.py — verify every integration package's embedded copy of the
tool-profile contract agrees with contracts/integration-tool-contract.json.

The contract is the single source of truth for the model-facing tool surface
of every package in this repository: the four profile names
(observe/govern/execute/full), the exact tool set each profile exposes, the
default profile, and the execute_decision fields that must never become
model-facing. Each tool-exposing package carries a runtime-embedded copy of
that boundary (a `contract.py` / `contract.ts` module) because the JSON file
is not on disk once the package is installed from PyPI/npm. Those embedded
copies are what drift; this script is the monorepo-level gate that catches
drift in one run, across both languages, instead of relying on each package's
own `test_contract_parity` suite being executed separately.

What it checks, in order:

  1. The contract file itself is present, valid JSON, and self-consistent:
     exactly the four profiles, a single default (observe), a valid
     extends chain, the "*" sentinel + opt_in_only on "full", and exact
     two-way agreement between mcp_tool_reference and the profile tool lists
     (a tool name may not exist in one without the other).
  2. Every package that embeds the contract agrees with it:
       - python/<pkg>/<module>/contract.py  (parsed statically with `ast`)
       - typescript/<workspace>/**/contract.ts  (parsed statically with a small
         regex evaluator; node_modules and build output are never walked)
     Each embedded module must declare exactly the contract's four profiles,
     the contract's exact per-profile tool sets, the contract's default
     profile, the "*" sentinel for "full", and the contract's exact
     never-model-facing field set.
  3. Packages WITHOUT an embedded contract module (e.g. serving/deployment
     integrations that never expose model-facing MCP tools) are reported as
     skipped, not failed — there is no boundary for them to drift from.

Static parsing is deliberate: importing a package's module would execute
arbitrary package code in CI and would require every package's dependencies
to be installed. The embedded contract modules are pure module-level
constant declarations by convention; a module that computes its constants
instead of declaring them fails closed with a parse error explaining the
required shape.

No network access, no third-party dependencies, fully deterministic.

Invoked from two CI jobs with different working directories
(.github/workflows/ci.yml and sync-on-sdk-release.yml), so the repository
root is resolved from this file's location, not from the caller's cwd;
`--root` overrides it (used by this script's own regression tests).

Exit codes:
  0  contract is self-consistent and every embedded copy agrees with it
  1  one or more violations found (see stdout for details)
  2  usage / internal error (e.g. the contract file is missing or malformed,
     so there is no source of truth to check against)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_ROOT = SCRIPT_PATH.parent.parent
CONTRACT_RELATIVE_PATH = Path("contracts") / "integration-tool-contract.json"

#: The contract's fixed profile vocabulary. The per-package parity tests hardcode the same
#: four names; a fifth profile is a contract amendment, not something a package may add.
EXPECTED_PROFILES = ("observe", "govern", "execute", "full")

FULL_PROFILE_SENTINEL = "*"

#: MCP tool-registry names are lowercase snake_case (get_contract, query_data, ...).
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# ── directories we never walk into when discovering TypeScript modules ──────
EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".turbo",
    ".pnpm-store",
}

CONTRACT_POINTER = (
    "The contract file contracts/integration-tool-contract.json is the single source of "
    "truth. Either fix the package's embedded contract module to match it, or amend the "
    "contract deliberately in the same change -- never let the two drift."
)


@dataclass
class Violation:
    path: Path
    reason: str

    def render(self, root: Path) -> str:
        try:
            rel = self.path.relative_to(root)
        except ValueError:
            rel = self.path
        return f"  {rel}: {self.reason}"


class ContractParseError(Exception):
    """An embedded contract module could not be statically evaluated."""


# ── the embedded constants one module must declare ──────────────────────────
@dataclass
class EmbeddedContract:
    tool_profiles: dict[str, frozenset[str] | str]
    default_profile: str
    never_model_facing_fields: frozenset[str]
    declared_profile_names: frozenset[str] | None  # the ToolProfile type/Literal, if declared


# ════════════════════════════════════════════════════════════════════════════
# Phase 1 — the contract file itself
# ════════════════════════════════════════════════════════════════════════════


def load_contract(root: Path) -> dict:
    """Load the contract file, exiting fatally (code 2) if it cannot be the source of truth."""
    path = root / CONTRACT_RELATIVE_PATH
    if not path.is_file():
        print(f"check-parity: contract file missing at {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"check-parity: contract file is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not isinstance(contract, dict):
        print(
            "check-parity: contract file must contain a JSON object at the top level",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return contract


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) for item in value)


def validate_contract(contract: dict, contract_path: Path) -> list[Violation]:
    """Self-consistency checks on the source of truth itself."""
    violations: list[Violation] = []

    def bad(reason: str) -> None:
        violations.append(Violation(contract_path, reason))

    profiles = contract.get("profiles")
    if not isinstance(profiles, dict):
        return [Violation(contract_path, "contract has no 'profiles' object")]

    unknown = sorted(set(profiles) - set(EXPECTED_PROFILES))
    missing = sorted(set(EXPECTED_PROFILES) - set(profiles))
    if unknown:
        bad(
            f"contract declares profile(s) outside the fixed vocabulary {list(EXPECTED_PROFILES)}: {unknown}"
        )
    if missing:
        bad(f"contract is missing required profile(s): {missing}")
    for name, entry in profiles.items():
        if not isinstance(entry, dict):
            bad(f"profiles.{name} must be an object")

    defaults = [
        name
        for name, entry in profiles.items()
        if isinstance(entry, dict) and entry.get("default") is True
    ]
    if defaults != ["observe"]:
        bad(
            f"exactly one profile may carry 'default: true' and it must be 'observe'; found {defaults or 'none'}. "
            "The default profile is one shared decision, not per-package."
        )

    observe = profiles.get("observe", {})
    if isinstance(observe, dict) and not _is_string_list(observe.get("tools")):
        bad("profiles.observe.tools must be a non-empty list of tool names")

    for name, parent in (("govern", "observe"), ("execute", "govern")):
        entry = profiles.get(name, {})
        if not isinstance(entry, dict):
            continue
        if entry.get("extends") != parent:
            bad(
                f"profiles.{name}.extends must be {parent!r} (the profile chain is fixed), found {entry.get('extends')!r}"
            )
        if not _is_string_list(entry.get("adds_tools")):
            bad(f"profiles.{name}.adds_tools must be a non-empty list of tool names")

    full = profiles.get("full", {})
    if isinstance(full, dict):
        if full.get("tools") != FULL_PROFILE_SENTINEL:
            bad(
                "profiles.full.tools must be the '*' sentinel (the full live registry, enumerated at runtime)"
            )
        if full.get("opt_in_only") is not True:
            bad(
                "profiles.full.opt_in_only must be true -- the full registry is never a model-facing default"
            )

    execute = profiles.get("execute", {})
    never_fields = execute.get("never_model_facing_fields") if isinstance(execute, dict) else None
    if not _is_string_list(never_fields):
        bad("profiles.execute.never_model_facing_fields must be a non-empty list of field names")

    reference = contract.get("mcp_tool_reference")
    if not isinstance(reference, dict) or not reference:
        bad("contract has no non-empty 'mcp_tool_reference' object")
        reference = {}

    if violations:
        return violations

    # Cross-checks that need a well-formed skeleton first.
    assert isinstance(observe.get("tools"), list)
    assert isinstance(profiles["govern"].get("adds_tools"), list)
    assert isinstance(profiles["execute"].get("adds_tools"), list)
    profiled_names = (
        set(observe["tools"])
        | set(profiles["govern"]["adds_tools"])
        | set(profiles["execute"]["adds_tools"])
    )
    referenced_names = set(reference)

    for name in sorted(profiled_names | referenced_names):
        if not TOOL_NAME_RE.match(name):
            bad(
                f"tool name {name!r} is not lowercase snake_case — the MCP tool-registry naming convention"
            )

    unreferenced = sorted(profiled_names - referenced_names)
    if unreferenced:
        bad(f"profile tool list(s) name tool(s) absent from mcp_tool_reference: {unreferenced}")
    unprofiled = sorted(referenced_names - profiled_names)
    if unprofiled:
        bad(
            f"mcp_tool_reference name(s) belong to no profile: {unprofiled} — every referenced tool must be "
            "assigned to a profile (see the per-package parity tests, which assert this same invariant)"
        )

    for name, entry in sorted(reference.items()):
        if isinstance(entry, dict) and entry.get("safety_critical") is True:
            if name not in profiles["execute"]["adds_tools"]:
                bad(
                    f"mcp_tool_reference.{name} is marked safety_critical but is not added by the 'execute' "
                    "profile — a safety-critical tool must be reachable only at the execute tier"
                )

    return violations


def expected_profile_sets(contract: dict) -> dict[str, frozenset[str] | str]:
    """Resolve the contract's profiles (via the extends chain) to absolute tool sets."""
    profiles = contract["profiles"]
    observe = frozenset(profiles["observe"]["tools"])
    govern = observe | frozenset(profiles["govern"]["adds_tools"])
    execute = govern | frozenset(profiles["execute"]["adds_tools"])
    return {"observe": observe, "govern": govern, "execute": execute, "full": FULL_PROFILE_SENTINEL}


# ════════════════════════════════════════════════════════════════════════════
# Phase 2a — static evaluation of Python contract modules
# ════════════════════════════════════════════════════════════════════════════

_UNRESOLVED = object()


def _eval_python_node(node: ast.expr, env: dict[str, object]) -> object:
    """Evaluate the tiny declarative subset an embedded contract module is allowed to use."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id, _UNRESOLVED)
    if isinstance(node, ast.Set):
        values = [_eval_python_node(elt, env) for elt in node.elts]
        return _UNRESOLVED if any(v is _UNRESOLVED for v in values) else frozenset(values)
    if isinstance(node, (ast.List, ast.Tuple)):
        values = [_eval_python_node(elt, env) for elt in node.elts]
        return _UNRESOLVED if any(v is _UNRESOLVED for v in values) else list(values)
    if isinstance(node, ast.Dict):
        result: dict[object, object] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            key = _eval_python_node(key_node, env) if key_node is not None else _UNRESOLVED
            value = _eval_python_node(value_node, env)
            if key is _UNRESOLVED or value is _UNRESOLVED:
                return _UNRESOLVED
            result[key] = value
        return result
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set"}
    ):
        if len(node.args) != 1 or node.keywords:
            return _UNRESOLVED
        value = _eval_python_node(node.args[0], env)
        return _UNRESOLVED if value is _UNRESOLVED else frozenset(value)  # type: ignore[arg-type]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _eval_python_node(node.left, env)
        right = _eval_python_node(node.right, env)
        if left is _UNRESOLVED or right is _UNRESOLVED:
            return _UNRESOLVED
        return frozenset(left) | frozenset(right)  # type: ignore[arg-type]
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Literal"
    ):
        # `ToolProfile = Literal["observe", ...]` — evaluate the slice, not the subscript itself.
        return _eval_python_node(node.slice, env)
    return _UNRESOLVED


def evaluate_python_contract(path: Path) -> EmbeddedContract:
    """Statically read the embedded constants out of a `contract.py` module (never imports it)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ContractParseError(f"cannot parse as Python: {exc}") from exc

    env: dict[str, object] = {}
    for node in tree.body:
        target: ast.Name | None = None
        value_node: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value_node = node.targets[0], node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            target, value_node = node.target, node.value
        if target is None or value_node is None:
            continue
        value = _eval_python_node(value_node, env)
        if value is not _UNRESOLVED:
            env[target.id] = value

    def require(name: str) -> object:
        if name not in env:
            raise ContractParseError(
                f"could not statically resolve {name!r} — embedded contract modules must keep the shared "
                "declarative shape (module-level string/frozenset/dict constants only, no computed values); "
                "match a sibling package's contract module"
            )
        return env[name]

    profiles_value = require("TOOL_PROFILES")
    if not isinstance(profiles_value, dict):
        raise ContractParseError(
            "TOOL_PROFILES must be a dict literal of profile name -> frozenset or '*'"
        )
    tool_profiles: dict[str, frozenset[str] | str] = {}
    for key, value in profiles_value.items():
        if isinstance(value, (frozenset, set)):
            tool_profiles[str(key)] = frozenset(str(item) for item in value)
        elif isinstance(value, str):
            tool_profiles[str(key)] = value
        else:
            raise ContractParseError(
                f"TOOL_PROFILES[{key!r}] must be a frozenset of tool names or the '*' sentinel"
            )

    default_profile = require("DEFAULT_PROFILE")
    if not isinstance(default_profile, str):
        raise ContractParseError("DEFAULT_PROFILE must be a string constant")

    never_fields = require("NEVER_MODEL_FACING_FIELDS")
    if not isinstance(never_fields, (frozenset, set)):
        raise ContractParseError("NEVER_MODEL_FACING_FIELDS must be a frozenset of field names")

    declared: frozenset[str] | None = None
    tool_profile_type = env.get("ToolProfile")
    if isinstance(tool_profile_type, list) and all(
        isinstance(item, str) for item in tool_profile_type
    ):
        declared = frozenset(tool_profile_type)

    return EmbeddedContract(
        tool_profiles=tool_profiles,
        default_profile=default_profile,
        never_model_facing_fields=frozenset(str(item) for item in never_fields),
        declared_profile_names=declared,
    )


# ════════════════════════════════════════════════════════════════════════════
# Phase 2b — static evaluation of TypeScript contract modules
# ════════════════════════════════════════════════════════════════════════════

_TS_STRING_DECL_RE = re.compile(
    r"(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=;]+?)?\s*=\s*(['\"])((?:\\.|(?!\2).)*?)\2\s*(?:as\s+const)?\s*;"
)
_TS_SET_DECL_RE = re.compile(
    r"(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=;]+?)?\s*=\s*new\s+Set(?:\s*<[^>]*>)?\s*\(\s*\[([\s\S]*?)\]\s*\)\s*;"
)
_TS_PROFILES_RE = re.compile(r"TOOL_PROFILES\s*(?::\s*[^=;]+?)?\s*=\s*\{([\s\S]*?)\}\s*;")
_TS_PROFILE_ENTRY_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*:\s*([A-Za-z_$][\w$]*)")
_TS_TYPE_LITERALS_RE = re.compile(r"type\s+ToolProfile\s*=\s*([^;]+);")
_TS_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")


def _strip_ts_comments(source: str) -> str:
    """Remove // and /* */ comments without touching string/template literals."""
    out: list[str] = []
    i = 0
    quote: str | None = None
    while i < len(source):
        char = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if quote is not None:
            out.append(char)
            if char == "\\" and i + 1 < len(source):
                out.append(source[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
        elif char in {"'", '"', "`"}:
            quote = char
            out.append(char)
            i += 1
        elif char == "/" and nxt == "/":
            while i < len(source) and source[i] != "\n":
                i += 1
        elif char == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            i = len(source) if end == -1 else end + 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def evaluate_typescript_contract(path: Path) -> EmbeddedContract:
    """Statically read the embedded constants out of a `contract.ts` module (never executes it)."""
    source = _strip_ts_comments(path.read_text(encoding="utf-8"))

    strings: dict[str, str] = {m.group(1): m.group(3) for m in _TS_STRING_DECL_RE.finditer(source)}
    sets: dict[str, frozenset[str]] = {}
    for match in _TS_SET_DECL_RE.finditer(source):
        name, body = match.group(1), match.group(2)
        elements: set[str] = set()
        for raw_element in body.split(","):
            element = raw_element.strip()
            if not element:
                continue
            if element.startswith("..."):
                spread = sets.get(element[3:].strip())
                if spread is None:
                    raise ContractParseError(
                        f"new Set([...]) spread of unknown identifier {element[3:].strip()!r} in {name!r} — "
                        "keep the shared declarative shape (string constants and Set spreads of earlier constants)"
                    )
                elements |= spread
            elif element[0] in {"'", '"'}:
                elements.add(element.strip("'\""))
            else:
                resolved = strings.get(element)
                if resolved is None:
                    raise ContractParseError(
                        f"new Set([...]) element {element!r} in {name!r} is not a known string constant — "
                        "keep the shared declarative shape"
                    )
                elements.add(resolved)
        sets[name] = frozenset(elements)

    def require_string(name: str) -> str:
        value = strings.get(name)
        if value is None:
            raise ContractParseError(f"could not statically resolve string constant {name!r}")
        return value

    profiles_match = _TS_PROFILES_RE.search(source)
    if profiles_match is None:
        raise ContractParseError("could not find the TOOL_PROFILES object literal")
    tool_profiles: dict[str, frozenset[str] | str] = {}
    for entry in _TS_PROFILE_ENTRY_RE.finditer(profiles_match.group(1)):
        profile_name, identifier = entry.group(1), entry.group(2)
        if identifier in sets:
            tool_profiles[profile_name] = sets[identifier]
        elif identifier in strings:
            tool_profiles[profile_name] = strings[identifier]
        else:
            raise ContractParseError(
                f"TOOL_PROFILES.{profile_name} references unknown identifier {identifier!r}"
            )

    never_fields = sets.get("NEVER_MODEL_FACING_FIELDS")
    if never_fields is None:
        raise ContractParseError(
            "could not statically resolve NEVER_MODEL_FACING_FIELDS (expected a new Set([...]) constant)"
        )

    declared: frozenset[str] | None = None
    type_match = _TS_TYPE_LITERALS_RE.search(source)
    if type_match is not None:
        declared = frozenset(_TS_QUOTED_RE.findall(type_match.group(1)))

    return EmbeddedContract(
        tool_profiles=tool_profiles,
        default_profile=require_string("DEFAULT_PROFILE"),
        never_model_facing_fields=never_fields,
        declared_profile_names=declared,
    )


# ════════════════════════════════════════════════════════════════════════════
# Phase 2c — discovery + comparison
# ════════════════════════════════════════════════════════════════════════════


def discover_python_contracts(root: Path) -> tuple[list[Path], list[str], list[Violation]]:
    """Find every Python package's embedded contract module under python/*/."""
    modules: list[Path] = []
    skipped: list[str] = []
    violations: list[Violation] = []
    python_dir = root / "python"
    if not python_dir.is_dir():
        return modules, skipped, violations
    for package_dir in sorted(python_dir.iterdir()):
        if not package_dir.is_dir() or not (package_dir / "pyproject.toml").is_file():
            continue
        matches = sorted(
            path
            for path in package_dir.glob("*/contract.py")
            if not any(part in EXCLUDED_DIR_NAMES for part in path.parts)
        )
        if len(matches) > 1:
            violations.append(
                Violation(
                    package_dir,
                    f"multiple embedded contract modules found ({matches}); exactly one is allowed",
                )
            )
        elif len(matches) == 1:
            modules.append(matches[0])
        else:
            skipped.append(str(package_dir.relative_to(root)))
    return modules, skipped, violations


def discover_typescript_contracts(root: Path) -> tuple[list[Path], list[str]]:
    """Find every TypeScript workspace's embedded contract.ts modules."""
    modules: list[Path] = []
    skipped: list[str] = []
    typescript_dir = root / "typescript"
    if not typescript_dir.is_dir():
        return modules, skipped
    for workspace_dir in sorted(typescript_dir.iterdir()):
        if not workspace_dir.is_dir() or not (workspace_dir / "package.json").is_file():
            continue
        found = [
            path
            for path in workspace_dir.rglob("contract.ts")
            if path.is_file() and not any(part in EXCLUDED_DIR_NAMES for part in path.parts)
        ]
        if found:
            modules.extend(sorted(found))
        else:
            skipped.append(str(workspace_dir.relative_to(root)))
    return modules, skipped


def check_embedded_contract(
    path: Path,
    embedded: EmbeddedContract,
    expected_sets: dict[str, frozenset[str] | str],
    contract: dict,
) -> list[Violation]:
    violations: list[Violation] = []

    def bad(reason: str) -> None:
        violations.append(Violation(path, reason))

    declared = set(embedded.tool_profiles)
    expected = set(EXPECTED_PROFILES)
    unknown = sorted(declared - expected)
    missing = sorted(expected - declared)
    if unknown:
        bad(
            f"TOOL_PROFILES declares profile(s) the contract does not define: {unknown} — a package must not "
            f"invent its own profile boundaries; the contract defines exactly {list(EXPECTED_PROFILES)}"
        )
    if missing:
        bad(f"TOOL_PROFILES is missing contract-required profile(s): {missing}")

    if (
        embedded.declared_profile_names is not None
        and set(embedded.declared_profile_names) != expected
    ):
        bad(
            f"the ToolProfile type declares {sorted(embedded.declared_profile_names)} but the contract defines "
            f"exactly {list(EXPECTED_PROFILES)} — the type and TOOL_PROFILES must not drift apart"
        )

    for profile in EXPECTED_PROFILES:
        actual = embedded.tool_profiles.get(profile)
        if actual is None:
            continue
        if profile == "full":
            if actual != FULL_PROFILE_SENTINEL:
                bad(
                    "TOOL_PROFILES['full'] must be the '*' sentinel (the full live registry, discovered at "
                    "runtime) — mirroring profiles.full.tools == '*' in the contract"
                )
            continue
        if isinstance(actual, str):
            bad(
                f"TOOL_PROFILES[{profile!r}] must be a set of tool names, not the {actual!r} sentinel"
            )
            continue
        wanted = expected_sets[profile]
        assert isinstance(wanted, frozenset)
        omitted = sorted(wanted - actual)
        invented = sorted(actual - wanted)
        if omitted:
            bad(
                f"TOOL_PROFILES[{profile!r}] omits tool(s) the contract requires for this profile: {omitted} — "
                "every tool the contract assigns to a profile must be advertised to the model by every package"
            )
        if invented:
            bad(
                f"TOOL_PROFILES[{profile!r}] exposes tool(s) the contract does not assign to this profile: "
                f"{invented} — a package must not hand the model tools the contract does not sanction; remove "
                "them or amend the contract deliberately"
            )

    if embedded.default_profile != "observe":
        bad(
            f"DEFAULT_PROFILE is {embedded.default_profile!r} but the contract's default is 'observe' "
            "(profiles.observe.default is true) — the default profile is one shared decision, not per-package"
        )

    contract_never = frozenset(contract["profiles"]["execute"]["never_model_facing_fields"])
    if embedded.never_model_facing_fields != contract_never:
        leaked = sorted(contract_never - embedded.never_model_facing_fields)
        extra = sorted(embedded.never_model_facing_fields - contract_never)
        parts = [
            f"NEVER_MODEL_FACING_FIELDS must be exactly {sorted(contract_never)} "
            "(profiles.execute.never_model_facing_fields)"
        ]
        if leaked:
            parts.append(
                f"missing {leaked} — an operator/break-glass field the contract keeps out of the model's reach "
                "would stop being stripped from model-facing tool schemas"
            )
        if extra:
            parts.append(
                f"unexpected {extra} — a package must not strip fields the contract does not list"
            )
        bad("; ".join(parts))

    return violations


# ════════════════════════════════════════════════════════════════════════════
# Driver
# ════════════════════════════════════════════════════════════════════════════


def run(root: Path) -> tuple[list[Violation], list[str]]:
    """Returns (violations, progress notes). Never raises for expected failure shapes."""
    contract = load_contract(root)
    contract_path = root / CONTRACT_RELATIVE_PATH

    notes: list[str] = []
    violations = validate_contract(contract, contract_path)
    if violations:
        notes.append(
            "contract file failed self-consistency checks; package modules were not examined"
        )
        return violations, notes

    expected_sets = expected_profile_sets(contract)
    notes.append(
        f"contract file is self-consistent ({len(EXPECTED_PROFILES)} profiles, "
        f"{len(contract['mcp_tool_reference'])} referenced tools)"
    )

    python_modules, python_skipped, discovery_violations = discover_python_contracts(root)
    violations.extend(discovery_violations)
    typescript_modules, typescript_skipped = discover_typescript_contracts(root)

    checked = 0
    for path in python_modules:
        rel = path.relative_to(root)
        try:
            embedded = evaluate_python_contract(path)
        except ContractParseError as exc:
            violations.append(Violation(path, f"embedded contract module fails closed: {exc}"))
            continue
        module_violations = check_embedded_contract(path, embedded, expected_sets, contract)
        violations.extend(module_violations)
        if not module_violations:
            checked += 1
            notes.append(f"ok   {rel}")

    for path in typescript_modules:
        rel = path.relative_to(root)
        try:
            embedded = evaluate_typescript_contract(path)
        except ContractParseError as exc:
            violations.append(Violation(path, f"embedded contract module fails closed: {exc}"))
            continue
        module_violations = check_embedded_contract(path, embedded, expected_sets, contract)
        violations.extend(module_violations)
        if not module_violations:
            checked += 1
            notes.append(f"ok   {rel}")

    for skipped in python_skipped + typescript_skipped:
        notes.append(
            f"skip {skipped} (no embedded contract module — does not expose model-facing MCP tools)"
        )
    notes.append(
        f"{checked} embedded contract module(s) verified, {len(python_skipped) + len(typescript_skipped)} package(s) skipped"
    )
    return violations, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Repository root to check (default: resolved from this script's location, so any cwd works)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    violations, notes = run(root)

    for note in notes:
        print(f"check-parity: {note}")

    if not violations:
        print(
            "check-parity: clean — every embedded contract module agrees with contracts/integration-tool-contract.json"
        )
        return 0

    print(f"\ncheck-parity: {len(violations)} violation(s) found under {root}\n")
    for violation in violations:
        print(violation.render(root))
    print(f"\n{CONTRACT_POINTER}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
