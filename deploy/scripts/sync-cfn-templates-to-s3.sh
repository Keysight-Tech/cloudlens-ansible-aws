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
BOOTSTRAP=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bootstrap) BOOTSTRAP=true; shift ;;
    -h|--help) sed -n '1,/^set -e/p' "$0" | head -n -1 | tail -n +2; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

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

# The bucket holds only the CloudFormation templates that are already public
# on GitHub, and it exists solely because Quick-Create URLs reject non-S3
# URLs. It is recreatable, so --bootstrap builds it rather than pointing the
# operator at a procedure to run by hand. Everything except /$PREFIX/* stays
# private: the public grant is scoped to that prefix and nothing else.
bootstrap_bucket() {
  step "Bootstrapping bucket $BUCKET in $REGION"
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
  # Public-read on the template prefix needs the account-level block relaxed
  # for policies. ACLs stay blocked: the grant comes from the policy alone.
  aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false" >/dev/null
  aws s3api put-bucket-policy --bucket "$BUCKET" --policy "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Sid\": \"PublicReadTemplates\",
      \"Effect\": \"Allow\",
      \"Principal\": \"*\",
      \"Action\": \"s3:GetObject\",
      \"Resource\": \"arn:aws:s3:::$BUCKET/$PREFIX/*\"
    }]
  }" >/dev/null
  ok "Bucket created with public-read scoped to /$PREFIX/* only"
}

if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  if [[ "$BOOTSTRAP" == "true" ]]; then
    bootstrap_bucket
  else
    echo
    warn "Bucket $BUCKET does not exist in this account."
    echo "    It holds only the CloudFormation templates the Launch buttons point at,"
    echo "    and it is safe to recreate. Create it with:"
    echo "      bash $0 --bootstrap"
    fail "Nothing uploaded."
  fi
fi

POLICY=$(aws s3api get-bucket-policy --bucket "$BUCKET" --query Policy --output text 2>/dev/null || echo "")
if echo "$POLICY" | grep -q "arn:aws:s3:::$BUCKET/$PREFIX/\*"; then
  ok "Public-read policy on /$PREFIX/* is in place"
elif [[ "$BOOTSTRAP" == "true" ]]; then
  bootstrap_bucket
else
  warn "Bucket policy does NOT allow public-read on /$PREFIX/*."
  echo "    Repair it with:  bash $0 --bootstrap"
  fail "Nothing uploaded: the Launch buttons would 403."
fi

step "Uploading"
for f in "${TEMPLATES[@]}"; do
  aws s3 cp "$CFN_DIR/$f" "s3://$BUCKET/$PREFIX/$f" \
    --content-type "text/yaml" \
    --cache-control "public, max-age=300" \
    --metadata "source=github.com/Keysight-Tech/cloudlens-ansible-aws,commit=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  ok "$f uploaded"
done

step "Verifying public HTTPS reads match the local files"
# Compare the served object to the local file by content hash. The old check
# was "HTTP 200 and more than 100 bytes", which a truncated or stale object
# passes: a run of this script reported "200, 243 bytes" for a 41 KB template
# and still printed a tick, then declared it CloudFormation-validated. A
# partial template validates fine and deploys almost nothing, so size alone
# can never be the test. A short retry covers read-after-write settling.
FAILED=0
for f in "${TEMPLATES[@]}"; do
  URL="https://${BUCKET}.s3.${REGION}.amazonaws.com/$PREFIX/$f"
  WANT=$(shasum -a 256 "$CFN_DIR/$f" | cut -d' ' -f1)
  CODE=""; GOT=""; BYTES=0
  for attempt in 1 2 3 4 5; do
    TMPF=$(mktemp)
    CODE=$(curl -sSL -o "$TMPF" -w "%{http_code}" "$URL" || echo "000")
    # Hash the downloaded FILE, never a shell variable: command substitution
    # strips trailing newlines, so $(curl ...) loses the file's final byte and
    # the hash can never match. That cost one debugging round here.
    GOT=$(shasum -a 256 "$TMPF" | cut -d' ' -f1)
    BYTES=$(wc -c < "$TMPF" | tr -d ' ')
    rm -f "$TMPF"
    [[ "$CODE" == "200" && "$GOT" == "$WANT" ]] && break
    sleep 2
  done
  if [[ "$CODE" == "200" && "$GOT" == "$WANT" ]]; then
    ok "$f  [$CODE, $BYTES bytes, hash matches]  $URL"
  elif [[ "$CODE" == "200" ]]; then
    warn "$f  [$CODE, $BYTES bytes] SERVED COPY DOES NOT MATCH THE LOCAL FILE"
    echo "    local $WANT"
    echo "    s3    $GOT"
    FAILED=$((FAILED+1))
  else
    warn "$f  [$CODE] not readable  $URL"
    FAILED=$((FAILED+1))
  fi
done
[[ "$FAILED" -eq 0 ]] || fail "$FAILED template(s) do not match what is on disk - do not ship."

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
