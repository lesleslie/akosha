"""Locust load tests for Akosha ingestion pipeline.

Tests ingestion throughput, latency, and resource usage under load.
"""

from locust import HttpUser, task, between, events
from datetime import datetime
import json
import random


class AkoshaIngestionUser(HttpUser):
    """Simulates Session-Buddy instances uploading memory data."""

    wait_time = between(1, 3)

    def on_start(self):
        """Called when a user starts a new task."""
        self.system_id = f"test-system-{random.randint(1, 100)}"

    @task
    def upload_conversation(self):
        """Upload a single conversation to Akosha."""
        conversation = {
            "system_id": self.system_id,
            "conversation_id": f"conv-{random.randint(1, 10000)}",
            "content": "Test conversation content " + random.choice(["hello", "world", "test"]),
            "embedding": [random.random() for _ in range(384)],
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "source": "session-buddy",
            }
        }

        with self.client.post(
            "/ingest/upload",
            json=conversation,
            catch_response=True
        ) as response:
            if response.status_code != 200:
                events.request_failure.fire(
                    request_type="upload",
                    name=f"Upload failed: {response.status_code}",
                    response_time=response.elapsed.total_seconds(),
                )
            else:
                events.request_success.fire(
                    request_type="upload",
                    name="Upload successful",
                    response_time=response.elapsed.total_seconds(),
                )


class AkoshaQueryUser(HttpUser):
    """Simulates users querying Akosha for similar conversations."""

    wait_time = between(0.5, 2)

    @task
    def search_similar(self):
        """Search for conversations similar to query embedding."""
        query = {
            "embedding": [random.random() for _ in range(384)],
            "system_id": f"test-system-{random.randint(1, 100)}",
            "limit": 10,
        }

        with self.client.post(
            "/query/search",
            json=query,
            catch_response=True
        ) as response:
            if response.status_code == 200:
                data = response.json()
                events.request_success.fire(
                    request_type="query",
                    name="Query successful",
                    response_time=response.elapsed.total_seconds(),
                )


class AkoshaMixedUser(HttpUser):
    """Simulates realistic mixed workload (80% queries, 20% uploads)."""

    wait_time = between(0.5, 2)

    @task(3)
    def upload_conversation(self):
        """Upload conversation (20% of traffic)."""
        conversation = {
            "system_id": f"test-system-{random.randint(1, 100)}",
            "conversation_id": f"conv-{random.randint(1, 10000)}",
            "content": "Mixed workload test",
            "embedding": [random.random() for _ in range(384)],
        }

        self.client.post("/ingest/upload", json=conversation, catch_response=True)

    @task(7)
    def search_similar(self):
        """Search conversations (80% of traffic)."""
        query = {
            "embedding": [random.random() for _ in range(384)],
            "system_id": f"test-system-{random.randint(1, 100)}",
            "limit": 10,
        }

        self.client.post("/query/search", json=query, catch_response=True)


# Spike test user
class SpikeUser(HttpUser):
    """Generates sudden traffic spikes."""

    @task
    def spike_upload(self):
        """Rapid uploads during spike."""
        for _ in range(10):  # 10 rapid requests
            self.client.post(
                "/ingest/upload",
                json={
                    "system_id": "spike-system",
                    "conversation_id": f"conv-{random.randint(1, 10000)}",
                    "content": "Spike test",
                    "embedding": [random.random() for _ in range(384)],
                },
                catch_response=True
            )
            # Minimal wait between spike requests


if __name__ == "__main__":
    # Run load test
    # locust -f test_ingestion_load.py --host=http://localhost:8000 --users=10 --spawn-rate=1
    print("Akosha Load Testing Suite")
    print("=" * 50)
    print("\nUsage:")
    print("1. Baseline test (10 users, spawn rate 1/s):")
    print("   locust -f test_ingestion_load.py --host=http://localhost:8000 --users=10 --spawn-rate=1")
    print("\n2. Target test (100 users, spawn rate 10/s):")
    print("   locust -f test_ingestion_load.py --host=http://localhost:8000 --users=100 --spawn-rate=10")
    print("\n3. Spike test (50 users, spawn rate 50/s):")
    print("   locust -f test_ingestion_load.py --host=http://localhost:8000 --users=50 --spawn-rate=50")
    print("\n4. Run specific user classes:")
    print("   locust -f test_ingestion_load.py --host=http://localhost:8000 --users=20 AkoshaQueryUser")
    print("\n5. With HTML results:")
    print("   locust -f test_ingestion_load.py --host=http://localhost:8000 --users=10 --html results.html")
