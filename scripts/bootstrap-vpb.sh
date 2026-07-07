#!/usr/bin/env bash
# =====================================================================
# CloudLens vPB: Post-Deploy Bootstrap (v3.15+ on KCOS) - AWS
# =====================================================================
# Run this ONCE after SSHing into a fresh Marketplace vPB EC2. It:
#   1. Waits for KCOS (k3s) to finish initializing
#   2. Sets KUBECONFIG system-wide so plain `sudo kubectl ...` works
#   3. Installs the `sudo vpb` wrapper to /usr/local/bin
#   4. Verifies the vpbsystem pod is Running
#   5. Prints the next-step commands for KVO adoption + traffic config
#
# Why this exists: the vPB Marketplace image gives you a working K8s
# cluster but does not expose `vpb` or a friendly kubeconfig on the
# host PATH. Every operator hits the same two errors on first SSH:
#   - "kubectl: connection refused localhost:8080"
#   - "sudo vpb: command not found"
# This script makes both go away in one command.
#
# NOTE: the vPB management SSH port on AWS is 9022, not 22. Reach the box
# with:  ssh -i <key>.pem -p 9022 admin@<vpb-eip>
#
# Usage (from inside the vPB EC2, after the SSH above):
#   curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/scripts/bootstrap-vpb.sh | sudo bash
# =====================================================================
set -euo pipefail

REPO_RAW="${VPB_REPO_RAW:-https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main}"
WAIT_TIMEOUT_SEC="${VPB_WAIT_TIMEOUT_SEC:-600}"  # 10 minutes

C_GREEN='\033[0;32m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[0;34m'; C_RED='\033[0;31m'; C_DIM='\033[2m'; C_RESET='\033[0m'
banner() { echo -e "${C_BLUE}================================================================${C_RESET}"; echo -e "${C_BLUE}  $1${C_RESET}"; echo -e "${C_BLUE}================================================================${C_RESET}"; }
ok()     { echo -e "${C_GREEN}[ok]${C_RESET} $1"; }
warn()   { echo -e "${C_YELLOW}[warn]${C_RESET} $1"; }
fail()   { echo -e "${C_RED}[x]${C_RESET} $1"; exit 1; }
note()   { echo -e "${C_DIM}  $1${C_RESET}"; }
step()   { echo -e "${C_BLUE}--- $1 ---${C_RESET}"; }

if [[ $EUID -ne 0 ]]; then
  exec sudo -E "$0" "$@"
fi

banner "CloudLens vPB Post-Deploy Bootstrap (AWS)"
echo

# =====================================================================
# Step 1: Detect KCOS runtime (k3s or kubeadm) and locate kubeconfig
# =====================================================================
step "Detecting K8s runtime"

if [[ -r /etc/rancher/k3s/k3s.yaml ]]; then
  K8S_RUNTIME=k3s
  KCFG=/etc/rancher/k3s/k3s.yaml
  K8S_SERVICE=k3s
elif [[ -r /etc/kubernetes/admin.conf ]]; then
  K8S_RUNTIME=kubeadm
  KCFG=/etc/kubernetes/admin.conf
  K8S_SERVICE=kubelet
else
  fail "No kubeconfig found at /etc/rancher/k3s/k3s.yaml or /etc/kubernetes/admin.conf. KCOS may not be installed."
fi
ok "Runtime: $K8S_RUNTIME"
ok "Kubeconfig: $KCFG"

# =====================================================================
# Step 2: Wait for K8s API to be reachable
# =====================================================================
step "Waiting for K8s API"

START=$(date +%s)
while :; do
  if kubectl --kubeconfig="$KCFG" version --request-timeout=5s >/dev/null 2>&1; then
    ok "K8s API reachable"
    break
  fi
  ELAPSED=$(( $(date +%s) - START ))
  if (( ELAPSED > WAIT_TIMEOUT_SEC )); then
    fail "K8s API still down after ${WAIT_TIMEOUT_SEC}s. Check: systemctl status $K8S_SERVICE"
  fi
  printf "\r  Waiting... %ds elapsed (timeout %ds)" "$ELAPSED" "$WAIT_TIMEOUT_SEC"
  sleep 5
done
echo

# =====================================================================
# Step 3: Set KUBECONFIG system-wide so plain `sudo kubectl` works
# =====================================================================
step "Installing KUBECONFIG system-wide"

PROFILE=/etc/profile.d/cloudlens-vpb-kubeconfig.sh
cat > "$PROFILE" <<EOF
# Installed by bootstrap-vpb.sh
export KUBECONFIG=$KCFG
EOF
chmod +x "$PROFILE"
ok "$PROFILE installed"
note "New SSH sessions: kubectl will work without --kubeconfig"
note "This session: run  source $PROFILE  before using kubectl"

# =====================================================================
# Step 4: Install the `sudo vpb` wrapper
# =====================================================================
step "Installing sudo vpb wrapper"

WRAPPER=/usr/local/bin/vpb
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$REPO_RAW/scripts/vpb-cli-wrapper.sh" -o "$WRAPPER"
else
  fail "curl not found. Install curl and re-run."
fi
chmod +x "$WRAPPER"
ok "$WRAPPER installed"

# =====================================================================
# Step 5: Verify vpbsystem pod is Running (poll, do not block forever)
# =====================================================================
step "Verifying vpbsystem pod"

POD_NS=""; POD_NAME=""
START=$(date +%s)
while :; do
  POD_LINE=$(kubectl --kubeconfig="$KCFG" get pods -A --no-headers 2>/dev/null | awk '/vpbsystem/{print; exit}')
  if [[ -n "$POD_LINE" ]]; then
    POD_STATUS=$(echo "$POD_LINE" | awk '{print $4}')
    POD_NS=$(echo "$POD_LINE" | awk '{print $1}')
    POD_NAME=$(echo "$POD_LINE" | awk '{print $2}')
    if [[ "$POD_STATUS" == "Running" ]]; then
      ok "vpbsystem pod Running: $POD_NS/$POD_NAME"
      break
    fi
  fi
  ELAPSED=$(( $(date +%s) - START ))
  if (( ELAPSED > WAIT_TIMEOUT_SEC )); then
    warn "vpbsystem pod did not reach Running within ${WAIT_TIMEOUT_SEC}s."
    note "Continue anyway. Check later with: sudo kubectl get pods -A"
    break
  fi
  printf "\r  Waiting for vpbsystem... %ds (timeout %ds)" "$ELAPSED" "$WAIT_TIMEOUT_SEC"
  sleep 10
done
echo

# =====================================================================
# Step 6: Print next-step commands
# =====================================================================
banner "Bootstrap complete. Try these in a NEW shell:"
echo
echo "  # Confirm the cluster is happy"
echo "  sudo kubectl get pods -A"
echo
echo "  # Drop into the vPB CLI (this used to be 'command not found')"
echo "  sudo vpb"
echo
echo "  # Run one-off CLI command"
echo "  sudo vpb -c \"show version\""
echo
if [[ -n "$POD_NS" && -n "$POD_NAME" ]]; then
  echo "  # Tail vpbsystem logs"
  echo "  sudo kubectl logs -f -n $POD_NS $POD_NAME -c vpbsystem"
  echo
fi
echo "Full operations guide:"
echo "  $REPO_RAW/docs/TROUBLESHOOTING.md"
echo
