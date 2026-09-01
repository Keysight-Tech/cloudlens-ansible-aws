#!/usr/bin/env bash
#
# Verify every "Launch Stack" button on the docs site actually works.
#
# Why this exists:
#   On 2026-09-01 an SE clicked Launch Stack and got "The specified bucket does
#   not exist". The bucket keysight-cloudlens-templates had been deleted, so all
#   four buttons had been dead for an unknown length of time. Nothing noticed,
#   because the only automation pointed at S3 was the SYNC workflow, and that
#   workflow (a) fires only when a template file changes, and (b) skips silently
#   and exits green when the AWS_ROLE_ARN secret is unset.
#
#   So the failure mode was: no one changes a template for weeks, the bucket
#   disappears, nothing runs, nothing checks, and the first detection is a
#   customer-facing error.
#
# The rule this encodes:
#   The buttons are the product's front door. Their health must be checked on a
#   schedule, by something that CANNOT silently skip. This script therefore uses
#   NO AWS credentials at all: it fetches the same public URLs a customer's
#   browser fetches. Nothing to configure, nothing to expire, nothing to skip.
#
# What it checks, per template:
#   1. The public HTTPS URL returns 200 (catches a missing bucket or object,
#      and a bucket policy that stopped granting public read).
#   2. The bytes served match the git-tracked YAML (catches drift, i.e. the
#      button deploying a stale template).
#
# Exit codes:
#   0  every button is live and in sync
#   1  at least one is broken; the output names which and why
#
# Usage:
#   bash deploy/scripts/check-launch-buttons.sh
#   BUCKET=other-bucket bash deploy/scripts/check-launch-buttons.sh
#
set -uo pipefail

BUCKET="${BUCKET:-keysight-cloudlens-templates}"
REGION="${REGION:-us-east-1}"
PREFIX="${PREFIX:-aws}"
TEMPLATE_DIR="${TEMPLATE_DIR:-deploy/cloudformation}"

C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[0;34m'; C_RESET='\033[0m'
ok()   { echo -e "${C_GREEN}\xE2\x9C\x93${C_RESET} $1"; }
bad()  { echo -e "${C_RED}\xE2\x9C\x97${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}\xE2\x9A\xA0${C_RESET} $1"; }
step() { echo; echo -e "${C_BLUE}--- $1 ---${C_RESET}"; }

# Resolve the repo root so the script works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d "$TEMPLATE_DIR" ]]; then
  bad "No $TEMPLATE_DIR under $REPO_ROOT. Run this from a checkout."
  exit 1
fi

# Deliberately unset any AWS credentials that happen to be in the environment.
# A check that passes only because the operator is authenticated would not have
# caught the outage this script exists to prevent: the buttons are used by
# people with no access to our account.
unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN 2>/dev/null || true

step "Checking Launch Stack templates in s3://${BUCKET}/${PREFIX}/"
echo "  (anonymous fetch, exactly what a customer's browser does)"
echo

FAILED=0
CHECKED=0

for path in "$TEMPLATE_DIR"/*.yaml; do
  [[ -e "$path" ]] || continue
  name="$(basename "$path")"
  url="https://${BUCKET}.s3.${REGION}.amazonaws.com/${PREFIX}/${name}"
  CHECKED=$(( CHECKED + 1 ))

  body="$(curl -sS --max-time 30 -w '\n%{http_code}' "$url" 2>/dev/null)"
  code="$(printf '%s' "$body" | tail -n1)"
  payload="$(printf '%s' "$body" | sed '$d')"

  if [[ "$code" != "200" ]]; then
    bad "${name}  HTTP ${code:-no response}"
    case "$code" in
      404) echo "      The bucket or the object is gone. Recreate and upload with:"
           echo "      bash deploy/scripts/sync-cfn-templates-to-s3.sh --bootstrap" ;;
      403) echo "      Served but not public. The bucket policy no longer grants"
           echo "      s3:GetObject on ${PREFIX}/*. Repair with:"
           echo "      bash deploy/scripts/sync-cfn-templates-to-s3.sh --bootstrap" ;;
      *)   echo "      Unexpected response. Check the bucket and region." ;;
    esac
    echo "      URL: $url"
    FAILED=$(( FAILED + 1 ))
    continue
  fi

  live_hash="$(printf '%s' "$payload" | shasum -a 256 | awk '{print $1}')"
  # Strip the trailing newline the same way, so the comparison is like for like.
  repo_hash="$(printf '%s' "$(cat "$path")" | shasum -a 256 | awk '{print $1}')"

  if [[ "$live_hash" != "$repo_hash" ]]; then
    bad "${name}  HTTP 200 but the served template does NOT match git"
    echo "      The button would deploy a stale template. Re-sync with:"
    echo "      bash deploy/scripts/sync-cfn-templates-to-s3.sh"
    echo "      URL: $url"
    FAILED=$(( FAILED + 1 ))
    continue
  fi

  ok "${name}  HTTP 200, matches git"
done

step "Result"
if (( CHECKED == 0 )); then
  bad "No templates found in $TEMPLATE_DIR. Nothing was verified."
  exit 1
fi
if (( FAILED > 0 )); then
  bad "${FAILED} of ${CHECKED} Launch buttons are broken."
  echo
  echo "  Every SE and customer clicking those buttons hits this right now."
  echo "  Fix, then re-run this check."
  exit 1
fi
ok "All ${CHECKED} Launch buttons are live and in sync with git."
