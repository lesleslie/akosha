"""JSON-column coercion helpers for the DuckDB boundary.

DuckDB's ``JSON`` column type is returned by the Python client as a
``str`` — the raw JSON *text* — NOT as a decoded Python object. This
holds on both DuckDB 1.5.x and 2.0.0-dev, and it does not matter
whether the value was bound as a Python ``dict`` or as a JSON string:
the fetch returns text either way.

That makes the JSON text a LEAKED type at the storage boundary. A
caller that writes ``metadata.get("user_id")`` on a value that came out
of a ``JSON`` column raises ``AttributeError: 'str' object has no
attribute 'get'`` — which is exactly how the knowledge-graph refresh
loop silently produced zero entities for every row (the exception was
swallowed at DEBUG level).

This module owns the SINGLE decode site for that leak.
:func:`coerce_json_object` is the one place that turns a raw column
value into a ``dict``; every layer that crosses the DuckDB boundary
normalises through it, so no consumer has to know that DuckDB hands
back text.

Decode sites (keep this list short — a second ad-hoc ``json.loads`` on
a JSON column is a bug):

- ``akosha/storage/hot_store.py`` — ``HotStore.query_traces`` returns
  ``metadata`` as a ``dict`` (the storage boundary owns the type).
- ``akosha/processing/knowledge_graph.py`` — ``extract_entities``
  normalises defensively so a directly-supplied raw row cannot crash
  it; it reuses the helper below rather than re-implementing the
  decode.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def coerce_json_object(raw: Any) -> dict[str, Any]:
    """Normalise a raw DuckDB ``JSON`` column value into a ``dict``.

    Args:
        raw: The value as returned by DuckDB for a ``JSON`` column. May
            be ``None`` (SQL NULL), a ``dict`` (already decoded by the
            caller), a ``str``/``bytes`` holding JSON text, or anything
            else.

    Returns:
        The decoded JSON object. An empty ``dict`` when the value is
        NULL, blank, not valid JSON, or decodes to a non-object (a JSON
        array or scalar is not a metadata mapping, so it is dropped
        rather than surfaced as a surprising shape).

    Notes:
        Every failure path logs at WARNING. A metadata blob that cannot
        be decoded is a data-shape problem, and the whole point of the
        fix is that it must never be silent again.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Could not decode JSON column value (%s); treating it as an empty object",
                exc,
            )
            return {}
        if isinstance(decoded, dict):
            return decoded
        logger.warning(
            "JSON column value decoded to %s, not an object; treating it as an empty object",
            type(decoded).__name__,
        )
        return {}
    logger.warning(
        "JSON column value has unexpected type %s; treating it as an empty object",
        type(raw).__name__,
    )
    return {}
