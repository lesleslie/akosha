"""Cold-tier storage tests.

Covers ColdStore.initialize() wiring for new fields (endpoint_url, project,
access_key_id, secret_access_key) and the end-to-end round trip against
fake-gcs-server. Tests are appended incrementally as the plan progresses:
- B2.1: gcs wiring (this file's initial state)
- B2.3: s3 wiring (added in B2.3)
- B2.4: StandardMode wiring (added in B2.4)
- C1: end-to-end subprocess test (added in C1)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


def test_cold_store_gcs_branch_forwards_endpoint_url() -> None:
    """ColdStore.initialize() with backend='gcs' passes endpoint_url to GCSStorageSettings.

    Without the wiring fix, GCSStorageSettings would only receive
    bucket + project + credentials_file — endpoint_url would be dropped.
    """
    import asyncio

    from akosha.storage.cold_store import ColdStore

    captured: list[dict] = []

    class FakeGCSStorageAdapter:
        def __init__(self, settings: Any) -> None:
            captured.append(settings.model_dump())

        async def init(self) -> None:
            pass

        async def cleanup(self) -> None:
            pass

    store = ColdStore(
        bucket="akosha-cold-data",
        prefix="conversations/",
        storage_backend="gcs",
        project="local-dev",
        endpoint_url="http://127.0.0.1:4443",
    )

    with patch("akosha.storage.cold_store.GCSStorageAdapter", FakeGCSStorageAdapter):
        asyncio.run(store.initialize())

    assert len(captured) == 1
    settings = captured[0]
    assert settings["bucket"] == "akosha-cold-data"
    assert settings["endpoint_url"] == "http://127.0.0.1:4443"
    assert settings["project"] == "local-dev"
