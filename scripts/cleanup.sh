#!/usr/bin/env bash
# =====================================================================
# CloudLens Ansible Azure Cleanup Wrapper
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

INPUT_FILE="${1:-customer_input.yaml}"

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "✗ Input file not found: $INPUT_FILE"
  exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo "CloudLens Ansible for Azure: CLEANUP"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "This will REMOVE CloudLens sensors from all matching VMs."
echo ""

ansible-inventory -i inventory/azure_rm.yaml --graph

echo ""
read -rp "Continue with cleanup? [y/N] " confirm
if [[ "${confirm,,}" != "y" ]]; then
  echo "Cancelled."
  exit 0
fi

ansible-playbook cleanup.yaml \
  -e "@$INPUT_FILE" \
  -i inventory/azure_rm.yaml

echo ""
echo "Cleanup complete."
