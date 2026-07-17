---
status: active
role: canonical
date: 2026-07-16
last_reviewed: 2026-07-16
superseded_by: null
blocks_on: []
topic: observability
---

# HOT_STORE_FAILURE - Hot Store Corruption Failover

## Severity

**CRITICAL**

## Detection

- Alert: akosha_hot_store_health_status == "unhealthy"
- Dashboard: Grafana "Circuit Breaker Status"
- Symptom: Search requests returning errors or timeouts
