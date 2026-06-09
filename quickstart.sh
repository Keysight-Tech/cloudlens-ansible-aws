#!/usr/bin/env bash
# CloudLens Ansible for AWS — quickstart
# Detects environment, installs collections, runs deploy.yaml

set -e

C_GREEN='\033[0;32m'
C_RED='\033[0;31m'
C_YELLOW='\033[1;33m'
C_RESET='\033[0m'

ok()   { echo -e "${C_GREEN}✓${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $1"; }
fail() { echo -e "${C_RED}✗${C_RESET} $1"; exit 1; }

echo "================================================================"
echo " CloudLens Ansible for AWS — Quickstart"
echo " v1.0.0 · Keysight Technologies"
echo "================================================================"

# === Detect environment ===
if [ -n "${AWS_EXECUTION_ENV:-}" ] || [ -n "${CLOUDSHELL_USER:-}" ]; then
  ok "Detected AWS CloudShell environment"
elif [ -d "$HOME/.aws" ]; then
  ok "Detected local AWS CLI configuration ($HOME/.aws)"
else
  warn "No AWS profile detected. Run 'aws configure' or 'aws sso login' first."
fi

# === Verify customer_input.yaml exists ===
if [ ! -f customer_input.yaml ]; then
  warn "customer_input.yaml not found — creating from example"
  cp customer_input.yaml.example customer_input.yaml
  fail "Edit customer_input.yaml with your values and re-run: bash quickstart.sh"
fi
ok "customer_input.yaml present"

# === Verify Ansible installed ===
if ! command -v ansible-playbook >/dev/null 2>&1; then
  fail "Ansible not installed. Install: pip install ansible boto3"
fi
ok "Ansible $(ansible --version | head -1 | awk '{print $2}') detected"

# === Verify boto3 installed (required for AWS dynamic inventory) ===
if ! python3 -c "import boto3" 2>/dev/null; then
  warn "boto3 not installed — installing..."
  pip install boto3 botocore || fail "pip install boto3 failed"
fi
ok "boto3 $(python3 -c 'import boto3; print(boto3.__version__)') detected"

# === Install required Ansible collections ===
echo ""
echo "→ Installing required Ansible collections..."
ansible-galaxy collection install -r requirements.yml --force >/dev/null 2>&1
ok "Collections installed (amazon.aws, community.aws, ansible.windows, etc.)"

# === Verify AWS auth works ===
echo ""
echo "→ Verifying AWS credentials..."
if aws sts get-caller-identity >/dev/null 2>&1; then
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
  ok "AWS auth verified — account $ACCOUNT"
else
  fail "AWS credentials invalid. Run: aws sso login   OR   aws configure"
fi

# === Run dynamic inventory probe ===
echo ""
echo "→ Probing AWS EC2 inventory..."
DISCOVERED=$(ansible-inventory --list -i inventory/aws_ec2.yaml 2>/dev/null | python3 -c "import sys, json; d=json.load(sys.stdin); print(sum(len(g.get('hosts', [])) for k,g in d.items() if k not in ('_meta','all')))" 2>/dev/null || echo "0")
ok "Discovered $DISCOVERED tagged EC2 instance(s)"

if [ "$DISCOVERED" = "0" ]; then
  warn "Zero discovered. Verify tags on your EC2 instances:"
  warn "  cloudlens=yes  os=ubuntu|rhel|windows  env=prod"
  warn "Or run: aws ec2 describe-instances --filters Name=tag:cloudlens,Values=yes"
fi

# === Run the deploy ===
echo ""
echo "→ Running deploy.yaml..."
ansible-playbook deploy.yaml --extra-vars "@customer_input.yaml"

ok "Deploy complete. Verify in CLMS UI: https://$(grep manager_ip_or_fqdn customer_input.yaml | awk '{print $2}' | tr -d '\"')/"
