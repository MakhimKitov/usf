#!/usr/bin/env bash
# A throwaway cluster to run bursts on (G3, #44) — what `JobsRunner`'s half of
# `tests/test_runner_contract.py` needs, and what the Stage G done-when is
# rehearsed against. Idempotent: run it again and it repairs what is missing.
#
#   ./scripts/burst-cluster.sh            # build it, print the exports
#   ./scripts/burst-cluster.sh --gvisor   # ...and a second RuntimeClass, `gvisor` (G4)
#   ./scripts/burst-cluster.sh --cilium   # ...on Cilium, bursts' egress walled to the declaration (G6)
#                                         #    (at creation only: delete a kindnet cluster first)
#   ./scripts/burst-cluster.sh --delete   # take it away
#
# Two things here are not obvious, and both are about a laptop rather than about
# k8s:
#
#   * **one address for the archive, from both sides.** A presigned URL is signed
#     over its Host header, so the control plane (on this host) and the pod (in
#     the cluster) have to name the object store identically. Container IPs are
#     not routable from macOS and `localhost` means something different inside a
#     pod — but this host's own LAN address works from both, so that is what the
#     archive URL is built from.
#   * **no registry.** The phases image is loaded into the node with `kind load`,
#     and the contract suite's bursts run that same image (their "agent" is a
#     shell script), so the cluster needs exactly one artifact.
set -euo pipefail

CLUSTER="${BURST_CLUSTER:-usf}"
NAMESPACE="${BURST_NAMESPACE:-bursts}"
BUCKET="${BURST_BUCKET:-burst-cluster}"
PHASES_IMAGE="${BURST_PHASES_IMAGE:-usf/burst-phases:latest}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The golden replay's second runtime (G4): gVisor, pinned and checksummed. It is
# the candidate a kind node can carry — Kata wants nested virtualization, which
# neither a laptop's VM nor a CI runner offers — so the harness is proven here
# on two runtimes, and the bake-off proper (G5) runs it on hardware with all three.
GVISOR_RELEASE="${BURST_GVISOR_RELEASE:-20260921.0}"
# The M4 rig's CNI (G6): an FQDN-capable one, because the declaration names hosts.
# A testing rig's instrument, never the platform's — the reference policy it
# enforces is in examples/egress/, and an adopter's CNI is theirs.
CILIUM_VERSION="${BURST_CILIUM_VERSION:-1.19.8}"

gvisor=0 cilium=0
for arg in "$@"; do
  case "$arg" in
    --delete)
      kind delete cluster --name "$CLUSTER"
      docker rm -f burst-archive >/dev/null 2>&1 || true
      exit 0 ;;
    --gvisor) gvisor=1 ;;
    --cilium) cilium=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

for tool in kind kubectl docker; do
  command -v "$tool" >/dev/null || { echo "$tool is not on PATH" >&2; exit 1; }
done

host_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I | awk '{print $1}')"
[[ -n "$host_ip" ]] || { echo "could not find this host's LAN address" >&2; exit 1; }

if ! kind get clusters | grep -qx "$CLUSTER"; then
  echo "==> creating the cluster"
  if (( cilium )); then
    printf 'kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnetworking:\n  disableDefaultCNI: true\n' \
      | kind create cluster --name "$CLUSTER" --config=-
  else
    kind create cluster --name "$CLUSTER"
  fi
fi
ctx="kind-$CLUSTER"

if (( cilium )); then
  echo "==> Cilium $CILIUM_VERSION as the CNI"
  command -v helm >/dev/null || { echo "helm is not on PATH" >&2; exit 1; }
  helm repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
  helm repo update cilium >/dev/null
  helm upgrade --install cilium cilium/cilium --version "$CILIUM_VERSION" \
    --kube-context "$ctx" --namespace kube-system \
    --set image.pullPolicy=IfNotPresent --set ipam.mode=kubernetes --set operator.replicas=1 \
    --wait >/dev/null
  kubectl --context "$ctx" -n kube-system rollout status ds/cilium --timeout=300s >/dev/null
  kubectl --context "$ctx" wait --for=condition=Ready node --all --timeout=300s >/dev/null
fi

echo "==> the archive, published where both sides can reach it"
if ! docker ps --format '{{.Names}}' | grep -qx burst-archive; then
  docker rm -f burst-archive >/dev/null 2>&1 || true
  docker run -d --name burst-archive -p 8333:8333 \
    -v "$ROOT/docker/seaweedfs/s3.json:/etc/seaweedfs/s3.json:ro" \
    chrislusf/seaweedfs:4.41 \
    server -dir=/data -master.volumeSizeLimitMB=256 -s3 -s3.port=8333 \
    -s3.config=/etc/seaweedfs/s3.json >/dev/null
fi

echo "==> building and loading the phases image"
docker build -q -f "$ROOT/docker/burst-phases/Dockerfile" -t "$PHASES_IMAGE" "$ROOT" >/dev/null
kind load docker-image --name "$CLUSTER" "$PHASES_IMAGE"
if [[ -n "${BURST_AGENT_IMAGE:-}" ]]; then
  # the golden replay's agent: the real dev-agent image, built beforehand
  kind load docker-image --name "$CLUSTER" "$BURST_AGENT_IMAGE"
fi

if (( gvisor )); then
  echo "==> gVisor $GVISOR_RELEASE in the node, as RuntimeClass gvisor"
  node="$CLUSTER-control-plane"
  if ! docker exec "$node" test -x /usr/local/bin/runsc; then
    # unpacked here, not in the node: the node image has no bzip2
    arch="$(docker exec "$node" uname -m)"
    base="https://storage.googleapis.com/gvisor/releases/release/$GVISOR_RELEASE/$arch"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    curl -fsSL -o "$work/gvisor.tar.bz2" "$base/gvisor.tar.bz2"
    curl -fsSL -o "$work/gvisor.tar.bz2.sha512" "$base/gvisor.tar.bz2.sha512"
    (cd "$work" && shasum -a 512 -c gvisor.tar.bz2.sha512 >/dev/null)
    mkdir "$work/bin" && tar -xjf "$work/gvisor.tar.bz2" -C "$work/bin"
    docker cp "$work/bin/." "$node:/usr/local/bin/"
  fi
  if ! docker exec "$node" grep -q "runtimes.runsc" /etc/containerd/config.toml; then
    docker exec "$node" sh -c 'cat >> /etc/containerd/config.toml <<TOML

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
TOML
systemctl restart containerd'
  fi
  kubectl --context "$ctx" apply -f "$ROOT/examples/k8s/runtimeclass-gvisor.yaml" >/dev/null
  kubectl --context "$ctx" wait --for=condition=Ready node --all --timeout=120s >/dev/null
fi

echo "==> the namespace and the runner's account"
kubectl --context "$ctx" apply -f "$ROOT/examples/k8s/burst-runner.yaml" >/dev/null

if (( cilium )); then
  echo "==> bursts' egress: the reference policy, and this rig's archive"
  kubectl --context "$ctx" apply -f "$ROOT/examples/egress/cilium-burst-egress.yaml" >/dev/null
  # the deployment-specific half of the declaration: where this rig's archive is
  kubectl --context "$ctx" apply -f - >/dev/null <<POLICY
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: burst-egress-archive
  namespace: $NAMESPACE
spec:
  endpointSelector:
    matchLabels:
      app: burst
  egress:
    - toCIDR:
        - $host_ip/32
      toPorts:
        - ports:
            - port: "8333"
              protocol: TCP
POLICY
fi

cat <<EXPORTS

The cluster is up. Point a shell at it:

  export KUBECONFIG=\${KUBECONFIG:-~/.kube/config}
  kubectl config use-context $ctx
  export BURST_NAMESPACE=$NAMESPACE
  export ARCHIVE_URL='s3://platform:platform-dev-secret@$host_ip:8333/$BUCKET?tls=false'
  export BURST_PHASES_IMAGE=$PHASES_IMAGE
  export BURST_REPLAY_RUNTIMES=$( (( gvisor )) && echo default,gvisor || echo default)

  uv run pytest tests/test_runner_contract.py tests/test_jobs_runner.py tests/test_replay.py

EXPORTS
