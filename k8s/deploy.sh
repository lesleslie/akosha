#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Akosha Kubernetes Deployment Script ===${NC}"
echo ""

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}Error: kubectl not found. Please install kubectl first.${NC}"
    exit 1
fi

# Check if cluster is accessible
if ! kubectl cluster-info &> /dev/null; then
    echo -e "${RED}Error: Cannot connect to Kubernetes cluster. Please configure kubectl.${NC}"
    exit 1
fi

echo -e "${GREEN}✓ kubectl is configured${NC}"
echo ""

# Parse command line arguments
COMMAND=${1:-apply}
NAMESPACE=${2:-akosha}

case $COMMAND in
  apply)
    echo -e "${YELLOW}Applying Akosha manifests to namespace: $NAMESPACE${NC}"
    kubectl apply -f k8s/
    echo ""
    echo -e "${GREEN}✓ Manifests applied successfully${NC}"
    echo ""
    echo "Waiting for deployment to be ready..."
    kubectl wait --for=condition=available --timeout=300s deployment/akosha-mcp -n $NAMESPACE
    echo -e "${GREEN}✓ Deployment is ready${NC}"
    echo ""
    echo "Services:"
    kubectl get svc -n $NAMESPACE
    echo ""
    echo "Pods:"
    kubectl get pods -n $NAMESPACE
    echo ""
    echo -e "${GREEN}=== Deployment Complete ===${NC}"
    echo ""
    echo "To access Akosha MCP server:"
    echo "  kubectl port-forward svc/akosha-mcp 3002:3002 -n $NAMESPACE"
    echo ""
    ;;

  delete)
    echo -e "${YELLOW}Deleting Akosha deployment from namespace: $NAMESPACE${NC}"
    kubectl delete -f k8s/
    echo -e "${GREEN}✓ Deployment deleted${NC}"
    ;;

  status)
    echo "=== Akosha Deployment Status ==="
    echo ""
    echo "Pods:"
    kubectl get pods -n $NAMESPACE
    echo ""
    echo "Services:"
    kubectl get svc -n $NAMESPACE
    echo ""
    echo "HPA:"
    kubectl get hpa -n $NAMESPACE
    echo ""
    echo "PVCs:"
    kubectl get pvc -n $NAMESPACE
    echo ""
    ;;

  logs)
    POD_NAME=${3:-$(kubectl get pods -n $NAMESPACE -l app=akosha-mcp -o jsonpath='{.items[0].metadata.name}'}
    echo "Showing logs for pod: $POD_NAME"
    echo "Press Ctrl+C to exit..."
    echo ""
    kubectl logs -f $POD_NAME -n $NAMESPACE
    ;;

  shell)
    POD_NAME=${3:-$(kubectl get pods -n $NAMESPACE -l app=akosha-mcp -o jsonpath='{.items[0].metadata.name}')}
    echo "Opening shell to pod: $POD_NAME"
    kubectl exec -it $POD_NAME -n $NAMESPACE -- /bin/bash
    ;;

  port-forward)
    echo "Port-forwarding Akosha MCP service to localhost:3002"
    echo "Press Ctrl+C to stop..."
    echo ""
    kubectl port-forward svc/akosha-mcp 3002:3002 -n $NAMESPACE
    ;;

  scale)
    REPLICAS=${3:-3}
    echo "Scaling Akosha to $REPLICAS replicas"
    kubectl scale deployment/akosha-mcp --replicas=$REPLICAS -n $NAMESPACE
    ;;

  restart)
    echo "Restarting Akosha deployment..."
    kubectl rollout restart deployment/akosha-mcp -n $NAMESPACE
    echo -e "${GREEN}✓ Restart initiated${NC}"
    kubectl rollout status deployment/akosha-mcp -n $NAMESPACE
    ;;

  *)
    echo "Usage: $0 {apply|delete|status|logs|shell|port-forward|scale|restart} [namespace] [pod_name|replicas]"
    echo ""
    echo "Commands:"
    echo "  apply      - Deploy Akosha to Kubernetes (default)"
    echo "  delete     - Remove Akosha from Kubernetes"
    echo "  status     - Show deployment status"
    echo "  logs       - Show logs (optional: specify pod name)"
    echo "  shell      - Open shell to pod (optional: specify pod name)"
    echo "  port-forward - Forward port 3002 to localhost"
    echo "  scale      - Scale deployment (default: 3 replicas)"
    echo "  restart    - Restart deployment (rolling update)"
    echo ""
    echo "Examples:"
    echo "  $0 apply                    # Deploy to default namespace"
    echo "  $0 apply akosha              # Deploy to custom namespace"
    echo "  $0 logs                      # Show logs from newest pod"
    echo "  $0 logs akosha-mcp-xxxxx     # Show logs from specific pod"
    echo "  $0 scale 5                   # Scale to 5 replicas"
    echo "  $0 port-forward              # Forward port for local access"
    echo ""
    exit 1
    ;;
esac
