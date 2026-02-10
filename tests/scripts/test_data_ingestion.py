#!/usr/bin/env python3
"""Test data ingestion script for Akosha.

This script verifies the data pipeline by:
1. Inserting test conversations into hot store
2. Testing vector similarity search
3. Testing cold storage export
4. Verifying Redis caching

Usage:
    python tests/scripts/test_data_ingestion.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from akosha.storage.hot_store import HotStore
from akosha.storage.models import HotRecord
from akosha.storage.warm_store import WarmStore


async def test_hot_store() -> bool:
    """Test hot store data insertion and vector search."""
    print("\n" + "=" * 70)
    print("TEST 1: HOT STORE (In-Memory DuckDB)")
    print("=" * 70)

    try:
        # Initialize hot store
        hot_store = HotStore(database_path=":memory:")
        await hot_store.initialize()
        print("✅ Hot store initialized")

        # Insert test conversations (use dict metadata, not Pydantic model)
        test_conversations = [
            HotRecord(
                system_id="test-system-1",
                conversation_id="conv-001",
                content="Python async/await makes concurrent code easier to write and understand.",
                embedding=[0.1, 0.2, 0.3] + [0.0] * 381,  # Mock 384D embedding
                timestamp=datetime.now(UTC),
                metadata={"topic": "python", "language": "python"},  # Plain dict
            ),
            HotRecord(
                system_id="test-system-1",
                conversation_id="conv-002",
                content="Rust ownership system ensures memory safety without garbage collection.",
                embedding=[0.15, 0.25, 0.35] + [0.0] * 381,
                timestamp=datetime.now(UTC),
                metadata={"topic": "rust", "language": "rust"},  # Plain dict
            ),
            HotRecord(
                system_id="test-system-2",
                conversation_id="conv-003",
                content="TypeScript adds static typing to JavaScript for better developer experience.",
                embedding=[0.12, 0.22, 0.32] + [0.0] * 381,
                timestamp=datetime.now(UTC),
                metadata={"topic": "typescript", "language": "typescript"},  # Plain dict
            ),
        ]

        print(f"\n📝 Inserting {len(test_conversations)} test conversations...")
        for record in test_conversations:
            await hot_store.insert(record)
            print(f"  ✅ Inserted {record.conversation_id}")

        # Verify insertion
        count = hot_store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        print(f"\n📊 Total conversations in hot store: {count}")

        # Test vector similarity search
        print("\n🔍 Testing vector similarity search...")
        query_embedding = [0.1, 0.2, 0.3] + [0.0] * 381
        results = await hot_store.search_similar(
            query_embedding=query_embedding,
            system_id="test-system-1",
            limit=5,
            threshold=0.5,
        )

        print(f"  Found {len(results)} similar conversations")
        for i, r in enumerate(results[:3], 1):
            print(f"    {i}. {r['conversation_id']}: similarity={r['similarity']:.4f}")
            print(f"       Content: {r['content'][:60]}...")

        await hot_store.close()
        print("\n✅ HOT STORE TEST: PASSED")
        return True

    except Exception as e:
        print(f"\n❌ HOT STORE TEST: FAILED - {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_warm_store() -> bool:
    """Test warm store initialization."""
    print("\n" + "=" * 70)
    print("TEST 2: WARM STORE (DuckDB On-Disk)")
    print("=" * 70)

    try:
        # Initialize warm store with local path
        warm_path = Path("./data/warm/warm.db")
        warm_path.parent.mkdir(parents=True, exist_ok=True)

        warm_store = WarmStore(database_path=warm_path)
        await warm_store.initialize()
        print(f"✅ Warm store initialized at {warm_path}")

        # Check schema
        tables = warm_store.conn.execute("SHOW TABLES").fetchall()
        print(f"📊 Tables: {[t[0] for t in tables]}")

        await warm_store.close()
        print("\n✅ WARM STORE TEST: PASSED")
        return True

    except Exception as e:
        print(f"\n❌ WARM STORE TEST: FAILED - {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_cold_storage() -> bool:
    """Test cold storage with Oneiric adapter."""
    print("\n" + "=" * 70)
    print("TEST 3: COLD STORAGE (Oneiric Local Adapter)")
    print("=" * 70)

    try:
        from oneiric.adapters.storage import LocalStorageAdapter, LocalStorageSettings

        cold_path = Path("./data/cold").absolute()
        settings = LocalStorageSettings(base_path=str(cold_path))
        storage = LocalStorageAdapter(settings)
        await storage.init()

        print(f"✅ Cold storage initialized at {cold_path}")

        # Test save
        test_data = b'{"test": "conversation data", "id": "conv-001"}'
        key = "conversations/test-conv-001.parquet"
        await storage.save(key, test_data)
        print(f"✅ Saved test data to {key}")

        # Test read
        retrieved = await storage.read(key)
        if retrieved == test_data:
            print(f"✅ Data verification: PASSED")
        else:
            print(f"❌ Data verification: FAILED")
            return False

        # Test list
        files = await storage.list("conversations/")
        print(f"✅ Files in conversations/: {len(files)}")

        # Cleanup
        await storage.delete(key)
        print(f"✅ Cleaned up test file")

        print("\n✅ COLD STORAGE TEST: PASSED")
        return True

    except Exception as e:
        print(f"\n❌ COLD STORAGE TEST: FAILED - {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_redis_cache() -> bool:
    """Test Redis cache connectivity."""
    print("\n" + "=" * 70)
    print("TEST 4: REDIS CACHE (L2 Cache Layer)")
    print("=" * 70)

    try:
        import redis

        r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

        # Test connection
        r.ping()
        print("✅ Redis connection established")

        # Test set/get
        test_key = "akosha:test:cache"
        test_value = "test-data-123"
        r.set(test_key, test_value, ex=60)
        retrieved = r.get(test_key)

        if retrieved == test_value:
            print(f"✅ Redis cache read/write: PASSED")
        else:
            print(f"❌ Redis cache read/write: FAILED")
            return False

        # Cleanup
        r.delete(test_key)

        # Check stats
        info = r.info()
        print(f"📊 Redis memory used: {info['used_memory_human']}")
        print(f"📊 Redis connected clients: {info['connected_clients']}")

        print("\n✅ REDIS CACHE TEST: PASSED")
        return True

    except Exception as e:
        print(f"\n❌ REDIS CACHE TEST: FAILED - {e}")
        import traceback

        traceback.print_exc()
        return False


async def main() -> int:
    """Run all tests."""
    print("\n" + "=" * 70)
    print("AKOSHA DATA INGESTION TEST SUITE")
    print("=" * 70)
    print(f"\nStarted at: {datetime.now(UTC).isoformat()}")

    results = {
        "Hot Store": await test_hot_store(),
        "Warm Store": await test_warm_store(),
        "Cold Storage": await test_cold_storage(),
        "Redis Cache": await test_redis_cache(),
    }

    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for test_name, passed_test in results.items():
        status = "✅ PASSED" if passed_test else "❌ FAILED"
        print(f"  {test_name}: {status}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED! Data pipeline is working correctly.")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed. Check output above for details.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
