"""CI guard: keep version stamps in lockstep with pyproject.toml.

Fails when any of the canonical version locations drift from the
package version declared in pyproject.toml. Prevents the
version-stamp-drift pattern flagged in the 2026-08-19 doc audit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _read_pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        pytest.fail("pyproject.toml does not contain a version field")
    return match.group(1)


def _read_source_version(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match:
        return match.group(1)
    return None


def _read_constant_version(path: Path, constant: str) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    pattern = rf'{constant}\s*:\s*(?:Final|str|str\s*\|\s*None)\s*=\s*"([^"]+)"'
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    return None


def _read_constant_assignment(path: Path, constant: str) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    pattern = rf'{constant}\s*=\s*"([^"]+)"'
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    return None


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


def test_package_init_matches_pyproject() -> None:
    pyproject = _read_pyproject_version()
    source = _read_source_version(ROOT / "akosha" / "__init__.py")
    assert source == pyproject, (
        f"akosha/__init__.py: __version__={source!r} != pyproject={pyproject!r}"
    )


def test_mcp_init_matches_pyproject() -> None:
    pyproject = _read_pyproject_version()
    source = _read_source_version(ROOT / "akosha" / "mcp" / "__init__.py")
    assert source == pyproject, (
        f"akosha/mcp/__init__.py: __version__={source!r} != pyproject={pyproject!r}"
    )


def test_app_version_matches_pyproject() -> None:
    pyproject = _read_pyproject_version()
    server = _read_constant_version(ROOT / "akosha" / "mcp" / "server.py", "APP_VERSION")
    assert server == pyproject, (
        f"akosha/mcp/server.py: APP_VERSION={server!r} != pyproject={pyproject!r}"
    )


def test_service_version_matches_pyproject() -> None:
    pyproject = _read_pyproject_version()
    service = _read_constant_assignment(
        ROOT / "akosha" / "mcp" / "tools" / "__init__.py", "SERVICE_VERSION"
    )
    assert service == pyproject, (
        f"akosha/mcp/tools/__init__.py: SERVICE_VERSION={service!r} "
        f"!= pyproject={pyproject!r}"
    )


def test_readme_version_matches_pyproject() -> None:
    pyproject = _read_pyproject_version()
    readme = _read_readme_version()
    assert readme == pyproject, (
        f"README.md: Version header={readme!r} != pyproject={pyproject!r}"
    )
