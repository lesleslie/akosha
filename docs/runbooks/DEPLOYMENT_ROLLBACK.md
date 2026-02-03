# DEPLOYMENT_ROLLBACK - Failed Deployment Recovery

## Severity

**CRITICAL**

## Detection

- Alert: Deployment health check failing post-rollout
- Symptom: New version crashing or unhealthy
- Dashboard: Kubernetes deployment status

## Immediate Actions

1. Check deployment status:
   kubectl rollout status deployment/akosha-api -n akosha

1. Check pod logs for errors:
   kubectl logs -f deployment/akosha-api -n akosha --tail=100

1. Identify failure pattern (crash vs. unhealthy)

## Recovery Steps

### Option 1: Immediate Rollback

1. Rollback to previous version:
   kubectl rollout undo deployment/akosha-api -n akosha

1. Verify rollback successful:
   kubectl rollout status deployment/akosha-api -n akosha

1. Monitor for 5 minutes to ensure stability

### Option 2: Fix and Redeploy

If quick fix is possible:

1. Fix the issue in code

1. Build and push new image

1. Update deployment:
   kubectl set image deployment/akosha-api akosha-api=your-registry/akosha:vX.Y.Z -n akosha

1. Monitor rollout:
   kubectl rollout status deployment/akosha-api -n akosha

### Option 3: Scale Down (Critical Failure Only)

If system is completely broken:

1. Scale to zero to prevent further damage:
   kubectl scale deployment/akosha-api --replicas=0 -n akosha

1. Investigate offline

1. Fix issues

1. Scale back up when ready

## Prevention

- Canary deployments: Route small percentage of traffic first
- Health checks: Ensure comprehensive /health endpoint
- Load testing: Test at scale before deploying
- Monitoring: Set up deployment monitoring dashboards
