"""Tests for the alpha-shell-commands gate (Plan Task 3.2.1).

Validates that the 5 distributed-intelligence commands (aggregate,
search, detect, graph, trends) are gated behind
``AikoshaConfig.alpha_shell_commands_enabled`` and absent from the shell
namespace by default.
"""
from __future__ import annotations

from typing import Any

from akosha.shell.adapter import _read_alpha_shell_flag

ALPHA_COMMANDS = ("aggregate", "search", "detect", "graph", "trends")


class _StubConfig:
    def __init__(self, alpha: bool) -> None:
        self.alpha_shell_commands_enabled = alpha


class _StubApp:
    def __init__(self, alpha: bool) -> None:
        self.config = _StubConfig(alpha)


def test_flag_false_when_app_is_none() -> None:
    assert _read_alpha_shell_flag(None) is False


def test_flag_false_when_config_is_none() -> None:
    assert _read_alpha_shell_flag(_StubApp.__new__(_StubApp)) is False  # type: ignore[arg-type]


def test_flag_picks_up_true_value() -> None:
    assert _read_alpha_shell_flag(_StubApp(True)) is True


def test_flag_picks_up_false_value() -> None:
    assert _read_alpha_shell_flag(_StubApp(False)) is False


class _NestedApp:
    def __init__(self, alpha: bool) -> None:
        self.config = type("_C", (), {"settings": type("_S", (), {"alpha_shell_commands_enabled": alpha})()})


def test_flag_walks_nested_settings() -> None:
    """Some configs nest settings under config.settings — handle that shape too."""
    assert _read_alpha_shell_flag(_NestedApp(True)) is True
    assert _read_alpha_shell_flag(_NestedApp(False)) is False


def test_namespace_excludes_alpha_when_flag_off() -> None:
    """Smoke test: with flag off, _add_akasha_namespace skips the 5 commands.

    Constructs a minimal AdminShell-like object via __new__ so we don't
    pull in AkoshaApplication. We call _add_akasha_namespace directly
    after seeding self.app and self.namespace.
    """
    from akosha.shell.adapter import AkoshaShell

    shell = AkoshaShell.__new__(AkoshaShell)
    shell.app = _StubApp(alpha=False)
    shell.namespace = {}
    shell._add_akasha_namespace()
    for cmd in ALPHA_COMMANDS:
        assert cmd not in shell.namespace, f"'{cmd}' should be gated off but was present"


def test_namespace_includes_alpha_when_flag_on() -> None:
    from akosha.shell.adapter import AkoshaShell

    shell = AkoshaShell.__new__(AkoshaShell)
    shell.app = _StubApp(alpha=True)
    shell.namespace = {}
    shell._add_akasha_namespace()
    for cmd in ALPHA_COMMANDS:
        assert cmd in shell.namespace, f"'{cmd}' should be enabled but was absent"


def test_adapters_and_version_always_present() -> None:
    """adapters and version are not alpha; always in namespace regardless of flag."""
    from akosha.shell.adapter import AkoshaShell

    shell = AkoshaShell.__new__(AkoshaShell)
    shell.app = _StubApp(alpha=False)
    shell.namespace = {}
    shell._add_akasha_namespace()
    assert "adapters" in shell.namespace
    assert "version" in shell.namespace