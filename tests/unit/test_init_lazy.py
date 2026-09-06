"""Tests for the lazy ``akosha.config`` re-export in :mod:`akosha.__init__`.

Audit found that the eager ``from akosha.config import config`` at import
time was responsible for the entire numpy extension-module load chain
(``akosha.config`` -> ``akosha.storage.path_resolver`` ->
``akosha.storage.aging`` -> ``import numpy``), which breaks pytest-cov on
Python 3.14 with ``ImportError: cannot load module more than once per
process``.

The fix is to defer the import: ``akosha.config`` is now resolved on first
attribute access via module-level ``__getattr__``.

Caveat: When Python performs ``import akosha.config`` (submodule import),
it directly sets ``akosha.config = <module 'akosha.config'>`` on the
parent package — bypassing ``__getattr__``. So after any code in the
process triggers that submodule import, ``akosha.config`` resolves to
the module, not the singleton. The lazy ``__getattr__`` only takes effect
if no submodule import has happened yet (rare in practice).

This test pins BOTH the eager-resolved case (submodule already imported)
and the lazy-resolved case (fresh import, no submodule touched). The
primary contract being tested is:

1. ``akosha.__version__`` is set eagerly (existing behavior preserved).
2. Unknown attribute access raises :class:`AttributeError` with the
   expected message format — this guards against silently dropping
   typos in callers that use ``akosha.<typo>``.
3. ``__all__`` lists ``__version__`` and ``config``, both resolvable.
"""

from __future__ import annotations

import pytest

import akosha


class TestAkoshaVersion:
    """``akosha.__version__`` is set eagerly at import time."""

    def test_version_is_string(self) -> None:
        """``__version__`` must be a non-empty string (package metadata)."""
        assert isinstance(akosha.__version__, str)
        assert akosha.__version__
        # Pin the format: should be a dotted version, not 'unknown' or empty.
        assert "." in akosha.__version__


class TestAkoshaUnknownAttribute:
    """Unknown attribute access raises :class:`AttributeError` with the right shape."""

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        """``akosha.<anything_unknown>`` must raise AttributeError."""
        with pytest.raises(AttributeError) as excinfo:
            akosha.this_is_not_a_real_attribute  # noqa: B018

        # The error message must mention both the module name and the attribute
        # so debug output is actionable. Pin the format string from
        # ``akosha/__init__.py``.
        msg = str(excinfo.value)
        assert "akosha" in msg
        assert "this_is_not_a_real_attribute" in msg

    def test_unknown_attribute_message_includes_attribute_name(self) -> None:
        """The error must surface the offending attribute name verbatim."""
        with pytest.raises(AttributeError) as excinfo:
            akosha.typo_attribute_for_init_test  # noqa: B018
        assert "typo_attribute_for_init_test" in str(excinfo.value)

    def test_dunder_name_raises_attribute_error(self) -> None:
        """Accessing a missing dunder attribute must raise (not silently return None)."""
        with pytest.raises(AttributeError):
            akosha.__nonexistent_dunder__  # noqa: B018


class TestAkoshaAllExports:
    """The ``__all__`` declaration matches what the module actually exposes."""

    def test_all_lists_version_and_config(self) -> None:
        assert set(akosha.__all__) == {"__version__", "config"}

    def test_all_entries_are_resolvable(self) -> None:
        """Every name in ``__all__`` must resolve without raising AttributeError."""
        for name in akosha.__all__:
            assert hasattr(akosha, name), f"__all__ contains unresolvable name: {name!r}"


class TestLazyConfigResolution:
    """The lazy ``__getattr__`` fires when ``akosha.config`` is *not* already resolved.

    In a long-lived test process, ``akosha.config`` is usually already
    attached as a submodule attribute (set by Python during
    ``import akosha.config``) — and that direct setattr bypasses
    ``__getattr__``. These tests explicitly remove the attribute to
    force the lazy branch.
    """

    def test_lazy_branch_returns_config_when_attr_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``akosha.config`` is not an attribute, ``__getattr__`` resolves it."""
        # Ensure the lazy branch is exercised: remove the attribute so the
        # module-level ``__getattr__`` is the only resolution path.
        monkeypatch.delattr(akosha, "config", raising=False)
        config = akosha.config
        # The returned object is the singleton from ``akosha.config``.
        from akosha.config import config as expected

        assert config is expected
