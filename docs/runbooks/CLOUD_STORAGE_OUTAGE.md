# CLOUD_STORAGE_OUTAGE - S3/R2 Unavailable

## Severity
**HIGH**

## Detection
- Alert: akosha_cloud_storage_errors > 100/min
- Symptom: Upload discovery failing
- Check: Cannot list S3 buckets

## Immediate Actions

1. Check S3/R2 connectivity:
   aws s3 ls s3://akosha-cold-data --region us-west-2

2. Verify Oneiric circuit breaker status:
   curl http://akosha:8000/api/v1/metrics | jq .akosha_circuit_breaker_storage_state

3. Check for credentials issues:
   kubectl get secret akosha-secrets -n akosha -o yaml
