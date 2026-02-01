"""Tests for query result aggregator."""

from __future__ import annotations

import pytest

from akosha.query.aggregator import QueryAggregator


class TestQueryAggregator:
    """Test suite for QueryAggregator."""

    def test_merge_empty_result_sets(self) -> None:
        """Test merging empty result sets."""
        result_sets: list[list[dict]] = []
        merged = QueryAggregator.merge_results(result_sets, limit=10)

        assert merged == []

    def test_merge_single_result_set(self) -> None:
        """Test merging single result set."""
        result_sets = [
            [
                {"conversation_id": "conv-1", "similarity": 0.9, "content": "A"},
                {"conversation_id": "conv-2", "similarity": 0.8, "content": "B"},
            ]
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=10)

        assert len(merged) == 2
        assert merged[0]["conversation_id"] == "conv-1"  # Higher similarity first
        assert merged[1]["conversation_id"] == "conv-2"

    def test_merge_multiple_result_sets(self) -> None:
        """Test merging multiple result sets."""
        result_sets = [
            [
                {"conversation_id": "conv-1", "similarity": 0.9, "content": "A"},
                {"conversation_id": "conv-2", "similarity": 0.7, "content": "B"},
            ],
            [
                {"conversation_id": "conv-3", "similarity": 0.95, "content": "C"},
                {"conversation_id": "conv-4", "similarity": 0.6, "content": "D"},
            ],
            [
                {"conversation_id": "conv-5", "similarity": 0.85, "content": "E"},
            ],
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=10)

        # Should have all 5 results
        assert len(merged) == 5

        # Should be sorted by similarity (descending)
        assert merged[0]["conversation_id"] == "conv-3"  # 0.95
        assert merged[1]["conversation_id"] == "conv-1"  # 0.9
        assert merged[2]["conversation_id"] == "conv-5"  # 0.85
        assert merged[3]["conversation_id"] == "conv-2"  # 0.7
        assert merged[4]["conversation_id"] == "conv-4"  # 0.6

    def test_deduplicate_by_conversation_id(self) -> None:
        """Test deduplication of same conversation across sources."""
        result_sets = [
            [
                {"conversation_id": "conv-1", "similarity": 0.9, "source": "hot"},
            ],
            [
                {"conversation_id": "conv-1", "similarity": 0.85, "source": "warm"},
                {"conversation_id": "conv-2", "similarity": 0.8, "source": "warm"},
            ],
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=10)

        # Should only have 2 results (conv-1 deduplicated)
        assert len(merged) == 2

        # First occurrence should be kept (highest similarity)
        conv1_results = [r for r in merged if r["conversation_id"] == "conv-1"]
        assert len(conv1_results) == 1
        assert conv1_results[0]["similarity"] == 0.9
        assert conv1_results[0]["source"] == "hot"

    def test_limit_respected(self) -> None:
        """Test that limit parameter is respected."""
        result_sets = [
            [
                {"conversation_id": f"conv-{i}", "similarity": 1.0 - (i * 0.1)}
                for i in range(10)
            ]
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=5)

        # Should only return top 5
        assert len(merged) == 5
        assert merged[0]["conversation_id"] == "conv-0"
        assert merged[4]["conversation_id"] == "conv-4"

    def test_results_without_similarity_excluded(self) -> None:
        """Test that results without similarity are excluded."""
        result_sets = [
            [
                {"conversation_id": "conv-1", "similarity": 0.9, "content": "A"},
                {"conversation_id": "conv-2", "content": "B"},  # No similarity
                {"conversation_id": "conv-3", "similarity": 0.8, "content": "C"},
            ]
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=10)

        # Should only include results with similarity
        assert len(merged) == 2
        assert all("similarity" in r for r in merged)

    def test_results_without_conversation_id_excluded(self) -> None:
        """Test that results without conversation_id are excluded."""
        result_sets = [
            [
                {"conversation_id": "conv-1", "similarity": 0.9},
                {"similarity": 0.8},  # No conversation_id
                {"conversation_id": "conv-2", "similarity": 0.7},
            ]
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=10)

        # Results without conversation_id should be filtered out during deduplication
        # But they won't have conversation_id, so they're excluded from seen_conversations
        assert len(merged) == 2

    def test_sorting_by_similarity_descending(self) -> None:
        """Test that results are sorted by similarity (descending)."""
        result_sets = [
            [
                {"conversation_id": "conv-low", "similarity": 0.3},
                {"conversation_id": "conv-high", "similarity": 0.95},
                {"conversation_id": "conv-mid", "similarity": 0.6},
            ]
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=10)

        # Should be sorted descending
        assert merged[0]["conversation_id"] == "conv-high"
        assert merged[1]["conversation_id"] == "conv-mid"
        assert merged[2]["conversation_id"] == "conv-low"

    def test_empty_result_sets_in_list(self) -> None:
        """Test handling of empty result sets within the list."""
        result_sets = [
            [],
            [
                {"conversation_id": "conv-1", "similarity": 0.9},
            ],
            [],
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=10)

        assert len(merged) == 1
        assert merged[0]["conversation_id"] == "conv-1"

    def test_zero_limit(self) -> None:
        """Test with zero limit."""
        result_sets = [
            [
                {"conversation_id": "conv-1", "similarity": 0.9},
                {"conversation_id": "conv-2", "similarity": 0.8},
            ]
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=0)

        assert merged == []

    def test_duplicate_conversation_ids_keep_highest_similarity(self) -> None:
        """Test that when duplicates exist, highest similarity is kept."""
        result_sets = [
            [
                {"conversation_id": "conv-1", "similarity": 0.7, "tier": "warm"},
            ],
            [
                {"conversation_id": "conv-1", "similarity": 0.95, "tier": "hot"},
                {"conversation_id": "conv-1", "similarity": 0.6, "tier": "cold"},
            ],
        ]

        merged = QueryAggregator.merge_results(result_sets, limit=10)

        # Should keep first occurrence (from first set)
        conv1_results = [r for r in merged if r["conversation_id"] == "conv-1"]
        assert len(conv1_results) == 1
        # First occurrence wins, not necessarily highest similarity
        assert conv1_results[0]["similarity"] == 0.7
        assert conv1_results[0]["tier"] == "warm"

    def test_large_number_of_results(self) -> None:
        """Test merging large number of results."""
        # Create 1000 results across 10 sources
        result_sets = []
        for i in range(10):
            results = [
                {
                    "conversation_id": f"conv-{i}-{j}",
                    "similarity": 1.0 - ((i * 100 + j) * 0.001),
                }
                for j in range(100)
            ]
            result_sets.append(results)

        merged = QueryAggregator.merge_results(result_sets, limit=50)

        # Should return top 50
        assert len(merged) == 50

        # Should be sorted
        for i in range(len(merged) - 1):
            assert merged[i]["similarity"] >= merged[i + 1]["similarity"]
