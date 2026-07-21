______________________________________________________________________

## status: active role: canonical date: 2026-07-16 last_reviewed: 2026-07-16 superseded_by: null blocks_on: [] topic: observability

# INGESTION_BACKLOG - Large Backlog Response

## Severity

**HIGH**

## Detection

- Alert: akosha_ingestion_backlog_count > 10000
- Dashboard: Grafana dashboard: "Akosha System Health"
- Symptom: Uploads taking >30 minutes to appear in search results
