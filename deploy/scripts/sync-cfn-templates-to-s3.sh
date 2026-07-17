#!/usr/bin/env bash
#
# Sync deploy/cloudformation/*.yaml to the public S3 hosting bucket.
#
# Why this exists:
#   AWS CloudFormation Quick-Create URLs (?templateURL=...) only accept S3
#   URLs. GitHub raw URLs are silently rejected with:
#     "TemplateURL must be a supported URL."
#   So every Launch button on the docs site points at S3, not GitHub, and
#   this script keeps S3 aligned with the git-tracked YAML.
#
# When to run:
#   After ANY change to deploy/cloudformation/*.yaml on main. If the S3 copy
#   drifts, the Launch buttons deploy stale templates.
#
# Prerequisites:
#   - AWS creds for the account that owns the bucket (default: 466778915280)
#   - Bucket already exists with a public-read policy on /aws/*
#
# Usage:
#   AWS_PROFILE=AdministratorAccess-466778915280 \
#     bash deploy/scripts/sync-cfn-templates-to-s3.sh
#
#   # or override bucket / region:
#   BUCKET=other-bucket REGION=us-west-2 \
#     bash deploy/scripts/sync-cfn-templates-to-s3.sh
#
set -euo pipefail

BUCKET="${BUCKET:-keysight-cloudlens-templates}"
REGION="${REGION:-us-east-1}"
PREFIX="${PREFIX:-aws}"

C_GREEN='\033[0;32m'; C_BLUE='\033[0;34m'; C_RED='\033[0;31m'; C_YELLOW='\033[1;33m'; C_RESET='\033[0m'
ok()   { echo -e "${C_GREEN}\xE2\x9C\x93${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}\xE2\x9A\xA0${C_RESET} $1"; }
fail() { echo -e "${C_RED}\xE2\x9C\x97${C_RESET} $1" >&2; exit 1; }
step() { echo; echo -e "${C_BLUE}--- $1 ---${C_RESET}"; }

command -v aws >/dev/null 2>&1 || fail "AWS CLI not found."
aws sts get-caller-identity >/dev/null 2>&1 || fail "AWS auth missing. Run: aws sso login --profile <profile>"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CFN_DIR="$REPO_ROOT/deploy/cloudformation"
[[ -d "$CFN_DIR" ]] || fail "Not found: $CFN_DIR"

TEMPLATES=(stack.yaml clms.yaml kvo.yaml vpb.yaml)

step "Pre-flight"
CALLER=$(aws sts get-caller-identity --query Arn --output text)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "  Caller:  $CALLER"
echo "  Account: $ACCOUNT"
echo "  Bucket:  s3://$BUCKET/$PREFIX/  (region $REGION)"

step "Validating each template with cfn-lint (if installed)"
# --non-zero-exit-code error: only fail on real errors, not warnings.
# Ref-type warnings on Existing* params are expected (values come from user at deploy time).
if command -v cfn-lint >/dev/null 2>&1; then
  for f in "${TEMPLATES[@]}"; do
    if cfn-lint --non-zero-exit-code error "$CFN_DIR/$f" >/dev/null 2>&1; then
      ok "$f lint clean (warnings allowed)"
    else
      cfn-lint "$CFN_DIR/$f" || true
      fail "$f cfn-lint reported errors - fix before uploading"
    fi
  done
else
  warn "cfn-lint not installed - skipping (pip install cfn-lint)"
fi

step "Verifying bucket exists + public-read on /$PREFIX/*"
aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 \
  || fail "Bucket $BUCKET missing. Run the one-time bootstrap first (see docs/OPERATIONS.md)."
POLICY=$(aws s3api get-bucket-policy --bucket "$BUCKET" --query Policy --output text 2>/dev/null || echo "")
if echo "$POLICY" | grep -q "arn:aws:s3:::$BUCKET/$PREFIX/\*"; then
  ok "Public-read policy on /$PREFIX/* is in place"
else
  fail "Bucket policy does NOT allow public-read on /$PREFIX/*. Fix before syncing."
fi

step "Uploading"
for f in "${TEMPLATES[@]}"; do
  aws s3 cp "$CFN_DIR/$f" "s3://$BUCKET/$PREFIX/$f" \
    --content-type "text/yaml" \
    --cache-control "public, max-age=300" \
    --metadata "source=github.com/Keysight-Tech/cloudlens-ansible-aws,commit=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  ok "$f uploaded"
done

step "Verifying public HTTPS reads"
FAILED=0
for f in "${TEMPLATES[@]}"; do
  URL="https://${BUCKET}.s3.${REGION}.amazonaws.com/$PREFIX/$f"
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL")
  BYTES=$(curl -sSL "$URL" | wc -c | tr -d ' ')
  if [[ "$CODE" == "200" && "$BYTES" -gt 100 ]]; then
    ok "$f  [$CODE, $BYTES bytes]  $URL"
  else
    warn "$f  [$CODE, $BYTES bytes]  $URL"
    FAILED=$((FAILED+1))
  fi
done
[[ "$FAILED" -eq 0 ]] || fail "$FAILED template(s) failed public read - do not ship."

step "Testing CloudFormation acceptance (validate-template)"
for f in "${TEMPLATES[@]}"; do
  URL="https://${BUCKET}.s3.${REGION}.amazonaws.com/$PREFIX/$f"
  if aws cloudformation validate-template --template-url "$URL" >/dev/null 2>&1; then
    ok "$f  CloudFormation validated OK"
  else
    ERR=$(aws cloudformation validate-template --template-url "$URL" 2>&1 | tail -1)
    fail "$f  CloudFormation rejected: $ERR"
  fi
done

step "Done"
echo "  All 4 templates live and CFN-validated. Launch buttons will work."
echo "  Docs site auto-refreshes within ~60s once you push the code changes."
