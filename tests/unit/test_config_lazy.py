"""Tests for the lazy ``akosha.config.config`` singleton.

``akosha/config.py`` used to end with a module-scope ``config = get_config()``.
That made every import under ``akosha.`` pay for the whole oneiric settings
chain -- the settings-source-chain load for the akosha project plus oneiric's
own, thirteen times over -- before any caller had asked for a configuration
value.

The singleton is now served by a PEP 562 module ``__getattr__``, so it is
built on first access. These tests pin the property that matters: the IMPORT
has no side effect, and the VALUE is unchanged.

The import-time property cannot be observed inside the test process (a
long-lived pytest session has almost certainly resolved the singleton already),
so the import checks run in a fresh interpreter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter rooted at the repo."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


class TestImportHasNoSideEffect:
    """Importing ``akosha.config`` must not build the config."""

    def test_import_does_not_materialize_the_singleton(self) -> None:
        """``config`` must not be a module attribute until something asks for it.

        The old module-scope assignment put ``config`` in ``vars(module)``
        the moment the import completed. With PEP 562 the name is served by
        ``__getattr__`` instead, so ``vars()`` stays clean until first use.
        """
        result = _run_python(
            "import akosha.config as m; "
            "assert 'config' not in vars(m), sorted(vars(m)); "
            "print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_import_does_not_pull_in_the_numpy_storage_chain(self) -> None:
        """Importing the config must not drag in ``akosha.storage``.

        ``akosha.config`` imported ``StoragePathResolver`` at module scope,
        which pulls ``akosha.storage.aging`` -> ``numpy``. The resolver is
        only used inside model validators, which run when a config is
        BUILT, so the import belongs inside them.
        """
        result = _run_python(
            "import akosha.config, sys; "
            "assert 'akosha.storage' not in sys.modules, sorted(sys.modules); "
            "print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_importing_sharding_does_not_materialize_the_singleton(self) -> None:
        """``akosha.storage.sharding`` must not build the config either.

        It used to do ``from akosha.config import config`` at module scope,
        which triggers the singleton build on import.
        """
        result = _run_python(
            "import akosha.storage.sharding; "
            "import akosha.config as m; "
            "assert 'config' not in vars(m), sorted(vars(m)); "
            "print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout


class TestSingletonValue:
    """The lazily-resolved singleton behaves like the old module attribute."""

    def test_from_import_resolves_the_singleton(self) -> None:
        """``from akosha.config import config`` still works.

        The import machinery falls back to ``getattr`` on the module, which
        is exactly the PEP 562 hook.
        """
        result = _run_python(
            "from akosha.config import config as a; "
            "from akosha.config import config as b; "
            "assert a is b; "
            "assert type(a).__name__ == 'AkoshaConfig'; "
            "print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_attribute_access_resolves_the_singleton(self) -> None:
        """``akosha.config.config`` resolves to the same object every time."""
        result = _run_python(
            "import akosha.config as m; "
            "assert m.config is m.config; "
            "assert type(m.config).__name__ == 'AkoshaConfig'; "
            "print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_the_singleton_is_cached_not_rebuilt(self) -> None:
        """``get_config_singleton`` must return the SAME object every time.

        The old module-scope assignment built the config exactly once. A
        lazy accessor that rebuilt on every access would silently change
        that -- config values would drift between reads.
        """
        from akosha.config import get_config_singleton

        assert get_config_singleton() is get_config_singleton()

    def test_get_config_still_builds_a_fresh_object(self) -> None:
        """``get_config()`` keeps its contract: a fresh load per call.

        It is the explicit "load it now, from disk" entry point; the
        singleton is the "give me the shared one" accessor. Collapsing the
        two would make a config reload impossible.
        """
        from akosha.config import get_config

        assert get_config() is not get_config()

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        """The hook must not swallow typos into a silent ``None``."""
        import akosha.config as module

        with pytest.raises(AttributeError, match="no attribute 'not_a_real_name'"):
            module.not_a_real_name  # noqa: B018
