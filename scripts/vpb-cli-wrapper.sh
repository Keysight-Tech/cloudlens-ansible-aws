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
  #
  # printf '%b', not echo. Several CLI sequences are multi-line and are passed
  # as one argument with \n separators: the EULA needs two answers before any
  # command, and `kvo` is an interactive context needing ip/port/enable/exit.
  # bash's echo does not expand \n, and the single quotes preserve it, so
  # `echo 'n\ny\nshow version'` sent the literal 16 characters on ONE line.
  # The EULA was therefore never accepted, the CLI stayed blocked, and adoption
  # failed with "vPB CLI never came up" after all 20 retries. Verified on the
  # appliance with od -c: the bytes on the wire were n \ n y \ n s h o w ...
  if ! $KUBECTL --kubeconfig="$KCFG" exec -i -n "$NAMESPACE" "$POD" \
       -c "$CONTAINER" -- bash -c "mkdir -p /data/xfilter/logs; printf '%b\\n' '$2' | $CLI" 2>/tmp/vpb-cli.err; then
    if grep -q "FileNotFoundError\|Connection refused" /tmp/vpb-cli.err 2>/dev/null; then
      echo "vpb: the vPB management plane is not answering yet." >&2
      echo "     A fresh appliance starts its management plane after first configuration." >&2
      echo "     Adopt this vPB in KVO (https://<kvo-ip>) and retry." >&2
      echo "     Cluster health meanwhile: sudo kubectl get pods -A" >&2
    else
      cat /tmp/vpb-cli.err >&2
    fi
    exit 1
  fi
  exit 0
fi

# Pre-flight: probe the CLI BEFORE dropping into it. The usual reason a fresh
# appliance's `show version` "fails" is NOT that the management plane is down:
# it is the one-time EULA gate. xf-client prints "YOU MUST ACCEPT THE IXIA
# SOFTWARE EULA" and blocks for a y/n, so a plain `echo | xf-client` returns
# non-zero with the EULA text. Detect that and tell the operator to accept it,
# rather than sending them to KVO for an adoption that cannot happen until the
# EULA is accepted anyway.
_probe="$($KUBECTL --kubeconfig="$KCFG" exec -n "$NAMESPACE" "$POD" -c "$CONTAINER" \
     -- bash -c "mkdir -p /data/xfilter/logs; echo | timeout 10 $CLI" 2>&1)"
if [[ $? -ne 0 ]]; then
  if printf '%s' "$_probe" | grep -qiE "accept the ixia software|end user license|eula"; then
    echo "vpb: the CLI is up but the one-time EULA has not been accepted yet." >&2
    echo "     Accept it (this is a legal acceptance), then the CLI is usable:" >&2
    echo "       sudo vpb -c 'n\\ny\\nshow version'" >&2
    echo "     After that, 'sudo vpb' works and KVO adoption can proceed." >&2
  else
    echo "vpb: the vPB management plane is not answering yet." >&2
    echo "     Cluster health: sudo kubectl get pods -A   (vpbsystem should be Running)" >&2
    echo "     If pods are Running, retry in a minute; the app may still be starting." >&2
  fi
  exit 1
fi
exec $KUBECTL --kubeconfig="$KCFG" exec -it -n "$NAMESPACE" "$POD" \
     -c "$CONTAINER" -- bash -c "mkdir -p /data/xfilter/logs; exec $CLI"
