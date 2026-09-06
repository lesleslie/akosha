"""Akosha - Universal Memory Aggregation System.

Lazy import of :mod:`akosha.config` so importing :mod:`akosha` does not
eagerly transitively import ``numpy`` (via ``akosha.storage.aging``).
This both speeds startup and avoids ``ImportError: cannot load module
more than once per process`` on Python 3.14 + ``pytest-cov`` where the
extension module gets loaded twice through different paths.

Public access via ``akosha.config`` continues to work; the lookup is
deferred to first attribute access.
"""

from __future__ import annotations

__version__ = "0.15.0"

__all__ = ["__version__", "config"]


def __getattr__(name: str):
    """Resolve ``akosha.config`` on first access (lazy)."""
    if name == "config":
        from akosha.config import config

        return config
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
