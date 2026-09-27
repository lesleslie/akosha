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


def test_cold_store_s3_branch_forwards_credentials() -> None:
    """ColdStore.initialize() with backend='s3' passes access_key_id + secret_access_key to S3StorageSettings.

    Without the wiring fix, S3StorageSettings would only receive bucket + region +
    endpoint_url — credentials would be dropped and R2 would 401 at runtime.
    """
    import asyncio
    from unittest.mock import patch

    from akosha.storage.cold_store import ColdStore

    captured: list[dict] = []

    class FakeS3StorageAdapter:
        def __init__(self, settings: Any) -> None:
            captured.append(settings.model_dump())

        async def init(self) -> None:
            pass

        async def cleanup(self) -> None:
            pass

    store = ColdStore(
        bucket="akosha-cold-data",
        prefix="conversations/",
        storage_backend="s3",
        endpoint_url="https://test-account.r2.cloudflarestorage.com",
        region="auto",
        access_key_id="AKIA_test",
        secret_access_key="secret_test",
    )

    with patch("akosha.storage.cold_store.S3StorageAdapter", FakeS3StorageAdapter):
        asyncio.run(store.initialize())

    assert len(captured) == 1
    settings = captured[0]
    assert settings["bucket"] == "akosha-cold-data"
    assert settings["endpoint_url"] == "https://test-account.r2.cloudflarestorage.com"
    assert settings["region"] == "auto"
    assert settings["access_key_id"] == "AKIA_test"
    assert settings["secret_access_key"] == "secret_test"


def test_standard_mode_cold_storage_forwards_s3_credentials() -> None:
    """StandardMode.initialize_cold_storage() must read cold-tier config from
    self.config (flat dict per BaseMode.__init__) and forward endpoint_url +
    credentials + region into S3StorageSettings.
    """
    import asyncio
    from unittest.mock import patch

    from akosha.modes.standard import StandardMode

    captured: list[dict] = []

    class FakeS3StorageAdapter:
        def __init__(self, settings: Any) -> None:
            captured.append(settings.model_dump())

    # Build the dict explicitly to mirror what load_config() would produce.
    mode = StandardMode(
        {
            "cold_storage_enabled": True,
            "cold_storage_backend": "s3",
            "cold_bucket": "akosha-r2",
            "cold_prefix": "conversations/",
            "cold_format": "parquet",
            "cold_region": "auto",
            "cold_endpoint_url": "https://test.r2.cloudflarestorage.com",
            "cold_access_key_id": "AKIA_test",
            "cold_secret_access_key": "secret_test",
        }
    )

    with patch("oneiric.adapters.storage.S3StorageAdapter", FakeS3StorageAdapter):
        asyncio.run(mode.initialize_cold_storage())

    assert len(captured) >= 1, "S3StorageAdapter was never constructed"
    settings = captured[0]
    assert settings["bucket"] == "akosha-r2"
    assert settings["endpoint_url"] == "https://test.r2.cloudflarestorage.com"
    assert settings["region"] == "auto"
    assert settings["access_key_id"] == "AKIA_test"
    assert settings["secret_access_key"] == "secret_test"


def test_cold_tier_fake_gcs_round_trip(tmp_path_factory: pytest.TempPathFactory) -> None:
    """End-to-end: fake-gcs-server subprocess + ColdStore.export_batch() + file-on-disk round trip.

    This test requires fake-gcs-server on PATH (brew install fake-gcs-server).
    Skipped automatically if the binary isn't found.
    """
    import shutil
    import socket
    import subprocess
    import time
    from datetime import UTC, datetime

    if shutil.which("fake-gcs-server") is None:
        pytest.skip("fake-gcs-server not on PATH; install via: brew install fake-gcs-server")

    # Pick a free port so concurrent test runs don't collide and an orphan
    # fake-gcs-server from a previous session doesn't intercept our requests.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    if shutil.which("fake-gcs-server") is None:
        pytest.skip("fake-gcs-server not on PATH; install via: brew install fake-gcs-server")

    from akosha.storage.models import ColdRecord
    from akosha.storage.cold_store import ColdStore

    data_dir = tmp_path_factory.mktemp("fake-gcs")

    proc = subprocess.Popen(
        [
            "fake-gcs-server",
            "-filesystem-root", str(data_dir),
            "-port", str(port),
            "-host", "127.0.0.1",
            "-public-host", f"127.0.0.1:{port}",
            "-location", "US-CENTRAL1",
            "-scheme", "http",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def port_open(host: str, port: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex((host, port)) == 0:
                    return True
            time.sleep(0.1)
        return False

    try:
        assert port_open("127.0.0.1", port), "fake-gcs-server did not start within 10s"

        # fake-gcs-server (unlike real GCS) exposes buckets as filesystem dirs.
        # Pre-create the bucket directory so the SDK's first upload finds a
        # valid bucket. (Real GCS auto-creates with project-permission; fake-gcs
        # does not.)
        (data_dir / "akosha-cold-data").mkdir(parents=True, exist_ok=True)

        async def run() -> str:
            store = ColdStore(
                bucket="akosha-cold-data",
                prefix="conversations/",
                storage_backend="gcs",
                project="local-dev",
                endpoint_url=f"http://127.0.0.1:{port}",
            )
            records = [
                ColdRecord(
                    system_id="test-system",
                    conversation_id="conv-1",
                    fingerprint=[1, 2, 3],
                    ultra_summary="hello world",
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    metadata={"events": 42},
                ),
            ]
            return await store.export_batch(records, partition_path="system-001/2026/01/01")

        import asyncio

        key = asyncio.run(run())
        assert key.endswith(".parquet")
        blob_path = data_dir / "akosha-cold-data" / key
        assert blob_path.exists(), f"blob not found at {blob_path}"
        assert blob_path.stat().st_size > 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
