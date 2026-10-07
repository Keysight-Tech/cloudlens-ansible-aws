#!/usr/bin/env bash
# deploy/tests/test_discover.sh: discovery of tapped VPCs and collector
# placement, and the deploy-stack.sh hook that consumes it.
#
# Why this exists. Every --source-vpc spec was typed by hand
# (vpc:az:mgmt:ingress:egress), which is the one step that stops a 100-VPC
# estate from being automated. scripts/discover_workloads.py derives the
# specs from the discovery tag. This test pins the rules it applies, with a
# stubbed aws CLI so no call ever leaves the machine:
#   - subnets tagged cloudlens:role=mgmt/ingress/egress in one AZ win;
#   - otherwise the AZ with most tapped instances, three subnets, private first;
#   - fewer than three subnets = INCOMPLETE, reported, never guessed;
#   - non-Nitro instances are counted for the sensor path, not mirrored;
#   - organizations:ListAccounts refused = this account only, and it says so;
#   - the profile it writes holds allowlisted CLOUDLENS_* keys only, 0600;
#   - deploy-stack.sh's run_discovery appends only COMPLETE specs and never
#     duplicates a VPC the operator already named.
#
# Usage: bash deploy/tests/test_discover.sh
set -u
cd "$(dirname "$0")/../.."
SCRIPT="${DEPLOY_STACK_SH:-deploy/deploy-stack.sh}"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT
PASS=0; FAIL=0
pass() { echo "PASS $*"; PASS=$((PASS+1)); }
failt() { echo "FAIL $*"; FAIL=$((FAIL+1)); }

# ---- the stubbed aws CLI: canned answers keyed on the sub-command ----------
mkdir -p "$S/bin"
cat > "$S/bin/aws" <<'STUB'
#!/usr/bin/env bash
# Records every call, answers from the fixture directory named by AWS_STUB_DIR.
echo "$*" >> "$AWS_STUB_DIR/calls.log"
args="$*"
case "$args" in
  *"sts get-caller-identity"*) cat "$AWS_STUB_DIR/identity.json" ;;
  *"organizations list-accounts"*)
      if [ -f "$AWS_STUB_DIR/accounts.json" ]; then cat "$AWS_STUB_DIR/accounts.json"
      else echo "An error occurred (AccessDeniedException) when calling the ListAccounts operation: not authorized" >&2; exit 254; fi ;;
  *"sts assume-role"*"222222222222"*) cat "$AWS_STUB_DIR/assume.json" ;;
  *"sts assume-role"*) echo "An error occurred (AccessDenied) when calling the AssumeRole operation: not authorized" >&2; exit 254 ;;
  *"ec2 describe-instances"*"--region us-east-2"*) echo '{"Reservations": []}' ;;
  *"ec2 describe-instances"*) if [ -n "${AWS_SESSION_TOKEN:-}" ]; then cat "$AWS_STUB_DIR/instances-other.json"; else cat "$AWS_STUB_DIR/instances.json"; fi ;;
  *"ec2 describe-instance-types"*) cat "$AWS_STUB_DIR/types.json" ;;
  *"ec2 describe-vpcs"*) cat "$AWS_STUB_DIR/vpcs.json" ;;
  *"ec2 describe-subnets"*"vpc-tagged"*) cat "$AWS_STUB_DIR/subnets-tagged.json" ;;
  *"ec2 describe-subnets"*"vpc-busy"*) cat "$AWS_STUB_DIR/subnets-busy.json" ;;
  *"ec2 describe-subnets"*"vpc-thin"*) cat "$AWS_STUB_DIR/subnets-thin.json" ;;
  *"ec2 describe-subnets"*) echo '{"Subnets": []}' ;;
  *) echo "unexpected: aws $args" >&2; exit 1 ;;
esac
STUB
chmod +x "$S/bin/aws"
export AWS_STUB_DIR="$S/fx"; mkdir -p "$AWS_STUB_DIR"
cat > "$AWS_STUB_DIR/identity.json" <<'J'
{"Account": "111111111111", "Arn": "arn:aws:iam::111111111111:user/ops"}
J
cat > "$AWS_STUB_DIR/assume.json" <<'J'
{"Credentials": {"AccessKeyId": "ASIAX", "SecretAccessKey": "s", "SessionToken": "t"}}
J
# Three VPCs in this account: one with role-tagged subnets, one busy (rule b),
# one too thin (incomplete). Instance i-xen is an old family: not mirrorable.
cat > "$AWS_STUB_DIR/instances.json" <<'J'
{"Reservations": [{"Instances": [
  {"InstanceId": "i-a1", "VpcId": "vpc-tagged", "InstanceType": "m5.large", "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": [{"Key": "Name", "Value": "api-1"}]},
  {"InstanceId": "i-b1", "VpcId": "vpc-busy", "InstanceType": "m5.large", "Placement": {"AvailabilityZone": "us-east-1b"}},
  {"InstanceId": "i-b2", "VpcId": "vpc-busy", "InstanceType": "c5.large", "Placement": {"AvailabilityZone": "us-east-1b"}},
  {"InstanceId": "i-b3", "VpcId": "vpc-busy", "InstanceType": "m5.large", "Placement": {"AvailabilityZone": "us-east-1a"}},
  {"InstanceId": "i-xen", "VpcId": "vpc-busy", "InstanceType": "m4.large", "Placement": {"AvailabilityZone": "us-east-1b"}},
  {"InstanceId": "i-t1", "VpcId": "vpc-thin", "InstanceType": "m5.large", "Placement": {"AvailabilityZone": "us-east-1c"}}
]}]}
J
cat > "$AWS_STUB_DIR/instances-other.json" <<'J'
{"Reservations": [{"Instances": [
  {"InstanceId": "i-o1", "VpcId": "vpc-tagged", "InstanceType": "m5.large", "Placement": {"AvailabilityZone": "us-east-1a"}}
]}]}
J
cat > "$AWS_STUB_DIR/types.json" <<'J'
{"InstanceTypes": [{"InstanceType": "m5.large", "Hypervisor": "nitro"}, {"InstanceType": "c5.large", "Hypervisor": "nitro"}, {"InstanceType": "m4.large", "Hypervisor": "xen"}]}
J
cat > "$AWS_STUB_DIR/vpcs.json" <<'J'
{"Vpcs": [{"VpcId": "vpc-tagged", "CidrBlock": "10.1.0.0/16", "Tags": [{"Key": "Name", "Value": "techstack-api"}]},
          {"VpcId": "vpc-busy", "CidrBlock": "10.2.0.0/16", "Tags": [{"Key": "Name", "Value": "techstack-2"}]},
          {"VpcId": "vpc-thin", "CidrBlock": "10.3.0.0/16"}]}
J
cat > "$AWS_STUB_DIR/subnets-tagged.json" <<'J'
{"Subnets": [
  {"SubnetId": "subnet-tm", "AvailabilityZone": "us-east-1a", "MapPublicIpOnLaunch": false, "Tags": [{"Key": "cloudlens:role", "Value": "mgmt"}]},
  {"SubnetId": "subnet-ti", "AvailabilityZone": "us-east-1a", "MapPublicIpOnLaunch": false, "Tags": [{"Key": "cloudlens:role", "Value": "ingress"}]},
  {"SubnetId": "subnet-te", "AvailabilityZone": "us-east-1a", "MapPublicIpOnLaunch": false, "Tags": [{"Key": "cloudlens:role", "Value": "egress"}]},
  {"SubnetId": "subnet-tx", "AvailabilityZone": "us-east-1a", "MapPublicIpOnLaunch": true}
]}
J
# busy: 3 instances in 1b, 1 in 1a; 1b has a public subnet and three private ones
cat > "$AWS_STUB_DIR/subnets-busy.json" <<'J'
{"Subnets": [
  {"SubnetId": "subnet-b-pub", "AvailabilityZone": "us-east-1b", "MapPublicIpOnLaunch": true},
  {"SubnetId": "subnet-b-p3", "AvailabilityZone": "us-east-1b", "MapPublicIpOnLaunch": false},
  {"SubnetId": "subnet-b-p1", "AvailabilityZone": "us-east-1b", "MapPublicIpOnLaunch": false},
  {"SubnetId": "subnet-b-p2", "AvailabilityZone": "us-east-1b", "MapPublicIpOnLaunch": false},
  {"SubnetId": "subnet-a-p1", "AvailabilityZone": "us-east-1a", "MapPublicIpOnLaunch": false}
]}
J
cat > "$AWS_STUB_DIR/subnets-thin.json" <<'J'
{"Subnets": [
  {"SubnetId": "subnet-c1", "AvailabilityZone": "us-east-1c", "MapPublicIpOnLaunch": false},
  {"SubnetId": "subnet-c2", "AvailabilityZone": "us-east-1c", "MapPublicIpOnLaunch": false}
]}
J

# ---- 1. this account, one region -------------------------------------------
: > "$AWS_STUB_DIR/calls.log"
( cd "$S" && PATH="$S/bin:$PATH" python3 "$OLDPWD/scripts/discover_workloads.py" --regions us-east-1 --tag cloudlens=yes \
    --out "$S/out/discovered.json" --profile-dir "$S" --print-specs > "$S/specs.txt" 2> "$S/err.txt" ); rc=$?
[[ $rc -eq 0 ]] && pass "1. exit 0 when at least one VPC is ready" || failt "1. exit $rc: $(tail -3 "$S/err.txt")"
grep -q '^vpc-tagged:us-east-1a:subnet-tm:subnet-ti:subnet-te$' "$S/specs.txt" \
  && pass "1. role-tagged subnets win, in their AZ, ignoring the untagged public one" \
  || failt "1. tagged spec wrong: $(cat "$S/specs.txt")"
grep -q '^vpc-busy:us-east-1b:subnet-b-p1:subnet-b-p2:subnet-b-p3$' "$S/specs.txt" \
  && pass "1. busiest AZ chosen (1b over 1a), three private subnets, public one skipped, stable order" \
  || failt "1. busy spec wrong: $(cat "$S/specs.txt")"
! grep -q 'vpc-thin' "$S/specs.txt" && grep -q 'vpc-thin.*INCOMPLETE.*only 2 subnet' "$S/err.txt" \
  && pass "1. two subnets = incomplete: reported with the count, never emitted as a spec" \
  || failt "1. thin VPC handling: specs=[$(cat "$S/specs.txt")] err=[$(grep vpc-thin "$S/err.txt")]"
python3 - "$S/out/discovered.json" <<'PY' && pass "1. non-Nitro instance counted for the sensor path by id, Nitro count right" || failt "1. nitro split wrong"
import json, sys
d = json.load(open(sys.argv[1]))
v = {x["vpc_id"]: x for a in d["accounts"] for r in a["regions"] for x in r["vpcs"]}
b = v["vpc-busy"]
assert b["matches"] == 4 and b["nitro"] == 3 and b["non_nitro"] == 1 and b["non_nitro_ids"] == ["i-xen"], b
assert v["vpc-tagged"]["placement"]["how"].startswith("subnets tagged"), v["vpc-tagged"]
assert d["accounts"][0]["id"] == "111111111111" and d["accounts"][0]["reachable"]
PY
grep -q '^2 VPC(s) ready to tap, 1 need subnets, 1 non-Nitro' "$S/err.txt" \
  && pass "1. summary line counts ready, incomplete and non-Nitro" || failt "1. summary: $(tail -1 "$S/err.txt")"
prof="$S/deploy-profile-discovered-111111111111-us-east-1.env"
if [[ -f "$prof" ]]; then
  bad="$(grep -v '^#' "$prof" | cut -d= -f1 | grep -v -E '^CLOUDLENS_(REGION|SOURCE_VPCS|DISCOVERY_TAG_KEY|DISCOVERY_TAG_VALUE)$' || true)"
  perm="$(stat -f %Lp "$prof" 2>/dev/null || stat -c %a "$prof")"
  [[ -z "$bad" && "$perm" == "600" ]] && grep -q '^CLOUDLENS_SOURCE_VPCS=.*vpc-tagged:us-east-1a:subnet-tm:subnet-ti:subnet-te' "$prof" \
    && grep -q '^CLOUDLENS_SOURCE_VPCS=.*vpc-busy:us-east-1b:subnet-b-p1:subnet-b-p2:subnet-b-p3' "$prof" && ! grep -q 'vpc-thin' "$prof" \
    && pass "1. profile written with allowlisted keys only, complete specs only, mode 600" \
    || failt "1. profile: bad=[$bad] perm=$perm content=[$(cat "$prof")]"
else failt "1. no profile written"; fi
! grep -q 'organizations' "$AWS_STUB_DIR/calls.log" && pass "1. --accounts self never calls Organizations" || failt "1. Organizations called in self mode"
! grep -q -- '--region us-east-2' "$AWS_STUB_DIR/calls.log" && pass "1. only the asked region is scanned" || failt "1. extra region scanned"

# ---- 2. organization mode, ListAccounts refused ------------------------------
: > "$AWS_STUB_DIR/calls.log"
( cd "$S" && PATH="$S/bin:$PATH" python3 "$OLDPWD/scripts/discover_workloads.py" --regions us-east-1 --accounts organization \
    --out "$S/out/d2.json" --profile-dir "$S/p2" --print-specs > "$S/specs2.txt" 2> "$S/err2.txt" ); rc=$?
[[ -f "$S/p2/deploy-profile-discovered-111111111111-us-east-1.env" ]] && pass "2. a profile directory that does not exist yet is created" || failt "2. profile dir not created"
[[ $rc -eq 0 ]] && grep -q 'ListAccounts refused or unavailable: scanning this account only' "$S/err2.txt" \
  && [[ "$(wc -l < "$S/specs2.txt" | tr -d ' ')" == "2" ]] \
  && pass "2. Organizations refused: says so, scans this account, still returns its specs" \
  || failt "2. refused handling: rc=$rc err=[$(grep -i organization "$S/err2.txt")] specs=$(wc -l < "$S/specs2.txt")"

# ---- 3. organization mode, one account assumable, one not ----------------------
cat > "$AWS_STUB_DIR/accounts.json" <<'J'
{"Accounts": [{"Id": "111111111111", "Name": "mgmt", "Status": "ACTIVE"}, {"Id": "222222222222", "Name": "techstack2", "Status": "ACTIVE"},
              {"Id": "333333333333", "Name": "locked", "Status": "ACTIVE"}, {"Id": "444444444444", "Name": "closed", "Status": "SUSPENDED"}]}
J
: > "$AWS_STUB_DIR/calls.log"
( cd "$S" && PATH="$S/bin:$PATH" python3 "$OLDPWD/scripts/discover_workloads.py" --regions us-east-1,us-east-2 --accounts organization \
    --out "$S/out/d3.json" --profile-dir "$S" --print-specs > "$S/specs3.txt" 2> "$S/err3.txt" ); rc=$?
python3 - "$S/out/d3.json" <<'PY' && pass "3. organization: self + assumable account scanned, unassumable reported, suspended skipped, both regions" || failt "3. org result wrong: $(grep -E 'UNREACH|2222|3333' "$S/err3.txt" | head -3)"
import json, sys
d = json.load(open(sys.argv[1]))
ids = [a["id"] for a in d["accounts"]]
assert ids == ["111111111111", "222222222222", "333333333333"], ids
a2 = d["accounts"][1]; a3 = d["accounts"][2]
assert a2["reachable"] and [r["region"] for r in a2["regions"]] == ["us-east-1", "us-east-2"], a2
assert a2["regions"][0]["vpcs"][0]["vpc_id"] == "vpc-tagged" and a2["regions"][0]["vpcs"][0]["matches"] == 1
assert not a3["reachable"] and "assume role OrganizationAccountAccessRole" in a3["reason"], a3
PY
grep -q '^vpc-tagged:us-east-1a:subnet-tm:subnet-ti:subnet-te$' "$S/specs3.txt" \
  && [[ -f "$S/deploy-profile-discovered-222222222222-us-east-1.env" ]] \
  && pass "3. the other account gets its own profile file" || failt "3. per-account profile missing"
grep -q 'assume-role --role-arn arn:aws:iam::222222222222:role/OrganizationAccountAccessRole' "$AWS_STUB_DIR/calls.log" \
  && pass "3. the default role is assumed by ARN in the other account" || failt "3. assume-role call missing"

# ---- 4. nothing tagged anywhere ---------------------------------------------
echo '{"Reservations": []}' > "$AWS_STUB_DIR/instances.json"; rm -f "$AWS_STUB_DIR/accounts.json"
( cd "$S" && PATH="$S/bin:$PATH" python3 "$OLDPWD/scripts/discover_workloads.py" --regions us-east-1 --out "$S/out/d4.json" --profile-dir "$S/p4" --print-specs > "$S/specs4.txt" 2> "$S/err4.txt" ); rc=$?
[[ $rc -eq 3 && ! -s "$S/specs4.txt" ]] && grep -q 'no running instance carries the tag' "$S/err4.txt" \
  && pass "4. nothing tagged: exit 3, empty specs, plain sentence" || failt "4. rc=$rc specs=[$(cat "$S/specs4.txt")]"

# ---- 5. bad input -------------------------------------------------------------
( cd "$S" && PATH="$S/bin:$PATH" python3 "$OLDPWD/scripts/discover_workloads.py" --regions "us-east-1; rm -rf /" --out "$S/out/d5.json" >/dev/null 2>&1 ); rc=$?
[[ $rc -eq 2 ]] && pass "5. a region that is not a region name is refused (exit 2)" || failt "5. rc=$rc"
( cd "$S" && PATH="$S/bin:$PATH" python3 "$OLDPWD/scripts/discover_workloads.py" --regions us-east-1 --tag nokey --out "$S/out/d5.json" >/dev/null 2>&1 ); rc=$?
[[ $rc -eq 2 ]] && pass "5. a tag without = is refused (exit 2)" || failt "5. tag rc=$rc"

# ---- 6. deploy-stack.sh run_discovery: appends complete specs, no duplicates ----
awk '
  /^(ok|warn|fail|note)\(\)/ { print; next }
  /^(discovery_script|run_discovery)\(\)/ { p=1 }
  p { print }
  p && /^}/ { p=0 }
' "$SCRIPT" > "$S/hook.sh"
if grep -q '^run_discovery()' "$S/hook.sh"; then
  cat > "$S/fake_discover.py" <<'PY'
import sys
sys.stderr.write("table\n")
print("vpc-tagged:us-east-1a:subnet-tm:subnet-ti:subnet-te")
print("vpc-busy:us-east-1b:subnet-b-p1:subnet-b-p2:subnet-b-p3")
PY
  out="$(cd "$S" && /bin/bash -c '
    set -u
    C_GREEN= C_YELLOW= C_BLUE= C_RED= C_GREY= C_BOLD= C_RESET=
    source "$1"
    REPO_DIR="$2"; REGION=us-east-1; DISCOVER_REGIONS=""; DISCOVER_ACCOUNTS=self; DISCOVER_ROLE=r
    DISCOVERY_TAG_KEY=cloudlens; DISCOVERY_TAG_VALUE=yes
    SOURCE_VPC_SPECS=("vpc-busy")            # the operator already named this one, by id
    FAKE="$3"; discovery_script() { echo "$FAKE"; }
    run_discovery; rc=$?
    echo "RC=$rc SPECS=${SOURCE_VPC_SPECS[*]}"' _ "$S/hook.sh" "$S" "$S/fake_discover.py" 2>&1)"
  if grep -q 'RC=0 SPECS=vpc-busy vpc-tagged:us-east-1a:subnet-tm:subnet-ti:subnet-te$' <<<"$out" \
     && grep -q 'Discovery: 1 VPC(s) added' <<<"$out"; then
    pass "6. run_discovery appends the new complete spec and skips the VPC the operator already named"
  else failt "6. run_discovery: [$out]"; fi
  cat > "$S/fake_none.py" <<'PY'
import sys
sys.exit(3)
PY
  out="$(cd "$S" && /bin/bash -c '
    set -u
    C_GREEN= C_YELLOW= C_BLUE= C_RED= C_GREY= C_BOLD= C_RESET=
    source "$1"
    REPO_DIR="$2"; REGION=us-east-1; DISCOVER_REGIONS=""; DISCOVER_ACCOUNTS=self; DISCOVER_ROLE=r
    DISCOVERY_TAG_KEY=cloudlens; DISCOVERY_TAG_VALUE=yes
    SOURCE_VPC_SPECS=()
    FAKE="$3"; discovery_script() { echo "$FAKE"; }
    run_discovery; rc=$?
    echo "RC=$rc N=${#SOURCE_VPC_SPECS[@]}"' _ "$S/hook.sh" "$S" "$S/fake_none.py" 2>&1)"
  grep -q 'RC=3 N=0' <<<"$out" && grep -q 'found nothing tappable' <<<"$out" \
    && pass "6. nothing found: returns 3, adds nothing, warns in plain words" || failt "6. none case: [$out]"
else
  failt "6. deploy-stack.sh has no run_discovery()"
fi
grep -q -- '--discover) DISCOVER=true' "$SCRIPT" && grep -q 'CLOUDLENS_DISCOVER|CLOUDLENS_DISCOVER_REGIONS|CLOUDLENS_DISCOVER_ACCOUNTS' "$SCRIPT" \
  && pass "6. --discover flag parsed and the profile allowlist knows the three keys" || failt "6. flag or allowlist missing"

echo; echo "$PASS PASS, $FAIL FAIL"
[[ $FAIL -eq 0 ]]
