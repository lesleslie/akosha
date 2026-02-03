"""Query result aggregator for merging cross-tier results."""

import operator
import typing as t


class QueryAggregator:
    """Aggregator for merging results from multiple sources."""

    @staticmethod
    def merge_results(
        result_sets: list[list[dict[str, t.Any]]],
        limit: int = 10,
    ) -> list[dict[str, t.Any]]:
        """Merge and re-rank results from multiple sources.

        Steps:
            1. Flatten results from all sources
            2. Deduplicate by conversation_id
            3. Re-rank by similarity score (descending)
            4. Return top N results

        Args:
            result_sets: List of result sets from different sources/shards
            limit: Maximum number of results to return

        Returns:
            Merged and re-ranked results with highest similarity scores
        """
        # Step 1: Flatten results from all sources
        all_results: list[dict[str, t.Any]] = []
        for result_set in result_sets:
            all_results.extend(result_set)

        # Step 2: Deduplicate by conversation_id
        seen_conversations: set[str] = set()
        unique_results: list[dict[str, t.Any]] = []

        for result in all_results:
            conversation_id = result.get("conversation_id")
            if conversation_id is not None and conversation_id not in seen_conversations:
                seen_conversations.add(conversation_id)
                unique_results.append(result)

        # Step 3: Re-rank by similarity score (descending)
        # Only include results that have similarity key
        results_with_similarity = [result for result in unique_results if "similarity" in result]

        # Sort by similarity in descending order
        results_with_similarity.sort(key=operator.itemgetter("similarity"), reverse=True)

        # Step 4: Return top N results
        return results_with_similarity[:limit]
