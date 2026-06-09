#!/usr/bin/env bash
# =====================================================================
# CloudLens Ansible Azure Deploy Wrapper
# =====================================================================
# Reads customer_input.yaml and runs full deployment.
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

INPUT_FILE="${1:-customer_input.yaml}"

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "✗ Input file not found: $INPUT_FILE"
  echo ""
  echo "Run: cp customer_input.yaml.example customer_input.yaml"
  echo "Then edit customer_input.yaml with your environment details."
  exit 1
fi

# --- Pre-flight checks ---
echo "═══════════════════════════════════════════════════════════════"
echo "CloudLens Ansible for Azure: Deployment"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Verify Azure CLI
if ! command -v az >/dev/null 2>&1; then
  echo "✗ Azure CLI not installed. See: https://learn.microsoft.com/cli/azure/install-azure-cli"
  exit 1
fi

# Verify Ansible
if ! command -v ansible-playbook >/dev/null 2>&1; then
  echo "✗ Ansible not installed. Run: pip3 install ansible-core==2.16"
  exit 1
fi

# Verify Service Principal env vars
for var in AZURE_SUBSCRIPTION_ID AZURE_TENANT AZURE_CLIENT_ID AZURE_SECRET; do
  if [[ -z "${!var:-}" ]]; then
    echo "✗ Environment variable $var is not set."
    echo "  Run: source scripts/load_sp_creds.sh"
    echo "  Or set it manually: export $var=..."
    exit 1
  fi
done

# Verify Windows installer present
WIN_INSTALLER=$(grep -E "^\s*installer_path:" "$INPUT_FILE" | head -1 | awk '{print $2}' | tr -d '"')
if [[ -n "$WIN_INSTALLER" ]] && [[ ! -f "$WIN_INSTALLER" ]]; then
  echo "⚠ Windows installer not found: $WIN_INSTALLER"
  echo "  Place it in files/. Windows VMs will be skipped if missing."
fi

# Verify WinRM password env var
if [[ -z "${ANSIBLE_WINRM_PASSWORD:-}" ]]; then
  echo "⚠ ANSIBLE_WINRM_PASSWORD env var not set; Windows tasks will fail."
  echo "  Set it with: export ANSIBLE_WINRM_PASSWORD='your-password'"
fi

echo "✓ Pre-flight checks passed"
echo ""

# --- Step 1: Display discovered inventory ---
echo "─── Step 1: Discovering Azure VMs ───"
ansible-inventory -i inventory/azure_rm.yaml --graph 2>&1 | tee inventory.txt
echo ""
read -rp "Continue with deployment? [y/N] " confirm
if [[ "${confirm,,}" != "y" ]]; then
  echo "Cancelled."
  exit 0
fi

# --- Step 2: Bootstrap WinRM on Windows VMs ---
echo ""
echo "─── Step 2: Bootstrapping WinRM on Windows VMs ───"
ansible-playbook playbooks/bootstrap_windows_winrm.yaml \
  -e "@$INPUT_FILE" \
  -i inventory/azure_rm.yaml || echo "⚠ WinRM bootstrap had errors; check ansible.log"

# --- Step 3: Deploy sensors across all OS families ---
echo ""
echo "─── Step 3: Deploying CloudLens sensors ───"
ansible-playbook deploy.yaml \
  -e "@$INPUT_FILE" \
  -i inventory/azure_rm.yaml

# --- Final summary ---
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "Deployment complete"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Verify in CloudLens Manager UI:"
MANAGER_IP=$(grep -E "^\s*manager_ip_or_fqdn:" "$INPUT_FILE" | head -1 | awk '{print $2}' | tr -d '"')
echo "  https://$MANAGER_IP"
echo ""
echo "Sensor logs:"
echo "  Linux VMs:   docker logs cloudlens-agent"
echo "  Windows VMs: Get-Service CloudLens; Get-Content C:\\ProgramData\\CloudLens\\Logs\\*.log"
echo ""
