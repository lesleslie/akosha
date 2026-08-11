"""Regression tests for Akosha MCP tool profile drift.

These tests verify that the registration metadata declared in
``akosha/mcp/tools/profiles.py`` stays in sync with the gating logic
in ``akosha/mcp/tools/__init__.py::register_all_tools``. Documented in
``docs/architecture/MEMORY_ARCHITECTURE.md`` (Section 5 — search for
"profile drift" and "Contract 5.x").

Two drift vectors are covered:

1. Names listed in ``PROFILE_REGISTRATIONS`` that aren't actually
   dispatched by ``register_all_tools`` (orphans).
2. ``REGISTRATION_DESCRIPTIONS`` and ``REGISTRATION_TOOLS`` drifting
   out of sync (one dict contains a key the other does not).

Implementation note: the tests are source-AST only — they parse
profiles.py to read constants and __init__.py to inspect the
gating blocks. No runtime imports of ``akosha.mcp.tools`` are
performed because the package's import chain currently fails
collection (a separate pre-existing pydantic forward-reference bug
in ``akosha/storage/models.py`` — out of scope here). If a test
fails, that IS the drift signal — surface it, do not paper over it.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import Any

# Locate the akosha package source. The tests directory sits next to
# the package in this repo (../akosha/), but be lenient — search
# upward for an ``akosha/mcp/tools`` layout so the test is also
# runnable from a copy in another directory.
_THIS_FILE = Path(__file__).resolve()
_CANDIDATES: list[Path] = []
for ancestor in [_THIS_FILE.parent, *_THIS_FILE.parents]:
    candidate = ancestor / "akosha" / "mcp" / "tools"
    if candidate.is_dir():
        _CANDIDATES.append(candidate)
        break
# Fallback: in-repo layout (tests/ and akosha/ are siblings).
if not _CANDIDATES:
    _CANDIDATES.append(_THIS_FILE.parents[2] / "akosha" / "mcp" / "tools")
PACKAGE_ROOT: Path = _CANDIDATES[0]
PROFILES_PATH = PACKAGE_ROOT / "profiles.py"
INIT_PATH = PACKAGE_ROOT / "__init__.py"

UNCONDITIONAL_HEALTH_REGISTER = "register_health_tools_akosha"
REGISTER_ALL_TOOLS_FN_NAME = "register_all_tools"


def _read_source(path: Path) -> str:
    """Read a source file as a single string. Raises if missing."""
    if not path.is_file():
        pytest_skip = pytest.skip if "pytest" in sys.modules else None  # noqa: F841
        raise FileNotFoundError(
            f"Required source not found: {path}. "
            f"Tried candidates: {[_CANDIDATES[0], _CANDIDATES[1]]}"
        )
    return path.read_text(encoding="utf-8")


def _parse_module(path: Path) -> ast.Module:
    """Parse a Python source file into an AST module. Raises on syntax error."""
    return ast.parse(_read_source(path), filename=str(path))


def _extract_module_constants(path: Path, names: list[str]) -> dict[str, Any]:
    """Extract top-level constants from a module's AST.

    Supports both ``X = ...`` (``ast.Assign``) and ``X: T = ...``
    (``ast.AnnAssign``) forms. Resolves starred references inside
    list literals by looking up the named constant in the scope.

    To make star resolution work, this function ALSO populates the
    scope with every other top-level constant it encounters in the
    same file — even if not requested. profiles.py declares constants
    in dependency order, so the transitively-referenced names are
    already resolved by the time we reach the dependent one.
    """
    tree = _parse_module(path)
    out: dict[str, Any] = {}
    name_set = set(names)
    for stmt in tree.body:
        target_id: str | None = None
        value_node: ast.AST | None = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            tgt = stmt.targets[0]
            if isinstance(tgt, ast.Name):
                target_id = tgt.id
                value_node = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.value is not None:
                target_id = stmt.target.id
                value_node = stmt.value
        if target_id is None or value_node is None:
            continue
        # Always compute the value so that later starred references
        # (``*STANDARD_REGISTRATIONS``) can resolve. profiles.py is
        # small — the cost of evaluating every constant is trivial.
        out[target_id] = _eval_literal(value_node, out)
    return {n: out[n] for n in names if n in out}


def _eval_literal(node: ast.AST, scope: dict[str, Any]) -> Any:
    """Evaluate an AST literal expression against ``scope``.

    Supports dict/list/set/scalars + starred references to names in
    ``scope`` (for ``[*OTHER_CONST, "x"]`` patterns). Returns a
    ``"<unparsed:...>"`` sentinel for anything else so callers can
    still see the failure rather than crashing.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        # Bare name in a list/dict position — could be a starred target's
        # inner expression; the caller (``_extract_list_with_staritems``)
        # handles that case via ``ast.Starred``.
        if node.id in scope:
            return scope[node.id]
        return f"<unresolved:{node.id}>"
    if isinstance(node, (ast.List, ast.Tuple)):
        return _extract_list_with_staritems(node, scope)
    if isinstance(node, ast.Set):
        return {_eval_literal(elt, scope) for elt in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _eval_literal(k, scope): _eval_literal(v, scope)
            for k, v in zip(node.keys, node.values)
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_literal(node.operand, scope)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _eval_literal(node.left, scope)
    return f"<unparsed:{type(node).__name__}>"


def _extract_list_with_staritems(
    node: ast.AST,
    scope: dict[str, Any],
) -> list[Any]:
    """Resolve a list literal that may include starred references to other constants.

    Handles patterns like ``[*MINIMAL_REGISTRATIONS, "register_X"]``
    by looking up ``MINIMAL_REGISTRATIONS`` in ``scope`` and flattening
    its contents into the result.
    """
    assert isinstance(node, ast.List), f"expected list literal, got {type(node).__name__}"
    out: list[Any] = []
    for elt in node.elts:
        if isinstance(elt, ast.Starred):
            ref = _eval_literal(elt.value, scope)
            if isinstance(ref, list):
                out.extend(ref)
            elif isinstance(ref, str):
                out.append(ref)
            else:
                out.extend(list(ref))
        else:
            out.append(_eval_literal(elt, scope))
    return out


def _has_profile_gated_block(init_source: str, name: str) -> bool:
    """Return True if register_all_tools has an `if "<name>" in allowed:` block."""
    needle = f'if "{name}" in allowed'
    return needle in init_source


def _has_unconditional_health_call(init_source: str) -> bool:
    """Return True if register_all_tools calls register_health_tools_akosha(app) unconditionally.

    "Unconditional" means the call exists AND is NOT gated by
    ``if "register_health_tools_akosha" in allowed:``. The latter would
    silently strip health endpoints from minimal-profile deployments.
    """
    if "register_health_tools_akosha(app)" not in init_source:
        return False
    return f'if "{UNCONDITIONAL_HEALTH_REGISTER}" in allowed' not in init_source


def test_no_orphan_registrations() -> None:
    """Every name in PROFILE_REGISTRATIONS must be reachable from register_all_tools.

    A name is "reachable" if EITHER:
    - it appears as a key in ``_ALL_REGISTERS`` (eager dispatch), OR
    - it appears as a ``if "<name>" in allowed:`` block in
      ``register_all_tools`` (gated dispatch, possibly with a lazy
      import inside the block).

    Special case: ``register_health_tools_akosha`` is mandatory and is
    invoked unconditionally — see :func:`test_health_register_always_called`.

    Walking every profile tier catches drift in any one of them
    (e.g. a name only orphaned in STANDARD but fine in FULL).
    """
    init_source = _read_source(INIT_PATH)

    # Find _ALL_REGISTERS keys via AST (literal dict with string keys
    # whose values are callables we can't evaluate; we only need keys).
    tree = _parse_module(INIT_PATH)
    eager_keys: set[str] = set()
    for stmt in tree.body:
        if not isinstance(stmt, ast.AnnAssign):
            continue
        if not (isinstance(stmt.target, ast.Name) and stmt.target.id == "_ALL_REGISTERS"):
            continue
        if not isinstance(stmt.value, ast.Dict):
            continue
        for key in stmt.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                eager_keys.add(key.value)

    # Resolve PROFILE_REGISTRATIONS to a {profile_label: [names]} mapping.
    # ToolProfile.X keys aren't evaluable; the dict is built via
    # ``{ToolProfile.MINIMAL: MINIMAL_REGISTRATIONS, ...}``, so we use the
    # order of subscription to label profiles.
    profile_constants = _extract_module_constants(
        PROFILES_PATH,
        ["MINIMAL_REGISTRATIONS", "STANDARD_REGISTRATIONS", "FULL_REGISTRATIONS"],
    )
    profile_lists: dict[str, list[str]] = {
        "MINIMAL": profile_constants["MINIMAL_REGISTRATIONS"],
        "STANDARD": profile_constants["STANDARD_REGISTRATIONS"],
        "FULL": profile_constants["FULL_REGISTRATIONS"],
    }

    for profile_label, names in profile_lists.items():
        for name in names:
            if name == UNCONDITIONAL_HEALTH_REGISTER:
                continue
            if name in eager_keys:
                continue
            assert _has_profile_gated_block(init_source, name), (
                f"Orphan registration: '{name}' is declared in "
                f"{profile_label}_REGISTRATIONS but is missing from "
                f"both _ALL_REGISTERS and the `if \"{name}\" in allowed:` "
                f"dispatch in register_all_tools. Either add the "
                f"gating block or remove the orphan from profiles.py."
            )


def test_registration_dicts_in_sync() -> None:
    """REGISTRATION_DESCRIPTIONS and REGISTRATION_TOOLS must share the same key set.

    Drift here means a group has a human description but no exported
    tools (or vice versa), so the ``discover_tools`` meta-tool will
    surface a description-with-no-tools phantom.
    """
    consts = _extract_module_constants(
        PROFILES_PATH,
        ["REGISTRATION_DESCRIPTIONS", "REGISTRATION_TOOLS"],
    )
    desc_keys = set(consts["REGISTRATION_DESCRIPTIONS"].keys())
    tool_keys = set(consts["REGISTRATION_TOOLS"].keys())

    only_in_desc = desc_keys - tool_keys
    only_in_tools = tool_keys - desc_keys

    assert only_in_desc == set(), (
        f"Keys present in REGISTRATION_DESCRIPTIONS but missing from "
        f"REGISTRATION_TOOLS (no exported tools): {sorted(only_in_desc)}"
    )
    assert only_in_tools == set(), (
        f"Keys present in REGISTRATION_TOOLS but missing from "
        f"REGISTRATION_DESCRIPTIONS (no description): "
        f"{sorted(only_in_tools)}"
    )


def test_profile_subset_invariant() -> None:
    """Profile tiers must be nested: MINIMAL ⊆ STANDARD ⊆ FULL.

    A higher tier must never have fewer groups than a lower tier; the
    ``discover_tools`` meta-tool relies on this nesting to compute
    ``not_loaded_tools = all - loaded``.
    """
    consts = _extract_module_constants(
        PROFILES_PATH,
        ["MINIMAL_REGISTRATIONS", "STANDARD_REGISTRATIONS", "FULL_REGISTRATIONS"],
    )
    minimal = set(consts["MINIMAL_REGISTRATIONS"])
    standard = set(consts["STANDARD_REGISTRATIONS"])
    full = set(consts["FULL_REGISTRATIONS"])

    missing_in_standard = minimal - standard
    missing_in_full = standard - full

    assert missing_in_standard == set(), (
        f"MINIMAL_REGISTRATIONS contains names not in "
        f"STANDARD_REGISTRATIONS (breaks MINIMAL ⊆ STANDARD): "
        f"{sorted(missing_in_standard)}"
    )
    assert missing_in_full == set(), (
        f"STANDARD_REGISTRATIONS contains names not in FULL_REGISTRATIONS "
        f"(breaks STANDARD ⊆ FULL): {sorted(missing_in_full)}"
    )


def test_registered_tool_names_match_export() -> None:
    """Every FULL-tier registration must export at least one tool.

    If a register_* group has an empty ``REGISTRATION_TOOLS`` entry,
    the profile promises tools that don't materialize — the discover
    meta-tool will report it as loaded_count=0, which is a misleading
    UI contract.
    """
    consts = _extract_module_constants(
        PROFILES_PATH, ["FULL_REGISTRATIONS", "REGISTRATION_TOOLS"]
    )
    full_list = consts["FULL_REGISTRATIONS"]
    tools_dict = consts["REGISTRATION_TOOLS"]

    empty_groups = [n for n in full_list if len(tools_dict.get(n, [])) == 0]

    assert empty_groups == [], (
        f"FULL_REGISTRATIONS groups with empty REGISTRATION_TOOLS list "
        f"(profile claims these but exports nothing): "
        f"{empty_groups}"
    )


def test_health_register_always_called() -> None:
    """register_health_tools_akosha must be invoked unconditionally.

    Health probes are mandatory infrastructure for every deployment
    (liveness/readiness checks feed orchestrator load-balancers), so
    the call must NOT be gated behind a profile check. If a future
    refactor accidentally moves the call inside
    ``if "register_health_tools_akosha" in allowed:``, the minimal
    profile would silently ship without health endpoints.
    """
    init_source = _read_source(INIT_PATH)

    assert _has_unconditional_health_call(init_source), (
        f"register_health_tools_akosha(app) must be called "
        f"unconditionally in register_all_tools. Found the call but it "
        f"appears to be gated by "
        f'`if "{UNCONDITIONAL_HEALTH_REGISTER}" in allowed:`, which '
        f"would break minimal-profile deployments (no health endpoints)."
    )


def test_register_all_tools_signature_present() -> None:
    """Smoke check: register_all_tools is defined with the expected parameters.

    Belt-and-suspenders for the profile-drift tests: if someone renamed
    ``register_all_tools`` or stripped its positional parameters, the
    introspection-based tests above would silently no-op.
    """
    tree = _parse_module(INIT_PATH)
    found = False
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == REGISTER_ALL_TOOLS_FN_NAME:
            args = stmt.args
            param_names = {a.arg for a in args.args}
            for expected in ("app", "embedding_service", "analytics_service", "graph_builder", "hot_store"):
                assert expected in param_names, (
                    f"register_all_tools is missing expected parameter '{expected}'. "
                    f"Current parameters: {sorted(param_names)}"
                )
            found = True
            break
    assert found, f"Function '{REGISTER_ALL_TOOLS_FN_NAME}' not found in {INIT_PATH}"


def test_drift_test_targets_resolve() -> None:
    """The two files this suite introspects must exist.

    Catches the case where someone reorganizes the package layout and
    the assertions above silently test against empty/missing files.
    """
    assert PROFILES_PATH.is_file(), (
        f"profiles.py not found at expected location: {PROFILES_PATH}. "
        f"Tried candidates: {[_CANDIDATES[0], _CANDIDATES[1]]}"
    )
    assert INIT_PATH.is_file(), (
        f"__init__.py not found at expected location: {INIT_PATH}. "
        f"Tried candidates: {[_CANDIDATES[0], _CANDIDATES[1]]}"
    )


# pytest import shim for the optional skip in _read_source.
try:
    import pytest  # noqa: F401  (used conditionally above)
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


# Ensure pytest is available for the standard assertion machinery.
import pytest  # noqa: E402,F811  (re-export for test framework)
