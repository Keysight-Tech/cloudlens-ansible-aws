#!/usr/bin/env bash
# vpb - one-command entry point to the CloudLens vPB CLI (xf-client) on v3.15+
#
# Installed at /usr/local/bin/vpb during Marketplace deploy or post-deploy
# bootstrap (scripts/bootstrap-vpb.sh). Usage:
#
#   sudo vpb                     # drop into the CloudLensVPB# CLI
#   sudo vpb -c "show version"   # run a single command non-interactively
#
# Why: vPB v3.15 runs the CLI inside a K8s pod. There is no host-side SSH
# on port 2222 anymore. This wrapper hides the kubectl exec ceremony so
# operators do not have to remember the full path.
#
# AWS note: you reach the vPB host itself over SSH on port 9022, then run
# this wrapper on the box:
#   ssh -i <key>.pem -p 9022 admin@<vpb-eip>
#   sudo vpb
#
# Kubeconfig is auto-detected: both kubeadm (/etc/kubernetes/admin.conf)
# and k3s (/etc/rancher/k3s/k3s.yaml) layouts are supported.
set -euo pipefail

KUBECTL=/usr/bin/kubectl
NAMESPACE="${VPB_NAMESPACE:-default}"
CONTAINER="${VPB_CONTAINER:-vpbsystem}"
CLI="${VPB_CLI_PATH:-/usr/local/bin/xf-client}"

if [[ $EUID -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

# Auto-detect kubeconfig: env override, then kubeadm, then k3s
if [[ -n "${KUBECONFIG:-}" ]] && [[ -r "$KUBECONFIG" ]]; then
  KCFG="$KUBECONFIG"
elif [[ -r /etc/kubernetes/admin.conf ]]; then
  KCFG=/etc/kubernetes/admin.conf
elif [[ -r /etc/rancher/k3s/k3s.yaml ]]; then
  KCFG=/etc/rancher/k3s/k3s.yaml
else
  echo "vpb: cannot find a kubeconfig." >&2
  echo "       Looked at: \$KUBECONFIG, /etc/kubernetes/admin.conf, /etc/rancher/k3s/k3s.yaml" >&2
  echo "       Has KCOS finished initializing? Run: sudo systemctl status k3s" >&2
  exit 1
fi

POD=$($KUBECTL --kubeconfig="$KCFG" get pods -n "$NAMESPACE" --no-headers 2>/dev/null \
        | awk '/vpbsystem/{print $1; exit}')

# If not in the default namespace, search all namespaces
if [[ -z "$POD" ]]; then
  read -r NAMESPACE POD < <($KUBECTL --kubeconfig="$KCFG" get pods -A --no-headers 2>/dev/null \
        | awk '/vpbsystem/{print $1, $2; exit}') || true
fi

if [[ -z "${POD:-}" ]]; then
  echo "vpb: no vpbsystem pod found in any namespace." >&2
  echo "       Check that KCOS is fully up:" >&2
  echo "       sudo kubectl --kubeconfig=$KCFG get pods -A" >&2
  exit 1
fi

if [[ "${1:-}" == "-c" ]] && [[ -n "${2:-}" ]]; then
  # vPB 3.13 images: xf-client crashes if /data/xfilter/logs does not exist
  # yet on a fresh appliance, so create it before invoking the CLI.
  exec $KUBECTL --kubeconfig="$KCFG" exec -i -n "$NAMESPACE" "$POD" \
       -c "$CONTAINER" -- bash -c "mkdir -p /data/xfilter/logs; echo '$2' | $CLI"
fi

exec $KUBECTL --kubeconfig="$KCFG" exec -it -n "$NAMESPACE" "$POD" \
     -c "$CONTAINER" -- bash -c "mkdir -p /data/xfilter/logs; exec $CLI"
