#!/usr/bin/env bash
# deploy/tests/test_teardown_mirror_sessions.sh: the teardown removes the
# traffic mirror sessions KVO cut to this stack's collectors, BEFORE the
# targets and the filter, and never anybody else's.
#
# Why this exists. The teardown of a proof-of-concept stack on 2026-10-16, in a
# customer (brownfield) VPC, ended with:
#   tmt-...: TrafficMirrorTargetInUse ... Target is in use by tms-...
#   tmf-...: TrafficMirrorFilterInUse ... Filter is in use by tms-...
# The sweep deleted targets and the filter on the belief that the sessions
# "go with the collectors". They do not: KVO cuts one session per tapped
# WORKLOAD ENI (source = the workload's ENI, target = the collector's target,
# filter = KVO's filter), and the KVO that would remove them is deleted with
# the stack. A session left behind is harmful, not cosmetic: AWS mirrors each
# packet on an ENI to the first matching session only, so a stale session to
# a dead target swallows the copies a redeployed KVO's new sessions should get.
#
# The rule under test: a session is this stack's iff it sends to a target the
# teardown attributed to this stack's collectors (a target on one of their
# ENIs), checked against AWS at the moment of the delete. Its filter is not
# evidence: KVO names it cloudlens.mirror_filter.<vpc>, per VPC and not per
# stack, so a second deploy tapping the same VPC has a filter of that name
# too, or reuses ours, and its sessions must survive our teardown. Neither is
# a session's location (the same VPC, even the same workload ENI), its tags,
# or a manifest entry from an earlier run: a session can be repointed since.
# A filter still used by a session that is not ours is left in place, which
# is exactly what AWS's ...InUse refusal enforces anyway. Customer sessions
# to a GWLB endpoint or an NLB, with their own filter, are never touched.
#
# Hermetic: no AWS account, no network. `aws` is a script on PATH that plays
# a brownfield stack (no AWS::EC2::VPC resource, no KVO licence gate) whose
# vController's collector ASG owns one collector, one mirror target and
# KVO's filter, and a sessions table it keeps in a file: it honours the
# target-id and filter-id filters and the session ids of
# describe-traffic-mirror-sessions, and REFUSES delete-traffic-mirror-target
# / -filter with AWS's own ...InUse error while any session still references
# them, exactly as AWS did live. The table changes while the teardown runs,
# as it can live: when the stack is deleted KVO has just cut one more session
# to our target (tms-0late), and optionally (STUB_REPOINT=1) one of ours was
# repointed at somebody else's target. Every call is recorded, so the ORDER
# of the deletes can be asserted.
#
# The script under test is copied into a temp tree before it runs, so the
# real state files next to the repo (a live deploy may be using them) are
# never read or written.
#
# Usage: bash deploy/tests/test_teardown_mirror_sessions.sh
#        TEARDOWN_STACK_SH=/path/to/teardown-stack.sh bash deploy/tests/test_teardown_mirror_sessions.sh
# The second form points the suite at another copy of the script. That is how
# the fix is proven: the suite has to FAIL against the version before it.
set -u
cd "$(dirname "$0")/../.."
SRC="$(cd "$(dirname "${TEARDOWN_STACK_SH:-deploy/teardown-stack.sh}")" && pwd)/$(basename "${TEARDOWN_STACK_SH:-deploy/teardown-stack.sh}")"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT
mkdir -p "$S/bin" "$S/td/deploy"
cp "$SRC" "$S/td/deploy/teardown-stack.sh"
SCRIPT="$S/td/deploy/teardown-stack.sh"
STACK=tmstest

# ---- the aws stub -------------------------------------------------------
# $AWS_STATE/sessions: one session per line, "id eni target filter nametag".
cat > "$S/bin/aws" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$AWS_CALLS"
ST="$AWS_STATE"
val() { local want="$1"; shift; while [[ $# -gt 0 ]]; do [[ "$1" == "$want" ]] && { printf '%s' "${2:-}"; return 0; }; shift; done; return 1; }
in_use_by() { awk -v f="$1" -v id="$2" '$f == id { print $1; exit }' "$ST/sessions"; }
case "$*" in
  *"sts get-caller-identity"*"Arn"*)               echo "arn:aws:iam::123456789012:user/test" ;;
  *"sts get-caller-identity"*"Account"*)           echo "123456789012" ;;
  *"cloudformation delete-stack"*)
    : > "$ST/deleted"
    # KVO is alive until now: it cut a session for a newly tagged workload
    # after Phase 2 had already looked.
    echo "tms-0late eni-0web8 tmt-0ours tmf-0ours cloudlens.mirror_session.vpc-0cust" >> "$ST/sessions"
    # ModifyTrafficMirrorSession: tms-0w2 now sends to somebody else's target.
    if [[ "${STUB_REPOINT:-}" == "1" ]]; then
      awk '$1 == "tms-0w2" { $3 = "tmt-0nlb" } { print }' "$ST/sessions" > "$ST/sessions.tmp"
      mv "$ST/sessions.tmp" "$ST/sessions"
    fi ;;
  *"describe-stacks"*"StackStatus"*)
    if [[ "${STUB_STACK:-}" == "missing" ]]; then
      echo "" >&2
      echo "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id tmstest does not exist" >&2
      exit 254
    fi
    if [[ -f "$ST/deleted" ]]; then echo DELETE_COMPLETE; else echo CREATE_COMPLETE; fi ;;
  *"describe-stacks"*"DeployKVO"*)                 echo no ;;
  *"describe-stack-resources"*"AWS::EC2::Instance"*)      echo "i-0vc" ;;
  *"describe-stack-resources"*"AWS::EC2::SecurityGroup"*) echo "sg-0stack" ;;
  *"describe-stack-resources"*"AWS::EC2::VPC"*)           echo "" ;;
  *"describe-stack-resources"*"VcontrollerInstance"*)     echo "i-0vc" ;;
  *"describe-instances --instance-ids i-0vc "*"VpcId"*)            echo "vpc-0cust" ;;
  *"describe-instances --instance-ids i-0vc "*"PrivateIpAddress"*) echo "10.0.0.5" ;;
  *"describe-instances --instance-ids i-0vc "*"BlockDeviceMappings"*) echo "vol-0vc" ;;
  *"describe-instances --instance-ids i-0coll1 "*"BlockDeviceMappings"*) echo "vol-0coll" ;;
  *"describe-instances --instance-ids i-0coll1 "*"NetworkInterfaces"*)   echo "eni-0coll1" ;;
  *"describe-volumes --volume-ids"*)               printf 'vol-0vc\t30\tgp3\tavailable\nvol-0coll\t8\tgp3\tavailable\n' ;;
  *"describe-security-groups"*"group-name"*)       printf 'sg-0coll\tcloudlens-collector-tmstest\n' ;;
  *"describe-auto-scaling-groups"*"length(AutoScalingGroups)"*) echo 0 ;;
  *"describe-auto-scaling-groups"*"cloudlens:ip"*"10.0.0.5"*)
    printf 'cloudlens.collector.vpc-0cust.us-east-1a\tlt-0coll\tNone\ti-0coll1\n' ;;
  *"describe-traffic-mirror-targets"*)
    # ours on the collector's ENI; a second deploy's on ITS collector's ENI;
    # the customer's GWLB endpoint and NLB targets have none
    printf 'tmt-0ours\teni-0coll1\ntmt-0other\teni-0collB\ntmt-0gwlb\tNone\ntmt-0nlb\tNone\n' ;;
  *"describe-traffic-mirror-filters"*"cloudlens.mirror_filter.vpc-0cust"*)
    # The Name tag is per VPC: a second KVO tapping it has a filter of the same name.
    if [[ "${STUB_TWO_FILTERS:-}" == "1" ]]; then printf 'tmf-0ours\ttmf-0other\n'; else echo "tmf-0ours"; fi ;;
  *"describe-traffic-mirror-sessions"*)
    tv=""; fv=""; nv=""; infl=0
    ids="$(val --traffic-mirror-session-ids "$@" || true)"
    if [[ -n "$ids" ]]; then
      # By id, as AWS does: an id that does not exist fails the whole call.
      for i in $ids; do
        grep -q "^$i " "$ST/sessions" && continue
        echo "" >&2
        echo "An error occurred (InvalidTrafficMirrorSessionId.NotFound) when calling the DescribeTrafficMirrorSessions operation: The traffic mirror session ID '$i' does not exist." >&2
        exit 254
      done
      for i in $ids; do awk -v id="$i" -v OFS='\t' '$1 == id { print $1, $2, $3, $4 }' "$ST/sessions"; done
      exit 0
    fi
    for a in "$@"; do
      if [[ "$a" == "--filters" ]]; then infl=1; continue; fi
      [[ "$a" == --* ]] && infl=0
      if (( infl )); then
        case "$a" in
          Name=traffic-mirror-target-id,Values=*) tv="${a#*,Values=}" ;;
          Name=traffic-mirror-filter-id,Values=*) fv="${a#*,Values=}" ;;
          Name=tag:Name,Values=*)                 nv="${a#*,Values=}" ;;
          *) echo "stub: unsupported session filter $a" >&2; exit 255 ;;
        esac
      fi
    done
    # AWS semantics: values of one filter are ORed, different filters ANDed.
    awk -v tv=",$tv," -v fv=",$fv," -v nv=",$nv," -v ht="${tv:+1}" -v hf="${fv:+1}" -v hn="${nv:+1}" -v OFS='\t' '
      (ht == "" || index(tv, "," $3 ",")) && (hf == "" || index(fv, "," $4 ",")) &&
      (hn == "" || index(nv, "," $5 ",")) { print $1, $2, $3, $4 }' "$ST/sessions" ;;
  *"delete-traffic-mirror-session "*)
    id="$(val --traffic-mirror-session-id "$@")"
    if [[ -n "$id" && "$id" == "${STUB_DENY_SESSION:-}" ]]; then
      echo "" >&2
      echo "An error occurred (UnauthorizedOperation) when calling the DeleteTrafficMirrorSession operation: You are not authorized to perform this operation." >&2
      exit 254
    fi
    if [[ -n "$id" ]] && grep -q "^$id " "$ST/sessions"; then
      grep -v "^$id " "$ST/sessions" > "$ST/sessions.tmp"; mv "$ST/sessions.tmp" "$ST/sessions"
      echo "$id" >> "$ST/deleted-sessions"
      printf '{ "TrafficMirrorSessionId": "%s" }\n' "$id"
    else
      echo "" >&2
      echo "An error occurred (InvalidTrafficMirrorSessionId.NotFound) when calling the DeleteTrafficMirrorSession operation: The traffic mirror session ID '$id' does not exist." >&2
      exit 254
    fi ;;
  *"delete-traffic-mirror-target "*)
    id="$(val --traffic-mirror-target-id "$@")"; by="$(in_use_by 3 "$id")"
    if [[ -n "$by" ]]; then
      echo "" >&2
      echo "An error occurred (TrafficMirrorTargetInUse) when calling the DeleteTrafficMirrorTarget operation: Cannot delete $id. Target is in use by $by" >&2
      exit 254
    fi
    echo "$id" >> "$ST/deleted-targets"; printf '{ "TrafficMirrorTargetId": "%s" }\n' "$id" ;;
  *"delete-traffic-mirror-filter "*)
    id="$(val --traffic-mirror-filter-id "$@")"; by="$(in_use_by 4 "$id")"
    if [[ -n "$by" ]]; then
      echo "" >&2
      echo "An error occurred (TrafficMirrorFilterInUse) when calling the DeleteTrafficMirrorFilter operation: Cannot delete $id. Filter is in use by $by" >&2
      exit 254
    fi
    echo "$id" >> "$ST/deleted-filters"; printf '{ "Return": true }\n' ;;
  *)                                               echo "" ;;
esac
exit 0
EOF
chmod +x "$S/bin/aws"

# ---- run the script with no controlling terminal --------------------------
cat > "$S/detach.py" <<'EOF'
import os, subprocess, sys
os.setsid()
sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))
EOF

rc=0
pass() { printf 'PASS %s\n' "$1"; }
failt() { printf 'FAIL %s\n' "$1"; rc=1; }
flat() { tr '\r\n' '||' < "$1" | cut -c1-2500; }
has() { grep -q -- "$2" "$1"; }
# first / last line number in the aws call log matching a pattern (0 if none)
first() { local n; n="$(grep -n -- "$1" "$AWS" | head -1 | cut -d: -f1)"; printf '%s' "${n:-0}"; }
last()  { local n; n="$(grep -n -- "$1" "$AWS" | tail -1 | cut -d: -f1)"; printf '%s' "${n:-0}"; }
# at STRING: character position of STRING's first occurrence in the output
at() { tr '\r\n' '  ' < "$OUT" | awk -v s="$1" '{ print index($0, s) }'; }

STATE_NAME=".cloudlens-deploy-${STACK}-us-east-1.state"

# setup NAME: fresh per-case files. The live sessions table: two sessions KVO
# cut from two workload ENIs to our collector target with KVO's filter (the
# shape seen live: SessionNumber 1, tagged cloudlens.mirror_session.<vpc>),
# and three that are the customer's: to their NLB and GWLB endpoint targets
# with their own filter, one of them on the SAME workload ENI as ours and one
# even carrying a cloudlens-looking Name tag. Only the first two are ours.
setup() {
  W="$S/$1"; mkdir -p "$W/work" "$W/home" "$W/aws-state"
  OUT="$W/out"; AWS="$W/aws-calls"; STATE_DIR="$W/aws-state"
  : > "$AWS"
  cat > "$STATE_DIR/sessions" <<'TBL'
tms-0w1 eni-0web1 tmt-0ours tmf-0ours cloudlens.mirror_session.vpc-0cust
tms-0w2 eni-0web2 tmt-0ours tmf-0ours cloudlens.mirror_session.vpc-0cust
tms-0nlb eni-0web1 tmt-0nlb tmf-0cust customer-nlb-mirror
tms-0gwlb eni-0web3 tmt-0gwlb tmf-0cust customer-gwlb-mirror
tms-0tagonly eni-0web4 tmt-0nlb tmf-0cust cloudlens.mirror_session.vpc-0cust
TBL
  # EXTRA_SESSIONS adds rows for one case only.
  [[ -n "${EXTRA_SESSIONS:-}" ]] && printf '%s\n' "$EXTRA_SESSIONS" >> "$STATE_DIR/sessions"
  ENV=(PATH="$S/bin:$PATH" HOME="$W/home" AWS_CALLS="$AWS" AWS_STATE="$STATE_DIR"
       CLOUDLENS_PROBE_TIMEOUT=5 TERM=dumb)
}

# run_bg NAME "EXTRA_ENV..." ARGS...: no terminal anywhere. $code, $OUT.
# SEED_STATE=<file> puts a manifest from an earlier run in the work dir.
run_bg() {
  local name="$1" extra="$2"; shift 2
  setup "$name"
  [[ -n "${SEED_STATE:-}" ]] && cp "$SEED_STATE" "$W/work/$STATE_NAME"
  ( cd "$W/work" && env "${ENV[@]}" $extra python3 "$S/detach.py" /bin/bash "$SCRIPT" --stack-name "$STACK" --region us-east-1 "$@" ) >"$OUT" 2>&1
  code=$?
}

foreign_untouched() {
  ! has "$AWS" "delete-traffic-mirror-session --traffic-mirror-session-id tms-0nlb" \
    && ! has "$AWS" "delete-traffic-mirror-session --traffic-mirror-session-id tms-0gwlb" \
    && ! has "$AWS" "delete-traffic-mirror-session --traffic-mirror-session-id tms-0tagonly" \
    && ! has "$AWS" "delete-traffic-mirror-target --traffic-mirror-target-id tmt-0nlb" \
    && ! has "$AWS" "delete-traffic-mirror-target --traffic-mirror-target-id tmt-0gwlb" \
    && ! has "$AWS" "delete-traffic-mirror-filter --traffic-mirror-filter-id tmf-0cust" \
    && [[ "$(grep -c -E '^tms-0(nlb|gwlb|tagonly) ' "$STATE_DIR/sessions")" == "3" ]]
}

# sessions_then_target_then_filter: both of our sessions were deleted, and
# the last session delete came before the target delete, which came before
# the filter delete; AWS (the stub) accepted the target and the filter.
sessions_then_target_then_filter() {
  local s1 s2 sl t f
  s1="$(first 'delete-traffic-mirror-session --traffic-mirror-session-id tms-0w1')"
  s2="$(first 'delete-traffic-mirror-session --traffic-mirror-session-id tms-0w2')"
  sl="$(last 'delete-traffic-mirror-session ')"
  t="$(first 'delete-traffic-mirror-target --traffic-mirror-target-id tmt-0ours')"
  f="$(first 'delete-traffic-mirror-filter --traffic-mirror-filter-id tmf-0ours')"
  (( s1 > 0 && s2 > 0 && t > 0 && f > 0 && sl < t && t < f )) \
    && grep -qx tmt-0ours "$STATE_DIR/deleted-targets" 2>/dev/null \
    && grep -qx tmf-0ours "$STATE_DIR/deleted-filters" 2>/dev/null \
    && ! grep -q -E '^tms-0w[12] ' "$STATE_DIR/sessions"
}

# (a) Full teardown, stack present, brownfield: our two sessions are found
#     through the collector's target, recorded in the manifest, deleted
#     first; then the target and the filter go, with no ...InUse refusal and
#     nothing left in the failure list. tms-0late, which KVO cut after Phase 2
#     had looked (the stub adds it at the stack delete), is caught by the
#     look-up the sweep repeats just before the deletes, and goes first too.
run_bg a_full "" --yes
if [[ $code -eq 0 ]] && sessions_then_target_then_filter \
   && ! has "$OUT" "InUse" && ! has "$OUT" "Some resources could not be removed" \
   && has "$OUT" "deleted traffic mirror session tms-0w1" && has "$OUT" "deleted traffic mirror target tmt-0ours" \
   && has "$OUT" "deleted traffic mirror filter tmf-0ours" && has "$OUT" "Mirror sessions:    3"; then
  pass "(a) sessions to our target deleted first, then the target, then the filter; AWS refused nothing"
else failt "(a) full teardown: exit $code, calls=[$(grep -n 'traffic-mirror' "$AWS" | tr '\n' '|')] out=[$(flat "$OUT")]"; fi
_late="$(first 'delete-traffic-mirror-session --traffic-mirror-session-id tms-0late')"
_tgt="$(first 'delete-traffic-mirror-target --traffic-mirror-target-id tmt-0ours')"
if (( _late > 0 && _late < _tgt )) && ! grep -q '^tms-0late ' "$STATE_DIR/sessions"; then
  pass "(a) a session cut after Phase 2 is found by the sweep's own look-up and deleted before the target"
else failt "(a) late session: late=$_late target=$_tgt calls=[$(grep -n 'traffic-mirror' "$AWS" | tr '\n' '|')]"; fi
if grep -q '^SWEEP_MIRROR_SESSIONS=tms-0w1 tms-0w2$' "$W/work/$STATE_NAME" 2>/dev/null; then
  pass "(a) the sessions are recorded in the manifest (SWEEP_MIRROR_SESSIONS) before anything is deleted"
else failt "(a) manifest: [$(tr '\n' '|' < "$W/work/$STATE_NAME" 2>/dev/null)]"; fi
if has "$OUT" "Traffic mirror sessions to this stack's collectors" \
   && has "$OUT" "tms-0w1 *source eni-0web1, target tmt-0ours" \
   && has "$OUT" "stops only the mirror COPIES" && has "$OUT" "production traffic" \
   && has "$OUT" "2 session(s) (tms-0w1 tms-0w2)" \
   && (( $(at "Traffic mirror sessions to this stack") < $(at "Proceeding (--yes)") )); then
  pass "(a) the report and the confirmation list name the sessions, and say production traffic is unaffected"
else failt "(a) report: out=[$(flat "$OUT")]"; fi

# (b) Same run: the customer's sessions in the same VPC (to their NLB and
#     GWLB endpoint targets, their own filter), including one on the SAME
#     workload ENI as ours and one with a cloudlens-looking Name tag, are
#     never deleted or listed, and neither are their targets or filter.
if foreign_untouched && ! has "$OUT" "tms-0nlb" && ! has "$OUT" "tms-0gwlb" && ! has "$OUT" "tms-0tagonly"; then
  pass "(b) foreign sessions (foreign target + foreign filter, same VPC, same ENI, lookalike tag) never touched or listed"
else failt "(b) foreign sessions: calls=[$(grep -n 'traffic-mirror' "$AWS" | tr '\n' '|')] left=[$(tr '\n' '|' < "$STATE_DIR/sessions")]"; fi

# (c) --sweep-only after the stack is already gone, with a manifest written
#     before sessions were recorded (targets and filter, no sessions key):
#     AWS is asked for the sessions on the recorded target and filter, they
#     are named in the confirmation list before anything is deleted, and they
#     are deleted before the target and the filter. This is the exact re-run
#     the live teardown told the operator to do.
cat > "$S/old-manifest" <<'EOF'
SWEEP_RECORDED_AT=2026-10-16T18:04:11Z
SWEEP_VPC_ID=vpc-0cust
SWEEP_STACK_OWNS_VPC=false
SWEEP_INSTANCES=i-0vc
SWEEP_VOLUMES=
SWEEP_ASGS=cloudlens.collector.vpc-0cust.us-east-1a
SWEEP_LTS=lt-0coll
SWEEP_SGS=
SWEEP_OWN_INSTANCES=
SWEEP_MIRROR_TARGETS=tmt-0ours
SWEEP_MIRROR_FILTERS=tmf-0ours
EOF
SEED_STATE="$S/old-manifest" run_bg c_sweep_old "STUB_STACK=missing" --sweep-only --yes
if [[ $code -eq 0 ]] && has "$OUT" "Using the manifest recorded by an earlier run" \
   && has "$OUT" "2 session(s) (tms-0w1 tms-0w2)" \
   && (( $(at "2 session(s) (tms-0w1 tms-0w2)") < $(at "Proceeding (--yes)") )) \
   && sessions_then_target_then_filter && foreign_untouched \
   && ! has "$OUT" "InUse" && ! has "$OUT" "Some resources could not be removed" \
   && ! has "$AWS" "cloudformation delete-stack"; then
  pass "(c) --sweep-only, stack gone, old manifest without sessions: live sessions found and deleted before target and filter"
else failt "(c) sweep-only old manifest: exit $code, calls=[$(grep -n 'traffic-mirror' "$AWS" | tr '\n' '|')] out=[$(flat "$OUT")]"; fi

# (c2) A manifest that does record sessions. A recorded id is evidence of
#      nothing now: tms-0gone was removed by an earlier run, and tms-0nlb
#      (recorded by mistake, or repointed since with ModifyTrafficMirrorSession)
#      now sends to the customer's NLB target with the customer's filter.
#      Only what AWS says sends to our target right now is deleted (tms-0w1,
#      and tms-0w2, which was never recorded); tms-0nlb is named and left
#      alone, tms-0gone is reported as gone, and neither is a failure.
cat > "$S/new-manifest" <<'EOF'
SWEEP_VPC_ID=vpc-0cust
SWEEP_STACK_OWNS_VPC=false
SWEEP_INSTANCES=i-0vc
SWEEP_MIRROR_TARGETS=tmt-0ours
SWEEP_MIRROR_FILTERS=tmf-0ours
SWEEP_MIRROR_SESSIONS=tms-0gone tms-0w1 tms-0nlb
EOF
SEED_STATE="$S/new-manifest" run_bg c2_sweep_recorded "STUB_STACK=missing" --sweep-only --yes
if [[ $code -eq 0 ]] && sessions_then_target_then_filter && foreign_untouched \
   && has "$OUT" "tms-0gone is already gone" && has "$OUT" "tms-0nlb now sends to tmt-0nlb" \
   && has "$OUT" "2 session(s) (tms-0w1 tms-0w2)" \
   && ! has "$OUT" "Some resources could not be removed"; then
  pass "(c2) recorded sessions re-checked against AWS: one repointed elsewhere is left alone, one already gone is not a failure"
else failt "(c2) recorded: exit $code, calls=[$(grep -n 'traffic-mirror' "$AWS" | tr '\n' '|')] out=[$(flat "$OUT")]"; fi

# (g) Attribution is by target alone. Added rows:
#       tms-0mix    our target, the customer's filter    ours: deleted
#       tms-0otherB a second deploy's target, OUR filter  not ours: kept
#       tms-0otherA a second deploy's target and filter   not ours: kept
#     and the filter look-up by Name returns both KVOs' filters, as it does
#     when two deploys tap one VPC (the tag is per VPC, not per stack). Our
#     sessions go, then our target; both filters are still used by the other
#     deploy's sessions, so both stay, said plainly and not as a failure.
#     Before the fix, tms-0otherA/B were deleted through the filter and then
#     the other deploy's filter with them.
EXTRA_SESSIONS="tms-0mix eni-0web5 tmt-0ours tmf-0cust operator-extra
tms-0otherB eni-0web6 tmt-0other tmf-0ours cloudlens.mirror_session.vpc-0cust
tms-0otherA eni-0web7 tmt-0other tmf-0other cloudlens.mirror_session.vpc-0cust" \
  run_bg g_target_only "STUB_TWO_FILTERS=1" --yes
_mix="$(first 'delete-traffic-mirror-session --traffic-mirror-session-id tms-0mix')"
_tgt="$(first 'delete-traffic-mirror-target --traffic-mirror-target-id tmt-0ours')"
if [[ $code -eq 0 ]] && (( _mix > 0 && _mix < _tgt )) && grep -qx tmt-0ours "$STATE_DIR/deleted-targets" 2>/dev/null \
   && ! has "$AWS" "delete-traffic-mirror-session --traffic-mirror-session-id tms-0other" \
   && ! has "$AWS" "delete-traffic-mirror-target --traffic-mirror-target-id tmt-0other" \
   && [[ "$(grep -c -E '^tms-0other[AB] ' "$STATE_DIR/sessions")" == "2" ]] \
   && ! grep -q -E '^tmf-0(ours|other)$' "$STATE_DIR/deleted-filters" 2>/dev/null \
   && ! has "$AWS" "delete-traffic-mirror-filter --traffic-mirror-filter-id tmf-0cust" \
   && foreign_untouched; then
  pass "(g) our target + any filter is ours; any other target is not, even with our filter or a same-named one"
else failt "(g) target-only: exit $code, calls=[$(grep -n 'traffic-mirror' "$AWS" | tr '\n' '|')] left=[$(tr '\n' '|' < "$STATE_DIR/sessions")]"; fi
if has "$OUT" "mirror filter tmf-0ours left in place" && has "$OUT" "mirror filter tmf-0other left in place" \
   && has "$OUT" "tms-0otherB" && ! has "$OUT" "Some resources could not be removed" \
   && (( $(at "Traffic mirror sessions that use this VPC's KVO filter") > $(at "Traffic mirror sessions to this stack's collectors") )) \
   && has "$OUT" "3 session(s) (tms-0w1 tms-0w2 tms-0mix)"; then
  pass "(g) the report names the other deploy's session on the shared filter as left alone, and the filters as left in place"
else failt "(g) report: out=[$(flat "$OUT")]"; fi

# (h) Stack present, and between Phase 2 and the sweep one of ours (tms-0w2,
#     recorded in the manifest) was repointed at somebody else's target. The
#     sweep checks again before deleting: tms-0w2 is left alone, the rest of
#     ours go, and KVO's filter, still used by tms-0w2, stays.
run_bg h_repointed "STUB_REPOINT=1" --yes
if [[ $code -eq 0 ]] && grep -q '^SWEEP_MIRROR_SESSIONS=tms-0w1 tms-0w2$' "$W/work/$STATE_NAME" 2>/dev/null \
   && ! has "$AWS" "delete-traffic-mirror-session --traffic-mirror-session-id tms-0w2" \
   && has "$AWS" "delete-traffic-mirror-session --traffic-mirror-session-id tms-0w1" \
   && grep -qx tmt-0ours "$STATE_DIR/deleted-targets" 2>/dev/null \
   && ! grep -qx tmf-0ours "$STATE_DIR/deleted-filters" 2>/dev/null \
   && has "$OUT" "tms-0w2 now sends to tmt-0nlb" && has "$OUT" "mirror filter tmf-0ours left in place" \
   && ! has "$OUT" "Some resources could not be removed"; then
  pass "(h) a recorded session repointed before the sweep is checked again and left alone"
else failt "(h) repointed: exit $code, calls=[$(grep -n 'traffic-mirror' "$AWS" | tr '\n' '|')] out=[$(flat "$OUT")]"; fi

# (i) One of OUR sessions will not delete (AWS refuses it). Its target and
#     KVO's filter then stay in use by a session of this stack's, and that is
#     a real leftover: all three are reported as failures to re-run, never
#     passed off as a filter shared with somebody else.
run_bg i_own_denied "STUB_DENY_SESSION=tms-0w2" --yes
if [[ $code -eq 0 ]] && has "$OUT" "Some resources could not be removed" \
   && grep -q '^  tms-0w2: .*UnauthorizedOperation' "$OUT" \
   && grep -q '^  tmt-0ours: .*TrafficMirrorTargetInUse' "$OUT" \
   && grep -q '^  tmf-0ours: .*TrafficMirrorFilterInUse' "$OUT" \
   && ! has "$OUT" "mirror filter tmf-0ours left in place" \
   && has "$AWS" "delete-traffic-mirror-session --traffic-mirror-session-id tms-0w1"; then
  pass "(i) our own session failing to delete leaves its target and filter reported as failures, not as shared"
else failt "(i) own denied: exit $code, out=[$(flat "$OUT")]"; fi

# (d) --dry-run: nothing is deleted (no delete call reaches aws, the
#     sessions table is untouched), and the sessions are listed in the report
#     and in the rehearsed commands, ahead of the target and the filter.
run_bg d_dry "" --dry-run
_dry_s="$(at 'delete-traffic-mirror-session --traffic-mirror-session-id tms-0w1')"
_dry_t="$(at 'delete-traffic-mirror-target --traffic-mirror-target-id tmt-0ours')"
_dry_f="$(at 'delete-traffic-mirror-filter --traffic-mirror-filter-id tmf-0ours')"
if [[ $code -eq 0 ]] && ! grep -q -E 'delete-|terminate-|release-address' "$AWS" \
   && [[ "$(wc -l < "$STATE_DIR/sessions" | tr -d ' ')" == "5" ]] \
   && has "$OUT" "Traffic mirror sessions to this stack's collectors" \
   && has "$OUT" "would delete traffic mirror session tms-0w1" && has "$OUT" "would delete traffic mirror session tms-0w2" \
   && (( _dry_s > 0 && _dry_s < _dry_t && _dry_t < _dry_f )) \
   && ! has "$OUT" "tms-0nlb" && ! has "$OUT" "tms-0gwlb" && ! has "$OUT" "tms-0tagonly" \
   && has "$OUT" "DRY RUN: nothing above was actually deleted" && [[ ! -f "$W/work/$STATE_NAME" ]]; then
  pass "(d) --dry-run deletes nothing, lists our sessions, rehearses them before the target and filter"
else failt "(d) dry-run: exit $code, deletes=[$(grep -E 'delete-|terminate-' "$AWS" | tr '\n' '|')] out=[$(flat "$OUT")]"; fi

# (e) --orphans (read-only audit) counts the sessions and deletes nothing.
run_bg e_audit "" --orphans
if [[ $code -eq 0 ]] && ! grep -q -E 'delete-|terminate-' "$AWS" \
   && has "$OUT" "Mirror sessions:       2" && has "$OUT" "tms-0w1"; then
  pass "(e) --orphans reports the 2 sessions and deletes nothing"
else failt "(e) audit: exit $code, out=[$(flat "$OUT")]"; fi

# (f) --help says the sweep removes the sessions, and in which order.
run_bg f_help "" --help
if [[ $code -eq 0 ]] && has "$OUT" "traffic mirror sessions KVO cut" \
   && has "$OUT" "never the workload's traffic" && has "$OUT" "then the targets and the filter"; then
  pass "(f) --help names the session sweep and its order"
else failt "(f) --help: exit $code, out=[$(flat "$OUT")]"; fi

exit $rc
