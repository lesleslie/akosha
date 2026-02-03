"""Code graph analysis tools for Akosha.

This module provides MCP tools for analyzing indexed code graphs
across repositories to detect patterns, similarities, and dependencies.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def register_code_graph_analysis_tools(
    registry: Any,
    hot_store: Any,
) -> None:
    """Register code graph analysis tools with MCP server.

    Args:
        registry: FastMCP tool registry
        hot_store: HotStore instance for data access
    """
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry

    if not isinstance(registry, FastMCPToolRegistry):
        logger.warning("Invalid registry type for code graph tools")
        return

    # FastMCPToolRegistry has an 'app' attribute
    mcp = registry.app  # type: ignore[attr-defined]

    @mcp.tool()
    async def list_ingested_code_graphs(
        repo_path: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List all code graphs ingested from Session-Buddy.

        Args:
            repo_path: Optional filter by repository path
            limit: Maximum number of results

        Returns:
            Dict with list of code graphs and metadata

        Example:
            >>> result = await list_ingested_code_graphs(repo_path="/path/to/repo")
            >>> print(f"Found {result['count']} code graphs")
        """
        try:
            graphs = await hot_store.list_code_graphs(repo_path=repo_path, limit=limit)

            return {
                "status": "success",
                "count": len(graphs),
                "code_graphs": graphs,
            }

        except Exception as e:
            logger.error(f"Failed to list code graphs: {e}")
            return {
                "status": "error",
                "message": f"Failed to list code graphs: {e}",
                "count": 0,
                "code_graphs": [],
            }

    @mcp.tool()
    async def get_code_graph_details(
        repo_path: str,
        commit_hash: str,
    ) -> dict[str, Any]:
        """Get detailed code graph data including nodes and edges.

        Args:
            repo_path: Path to the repository
            commit_hash: Git commit hash

        Returns:
            Dict with full code graph data or error message

        Example:
            >>> graph = await get_code_graph_details(
            ...     repo_path="/path/to/repo",
            ...     commit_hash="abc123"
            ... )
            >>> print(f"Graph has {graph['nodes_count']} nodes")
        """
        try:
            graph = await hot_store.get_code_graph(repo_path, commit_hash)

            if not graph:
                return {
                    "status": "not_found",
                    "message": f"Code graph not found for {repo_path} @ {commit_hash[:8]}",
                }

            return {
                "status": "success",
                **graph,
            }

        except Exception as e:
            logger.error(f"Failed to get code graph: {e}")
            return {
                "status": "error",
                "message": f"Failed to get code graph: {e}",
            }

    @mcp.tool()
    async def find_similar_repositories(
        repo_path: str,
        min_similarity: float = 0.3,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find repositories with similar code structure.

        This tool compares code graphs across repositories to find
        similar implementations, architectural patterns, or potential
        code duplication.

        Args:
            repo_path: Path to the reference repository
            min_similarity: Minimum similarity threshold (0-1)
            limit: Maximum number of results

        Returns:
            Dict with list of similar repositories and similarity scores

        Example:
            >>> similar = await find_similar_repositories(
            ...     repo_path="/path/to/mahavishnu",
            ...     min_similarity=0.4
            ... )
            >>> for repo in similar['repositories']:
            ...     print(f"{repo['repo_path']}: {repo['similarity']:.2f}")
        """
        try:
            # Get the reference graph
            graphs = await hot_store.list_code_graphs(limit=1000)

            # Find the reference graph
            reference_graph = None
            for graph in graphs:
                if graph["repo_path"] == repo_path:
                    full_graph = await hot_store.get_code_graph(
                        graph["repo_path"], graph["commit_hash"]
                    )
                    if full_graph:
                        reference_graph = full_graph
                        break

            if not reference_graph:
                return {
                    "status": "not_found",
                    "message": f"Reference graph not found for {repo_path}",
                    "repositories": [],
                }

            # Compare with other graphs
            import asyncio

            async def compare_graph(other_graph_summary: dict[str, Any]) -> dict[str, Any] | None:
                """Compare reference graph with another graph."""
                if other_graph_summary["repo_path"] == repo_path:
                    return None

                other_graph = await hot_store.get_code_graph(
                    other_graph_summary["repo_path"],
                    other_graph_summary["commit_hash"],
                )

                if not other_graph:
                    return None

                # Simple similarity based on node types and structure
                similarity = await _compute_graph_similarity(
                    reference_graph["graph_data"],
                    other_graph["graph_data"],
                )

                if similarity >= min_similarity:
                    return {
                        "repo_path": other_graph["repo_path"],
                        "commit_hash": other_graph["commit_hash"],
                        "nodes_count": other_graph["nodes_count"],
                        "similarity": similarity,
                    }

                return None

            # Compare concurrently
            tasks = [compare_graph(g) for g in graphs]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Filter and sort results (narrow BaseException to Exception)
            similar_repos: list[dict[str, Any]] = []
            for r in results:
                if isinstance(r, Exception):
                    continue
                if r:
                    similar_repos.append(r)  # type: ignore[arg-type]

            similar_repos.sort(key=lambda x: x["similarity"], reverse=True)

            return {
                "status": "success",
                "reference_repo": repo_path,
                "reference_nodes": reference_graph["nodes_count"],
                "count": len(similar_repos[:limit]),
                "repositories": similar_repos[:limit],
            }

        except Exception as e:
            logger.error(f"Failed to find similar repositories: {e}")
            return {
                "status": "error",
                "message": f"Failed to find similar repositories: {e}",
                "repositories": [],
            }

    @mcp.tool()
    async def get_cross_repo_function_usage(
        function_name: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Find usage of a function across all indexed repositories.

        This tool searches code graphs to find where a specific function
        is called, imported, or referenced across the entire codebase.

        Args:
            function_name: Name of the function to search for
            limit: Maximum number of results

        Returns:
            Dict with list of repositories and files using the function

        Example:
            >>> usage = await get_cross_repo_function_usage("parse_config")
            >>> for repo in usage['repositories']:
            ...     print(f"{repo['repo_path']}: {len(repo['files'])} files")
        """
        try:
            graphs = await hot_store.list_code_graphs(limit=1000)

            import asyncio

            async def search_graph(graph_summary: dict[str, Any]) -> dict[str, Any] | None:
                """Search for function usage in a graph."""
                graph = await hot_store.get_code_graph(
                    graph_summary["repo_path"],
                    graph_summary["commit_hash"],
                )

                if not graph:
                    return None

                graph_data = graph["graph_data"]
                matching_files = []

                # Search in nodes for the function
                for node in graph_data.get("nodes", {}).values():
                    if isinstance(node, dict):
                        node_name = node.get("name", "")
                        if function_name.lower() in node_name.lower():
                            file_path = node.get("file_path", node.get("file", "unknown"))
                            if file_path != "unknown":
                                matching_files.append(
                                    {
                                        "file": file_path,
                                        "node_type": node.get("type", "unknown"),
                                        "line": node.get("start_line", 0),
                                    }
                                )

                if matching_files:
                    return {
                        "repo_path": graph_summary["repo_path"],
                        "commit_hash": graph_summary["commit_hash"],
                        "files": matching_files,
                    }

                return None

            # Search concurrently
            tasks = [search_graph(g) for g in graphs]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Filter results (narrow BaseException to Exception)
            repos_with_usage: list[dict[str, Any]] = []
            for r in results:
                if isinstance(r, Exception):
                    continue
                if not r:
                    continue
                if r.get("files"):  # type: ignore[union-attr]
                    repos_with_usage.append(r)  # type: ignore[arg-type]

            return {
                "status": "success",
                "function_name": function_name,
                "count": len(repos_with_usage),
                "repositories": repos_with_usage[:limit],
            }

        except Exception as e:
            logger.error(f"Failed to get cross-repo function usage: {e}")
            return {
                "status": "error",
                "message": f"Failed to get cross-repo function usage: {e}",
                "repositories": [],
            }


async def _compute_graph_similarity(
    graph1: dict[str, Any],
    graph2: dict[str, Any],
) -> float:
    """Compute structural similarity between two code graphs.

    Args:
        graph1: First code graph data
        graph2: Second code graph data

    Returns:
        Similarity score between 0 and 1
    """
    try:
        # Extract node types
        nodes1 = graph1.get("nodes", {})
        nodes2 = graph2.get("nodes", {})

        # Count node types
        types1: dict[str, int] = {}
        for node in nodes1.values():
            if isinstance(node, dict):
                node_type = node.get("type", "unknown")
                types1[node_type] = types1.get(node_type, 0) + 1

        types2: dict[str, int] = {}
        for node in nodes2.values():
            if isinstance(node, dict):
                node_type = node.get("type", "unknown")
                types2[node_type] = types2.get(node_type, 0) + 1

        if not types1 or not types2:
            return 0.0

        # Compute cosine similarity of type distributions
        all_types = set(types1.keys()) | set(types2.keys())

        dot_product = sum(types1.get(t, 0) * types2.get(t, 0) for t in all_types)
        norm1 = sum(v**2 for v in types1.values()) ** 0.5
        norm2 = sum(v**2 for v in types2.values()) ** 0.5

        if 0.0 in (norm1, norm2):
            return 0.0

        return float(dot_product / (norm1 * norm2))

    except Exception:
        return 0.0
