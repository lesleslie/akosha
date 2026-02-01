# HOT_STORE_FAILURE - Hot Store Corruption Failover

## Severity
**CRITICAL**

## Detection
- Alert: akosha_hot_store_health_status == "unhealthy"
- Dashboard: Grafana "Circuit Breaker Status"
- Symptom: Search requests returning errors or timeouts
