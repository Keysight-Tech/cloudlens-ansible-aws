#!/usr/bin/env bash
# =====================================================================
# AWS Credentials Setup Helper (IAM)
# =====================================================================
# The AWS equivalent of Azure's setup_azure_sp.sh. Where Azure needs a
# Service Principal, AWS needs a set of credentials that can:
#   - describe/run EC2 (for the CloudFormation/Terraform deploy)
#   - read EC2 for the Ansible dynamic inventory (amazon.aws.aws_ec2)
#   - use SSM (for Windows WinRM bootstrap / SSM connections)
#
# This script guides you to the RIGHT auth style for your setup:
#   1. AWS SSO / IAM Identity Center  (recommended - short-lived creds)
#   2. A named CLI profile             (aws configure --profile ...)
#   3. An IAM user access key          (long-lived - least preferred)
#   4. Assume-role into another account
#
# It does NOT mint long-lived keys for you by default (that is an IAM
# anti-pattern). It configures whichever style you pick and verifies it
# with `aws sts get-caller-identity`, then writes scripts/load_aws_creds.sh
# so the rest of the tooling can `source` a single file.
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

C_GREEN='\033[0;32m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[0;34m'; C_RED='\033[0;31m'; C_RESET='\033[0m'
ok()   { echo -e "${C_GREEN}[ok]${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}[warn]${C_RESET} $1"; }
fail() { echo -e "${C_RED}[x]${C_RESET} $1" >&2; exit 1; }
step() { echo -e "${C_BLUE}--- $1 ---${C_RESET}"; }

command -v aws >/dev/null 2>&1 || fail "AWS CLI not installed. See https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"

DEFAULT_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

echo "================================================================"
echo " CloudLens AWS Credentials Setup"
echo "================================================================"
echo
echo "Pick how you want to authenticate to AWS:"
echo "  1) AWS SSO / IAM Identity Center   (recommended)"
echo "  2) Named CLI profile               (aws configure --profile)"
echo "  3) IAM user access key             (long-lived, least preferred)"
echo "  4) Assume a role in another account"
echo
read -rp "Choice [1-4, default 1]: " choice
choice="${choice:-1}"

PROFILE=""
ROLE_ARN=""

case "$choice" in
  1)
    step "AWS SSO / IAM Identity Center"
    read -rp "Profile name to configure [cloudlens-sso]: " PROFILE
    PROFILE="${PROFILE:-cloudlens-sso}"
    echo "Launching 'aws configure sso' - follow the browser prompts."
    echo "When asked for the default region, use e.g. ${DEFAULT_REGION}."
    aws configure sso --profile "$PROFILE"
    echo "Signing in..."
    aws sso login --profile "$PROFILE"
    ;;

  2)
    step "Named CLI profile"
    read -rp "Profile name [cloudlens]: " PROFILE
    PROFILE="${PROFILE:-cloudlens}"
    aws configure --profile "$PROFILE"
    ;;

  3)
    step "IAM user access key"
    warn "Long-lived keys are an IAM anti-pattern. Prefer SSO (option 1)."
    echo "Create an access key in the IAM console for a user that has EC2,"
    echo "CloudFormation, and SSM permissions, then paste it here."
    read -rp "Profile name [cloudlens]: " PROFILE
    PROFILE="${PROFILE:-cloudlens}"
    aws configure --profile "$PROFILE"
    ;;

  4)
    step "Assume-role"
    read -rp "Base profile to assume FROM [default]: " BASE_PROFILE
    BASE_PROFILE="${BASE_PROFILE:-default}"
    read -rp "Role ARN to assume (arn:aws:iam::<acct>:role/<name>): " ROLE_ARN
    [[ -z "$ROLE_ARN" ]] && fail "Role ARN required."
    read -rp "New profile name to write [cloudlens-assumed]: " PROFILE
    PROFILE="${PROFILE:-cloudlens-assumed}"
    # Write a profile that source-profiles the base and assumes the role.
    aws configure set role_arn "$ROLE_ARN" --profile "$PROFILE"
    aws configure set source_profile "$BASE_PROFILE" --profile "$PROFILE"
    aws configure set region "$DEFAULT_REGION" --profile "$PROFILE"
    ok "Wrote assume-role profile '$PROFILE' (source_profile=$BASE_PROFILE)"
    ;;

  *) fail "Invalid choice." ;;
esac

# --- Verify the credentials actually work ---
step "Verifying credentials"
if aws sts get-caller-identity --profile "$PROFILE" >/dev/null 2>&1; then
  ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
  ARN=$(aws sts get-caller-identity --profile "$PROFILE" --query Arn --output text)
  ok "Authenticated as $ARN"
  ok "Account: $ACCOUNT"
else
  fail "Could not authenticate with profile '$PROFILE'. Re-run and check the values."
fi

# --- Generate an env-export helper the rest of the tooling can source ---
CREDS_FILE="$REPO_DIR/scripts/load_aws_creds.sh"
cat > "$CREDS_FILE" <<EOF
#!/usr/bin/env bash
# Source this file: source scripts/load_aws_creds.sh
# amazon.aws dynamic inventory, deploy-stack.sh, and quickstart.sh all read
# AWS_PROFILE / AWS_REGION from the environment.
export AWS_PROFILE="$PROFILE"
export AWS_REGION="$DEFAULT_REGION"
export AWS_DEFAULT_REGION="$DEFAULT_REGION"
echo "[ok] AWS profile '$PROFILE' (region $DEFAULT_REGION) loaded"
EOF
chmod 600 "$CREDS_FILE"

echo
echo "================================================================"
ok "AWS credentials configured"
echo "================================================================"
echo
echo "Load them in your shell:"
echo "  source scripts/load_aws_creds.sh"
echo
echo "Or set manually:"
echo "  export AWS_PROFILE=$PROFILE"
echo "  export AWS_REGION=$DEFAULT_REGION"
echo
if [[ "$choice" == "1" ]]; then
  echo "SSO sessions expire. Re-authenticate any time with:"
  echo "  aws sso login --profile $PROFILE"
  echo
fi
