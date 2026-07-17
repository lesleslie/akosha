---
status: active
role: canonical
date: 2026-07-16
last_reviewed: 2026-07-16
superseded_by: null
blocks_on: []
topic: observability
---

# MAHAVISHNU_DOWN - Orchestrator Unavailable

## Severity

**MEDIUM** (Akosha has fallback mode)

## Detection

- Alert: mahavishnu_availability == "down"
- Symptom: Akosha not receiving scheduled triggers
- Check: No ingestion workflows starting on schedule
