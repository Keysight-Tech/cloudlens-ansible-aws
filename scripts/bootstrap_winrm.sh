#!/usr/bin/env bash
# =====================================================================
# Standalone WinRM Bootstrap via Azure CLI
# =====================================================================
# Use this if you want to enable WinRM on a single VM WITHOUT Ansible.
# For bulk operations, use playbooks/bootstrap_windows_winrm.yaml.
# =====================================================================
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <resource-group> <vm-name>"
  echo ""
  echo "Example: $0 customer-prod-rg windowsvm01"
  exit 1
fi

RG="$1"
VM="$2"

echo "Enabling WinRM on $VM in $RG..."

az vm run-command invoke \
  --resource-group "$RG" \
  --name "$VM" \
  --command-id RunPowerShellScript \
  --scripts "winrm quickconfig -force; Enable-PSRemoting -Force; New-NetFirewallRule -DisplayName 'WinRM-HTTP' -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -ErrorAction SilentlyContinue; Set-Item WSMan:\\localhost\\Service\\Auth\\Basic \$true -Force; Set-Item WSMan:\\localhost\\Service\\AllowUnencrypted \$true -Force; Restart-Service WinRM; 'WinRM enabled successfully'"

echo ""
echo "Opening NSG rule for port 5985..."
NSG_NAME="${VM}-nsg"
az network nsg rule create \
  --resource-group "$RG" \
  --nsg-name "$NSG_NAME" \
  --name AllowWinRM \
  --priority 1010 \
  --source-address-prefixes Internet \
  --destination-port-ranges 5985 \
  --access Allow \
  --protocol Tcp \
  --direction Inbound 2>/dev/null || echo "(NSG rule may already exist)"

echo ""
echo "✓ WinRM bootstrap complete for $VM"
