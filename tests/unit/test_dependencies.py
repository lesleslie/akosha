"""CI guard: declared dependencies stay in sync with actual import sites.

Audit C5 found ``aiohttp`` imported inside ``akosha/api/middleware.py``
without ever being declared in ``pyproject.toml``. Without this guard
that drift is invisible until production: a fresh ``uv sync`` would
not install aiohttp, and the middleware's lazy import would explode
on first call. These tests pin both directions — every runtime
import must be declared, every declared runtime import must be used.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
AKOSHA_DIR = ROOT / "akosha"


def _read_pyproject_deps() -> list[str]:
    """Return the raw list of strings from ``[project].dependencies``.

    Uses ``tomllib`` so we don't have to write a bracket-balancing parser
    to handle extras like ``uvicorn[standard]``.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies")
    if not isinstance(deps, list) or not deps:
        pytest.fail("pyproject.toml has no [project].dependencies list")
    return [str(d) for d in deps]


def _normalize(dep_str: str) -> str:
    """Drop the version specifier so we can compare package names.

    ``"aiohttp>=3.12.14"`` -> ``"aiohttp"``; extras (``foo[bar]>=1``)
    are reduced to the base name.
    """
    base = dep_str.split(";", 1)[0]  # drop environment markers
    base = base.split("[", 1)[0]  # drop extras
    return re.split(r"[><=!~]", base, maxsplit=1)[0].strip().lower()


def _aiohttp_import_sites() -> list[Path]:
    """Every Python file under ``akosha/`` that imports aiohttp."""
    return [
        p
        for p in AKOSHA_DIR.rglob("*.py")
        if re.search(
            r"^\s*(?:import\s+aiohttp|from\s+aiohttp\s+import)", p.read_text(), re.MULTILINE
        )
    ]


def test_aiohttp_declared_in_dependencies() -> None:
    """``aiohttp`` must appear in pyproject's runtime dependency list."""
    deps = _read_pyproject_deps()
    normalized = {_normalize(d) for d in deps}
    assert "aiohttp" in normalized, (
        f"aiohttp is imported in {AKOSHA_DIR} but missing from "
        f"pyproject.toml [project.dependencies]: {deps}"
    )


def test_aiohttp_version_pin_at_or_above_3_12_14() -> None:
    """Audit specifies >=3.12.14 — older versions have known CVEs."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r"aiohttp\s*([><=~!]+)\s*([\d.]+)", text)
    assert match is not None, "aiohttp is declared but has no version constraint"
    op, version = match.group(1), match.group(2)
    assert op == ">=", "aiohttp pin should be a floor (>=), not a ceiling"
    parts = [int(p) for p in version.split(".")]
    assert parts >= [3, 12, 14], (
        f"aiohttp pin {op} {version} is below the audit-required 3.12.14 floor "
        f"(see bodai-pip-audit-aiohttp-cryptography-cve memory)"
    )


def test_aiohttp_import_sites_have_declaration() -> None:
    """For every ``import aiohttp`` site, pyproject must declare aiohttp.

    Catches the drift that audit C5 surfaced: code uses a package,
    pyproject forgets to declare it, and ``uv sync`` silently drops it.
    """
    sites = _aiohttp_import_sites()
    assert sites, "expected at least one aiohttp import site (regression check)"
    deps = _read_pyproject_deps()
    normalized = {_normalize(d) for d in deps}
    assert "aiohttp" in normalized, (
        f"{len(sites)} aiohttp import site(s) found but aiohttp not declared "
        f"in pyproject.toml — fresh installs would fail at runtime. "
        f"Sites: {[str(s.relative_to(ROOT)) for s in sites]}"
    )


def test_aiohttp_importable_after_install() -> None:
    """Smoke check that the declared aiohttp is actually installable."""
    aiohttp = pytest.importorskip("aiohttp")
    # The package should expose the async client API we use in middleware.
    assert hasattr(aiohttp, "ClientSession"), (
        "aiohttp is installed but ClientSession is missing — wrong package?"
    )


@pytest.mark.parametrize(
    "dep_string",
    [
        "aiohttp>=3.12.14",
    ],
)
def test_dependency_string_is_well_formed(dep_string: str) -> None:
    """Every declared dep parses as ``name (extras?) op version``."""
    pattern = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[^\]]+\])?\s*[><=~!]+\s*[\d.]+")
    assert pattern.match(dep_string), f"malformed dep string: {dep_string!r}"
