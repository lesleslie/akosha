# MAHAVISHNU_DOWN - Orchestrator Unavailable

## Severity
**MEDIUM** (Akosha has fallback mode)

## Detection
- Alert: mahavishnu_availability == "down"
- Symptom: Akosha not receiving scheduled triggers
- Check: No ingestion workflows starting on schedule
