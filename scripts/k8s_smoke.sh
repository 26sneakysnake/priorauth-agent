#!/usr/bin/env bash
# The demo on a throwaway kind cluster: build the images, load them, apply deploy/k8s/overlays/kind, wait for the
# rollouts, then run the demo smoke test through port-forwards. Needs docker, kind, kubectl and uv on PATH.
#
#   scripts/k8s_smoke.sh                 build, deploy, smoke test, delete the cluster
#   SKIP_BUILD=1 KEEP_CLUSTER=1 ...      reuse priorauth-api:local and priorauth-ui:local; keep the cluster
set -euo pipefail

cluster=${KIND_CLUSTER:-priorauth-smoke}
node_image=kindest/node:v1.34.0@sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a
ns=priorauth-demo
api_port=${K8S_API_PORT:-18000}
ui_port=${K8S_UI_PORT:-13000}
ctx=kind-$cluster
pids=()

step() { printf '\n==> %s\n' "$*"; }

cleanup() {
  status=$?
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  if [ "$status" -ne 0 ] && kubectl --context "$ctx" get ns "$ns" >/dev/null 2>&1; then
    kubectl --context "$ctx" -n "$ns" get pods -o wide || true
    kubectl --context "$ctx" -n "$ns" describe pods | tail -n 60 || true
    kubectl --context "$ctx" -n "$ns" logs deployment/priorauth-api --tail=50 || true
    kubectl --context "$ctx" -n "$ns" logs deployment/priorauth-ui --tail=50 || true
  fi
  if [ -z "${KEEP_CLUSTER:-}" ]; then kind delete cluster --name "$cluster" >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

if [ -z "${SKIP_BUILD:-}" ]; then
  step "build the demo images"
  docker compose -f docker-compose.demo.yml build
fi

step "kind cluster $cluster"
if ! kind get clusters | grep -qx "$cluster"; then
  kind create cluster --name "$cluster" --image "$node_image" --wait 120s
fi
kind load docker-image --name "$cluster" priorauth-api:local priorauth-ui:local

step "apply deploy/k8s/overlays/kind"
kubectl --context "$ctx" apply -k deploy/k8s/overlays/kind
kubectl --context "$ctx" -n "$ns" rollout status deployment/priorauth-api --timeout=180s
kubectl --context "$ctx" -n "$ns" rollout status deployment/priorauth-ui --timeout=180s

step "port-forward the services and run the smoke test"
kubectl --context "$ctx" -n "$ns" port-forward service/priorauth-api "$api_port:8000" >/dev/null &
pids+=($!)
kubectl --context "$ctx" -n "$ns" port-forward service/priorauth-ui "$ui_port:3000" >/dev/null &
pids+=($!)
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$api_port/healthz" >/dev/null && curl -sf "http://127.0.0.1:$ui_port/login" >/dev/null
  then break; fi
  sleep 1
done
uv run python -m priorauth.demo.smoke --api "http://127.0.0.1:$api_port" --ui "http://127.0.0.1:$ui_port"

step "pods"
kubectl --context "$ctx" -n "$ns" get pods -o wide
