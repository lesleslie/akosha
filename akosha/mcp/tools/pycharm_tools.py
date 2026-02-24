"""PyCharm MCP tools for Akosha cross-system code analysis.

This module provides MCP tools that leverage PyCharm's IDE capabilities
for enhanced code search, diagnostics, and analysis across all indexed
repositories in Akosha's memory aggregation system.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreakerState:
    """Circuit breaker for PyCharm MCP resilience."""

    failure_count: int = 0
    last_failure_time: float = 0.0
    is_open: bool = False
    failure_threshold: int = 3
    recovery_timeout: float = 60.0

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.is_open = True
            logger.warning(f"Circuit breaker opened after {self.failure_count} failures")

    def record_success(self) -> None:
        self.failure_count = 0
        self.is_open = False

    def can_execute(self) -> bool:
        if not self.is_open:
            return True
        elapsed = time.time() - self.last_failure_time
        if elapsed >= self.recovery_timeout:
            logger.info("Circuit breaker entering half-open state")
            return True
        return False


@dataclass
class SearchResult:
    """Search result from PyCharm index."""

    file_path: str
    line_number: int
    column: int
    match_text: str
    repo_path: str | None = None
    context_before: str | None = None
    context_after: str | None = None


class PyCharmMCPAdapter:
    """Adapter for PyCharm MCP integration with circuit breaker and caching."""

    def __init__(
        self,
        mcp_client: Any | None = None,
        timeout: float = 30.0,
        max_results: int = 100,
    ) -> None:
        self._mcp = mcp_client
        self._timeout = timeout
        self._max_results = max_results
        self._circuit_breaker = CircuitBreakerState()
        self._cache: dict[str, Any] = {}
        self._cache_ttl: dict[str, float] = {}
        self._available = mcp_client is not None

    async def search_regex(
        self,
        pattern: str,
        file_pattern: str | None = None,
        scope: str = "all",
    ) -> list[SearchResult]:
        """Search for regex pattern across indexed files."""
        sanitized = self._sanitize_regex(pattern)
        if not sanitized:
            logger.warning(f"Invalid regex pattern rejected: {pattern[:50]}")
            return []

        cache_key = f"search:{sanitized}:{file_pattern}:{scope}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        results = await self._execute_with_circuit_breaker(
            self._search_regex_impl,
            sanitized,
            file_pattern,
            scope,
        )

        self._set_cached(cache_key, results, ttl=60.0)
        return results

    async def get_file_problems(
        self,
        file_path: str,
        errors_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Get IDE diagnostics for a file."""
        if not self._is_safe_path(file_path):
            return []

        cache_key = f"problems:{file_path}:{errors_only}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        problems = await self._execute_with_circuit_breaker(
            self._get_file_problems_impl,
            file_path,
            errors_only,
        )

        self._set_cached(cache_key, problems, ttl=10.0)
        return problems

    async def find_usages(
        self,
        symbol_name: str,
        file_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find all usages of a symbol."""
        cache_key = f"usages:{symbol_name}:{file_path}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        usages = await self._execute_with_circuit_breaker(
            self._find_usages_impl,
            symbol_name,
            file_path,
        )

        self._set_cached(cache_key, usages, ttl=30.0)
        return usages

    async def _search_regex_impl(
        self,
        pattern: str,
        file_pattern: str | None,
        scope: str,
    ) -> list[SearchResult]:
        if not self._mcp:
            return []

        try:
            results = await asyncio.wait_for(
                self._mcp.search_regex(
                    pattern=pattern,
                    file_pattern=file_pattern,
                ),
                timeout=self._timeout,
            )

            search_results = []
            for item in results[: self._max_results]:
                search_results.append(
                    SearchResult(
                        file_path=item.get("file_path", ""),
                        line_number=item.get("line", 0),
                        column=item.get("column", 0),
                        match_text=item.get("match", ""),
                        context_before=item.get("context_before"),
                        context_after=item.get("context_after"),
                    )
                )

            return search_results

        except TimeoutError:
            logger.warning(f"Search timed out for pattern: {pattern[:50]}")
            return []
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []

    async def _get_file_problems_impl(
        self,
        file_path: str,
        errors_only: bool,
    ) -> list[dict[str, Any]]:
        if not self._mcp:
            return []

        try:
            problems = await asyncio.wait_for(
                self._mcp.get_file_problems(
                    file_path=file_path,
                    errors_only=errors_only,
                ),
                timeout=self._timeout,
            )
            return list(problems) if problems else []
        except Exception as e:
            logger.error(f"Get problems failed: {e}")
            return []

    async def _find_usages_impl(
        self,
        symbol_name: str,
        file_path: str | None,
    ) -> list[dict[str, Any]]:
        if not self._mcp:
            return []

        try:
            usages = await asyncio.wait_for(
                self._mcp.find_usages(
                    symbol_name=symbol_name,
                    file_path=file_path,
                ),
                timeout=self._timeout,
            )
            return list(usages) if usages else []
        except Exception as e:
            logger.error(f"Find usages failed: {e}")
            return []

    def _sanitize_regex(self, pattern: str) -> str:
        """Sanitize regex pattern to prevent ReDoS."""
        if len(pattern) > 500:
            return ""

        dangerous_patterns = [
            r"\(\.\*\)\+",
            r"\(\.\+\)\+",
            r"\(\.\*\)\*",
            r"\(\.\+\)\*",
            r"\(\.\*\)\{",
            r"\(\.\+\)\{",
        ]

        for dangerous in dangerous_patterns:
            if re.search(dangerous, pattern):
                return ""

        try:
            re.compile(pattern)
            return pattern
        except re.error:
            return ""

    def _is_safe_path(self, file_path: str) -> bool:
        """Validate file path for safety."""
        if not file_path:
            return False
        if ".." in file_path:
            return False
        if "\x00" in file_path:
            return False
        return True

    async def _execute_with_circuit_breaker(
        self,
        func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if not self._circuit_breaker.can_execute():
            logger.debug("Circuit breaker is open, skipping operation")
            return []

        try:
            result = await func(*args, **kwargs)
            self._circuit_breaker.record_success()
            return result
        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.error(f"Operation failed (circuit breaker): {e}")
            raise

    def _get_cached(self, key: str) -> Any | None:
        if key in self._cache:
            expiry = self._cache_ttl.get(key, 0)
            if time.time() < expiry:
                return self._cache[key]
            del self._cache[key]
            self._cache_ttl.pop(key, None)
        return None

    def _set_cached(self, key: str, value: Any, ttl: float = 60.0) -> None:
        self._cache[key] = value
        self._cache_ttl[key] = time.time() + ttl

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_ttl.clear()

    async def health_check(self) -> dict[str, Any]:
        return {
            "mcp_available": self._mcp is not None,
            "circuit_breaker_open": self._circuit_breaker.is_open,
            "failure_count": self._circuit_breaker.failure_count,
            "cache_size": len(self._cache),
        }


# Global adapter instance
_pycharm_adapter: PyCharmMCPAdapter | None = None


def get_pycharm_adapter() -> PyCharmMCPAdapter:
    """Get or create the global PyCharm adapter instance."""
    global _pycharm_adapter
    if _pycharm_adapter is None:
        _pycharm_adapter = PyCharmMCPAdapter()
    return _pycharm_adapter


def register_pycharm_tools(
    registry: Any,
    hot_store: Any,
) -> None:
    """Register PyCharm integration tools with MCP server.

    Args:
        registry: FastMCP tool registry
        hot_store: HotStore instance for code graph data
    """
    from akosha.mcp.tools.tool_registry import FastMCPToolRegistry, ToolCategory, ToolMetadata

    if not isinstance(registry, FastMCPToolRegistry):
        logger.warning("Invalid registry type for PyCharm tools")
        return

    adapter = get_pycharm_adapter()

    @registry.register(
        ToolMetadata(
            name="search_code_patterns",
            description="Search for code patterns across all indexed repositories using regex",
            category=ToolCategory.SEARCH,
        )
    )
    async def search_code_patterns(
        pattern: str,
        file_pattern: str | None = None,
        scope: str = "all",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Search for code patterns across all indexed repositories.

        Uses PyCharm's file index for fast, accurate regex searches
        across the entire codebase. Combines results from both the
        PyCharm index and Akosha's code graph data.

        Args:
            pattern: Regex pattern to search for
            file_pattern: Optional file glob filter (e.g., "*.py")
            scope: Search scope - "all", "python", "typescript"
            limit: Maximum number of results

        Returns:
            Dict with list of matches including file, line, and context

        Example:
            >>> results = await search_code_patterns(
            ...     pattern=r"async def\\s+\\w+\\(",
            ...     file_pattern="*.py"
            ... )
        """
        try:
            # Search via PyCharm adapter
            pycharm_results = await adapter.search_regex(
                pattern=pattern,
                file_pattern=file_pattern,
                scope=scope,
            )

            # Also search code graphs for cross-repo context
            code_graph_results = []
            if hot_store:
                graphs = await hot_store.list_code_graphs(limit=50)
                for graph_summary in graphs[:10]:
                    graph = await hot_store.get_code_graph(
                        graph_summary["repo_path"],
                        graph_summary["commit_hash"],
                    )
                    if graph and "graph_data" in graph:
                        for node in graph["graph_data"].get("nodes", {}).values():
                            if isinstance(node, dict):
                                source = node.get("source", "")
                                if source and re.search(pattern, source):
                                    code_graph_results.append({
                                        "repo_path": graph_summary["repo_path"],
                                        "file_path": node.get("file_path", "unknown"),
                                        "line_number": node.get("start_line", 0),
                                        "match_text": source[:100],
                                        "source": "code_graph",
                                    })

            # Combine and deduplicate results
            all_results = []

            for r in pycharm_results[:limit]:
                all_results.append({
                    "file_path": r.file_path,
                    "line_number": r.line_number,
                    "column": r.column,
                    "match_text": r.match_text,
                    "context_before": r.context_before,
                    "context_after": r.context_after,
                    "source": "pycharm_index",
                })

            for r in code_graph_results[:limit]:
                all_results.append(r)

            return {
                "status": "success",
                "pattern": pattern,
                "count": len(all_results[:limit]),
                "results": all_results[:limit],
                "pycharm_available": adapter._available,
            }

        except Exception as e:
            logger.error(f"Failed to search code patterns: {e}")
            return {
                "status": "error",
                "message": f"Search failed: {e}",
                "results": [],
            }

    @registry.register(
        ToolMetadata(
            name="get_code_problems",
            description="Get code problems (diagnostics) across indexed repositories",
            category=ToolCategory.ANALYTICS,
        )
    )
    async def get_code_problems(
        repo_path: str | None = None,
        severity: str = "ERROR",
        category: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get code problems (diagnostics) across indexed repositories.

        Retrieves IDE-level diagnostics from PyCharm including syntax errors,
        type checking issues, code style violations, and potential bugs.

        Args:
            repo_path: Optional filter by repository path
            severity: Filter by severity - "ERROR", "WARNING", "INFO"
            category: Optional category filter - "STYLE", "ERROR", "TYPE"
            limit: Maximum number of results

        Returns:
            Dict with list of problems grouped by repository

        Example:
            >>> problems = await get_code_problems(severity="ERROR")
            >>> for prob in problems['problems']:
            ...     print(f"{prob['file']}:{prob['line']}: {prob['message']}")
        """
        try:
            problems = []

            if hot_store:
                graphs = await hot_store.list_code_graphs(
                    repo_path=repo_path,
                    limit=100,
                )

                for graph_summary in graphs[:20]:
                    graph = await hot_store.get_code_graph(
                        graph_summary["repo_path"],
                        graph_summary["commit_hash"],
                    )

                    if not graph:
                        continue

                    # Extract problems from code graph if available
                    graph_data = graph.get("graph_data", {})
                    for node in graph_data.get("nodes", {}).values():
                        if isinstance(node, dict):
                            node_problems = node.get("problems", [])
                            for prob in node_problems:
                                if severity == "ERROR" and prob.get("severity") != "ERROR":
                                    continue
                                if severity == "WARNING" and prob.get("severity") not in ("ERROR", "WARNING"):
                                    continue

                                problems.append({
                                    "repo_path": graph_summary["repo_path"],
                                    "file_path": node.get("file_path", "unknown"),
                                    "line_number": node.get("start_line", 0),
                                    "message": prob.get("message", "Unknown issue"),
                                    "severity": prob.get("severity", "UNKNOWN"),
                                    "category": prob.get("category", "GENERAL"),
                                    "source": "code_graph",
                                })

            # Sort by severity
            severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
            problems.sort(key=lambda x: severity_order.get(x.get("severity", "INFO"), 3))

            return {
                "status": "success",
                "severity_filter": severity,
                "count": len(problems[:limit]),
                "problems": problems[:limit],
            }

        except Exception as e:
            logger.error(f"Failed to get code problems: {e}")
            return {
                "status": "error",
                "message": f"Failed to get problems: {e}",
                "problems": [],
            }

    @registry.register(
        ToolMetadata(
            name="find_function_usage",
            description="Find usage of a function across all indexed repositories",
            category=ToolCategory.SEARCH,
        )
    )
    async def find_function_usage(
        function_name: str,
        language: str = "python",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Find usage of a function across all indexed repositories.

        Searches code graphs to find where a specific function is called,
        imported, or referenced across the entire codebase. Enhanced with
        PyCharm's symbol index when available.

        Args:
            function_name: Name of the function to search for
            language: Programming language filter
            limit: Maximum number of results per repository

        Returns:
            Dict with list of repositories and files using the function

        Example:
            >>> usage = await find_function_usage("parse_config")
            >>> for repo in usage['repositories']:
            ...     print(f"{repo['repo_path']}: {len(repo['files'])} usages")
        """
        try:
            # First use PyCharm adapter if available
            pycharm_usages = []
            if adapter._available:
                pycharm_usages = await adapter.find_usages(function_name)

            # Search code graphs
            graph_usages = []
            if hot_store:
                graphs = await hot_store.list_code_graphs(limit=1000)

                async def search_graph(graph_summary: dict[str, Any]) -> dict[str, Any] | None:
                    graph = await hot_store.get_code_graph(
                        graph_summary["repo_path"],
                        graph_summary["commit_hash"],
                    )

                    if not graph:
                        return None

                    graph_data = graph.get("graph_data", {})
                    matching_files = []

                    for node in graph_data.get("nodes", {}).values():
                        if isinstance(node, dict):
                            node_name = node.get("name", "")
                            node_type = node.get("type", "")

                            # Match function definitions, calls, and imports
                            if function_name.lower() in node_name.lower():
                                if node_type in ("function", "method", "call", "import"):
                                    matching_files.append({
                                        "file": node.get("file_path", "unknown"),
                                        "node_type": node_type,
                                        "line": node.get("start_line", 0),
                                        "name": node_name,
                                    })

                    if matching_files:
                        return {
                            "repo_path": graph_summary["repo_path"],
                            "commit_hash": graph_summary["commit_hash"],
                            "files": matching_files[:limit],
                        }

                    return None

                tasks = [search_graph(g) for g in graphs]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for r in results:
                    if isinstance(r, Exception):
                        continue
                    if r and r.get("files"):
                        graph_usages.append(r)

            # Combine results
            all_usages = graph_usages

            # Add PyCharm results
            for usage in pycharm_usages:
                all_usages.append({
                    "repo_path": "pycharm_index",
                    "files": [{
                        "file": usage.get("file_path", "unknown"),
                        "line": usage.get("line", 0),
                        "node_type": usage.get("type", "usage"),
                    }],
                })

            return {
                "status": "success",
                "function_name": function_name,
                "language": language,
                "count": len(all_usages),
                "repositories": all_usages[:limit],
                "pycharm_available": adapter._available,
            }

        except Exception as e:
            logger.error(f"Failed to find function usage: {e}")
            return {
                "status": "error",
                "message": f"Failed to find function usage: {e}",
                "repositories": [],
            }

    @registry.register(
        ToolMetadata(
            name="analyze_imports",
            description="Analyze imports across indexed repositories (unused, circular, patterns)",
            category=ToolCategory.ANALYTICS,
        )
    )
    async def analyze_imports(
        repo_path: str | None = None,
        analysis_type: str = "unused",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Analyze imports across indexed repositories.

        Provides import analysis including unused imports, circular
        dependencies, and import patterns across the codebase.

        Args:
            repo_path: Optional filter by repository path
            analysis_type: Type of analysis - "unused", "circular", "patterns"
            limit: Maximum number of results

        Returns:
            Dict with analysis results based on analysis_type

        Example:
            >>> unused = await analyze_imports(analysis_type="unused")
            >>> for imp in unused['imports']:
            ...     print(f"{imp['file']}: unused {imp['import']}")
        """
        try:
            results = []

            if hot_store:
                graphs = await hot_store.list_code_graphs(
                    repo_path=repo_path,
                    limit=100,
                )

                for graph_summary in graphs[:30]:
                    graph = await hot_store.get_code_graph(
                        graph_summary["repo_path"],
                        graph_summary["commit_hash"],
                    )

                    if not graph:
                        continue

                    graph_data = graph.get("graph_data", {})

                    # Analyze imports based on type
                    if analysis_type == "unused":
                        # Find imports that are declared but not used
                        imports = set()
                        usages = set()

                        for node in graph_data.get("nodes", {}).values():
                            if isinstance(node, dict):
                                node_type = node.get("type", "")
                                node_name = node.get("name", "")

                                if node_type == "import":
                                    imports.add(node_name.split(".")[-1])
                                elif node_type in ("call", "reference"):
                                    usages.add(node_name)

                        unused = imports - usages
                        for imp in unused:
                            results.append({
                                "repo_path": graph_summary["repo_path"],
                                "import": imp,
                                "type": "unused_import",
                            })

                    elif analysis_type == "circular":
                        # Detect potential circular imports
                        edges = graph_data.get("edges", [])
                        import_graph: dict[str, set[str]] = {}

                        for edge in edges:
                            if isinstance(edge, dict):
                                if edge.get("type") == "imports":
                                    src = edge.get("source", "")
                                    tgt = edge.get("target", "")
                                    if src and tgt:
                                        import_graph.setdefault(src, set()).add(tgt)

                        # Check for cycles
                        for node, deps in import_graph.items():
                            for dep in deps:
                                if dep in import_graph and node in import_graph[dep]:
                                    results.append({
                                        "repo_path": graph_summary["repo_path"],
                                        "cycle": [node, dep],
                                        "type": "circular_import",
                                    })

                    elif analysis_type == "patterns":
                        # Find common import patterns
                        import_counts: dict[str, int] = {}

                        for node in graph_data.get("nodes", {}).values():
                            if isinstance(node, dict) and node.get("type") == "import":
                                imp = node.get("name", "")
                                import_counts[imp] = import_counts.get(imp, 0) + 1

                        for imp, count in sorted(
                            import_counts.items(),
                            key=lambda x: x[1],
                            reverse=True,
                        )[:10]:
                            results.append({
                                "repo_path": graph_summary["repo_path"],
                                "import": imp,
                                "count": count,
                                "type": "import_pattern",
                            })

            return {
                "status": "success",
                "analysis_type": analysis_type,
                "count": len(results[:limit]),
                "imports": results[:limit],
            }

        except Exception as e:
            logger.error(f"Failed to analyze imports: {e}")
            return {
                "status": "error",
                "message": f"Import analysis failed: {e}",
                "imports": [],
            }

    @registry.register(
        ToolMetadata(
            name="pycharm_health",
            description="Check health status of PyCharm MCP integration",
            category=ToolCategory.SYSTEM,
        )
    )
    async def pycharm_health() -> dict[str, Any]:
        """Check health status of PyCharm MCP integration.

        Returns diagnostic information about the PyCharm integration
        including availability, circuit breaker state, and cache status.

        Returns:
            Dict with health status and diagnostic information
        """
        try:
            adapter_health = await adapter.health_check()

            return {
                "status": "success",
                "healthy": adapter_health.get("mcp_available", False),
                "details": {
                    "pycharm_mcp_available": adapter_health.get("mcp_available", False),
                    "circuit_breaker_open": adapter_health.get("circuit_breaker_open", True),
                    "failure_count": adapter_health.get("failure_count", 0),
                    "cache_size": adapter_health.get("cache_size", 0),
                },
            }

        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return {
                "status": "error",
                "healthy": False,
                "message": f"Health check failed: {e}",
            }

    logger.info("Registered PyCharm integration tools")


__all__ = [
    "register_pycharm_tools",
    "PyCharmMCPAdapter",
    "CircuitBreakerState",
    "SearchResult",
    "get_pycharm_adapter",
]
