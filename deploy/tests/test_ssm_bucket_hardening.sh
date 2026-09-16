#!/usr/bin/env bash
# deploy/tests/test_ssm_bucket_hardening.sh: the Windows SSM transfer bucket
# requires HTTPS, whether this run created it or adopted one from an earlier
# run, and a policy the script did not write is never overwritten.
#
# Why this exists. Blocking public access does not stop cleartext: a bucket
# with all four blocks on still answers plain HTTP, and a CIS scan reports it
# (S3.5 / CIS 2.1.1). The first version of the fix only ran on the create
# path, so every account that already had the bucket stayed non-compliant.
#
# The overwrite case matters just as much. The bucket name is predictable, so
# an operator may have attached their own policy to it. Merging JSON in bash
# is how a statement gets silently dropped, so the script leaves a foreign
# policy alone and tells the operator what to add.
#
# Hermetic: no AWS account, no network. `aws` is a shell function per case.
#
# Usage: bash deploy/tests/test_ssm_bucket_hardening.sh
#        DEPLOY_STACK_SH=/path/to/deploy-stack.sh bash deploy/tests/test_ssm_bucket_hardening.sh
set -u
cd "$(dirname "$0")/../.."
SCRIPT="${DEPLOY_STACK_SH:-deploy/deploy-stack.sh}"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT

awk '
  /^(ok|warn|fail|step|note)\(\)/ { print; next }
  /^(harden_ssm_bucket)\(\)/ { p=1 }
  p { print }
  p && /^}/ { p=0 }
' "$SCRIPT" > "$S/helpers.sh"

if ! grep -q '^harden_ssm_bucket()' "$S/helpers.sh"; then
  echo "FAIL harden_ssm_bucket is not defined in $SCRIPT"
  echo; echo "0 PASS, 1 FAIL"; exit 1
fi

PASS=0; FAIL=0
ok_()  { echo "PASS $*"; PASS=$((PASS+1)); }
bad_() { echo "FAIL $*"; FAIL=$((FAIL+1)); }

# run CASE_BODY -> writes the calls the function made to $S/calls
run_case() {
  local aws_stub="$1"
  : > "$S/calls"
  /bin/bash -c '
    set -euo pipefail
    C_GREEN= C_YELLOW= C_BLUE= C_RED= C_GREY= C_BOLD= C_RESET=
    LOG_FILE="$1"; CALLS="$2"
    '"$aws_stub"'
    source "$3"
    harden_ssm_bucket cloudlens-ssm-transfer-123456789012
  ' _ "$S/log" "$S/calls" "$S/helpers.sh" > "$S/out" 2>&1
}

NO_POLICY='
  aws() {
    case "$1 $2" in
      "s3api get-bucket-policy") echo "" ; return 0 ;;
      "s3api put-bucket-policy") echo "put-policy" >> "$CALLS"; shift 5; printf "%s\n" "$*" >> "$CALLS"; return 0 ;;
      "s3api put-bucket-versioning") echo "put-versioning" >> "$CALLS"; return 0 ;;
    esac
    echo "other: $*" >> "$CALLS"; return 0
  }
  probe() { "$@"; }
'
ALREADY_HTTPS='
  aws() {
    case "$1 $2" in
      "s3api get-bucket-policy") echo "{\"Statement\":[{\"Sid\":\"DenyInsecureTransport\",\"Condition\":{\"Bool\":{\"aws:SecureTransport\":\"false\"}}}]}"; return 0 ;;
      "s3api put-bucket-policy") echo "put-policy" >> "$CALLS"; return 0 ;;
      "s3api put-bucket-versioning") echo "put-versioning" >> "$CALLS"; return 0 ;;
    esac
    echo "other: $*" >> "$CALLS"; return 0
  }
  probe() { "$@"; }
'
FOREIGN_POLICY='
  aws() {
    case "$1 $2" in
      "s3api get-bucket-policy") echo "{\"Statement\":[{\"Sid\":\"SomeoneElsesRule\",\"Effect\":\"Allow\"}]}"; return 0 ;;
      "s3api put-bucket-policy") echo "put-policy" >> "$CALLS"; return 0 ;;
      "s3api put-bucket-versioning") echo "put-versioning" >> "$CALLS"; return 0 ;;
    esac
    echo "other: $*" >> "$CALLS"; return 0
  }
  probe() { "$@"; }
'

# 1. an adopted bucket with no policy gets the HTTPS deny and versioning
run_case "$NO_POLICY"
if grep -q "put-policy" "$S/calls"; then
  ok_ "1a. a bucket with no policy has the HTTPS deny applied"
else
  bad_ "1a. no bucket policy was written, so the bucket still answers HTTP"
fi
if grep -q "aws:SecureTransport" "$S/calls" && grep -q '"Effect": "Deny"' "$S/calls"; then
  ok_ "1b. the statement written is a Deny on aws:SecureTransport"
else
  bad_ "1b. the statement written is not a SecureTransport deny"
fi
if grep -q "put-versioning" "$S/calls"; then
  ok_ "1c. versioning is enabled on that bucket"
else
  bad_ "1c. versioning was not enabled"
fi

# 2. a bucket that already requires HTTPS is not rewritten
run_case "$ALREADY_HTTPS"
if grep -q "put-policy" "$S/calls"; then
  bad_ "2. a compliant bucket policy was rewritten"
else
  ok_ "2. a bucket already requiring HTTPS is left alone"
fi

# 3. someone else's policy is never overwritten, and the operator is told
run_case "$FOREIGN_POLICY"
if grep -q "put-policy" "$S/calls"; then
  bad_ "3a. a policy the script did not write was overwritten"
else
  ok_ "3a. a policy the script did not write is left untouched"
fi
if grep -qi "does not require HTTPS" "$S/out"; then
  ok_ "3b. the operator is told the bucket still allows HTTP"
else
  bad_ "3b. the operator is not told anything about the foreign policy"
fi

echo
echo "$PASS PASS, $FAIL FAIL"
[ "$FAIL" -eq 0 ]
