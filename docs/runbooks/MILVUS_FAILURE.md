______________________________________________________________________

## status: superseded role: historical date: 2026-09-09 last_reviewed: 2026-09-09 superseded_by: null blocks_on: [] topic: observability

# MILVUS_FAILURE - Vector Database Fallback (ARCHIVED)

> **⚠️ ARCHIVED 2026-09-09** — This runbook is entirely fictional. Akosha has
> **no Milvus integration**: `grep -rn "pymilvus|Milvus|MILVUS" akosha/ pyproject.toml`
> returns 0 matches. All Milvus-specific infrastructure referenced here
> (`milvus` Deployment, `milvus` StatefulSet, `milvusctl`, `app=milvus` label,
> `/api/v1/metrics` Prometheus circuit-breaker state) does not exist.
>
> Storage failure handling for Akosha lives in:
> - `HOT_STORE_FAILURE.md` (in this directory) — DuckDB hot-store recovery
> - The probe-driven `/health` endpoint (`akosha/mcp/server.py:health_check`) —
>   returns 503 with per-feed `checks` when degraded
> - `ARCHITECTURE.md` § Health Checks — canonical contract

This file is retained for provenance. **Do not follow its procedures.**

## Severity

**MEDIUM** (DuckDB fallback available)

## Detection

- Alert: milvus_health_status == "unhealthy"
- Symptom: Vector search failing for warm tier data
- Check: Search latency high or timeouts

## Background

Phase 2+ architecture includes Milvus for warm tier vector search. When Milvus is unavailable, Akosha falls back to DuckDB sequential scan.

## Immediate Actions

1. Verify DuckDB fallback is working:
   curl http://akosha:8000/api/v1/search -X POST \\ # ARCHIVED: Broken link: http://akosha:8000/api/v1/search - Network error: Connection failed
   -H "Content-Type: application/json" \
   -d '{"query": "test", "limit": 10}'

1. Check Milvus deployment:
   kubectl get pods -n akosha -l app=milvus

1. Check circuit breaker status:
   curl http://akosha:8000/api/v1/metrics | jq .akosha_circuit_breaker_milvus_state # ARCHIVED: Broken link: http://akosha:8000/api/v1/metrics - Network error: Connection fail

## Recovery Steps

1. Restart Milvus if needed:
   kubectl rollout restart statefulset/milvus -n akosha

1. Verify Milvus connectivity:
   kubectl exec -it milvus-0 -n akosha -- milvusctl check

1. Verify search still works during outage (DuckDB fallback)
