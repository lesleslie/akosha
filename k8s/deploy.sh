#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Akasha Kubernetes Deployment Script ===${NC}"
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
NAMESPACE=${2:-akasha}

case $COMMAND in
  apply)
    echo -e "${YELLOW}Applying Akasha manifests to namespace: $NAMESPACE${NC}"
    kubectl apply -f k8s/
    echo ""
    echo -e "${GREEN}✓ Manifests applied successfully${NC}"
    echo ""
    echo "Waiting for deployment to be ready..."
    kubectl wait --for=condition=available --timeout=300s deployment/akasha-mcp -n $NAMESPACE
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
    echo "To access Akasha MCP server:"
    echo "  kubectl port-forward svc/akasha-mcp 3002:3002 -n $NAMESPACE"
    echo ""
    ;;

  delete)
    echo -e "${YELLOW}Deleting Akasha deployment from namespace: $NAMESPACE${NC}"
    kubectl delete -f k8s/
    echo -e "${GREEN}✓ Deployment deleted${NC}"
    ;;

  status)
    echo "=== Akasha Deployment Status ==="
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
    POD_NAME=${3:-$(kubectl get pods -n $NAMESPACE -l app=akasha-mcp -o jsonpath='{.items[0].metadata.name}'}
    echo "Showing logs for pod: $POD_NAME"
    echo "Press Ctrl+C to exit..."
    echo ""
    kubectl logs -f $POD_NAME -n $NAMESPACE
    ;;

  shell)
    POD_NAME=${3:-$(kubectl get pods -n $NAMESPACE -l app=akasha-mcp -o jsonpath='{.items[0].metadata.name}')}
    echo "Opening shell to pod: $POD_NAME"
    kubectl exec -it $POD_NAME -n $NAMESPACE -- /bin/bash
    ;;

  port-forward)
    echo "Port-forwarding Akasha MCP service to localhost:3002"
    echo "Press Ctrl+C to stop..."
    echo ""
    kubectl port-forward svc/akasha-mcp 3002:3002 -n $NAMESPACE
    ;;

  scale)
    REPLICAS=${3:-3}
    echo "Scaling Akasha to $REPLICAS replicas"
    kubectl scale deployment/akasha-mcp --replicas=$REPLICAS -n $NAMESPACE
    ;;

  restart)
    echo "Restarting Akasha deployment..."
    kubectl rollout restart deployment/akasha-mcp -n $NAMESPACE
    echo -e "${GREEN}✓ Restart initiated${NC}"
    kubectl rollout status deployment/akasha-mcp -n $NAMESPACE
    ;;

  *)
    echo "Usage: $0 {apply|delete|status|logs|shell|port-forward|scale|restart} [namespace] [pod_name|replicas]"
    echo ""
    echo "Commands:"
    echo "  apply      - Deploy Akasha to Kubernetes (default)"
    echo "  delete     - Remove Akasha from Kubernetes"
    echo "  status     - Show deployment status"
    echo "  logs       - Show logs (optional: specify pod name)"
    echo "  shell      - Open shell to pod (optional: specify pod name)"
    echo "  port-forward - Forward port 3002 to localhost"
    echo "  scale      - Scale deployment (default: 3 replicas)"
    echo "  restart    - Restart deployment (rolling update)"
    echo ""
    echo "Examples:"
    echo "  $0 apply                    # Deploy to default namespace"
    echo "  $0 apply akasha              # Deploy to custom namespace"
    echo "  $0 logs                      # Show logs from newest pod"
    echo "  $0 logs akasha-mcp-xxxxx     # Show logs from specific pod"
    echo "  $0 scale 5                   # Scale to 5 replicas"
    echo "  $0 port-forward              # Forward port for local access"
    echo ""
    exit 1
    ;;
esac
