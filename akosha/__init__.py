"""Akosha - Universal Memory Aggregation System.

Lazy import of :mod:`akosha.config` so importing :mod:`akosha` does not
eagerly transitively import ``numpy`` (via ``akosha.storage.aging``).
This both speeds startup and avoids ``ImportError: cannot load module
more than once per process`` on Python 3.14 + ``pytest-cov`` where the
extension module gets loaded twice through different paths.

Public access via ``akosha.config`` continues to work; the lookup is
deferred to first attribute access.

``__version__`` is derived from the installed distribution metadata via
:func:`importlib.metadata.version` so :file:`pyproject.toml` is the
single source of truth for the version string. Editable installs without
a built ``.dist-info`` fall back to ``"0+unknown"`` (PEP 440 local-version
label) so the import never crashes.
"""

from __future__ import annotations

from importlib import metadata as _metadata

# PEP 440 local-version label; sentinel for "metadata not found" rather
# than a real release version. Distinct enough that downstream consumers
# can ``assert __version__ != '0+unknown'`` as a "was this installed
# properly" signal.
_VERSION_FALLBACK = "0+unknown"


def _resolve_version() -> str:
    """Return the distribution version, falling back to a sentinel.

    Wrapped in a function (not inline at module scope) so tests can
    monkeypatch the lookup without having to reload the package.
    """
    try:
        return _metadata.version("akosha")
    except _metadata.PackageNotFoundError:
        return _VERSION_FALLBACK


__version__ = _resolve_version()

__all__ = ["__version__", "config"]


def __getattr__(name: str):
    """Resolve ``akosha.config`` on first access (lazy)."""
    if name == "config":
        from akosha.config import config

        return config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
