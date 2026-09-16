#!/usr/bin/env bash
#
# sync-cfn-templates-to-s3.sh hardens the public template bucket. These cases
# pin the two properties a compliance scan checks, using a stub aws on PATH so
# nothing touches a real account:
#
#   1. a bucket whose policy has no HTTPS-only deny gets one, and the
#      public-read grant the Launch buttons depend on survives
#   2. a bucket that already has the deny is left alone
#   3. versioning is turned on when it is off, and not re-applied when it is on
#
# Run: bash deploy/tests/test_sync_bucket_policy.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${SYNC_SH:-$HERE/../scripts/sync-cfn-templates-to-s3.sh}"
PASS=0; FAIL=0
ok()   { echo "PASS $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL $*"; FAIL=$((FAIL+1)); }

# One run of the script against a stub account. $1 = policy JSON the stub
# returns, $2 = versioning status. Echoes the calls the script made.
run_sync() {
  local policy="$1" versioning="$2"
  local box; box="$(mktemp -d)"
  mkdir -p "$box/bin"
  cat > "$box/bin/aws" <<STUB
#!/usr/bin/env bash
# Records every mutating call; answers the read-only ones from fixtures.
log="\$CALLS"
case "\$1 \$2" in
  "s3api head-bucket")        exit 0 ;;
  "s3api get-bucket-policy")  printf '%s' '$policy'; exit 0 ;;
  "s3api get-bucket-versioning") printf '%s' '$versioning'; exit 0 ;;
  "s3api put-bucket-policy")  echo "put-bucket-policy" >> "\$log"
                              # keep the document so the test can inspect it
                              for a in "\$@"; do printf '%s\n' "\$a"; done > "\$POLICY_SENT"
                              exit 0 ;;
  "s3api put-bucket-versioning") echo "put-bucket-versioning" >> "\$log"; exit 0 ;;
  "s3 cp"|"s3api put-object") echo "upload" >> "\$log"; exit 0 ;;
  "sts get-caller-identity")  echo "466778915280"; exit 0 ;;
esac
echo "other: \$*" >> "\$log"
exit 0
STUB
  chmod +x "$box/bin/aws"
  export CALLS="$box/calls" POLICY_SENT="$box/policy-sent"
  : > "$CALLS"; : > "$POLICY_SENT"
  PATH="$box/bin:$PATH" bash "$SCRIPT" >"$box/out" 2>&1
  BOX="$box"
}

POL_NO_DENY='{"Version":"2012-10-17","Statement":[{"Sid":"PublicReadTemplates","Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::keysight-cloudlens-templates/aws/*"}]}'
POL_WITH_DENY='{"Version":"2012-10-17","Statement":[{"Sid":"PublicReadTemplates","Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::keysight-cloudlens-templates/aws/*"},{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::keysight-cloudlens-templates","arn:aws:s3:::keysight-cloudlens-templates/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}'

# 1. no deny in the policy: the script adds one
run_sync "$POL_NO_DENY" "None"
if grep -q "put-bucket-policy" "$CALLS"; then
  ok "1a. a bucket serving plain HTTP has the deny applied"
else
  bad "1a. the HTTPS-only deny was never applied"
fi
if grep -q "DenyInsecureTransport" "$POLICY_SENT" && grep -q "aws:SecureTransport" "$POLICY_SENT"; then
  ok "1b. the applied policy denies aws:SecureTransport false"
else
  bad "1b. the applied policy has no SecureTransport deny"
fi
if grep -q "PublicReadTemplates" "$POLICY_SENT" && grep -q "keysight-cloudlens-templates/aws/\*" "$POLICY_SENT"; then
  ok "1c. public read on /aws/* survives the repair, so the Launch buttons keep working"
else
  bad "1c. the repair dropped the public-read grant the Launch buttons need"
fi

# 2. deny already present: nothing is rewritten
run_sync "$POL_WITH_DENY" "Enabled"
if grep -q "put-bucket-policy" "$CALLS"; then
  bad "2. an already-compliant policy was rewritten"
else
  ok "2. an already-compliant policy is left alone"
fi
if grep -q "put-bucket-versioning" "$CALLS"; then
  bad "3a. versioning was re-applied when already Enabled"
else
  ok "3a. versioning already Enabled is left alone"
fi

# 3. versioning off: the script turns it on
run_sync "$POL_WITH_DENY" "None"
if grep -q "put-bucket-versioning" "$CALLS"; then
  ok "3b. versioning off is turned on"
else
  bad "3b. versioning stayed off"
fi

echo
echo "$PASS PASS, $FAIL FAIL"
[[ "$FAIL" -eq 0 ]]
