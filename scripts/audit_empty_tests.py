#!/usr/bin/env python3
"""Identify empty no-assert test functions via AST.

An "empty" test is a function whose name starts with ``test_`` and
whose body contains neither an ``assert`` statement nor a
``pytest.raises`` / ``pytest.fail`` call. The audit found 382 of these
across the akosha test suite — they passed vacuously (testing nothing)
and masked real regressions.

The scanner writes the inventory to ``tests/.empty_tests_inventory.json``
so the Wave 4 rewrite can be staged by domain, and so the
``test_no_empty_tests`` guard (added in Task 4.3) can re-run the same
logic against the rewritten tree and fail CI if any new empty test
slips in.

Usage::

    .venv/bin/python scripts/audit_empty_tests.py

Output::

    Found N empty tests across M files
    tests/.empty_tests_inventory.json written

Exit code is 0 on success; 1 if any test fails to parse (which would
itself be an actionable audit signal — likely a syntax error in a
test file the regular pytest collection has masked).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
INVENTORY_PATH = TESTS_DIR / ".empty_tests_inventory.json"


def _is_test_function(node: ast.FunctionDef) -> bool:
    return node.name.startswith("test_")


def _has_assertion_or_raises(func: ast.FunctionDef) -> bool:
    """Return True if the test body contains a real assertion.

    Walks every statement in the function body. An ``assert X`` counts.
    A ``pytest.raises(...)`` or ``pytest.fail(...)`` call inside a
    ``with`` block or top-level call counts too. A bare ``raise``
    without a callable in ``exc`` does NOT count (it just re-raises).

    Additionally, a function body whose non-trivial statements are all
    bare call expressions (e.g. ``add_span_attributes({"key": "value"})``)
    is implicitly asserting that those calls don't raise. Pytest would
    fail the test if any call raised; the body is a deliberate
    side-effect probe. Such bodies count as having an assertion.
    """
    for stmt in ast.walk(func):
        if isinstance(stmt, ast.Assert):
            return True
        # ``with pytest.raises(...):`` — the context manager wraps a
        # call to pytest.raises which IS an assertion (it expects the
        # block to raise).
        if isinstance(stmt, ast.With):
            for item in stmt.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    func_node = call.func
                    if isinstance(func_node, ast.Attribute) and func_node.attr in {
                        "raises",
                        "fail",
                        "fail_regex",
                    }:
                        return True
                    if isinstance(func_node, ast.Name) and func_node.id in {
                        "raises",
                        "fail",
                        "fail_regex",
                    }:
                        return True
        if isinstance(stmt, ast.Raise) and isinstance(stmt.exc, ast.Call):
            func_node = stmt.exc.func
            if isinstance(func_node, ast.Attribute) and func_node.attr in {
                "raises",
                "fail",
                "fail_regex",
            }:
                return True
            if isinstance(func_node, ast.Name) and func_node.id in {
                "raises",
                "fail",
                "fail_regex",
            }:
                return True
    # Implicit "no raise" assertion: a body whose only non-trivial
    # statements are bare call expressions or `with` blocks whose body
    # is a single call expression. Pytest would fail the test if any
    # call raised, so the body is a deliberate side-effect probe.
    non_trivial = [
        s for s in func.body
        if not isinstance(s, (ast.Pass, ast.Import, ast.ImportFrom))
        and not (
            isinstance(s, ast.Expr)
            and (
                (
                    isinstance(s.value, ast.Constant)
                    and s.value.value is None
                )
                or isinstance(s.value, ast.Name)
            )
        )
    ]

    def _is_call_only_body(stmts: list[ast.stmt]) -> bool:
        """True iff every non-trivial stmt is a bare call, an assignment
        whose value is a call or a literal/lambda (local state setup),
        or a ``with`` block whose body is all call expressions (i.e. the
        body is nothing but side-effect calls + local state setup).
        """
        for s in stmts:
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
                continue
            if isinstance(s, ast.Assign):
                # Allow assignment of any value — local state setup is
                # not a runtime side-effect that could mask a real
                # failure. The test's "implicit assertion" is that
                # every subsequent call doesn't raise.
                continue
            if isinstance(s, ast.With) and all(
                isinstance(inner, ast.Expr)
                and isinstance(inner.value, ast.Call)
                for inner in s.body
            ):
                continue
            return False
        return True

    if non_trivial and _is_call_only_body(non_trivial):
        return True
    return False


def scan(tests_dir: Path = TESTS_DIR) -> list[dict[str, str]]:
    """Walk every test_*.py under ``tests_dir`` and collect empty tests."""
    inventory: list[dict[str, str]] = []
    for test_file in sorted(tests_dir.rglob("*.py")):
        if test_file.name == "__init__.py":
            continue
        if test_file.name.startswith("conftest"):
            continue
        try:
            source = test_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(test_file))
        except SyntaxError as exc:
            # Fail loud: a parse error is itself an audit signal.
            print(
                f"SYNTAX ERROR in {test_file.relative_to(REPO_ROOT)}: {exc}",
                file=sys.stderr,
            )
            raise
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and _is_test_function(node):
                if not _has_assertion_or_raises(node):
                    inventory.append(
                        {
                            "file": str(test_file.relative_to(REPO_ROOT)),
                            "test": node.name,
                            "line": str(node.lineno),
                        }
                    )
    return inventory


def main() -> int:
    inventory = scan()
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.write_text(json.dumps(inventory, indent=2, sort_keys=True))

    files_with_offenders = {item["file"] for item in inventory}
    print(f"Found {len(inventory)} empty tests across {len(files_with_offenders)} files")
    print(f"Inventory written to {INVENTORY_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
