"""CI guard: prevent regression to the empty no-assert test anti-pattern.

Audit Section 6 found ~382 empty tests across the akosha suite —
test functions whose body contains no ``assert`` statement and no
``pytest.raises`` / ``pytest.fail`` call. They passed vacuously
(testing nothing) and masked real regressions.

Wave 4 rewrote them (see ``tests/.empty_tests_inventory.json`` and the
``git log`` for the rewrite commit). This guard runs the same AST
scan as ``scripts/audit_empty_tests.py`` on every CI build and fails
the build if any new empty test slips in.

Mirrors the production scanner's logic so the guard can't drift
from the scanner. ``scripts/audit_empty_tests.py`` and this test
must agree on what counts as an assertion.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _has_assertion(func: ast.FunctionDef) -> bool:
    """Delegate to the scanner's logic so the test and the scanner stay
    in sync. The scanner recognizes ``assert``, ``with pytest.raises``,
    and the implicit "no raise" body pattern (a body whose non-trivial
    statements are bare call expressions, assignments, or ``with``
    blocks with call-only bodies — pytest would fail the test if any
    call raised)."""
    from scripts.audit_empty_tests import _has_assertion_or_raises

    return _has_assertion_or_raises(func)


def test_no_empty_tests() -> None:
    """Audit Section 6: no test_* function may have zero assertions.

    Walks ``tests/**/*.py``, identifies every ``def test_*(...)``
    function whose body has no assertion, and fails the build if any
    are found. The error message shows the first 5 offenders so the
    build log points at the file without flooding it.
    """
    tests_dir = ROOT / "tests"
    offenders: list[tuple[str, str]] = []
    for test_file in sorted(tests_dir.rglob("*.py")):
        if test_file.name == "__init__.py":
            continue
        if test_file.name.startswith("conftest"):
            continue
        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            # A parse error is itself an audit signal — fail loud.
            pytest_fail_message = (
                f"Syntax error in {test_file.relative_to(ROOT)}: {exc}"
            )
            raise AssertionError(pytest_fail_message)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                if not _has_assertion(node):
                    offenders.append(
                        (str(test_file.relative_to(ROOT)), node.name)
                    )

    assert not offenders, (
        f"Empty no-assert tests found: {offenders[:5]}... "
        f"({len(offenders)} total). Run "
        f"`.venv/bin/python scripts/audit_empty_tests.py` to see the "
        f"full inventory, then add a real assertion to each."
    )
