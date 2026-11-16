#!/usr/bin/env python3
"""stamp_version.py — re-stamp this repo's version claims from pyproject.toml.

A release bumps ``version`` in ``pyproject.toml``. Every doc surface that
carries a version claim has to move with it. When it does not, the repo's
own version-consistency guard (``tests/unit/test_version_sync.py``)
fails on the freshly-bumped main — so the release commit is red on the
branch the next release starts from, and the drift stays invisible until
someone reads the failing test.

That is not hypothetical. The 0.27.0 bump wrote ``pyproject.toml`` and left
``README.md`` reading 0.26.1, so ``tests/unit/test_version_sync.py::
test_readme_version_matches_pyproject`` failed on main — one release of
drift, caught by the guard only after the fact.

This script closes that loop. It reads the version from ``pyproject.toml``
and rewrites the claims listed in ``SURFACES``. It is idempotent: with the
surfaces already in sync it changes nothing and exits 0.

The fleet runs this from the pre-commit hook, so the commit that bumps the
version carries the claims with it. ``--check`` asks the same question
without writing, for use outside a commit.

Exit codes:
    0 — surfaces are in sync, or were rewritten
    1 — a surface is out of sync and ``--check`` was given
    2 — a claim could not be found, or a file could not be read

Usage:
    python3 scripts/stamp_version.py [--stage] [--check] [REPO_ROOT]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

#: The surfaces in this repo that carry a version claim, as
#: ``(path relative to the repo root, pattern)`` pairs. Each pattern has ONE
#: named group, ``version``, spanning the version string and nothing else —
#: so the rewrite replaces the number and leaves the surrounding text (and any
#: suffix) alone.
SURFACES: tuple[tuple[str, str], ...] = (
    (
        "README.md",
        r"(\*\*Version:\*\* )(?P<version>\d+\.\d+\.\d+)",
    ),
)


def read_version(root: Path) -> str:
    """Return the canonical version declared in ``pyproject.toml``."""
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _substitute(match: re.Match[str], version: str) -> str:
    """Replace only the ``version`` group, leaving the rest of the match."""
    start = match.start("version") - match.start(0)
    end = match.end("version") - match.start(0)
    return match.group(0)[:start] + version + match.group(0)[end:]


def plan(root: Path, version: str) -> list[tuple[Path, str]]:
    """Return the ``(path, rewritten text)`` pairs that are out of sync.

    Raises:
        FileNotFoundError: a surface does not exist.
        ValueError: a surface carries no recognisable version claim.
    """
    planned: list[tuple[Path, str]] = []
    for relative, pattern in SURFACES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"{relative} does not exist")
        text = path.read_text()
        regex = re.compile(pattern)
        matches = list(regex.finditer(text))
        if not matches:
            raise ValueError(f"no version claim matched in {relative}")
        found = {match.group("version") for match in matches}
        if found == {version}:
            continue
        planned.append((path, regex.sub(lambda m: _substitute(m, version), text)))
    return planned


def stage(root: Path, paths: list[Path]) -> None:
    """Stage the rewritten surfaces so the commit carries them."""
    if not paths:
        return
    relatives = [str(path.relative_to(root)) for path in paths]
    subprocess.run(
        ["git", "-C", str(root), "add", "--", *relatives],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Re-stamp this repo's version claims from pyproject.toml.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="repo root (default: the current directory)",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="git add the rewritten surfaces, so the bump commit carries them",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if a surface is out of sync; change nothing",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()

    try:
        version = read_version(root)
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(
            f"ERROR: could not read the version from {root / 'pyproject.toml'}: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        planned = plan(root, version)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if planned:
            for path, _ in planned:
                print(
                    f"OUT OF SYNC: {path.relative_to(root)} does not carry {version}",
                    file=sys.stderr,
                )
            return 1
        print(f"version claims are in sync with pyproject.toml ({version})")
        return 0

    for path, text in planned:
        path.write_text(text)
        print(f"stamped {path.relative_to(root)} -> {version}")
    if planned:
        stage(root, [path for path, _ in planned])
    else:
        print(f"version claims already match pyproject.toml ({version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
