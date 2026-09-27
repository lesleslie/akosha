"""CI guard: keep version stamps in lockstep with pyproject.toml.

Fails when any of the canonical version locations drift from the
package version declared in pyproject.toml. Prevents the
version-stamp-drift pattern flagged in the 2026-08-19 doc audit.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


# Sentinel used when ``importlib.metadata.version`` raises
# ``PackageNotFoundError`` (e.g. editable install without .dist-info). The
# string is a PEP 440 local-version label so it remains parseable as a
# version while being visibly distinct from any real release.
_FALLBACK_VERSION = "0+unknown"


def _read_pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        pytest.fail("pyproject.toml does not contain a version field")
    return match.group(1)


def _read_readme_version() -> str | None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"\*\*Version:\*\*\s*([0-9]+\.[0-9]+\.[0-9]+)", text)
    if match:
        return match.group(1)
    return None


def test_pyproject_version_is_canonical() -> None:
    version = _read_pyproject_version()
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), (
        f"pyproject version {version!r} is not a valid PEP 440 stamp"
    )


def test_runtime_equals_metadata_version() -> None:
    """``akosha.__version__`` must equal ``metadata.version('akosha')`` at import time.

    This is the runtime mirror of the regex-based pyproject-sync guard:
    every consumer of the version stamp reads the same source-of-truth
    (the installed distribution metadata). Replaces the legacy hardcoded
    ``__version__ = 'X.Y.Z'`` literal path, which could drift silently
    when pyproject was bumped without 4-file stamp updates.
    """
    import akosha

    assert akosha.__version__ == metadata.version("akosha"), (
        f"akosha.__version__={akosha.__version__!r} "
        f"diverged from installed metadata "
        f"{metadata.version('akosha')!r}; pyproject was probably bumped "
        f"without re-installing. Run ``uv pip install -e '.[dev]'``."
    )


def test_version_fallback_when_metadata_unavailable() -> None:
    """Editable installs without a built ``.dist-info`` raise PackageNotFoundError.

    The runtime-read of ``metadata.version`` then falls back to a known
    sentinel (PEP 440 local-version label ``0+unknown``) rather than
    crashing at import time. Before Phase 1 the literal stamp made this
    crash impossible; this test pins the new fallback contract by
    re-executing the init module under a patched ``metadata.version``.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "akosha_fallback_probe",
        ROOT / "akosha" / "__init__.py",
    )
    assert spec is not None
    fresh = importlib.util.module_from_spec(spec)

    with patch("importlib.metadata.version", side_effect=metadata.PackageNotFoundError):
        spec.loader.exec_module(fresh)  # type: ignore[union-attr]

    assert fresh.__version__ == _FALLBACK_VERSION, (
        f"Fallback returned {fresh.__version__!r}; expected sentinel "
        f"{_FALLBACK_VERSION!r} when metadata is unavailable"
    )


def test_app_version_matches_runtime() -> None:
    """``APP_VERSION`` is a re-export of the package's runtime ``__version__``.

    The internal contract must hold: every MCP-server-side stamp reports
    the exact same value as the package-level ``akosha.__version__``.
    Was previously asserted by ``test_mcp_server.py:22``; mirrored here
    as part of the SSO-plan guard.
    """
    from akosha import __version__
    from akosha.mcp.server import APP_VERSION

    assert APP_VERSION == __version__, (
        f"APP_VERSION {APP_VERSION!r} drifted from akosha.__version__ "
        f"{__version__!r}"
    )


def test_service_version_matches_runtime() -> None:
    """``SERVICE_VERSION`` is a re-export of the package's runtime ``__version__``."""
    from akosha import __version__
    from akosha.mcp.tools import SERVICE_VERSION

    assert SERVICE_VERSION == __version__, (
        f"SERVICE_VERSION {SERVICE_VERSION!r} drifted from "
        f"akosha.__version__ {__version__!r}"
    )


def test_no_hardcoded_version_literals() -> None:
    """Regression guard: no hardcoded ``X.Y.Z`` version stamps in code.

    Every package-level constant ( ``__version__``, ``APP_VERSION``,
    ``SERVICE_VERSION`` ) must derive from ``importlib.metadata.version``
    via the package's ``__version__``. Hardcoded literals re-introduce
    the multi-source-of-truth drift the SSO plan exists to prevent.

    Scans package source files; allows the pyproject.toml + README + the
    new fallback sentinel (``0+unknown``) in __init__.py.
    """
    package_root = ROOT / "akosha"
    offenders: list[tuple[str, str]] = []
    # Pattern: identifier on the LHS, then a hardcoded PEP-440 numeric literal.
    # ``0+unknown`` is excluded by design (the fallback sentinel is local).
    literal_pattern = re.compile(
        r"""(?:__version__|APP_VERSION|SERVICE_VERSION)\s*[:=]\s*["']"""
        r"""(\d+\.\d+(?:\.\d+)?)["']"""
    )
    for py_file in sorted(package_root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8")
        for match in literal_pattern.finditer(text):
            offenders.append((str(py_file.relative_to(ROOT)), match.group(1)))
    assert not offenders, (
        f"Hardcoded version literals re-introduced drift; expected "
        f"importlib.metadata.version() reads. Offenders: {offenders}"
    )


def test_readme_version_matches_pyproject() -> None:
    pyproject = _read_pyproject_version()
    readme = _read_readme_version()
    assert readme == pyproject, f"README.md: Version header={readme!r} != pyproject={pyproject!r}"
