#!/usr/bin/env bash
# =====================================================================
# CloudLens Ansible AWS Deploy Wrapper
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
echo "CloudLens Ansible for AWS: Deployment"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Verify AWS CLI
if ! command -v aws >/dev/null 2>&1; then
  echo "✗ AWS CLI not installed. See: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  exit 1
fi

# Verify Ansible
if ! command -v ansible-playbook >/dev/null 2>&1; then
  echo "✗ Ansible not installed. Run: pip3 install ansible-core==2.16"
  exit 1
fi

# Verify AWS credentials resolve. This works for every auth method the
# aws_ec2 inventory plugin supports: SSO, a named profile, static env keys,
# or an EC2/CloudShell instance role. One check covers them all.
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "✗ AWS credentials are not configured or have expired."
  echo "  Guided setup:   bash scripts/setup_aws_creds.sh"
  echo "  SSO login:      aws sso login --profile <profile>"
  echo "  Static keys:    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=..."
  echo "  Named profile:  export AWS_PROFILE=<profile>"
  exit 1
fi

# Verify Windows installer present
WIN_INSTALLER=$(grep -E "^\s*installer_path:" "$INPUT_FILE" | head -1 | awk '{print $2}' | tr -d '"')
if [[ -n "$WIN_INSTALLER" ]] && [[ ! -f "$WIN_INSTALLER" ]]; then
  echo "⚠ Windows installer not found: $WIN_INSTALLER"
  echo "  Place it in files/. Windows instances will be skipped if missing."
fi

# Verify WinRM password env var
if [[ -z "${ANSIBLE_WINRM_PASSWORD:-}" ]]; then
  echo "⚠ ANSIBLE_WINRM_PASSWORD env var not set; Windows tasks will fail."
  echo "  Set it with: export ANSIBLE_WINRM_PASSWORD='your-password'"
fi

echo "✓ Pre-flight checks passed"
echo ""

# --- Step 1: Display discovered inventory ---
echo "─── Step 1: Discovering AWS EC2 instances ───"
ansible-inventory -i inventory/aws_ec2.yaml --graph 2>&1 | tee inventory.txt
echo ""
read -rp "Continue with deployment? [y/N] " confirm
if [[ "${confirm,,}" != "y" ]]; then
  echo "Cancelled."
  exit 0
fi

# --- Step 2: Bootstrap WinRM on Windows instances ---
echo ""
echo "─── Step 2: Bootstrapping WinRM on Windows instances ───"
ansible-playbook playbooks/bootstrap_windows_winrm.yaml \
  -e "@$INPUT_FILE" \
  -i inventory/aws_ec2.yaml || echo "⚠ WinRM bootstrap had errors; check ansible.log"

# --- Step 3: Deploy sensors across all OS families ---
echo ""
echo "─── Step 3: Deploying CloudLens sensors ───"
ansible-playbook deploy.yaml \
  -e "@$INPUT_FILE" \
  -i inventory/aws_ec2.yaml

# --- Final summary ---
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "Deployment complete"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Verify in CloudLens vController UI:"
MANAGER_IP=$(grep -E "^\s*manager_ip_or_fqdn:" "$INPUT_FILE" | head -1 | awk '{print $2}' | tr -d '"')
echo "  https://$MANAGER_IP"
echo ""
echo "Sensor logs:"
echo "  Linux instances:   docker logs cloudlens-agent"
echo "  Windows instances: Get-Service CloudLens; Get-Content C:\\ProgramData\\CloudLens\\Logs\\*.log"
echo ""
