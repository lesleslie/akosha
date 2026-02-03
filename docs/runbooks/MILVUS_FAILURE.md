# MILVUS_FAILURE - Vector Database Fallback

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
   curl http://akosha:8000/api/v1/search -X POST \
   -H "Content-Type: application/json" \
   -d '{"query": "test", "limit": 10}'

1. Check Milvus deployment:
   kubectl get pods -n akosha -l app=milvus

1. Check circuit breaker status:
   curl http://akosha:8000/api/v1/metrics | jq .akosha_circuit_breaker_milvus_state

## Recovery Steps

1. Restart Milvus if needed:
   kubectl rollout restart statefulset/milvus -n akosha

1. Verify Milvus connectivity:
   kubectl exec -it milvus-0 -n akosha -- milvusctl check

1. Verify search still works during outage (DuckDB fallback)
