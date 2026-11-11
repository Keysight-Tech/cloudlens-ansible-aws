#!/usr/bin/env bash
# =====================================================================
# CloudLens Stack Teardown (AWS): delete the stack, then sweep what the
# stack leaves behind.
# =====================================================================
# This lives in its own file, and NOT behind a --teardown flag on
# deploy-stack.sh, on purpose. deploy-stack.sh states in a dozen places that
# it never deletes anything, and that promise is what makes it safe to paste
# into a terminal from a curl pipe with whatever flags someone half-remembers.
# A mistyped argument to a deploy script must never be able to delete a stack,
# so the destructive tool is a separate name you have to reach for deliberately.
#
# What it removes, and why any of it needs removing at all:
#
#   1. The CloudFormation stack.
#   2. The EBS volumes the stack's instances leave behind. The Marketplace
#      AMIs set DeleteOnTermination=false on their root volumes, so deleting
#      the stack terminates the instances and leaves their disks sitting in
#      'available' forever. Three weeks of build/teardown cycles produced 43
#      of them, 6,348 GB, about $635/month, found by a cost anomaly alert and
#      not by anything in this repo.
#   3. Non-stack security groups left inside the stack's own VPC. These are
#      the classic DELETE_FAILED cause: CloudFormation cannot delete a VPC
#      that still contains a security group it does not own, and the error it
#      raises names nothing useful.
#   4. The collector Auto Scaling Group and launch templates KVO creates for
#      AWS zone tapping. KVO builds them with the IAM role the stack grants,
#      outside CloudFormation, so the stack has no idea they exist. Left
#      behind, the ASG rebuilds its instance every time someone terminates it.
#   5. The traffic mirror sessions KVO cuts from each tapped workload ENI to
#      those collectors, then their targets and filter. The KVO goes with the
#      stack, so nothing else ever removes the sessions, and one left behind
#      keeps its target and filter undeletable and still takes the mirror
#      copies on its ENI. Deleting a session never touches workload traffic.
#      A session is this stack's only when it sends to one of those
#      collectors' targets, as AWS reports it at the moment of the delete.
#
# ---------------------------------------------------------------------
# SCOPING RULE. This is the one rule the whole script rests on.
#
# Nothing is deleted unless it can be tied to THIS stack by one of exactly
# three pieces of evidence, every one of which comes from AWS and none of
# which is a guess:
#
#   1. Stack membership: the resource is a member of the CloudFormation
#      stack (describe-stack-resources), or it was attached to an instance
#      that was, recorded while the attachment still existed.
#   2. The stack's own VPC: the resource lives in the VPC the stack itself
#      created. Switched OFF for brownfield deploys (--existing-vpc-id),
#      where the VPC belongs to the customer and its other contents are
#      none of our business.
#   3. An owner tag naming the stack: aws:cloudformation:stack-name, or the
#      cloudlens:owner / cloudlens:cfn-stack tags this repo's scripts write.
#
# An 'available' volume with none of that evidence is REPORTED and never
# deleted. In a shared account "it was unattached" is not ownership, and a
# teardown that guessed would be worse than the leak it is fixing.
# ---------------------------------------------------------------------
#
# Usage:
#   bash deploy/teardown-stack.sh --stack-name NAME [--region REGION]
#   bash deploy/teardown-stack.sh --orphans          # read-only audit
#   bash deploy/teardown-stack.sh --dry-run          # print, delete nothing
#
# Nothing here deletes anything without an explicit confirmation: a yes on a
# terminal, or --yes when there is no terminal. There is no default that
# destroys.
# =====================================================================
set -euo pipefail
# Brace-wrapped for the same reason deploy-stack.sh is: bash must buffer the
# whole file before running any of it, or a `curl ... | bash` invocation would
# lose the pipe the moment stdin is re-attached to the terminal below. The
# closing brace is the last line of the file.
{

# ---------------------------------------------------------------------
# Re-attach stdin to the terminal when invoked via `curl ... | bash`, exactly
# as deploy-stack.sh does. Without it every prompt below would try to read the
# script itself, and a destructive tool must never mis-read its own body as an
# answer to "are you sure".
# ---------------------------------------------------------------------
if [[ ! -t 0 ]] && [[ -r /dev/tty ]]; then
  if { exec 3</dev/tty; } 2>/dev/null; then
    exec <&3 3<&-
  fi
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$PWD")"

# ---------------------------------------------------------------------
# Config + globals
# ---------------------------------------------------------------------
DEFAULT_REGION="${CLOUDLENS_REGION:-us-east-1}"
REGION=""
STACK_NAME="${CLOUDLENS_STACK_NAME:-}"
ARG_REGION=""
ARG_STACK=""

DRY_RUN=false
AUDIT_ONLY=false          # --orphans: read-only, deletes nothing, ever
SWEEP_ONLY=false          # skip the stack delete, sweep what is already loose
NO_SWEEP=false
ASSUME_YES=false          # the ONLY way a non-interactive run may delete
ACCEPT_LICENCE_LOSS=false # the second confirmation, required when a KVO exists
RELEASE_LICENCES=false    # --release-licences: release the KVO's licences with no terminal to ask on
ARG_KVO_USER=""           # --kvo-admin-user / --kvo-admin-pass / --kvo-address
ARG_KVO_PASS=""
ARG_KVO_ADDRESS=""
PROBE_TIMEOUT="${CLOUDLENS_PROBE_TIMEOUT:-25}"
DELETE_TIMEOUT="${CLOUDLENS_DELETE_TIMEOUT:-2700}"   # 45 minutes
KVO_HTTP_TIMEOUT="${CLOUDLENS_KVO_HTTP_TIMEOUT:-15}"        # per HTTP call to the KVO
KVO_RELEASE_TIMEOUT="${CLOUDLENS_KVO_RELEASE_TIMEOUT:-600}" # the whole release, overall

# Where scripts/kvo_license.py comes from when this script is run through
# `curl | bash` and the repo is not on disk: the same raw tree deploy-stack.sh
# clones from, fetched into a temp dir that is removed on exit.
REPO_OWNER="Keysight-Tech"
REPO_NAME="cloudlens-ansible-aws"
REPO_RAW="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/main"
LIC_PY=""                 # the kvo_license.py in use, once located
LIC_CACHE_DIR=""          # temp dir holding a fetched copy, if one was needed
LIC_RELEASED=false        # true ONLY on exit 0 from kvo_license.py --release-all,
                          # or a list the KVO answered with nothing on it
LIC_WHY=""                # with LIC_RELEASED: "released" or "held nothing", for the waiver
LIC_RC=0                  # set by lic_run
_lic_out=""               # the temp file holding kvo_license.py's output, while it exists
# How the licence count is read from kvo_license.py: the `count` of the JSON
# object it prints as the last line of its output (with --json, on every
# exit path), through the python3 the release already requires. Never its
# prose: a wording change must not be able to turn "2 licences" into "could
# not tell", or into 0. console/tests/test_kvo_license_cli.py greps this
# exact expression out of this file and feeds it the real script's output.
LIC_COUNT_PARSE='import json,sys; print(json.load(sys.stdin)["count"])'

# Owner tag scripts/deploy-test-workload-vms.sh writes on everything it makes.
# Evidence class 3 for the resources that script leaves in a stack's VPC.
TESTVM_OWNER_TAG_KEY="cloudlens:owner"
TESTVM_OWNER_TAG_VALUE="deploy-test-workload-vms"

STACK_STATUS="unknown"
STACK_VPC_ID=""
STACK_OWNS_VPC=false
STACK_INSTANCE_IDS=""
STACK_SG_IDS=""
HAS_KVO=false
STATE_FILE=""

SWEEP_VOLUMES=""      # ids recorded while still attached to stack instances
SWEEP_SGS=""          # non-stack SGs inside the stack's own VPC
SWEEP_ASGS=""
SWEEP_LTS=""
SWEEP_OWN_INSTANCES=""    # instances the deploy tagged cloudlens:stack=<stack>, any VPC
SWEEP_MIRROR_TARGETS=""   # traffic mirror targets on this stack's collectors
SWEEP_MIRROR_FILTERS=""   # the KVO mirror filter for this stack's VPC
SWEEP_MIRROR_SESSIONS=""  # sessions KVO cut from workload ENIs to those targets
MIRROR_SESSION_LINES=""   # "tms-id<TAB>source-eni<TAB>target<TAB>filter" per line, for the report
MIRROR_SESSIONS_DROPPED="" # recorded earlier, no longer sending to those targets: left alone
MIRROR_SHARED_LINES=""    # sessions on KVO's filter to a target that is NOT ours: left alone
SWEEP_GB=0
SWEEP_COST="0.00"
UNATTRIBUTED_GB=0
UNATTRIBUTED_COUNT=0
UNATTRIBUTED_COST="0.00"
VOLUME_LINES=""       # "vol-id<TAB>size<TAB>type<TAB>state" per line

DELETED_VOLUMES=0; DELETED_GB=0; DELETED_COST=0; DELETED_SGS=0; DELETED_ASGS=0; DELETED_LTS=0
DELETED_SESSIONS=0
FAILED_ITEMS=""
SCRIPT_DONE=false
PHASE_NAME="startup"

# ---------------------------------------------------------------------
# Pretty output (same vocabulary as deploy-stack.sh, on purpose: the two
# scripts are read back to back in the same terminal)
# ---------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_GREEN='\033[0;32m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[0;34m'
  C_RED='\033[0;31m'; C_GREY='\033[0;90m'; C_BOLD='\033[1m'; C_RESET='\033[0m'
else
  C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_RED=''; C_GREY=''; C_BOLD=''; C_RESET=''
fi

banner() {
  local msg="$1"
  echo -e "${C_BLUE}================================================================${C_RESET}"
  printf "${C_BLUE}  ${C_BOLD}%s${C_RESET}\n" "$msg"
  echo -e "${C_BLUE}================================================================${C_RESET}"
}
ok()    { echo -e "${C_GREEN}[ok]${C_RESET} $1"; }
warn()  { echo -e "${C_YELLOW}[warn]${C_RESET} $1"; }
fail()  { echo -e "${C_RED}[x]${C_RESET} $1" >&2; SCRIPT_DONE=true; exit 1; }
step()  { echo; echo -e "${C_BLUE}--- $1 ---${C_RESET}"; PHASE_NAME="$1"; }
note()  { echo -e "${C_GREY}  -> $1${C_RESET}"; }
dryrun_say() { echo -e "${C_YELLOW}[dry-run]${C_RESET} $1"; }

to_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# ---------------------------------------------------------------------
# Prompting that can never block. Identical mechanism to deploy-stack.sh:
# the /dev/tty re-attach above is what makes a piped invocation interactive
# at all, and this single -t 0 check AFTER it decides whether there is
# anybody to ask. When there is not, every prompt takes its default, and for
# this script every destructive default is "no".
# ---------------------------------------------------------------------
INTERACTIVE=false
[[ -t 0 ]] && INTERACTIVE=true

ask() {
  local prompt="$1" def="${2:-}" ans=""
  if [[ "$INTERACTIVE" == "true" ]]; then
    read -rp "$prompt" ans || true
  fi
  printf '%s' "${ans:-$def}"
}

# ask_yn "prompt" "y|n"  -> returns 0 for yes, 1 for no
ask_yn() {
  local ans; ans="$(to_lower "$(ask "$1" "$2")")"
  [[ "$ans" == "y" || "$ans" == "yes" ]]
}

# ---------------------------------------------------------------------
# Read-only probes. Lifted from deploy-stack.sh unchanged in behaviour: a
# probe may never abort the run and may never hang it. Anything that fails,
# times out or is killed yields EMPTY, and empty means "unknown", never
# "there is nothing there" and never "safe to delete".
# ---------------------------------------------------------------------
PROBE_TICK=""
probe() {
  local out="" tmp pid ticks=0 limit
  if [[ -z "$PROBE_TICK" ]]; then
    if sleep 0.2 >/dev/null 2>&1; then PROBE_TICK="0.2"; else PROBE_TICK="1"; fi
  fi
  if [[ "$PROBE_TICK" == "0.2" ]]; then limit=$(( PROBE_TIMEOUT * 5 )); else limit="$PROBE_TIMEOUT"; fi

  tmp="$(mktemp "${TMPDIR:-/tmp}/cloudlens-teardown.XXXXXX" 2>/dev/null)" || tmp=""
  if [[ -z "$tmp" ]]; then
    if [[ "${PROBE_MERGE_STDERR:-}" == "1" ]]; then "$@" 2>&1 || true; else "$@" 2>/dev/null || true; fi
    return 0
  fi

  if [[ "${PROBE_MERGE_STDERR:-}" == "1" ]]; then
    "$@" >"$tmp" 2>&1 &
  else
    "$@" >"$tmp" 2>/dev/null &
  fi
  pid=$!
  while kill -0 "$pid" 2>/dev/null && (( ticks < limit )); do
    sleep "$PROBE_TICK" 2>/dev/null || sleep 1
    ticks=$((ticks+1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    : > "$tmp"          # a half-written answer is not an answer
  fi
  wait "$pid" 2>/dev/null || true
  out="$(cat "$tmp" 2>/dev/null || true)"
  rm -f "$tmp" 2>/dev/null || true
  printf '%s' "$out"
  return 0
}

AWS_REGION_ARG=()
ro_aws() { probe aws "${AWS_REGION_ARG[@]}" "$@"; }

det_clean() { local v="$1"; [[ "$v" == "None" ]] && v=""; printf '%s' "$v"; }

# First NON-BLANK line: the AWS CLI v2 leads its error output with a blank one.
first_line() {
  printf '%s\n' "$1" | grep -v '^[[:space:]]*$' 2>/dev/null | head -1 || true
}

# Collapse tabs/newlines from `--output text` into a single space-separated
# token list, and drop AWS's "None".
tokens() {
  printf '%s' "$1" | tr '\t\r\n' '   ' | tr -s ' ' | sed 's/^ //; s/ $//' \
    | tr ' ' '\n' | grep -v '^None$' 2>/dev/null | tr '\n' ' ' | sed 's/ $//' || true
}

in_list() {
  local needle="$1" hay="$2"
  case " $hay " in *" $needle "*) return 0 ;; esac
  return 1
}

# ---------------------------------------------------------------------
# The DESTRUCTIVE wrapper. Prints instead of acting under --dry-run, refuses
# outright under --orphans, and never aborts the run on failure: one volume
# that will not delete must not stop the other forty two from being cleaned.
# ---------------------------------------------------------------------
DEL_ERR=""
del_aws() {
  local out rc=0
  if [[ "$AUDIT_ONLY" == "true" ]]; then
    DEL_ERR="refused: --orphans is read-only"
    return 1
  fi
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "aws ${AWS_REGION_ARG[*]:-} $*"
    DEL_ERR=""
    return 0
  fi
  out="$(aws "${AWS_REGION_ARG[@]}" "$@" 2>&1)" || rc=$?
  if (( rc != 0 )); then DEL_ERR="$(first_line "$out")"; return 1; fi
  DEL_ERR=""
  return 0
}

record_failure() { FAILED_ITEMS="${FAILED_ITEMS}${FAILED_ITEMS:+$'\n'}  $1: ${2:-unknown error}"; }

# "deleted" or "would delete", so a rehearsal never reads like it did something.
did() { if [[ "$DRY_RUN" == "true" ]]; then printf 'would delete'; else printf 'deleted'; fi; }

# ---------------------------------------------------------------------
# State file, shared with deploy-stack.sh (.cloudlens-deploy-<stack>-<region>
# .state, mode 600, git-ignored). Teardown writes the attribution manifest
# here BEFORE the delete, so a sweep run days later still knows which volumes
# belonged to this stack. Nothing secret is ever written.
# ---------------------------------------------------------------------
state_locate() {
  local base=".cloudlens-deploy-${STACK_NAME}-${REGION}.state"
  if [[ -f "$PWD/$base" ]]; then STATE_FILE="$PWD/$base"; return 0; fi
  if [[ -f "$SCRIPT_DIR/../$base" ]]; then STATE_FILE="$SCRIPT_DIR/../$base"; return 0; fi
  STATE_FILE="$PWD/$base"
  return 0
}

state_get() {
  local k="$1" line
  [[ -n "$STATE_FILE" && -f "$STATE_FILE" ]] || return 1
  line="$(grep -m1 "^${k}=" "$STATE_FILE" 2>/dev/null)" || return 1
  [[ -n "$line" ]] || return 1
  printf '%s' "${line#*=}"
}

state_set() {
  local k="$1" v="${2:-}" tmp
  [[ -n "$STATE_FILE" ]] || return 0
  [[ "$AUDIT_ONLY" == "true" ]] && return 0
  [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || return 0
  v="${v//$'\n'/ }"
  tmp="${STATE_FILE}.tmp.$$"
  ( umask 077; { grep -v "^${k}=" "$STATE_FILE" 2>/dev/null || true
                 printf '%s=%s\n' "$k" "$v"; } > "$tmp" ) 2>/dev/null || return 0
  chmod 600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$STATE_FILE" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
}

# ---------------------------------------------------------------------
# Money. EBS list price per GB-month, us-east-1. Other regions differ by a
# few percent: this is an ESTIMATE, and its job is to make the number visible
# rather than to reconcile a bill. gp2 at $0.10 is what turned 6,348 stranded
# GB into the "$635 a month" that finally got somebody's attention.
# ---------------------------------------------------------------------
ebs_rate() {
  case "$1" in
    gp3)     printf '0.08'  ;;
    gp2)     printf '0.10'  ;;
    io1|io2) printf '0.125' ;;
    st1)     printf '0.045' ;;
    sc1)     printf '0.015' ;;
    standard) printf '0.05' ;;
    *)       printf '0.10'  ;;
  esac
}

money() { awk -v n="${1:-0}" 'BEGIN { printf "%.2f", n }'; }

# ---------------------------------------------------------------------
show_help() {
  cat <<'HLP'
CloudLens Stack Teardown (AWS)

Usage:
  bash deploy/teardown-stack.sh --stack-name NAME [--region REGION] [options]

Deletes the CloudFormation stack AND sweeps what the stack leaves behind:
the extra vPBs that --vpb-rails launched outside CloudFormation and their
Elastic IPs (terminated BEFORE the stack delete), unattached EBS volumes,
non-stack security groups sitting in the stack's VPC, the collector Auto
Scaling Group / launch templates KVO creates outside CloudFormation, the
traffic mirror sessions KVO cut from the workload ENIs to those collectors
(deleting one stops only the mirror copies, never the workload's traffic),
the mirror targets and filter on those collectors, and whatever the
deploy stamped inside a customer VPC (instances tagged cloudlens:stack=<stack>,
security groups named cloudlens-*-<stack>). Deleting the stack alone leaves
all of that billing.

Nothing is deleted without an explicit confirmation: a yes on a terminal, or
--yes when there is no terminal. There is no default that destroys.

Modes:
  --orphans                 READ-ONLY audit. Reports every resource this stack
                            has left loose and what it costs per month, and
                            deletes nothing at all. This is the mode to run
                            after a cost anomaly alert.
  --dry-run                 Walk the whole teardown and print every aws command
                            it would run, touching nothing. Covers every path,
                            including the DELETE_FAILED blocker handling.
  --sweep-only              Do not touch the stack. Sweep only what is already
                            loose (use after the stack is already deleted).
  --no-sweep                Delete the stack and stop. Leaves the volumes.

Required:
  --stack-name NAME         CloudFormation stack to tear down. Prompted for
                            when there is a terminal.
  --region REGION           AWS region (default: us-east-1, or $CLOUDLENS_REGION)

Confirmation (a non-interactive run needs these, and never assumes them):
  --yes                     Confirm the teardown without a terminal to ask on.
  --accept-licence-loss     Second confirmation, required only when the stack
                            contains a KVO whose licences were NOT released.
                            Licences can be RELEASED from a live KVO: this
                            script offers to do it for you once the teardown
                            is confirmed and before anything is deleted
                            (Phase 4a, the licensing API's deactivate
                            operation, proven to return the counts), and the
                            KVO UI can too (Settings > Product Licensing >
                            Deactivate licenses). Once the KVO is deleted they
                            cannot: the quantity still activated on it is
                            stranded for good. A release that leaves the KVO
                            clear waives this flag; anything else needs it.

KVO licence release (only when the stack contains a KVO):
  --release-licences        Release every licence the KVO holds, without
                            asking. On a terminal the script asks instead
                            ("Release all N licences ...? [Y/n]", default
                            yes). Without a terminal and without this flag
                            nothing is released, exactly as before.
  --kvo-admin-user USER     KVO login for the release. Defaults: this flag,
  --kvo-admin-pass PASS     then $CLOUDLENS_KVO_ADMIN_USER / _PASS, then a
                            prompt on a terminal (admin / admin offered). The
                            script never echoes, logs or records the
                            password, but a value given with --kvo-admin-pass
                            sits in your shell history and is visible in ps
                            while the script runs. Prefer the
                            CLOUDLENS_KVO_ADMIN_PASS variable or the prompt.
  --kvo-address ADDR        Where to reach the KVO, if the stack cannot say.
                            Found from the stack's KvoAddress output (public
                            IP when it has one, else private), then the
                            KvoInstance resource, then the instance Name tag.

Scoping (why this is safe to run in a shared account):
  A resource is deleted only when AWS itself ties it to this stack: it is a
  member of the stack, or it was attached to an instance that was, or it lives
  in the VPC the stack itself created, or it carries the deploy's own mark (a
  cloudlens:stack=<stack> tag, a cloudlens-*-<stack> security group name, a
  collector ASG tagged with this stack's vController address). In a
  pre-existing VPC (--existing-vpc-id) the VPC itself and everything the deploy
  did not stamp are left alone: only the stamped resources are swept. Anything
  else is reported and left alone. An unattached volume with no such evidence
  is somebody else's.

Env-var overrides:
  CLOUDLENS_REGION, CLOUDLENS_STACK_NAME, CLOUDLENS_PROBE_TIMEOUT,
  CLOUDLENS_DELETE_TIMEOUT, CLOUDLENS_KVO_ADMIN_USER, CLOUDLENS_KVO_ADMIN_PASS,
  CLOUDLENS_KVO_HTTP_TIMEOUT (per call, 15s), CLOUDLENS_KVO_RELEASE_TIMEOUT
  (the whole release, 600s)

Examples:
  # What is this stack leaking, and what is it costing? Deletes nothing.
  bash deploy/teardown-stack.sh --stack-name cloudlens-stack --orphans

  # Rehearse the whole teardown, touch nothing.
  bash deploy/teardown-stack.sh --stack-name cloudlens-stack --dry-run

  # Real teardown, no terminal, stack contains a KVO: release its licences
  # first, then delete. Stops if the release leaves anything on the KVO.
  bash deploy/teardown-stack.sh --stack-name cloudlens-stack \
       --region us-east-1 --yes --release-licences

  # Same, but accept losing whatever is still activated on the KVO.
  bash deploy/teardown-stack.sh --stack-name cloudlens-stack \
       --region us-east-1 --yes --accept-licence-loss

  # The stack is already gone, clean up what it left.
  bash deploy/teardown-stack.sh --stack-name cloudlens-stack --sweep-only --yes

Order of operations:
  1. Identify the stack and read its status.
  2. Build the attribution manifest, read-only, and record it to the state
     file BEFORE anything is deleted: volume ids have to be captured while
     they are still attached, because a detached volume remembers nothing.
  3. Report everything found, with GB and estimated monthly cost.
  4. Confirm the teardown. Asked before any licence is touched, so the next
     step only ever strips a KVO you have already chosen to destroy.
  4a. List the licences on the stack's KVO and offer to release them all,
     while the KVO is still alive to release them. A release that leaves the
     KVO clear means nothing is stranded and step 4b is skipped.
  4b. Warn about stranded KVO licences and take the licence-loss
     confirmation (the typed stack name, or --accept-licence-loss).
  5. Terminate the rail vPBs that --vpb-rails launched (tagged
     cloudlens:vpb-rail) and release their Elastic IPs, THEN delete the stack
     and wait for a terminal state. They share the stack's security group,
     so with one still running the delete lands in DELETE_FAILED.
  6. On DELETE_FAILED, name the blocking resources, offer to remove the ones
     that are attributable, and retry the delete.
  7. Sweep what the delete leaves, in this order: any rail vPB that appeared
     in between, the instances the deploy stamped (cloudlens:stack=<stack>,
     in any VPC, a customer's included), the collector ASGs and launch
     templates, the traffic mirror sessions to those collectors (they hold
     the targets and the filter, and AWS refuses to delete either while one
     is in use), then the targets and the filter (left in place while
     another deployment's session still uses it), the security groups, and
     last the volumes (only those now 'available').
HLP
}

# ---------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) ARG_STACK="$2"; shift 2 ;;
    --region) ARG_REGION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --orphans|--audit) AUDIT_ONLY=true; shift ;;
    --sweep-only) SWEEP_ONLY=true; shift ;;
    --no-sweep) NO_SWEEP=true; shift ;;
    -y|--yes) ASSUME_YES=true; shift ;;
    --accept-licence-loss|--accept-license-loss) ACCEPT_LICENCE_LOSS=true; shift ;;
    --release-licences|--release-licenses) RELEASE_LICENCES=true; shift ;;
    --kvo-admin-user) ARG_KVO_USER="$2"; shift 2 ;;
    --kvo-admin-pass) ARG_KVO_PASS="$2"; shift 2 ;;
    --kvo-address) ARG_KVO_ADDRESS="$2"; shift 2 ;;
    -h|--help) show_help; SCRIPT_DONE=true; exit 0 ;;
    *) warn "Unknown argument: $1"; show_help; SCRIPT_DONE=true; exit 1 ;;
  esac
done

if [[ "$SWEEP_ONLY" == "true" && "$NO_SWEEP" == "true" ]]; then
  fail "--sweep-only and --no-sweep are mutually exclusive."
fi

# ---------------------------------------------------------------------
# Last line of defence: no exit may ever be silent. Same reasoning as
# deploy-stack.sh, and it matters more here: a teardown that stopped without
# saying why leaves the operator unsure whether anything was deleted.
# ---------------------------------------------------------------------
on_exit() {
  local code=$?
  [[ "${BASHPID:-$$}" == "$$" ]] || return 0
  [[ -n "$LIC_CACHE_DIR" ]] && rm -rf "$LIC_CACHE_DIR" 2>/dev/null
  [[ "$SCRIPT_DONE" == "true" ]] && return 0
  (( code == 0 )) && return 0
  echo
  echo -e "${C_RED}[x] Stopped in: ${PHASE_NAME} (exit ${code})${C_RESET}" >&2
  echo "    Nothing above explained this, which means a command failed under" >&2
  echo "    'set -e'. The usual cause is an AWS call: expired credentials, no" >&2
  echo "    network, or a missing permission." >&2
  echo "    Check with: aws sts get-caller-identity" >&2
  echo "    Re-run with --orphans to see what is still loose. It deletes nothing." >&2
}
trap on_exit EXIT

on_interrupt() {
  echo
  SCRIPT_DONE=true
  [[ -n "$LIC_CACHE_DIR" ]] && rm -rf "$LIC_CACHE_DIR" 2>/dev/null
  [[ -n "$_lic_out" ]] && rm -f "$_lic_out" 2>/dev/null
  warn "Interrupted in: ${PHASE_NAME}"
  warn "Re-run with --sweep-only to finish the sweep, or --orphans to see what is left."
  exit 130
}
trap on_interrupt INT TERM

# =====================================================================
# Phase 1: identify the stack
# =====================================================================
step "Phase 1: Identify the stack"
banner "CloudLens Stack Teardown (AWS)"
echo
if [[ "$AUDIT_ONLY" == "true" ]]; then
  warn "AUDIT MODE (--orphans): read-only. Nothing will be deleted."
elif [[ "$DRY_RUN" == "true" ]]; then
  warn "DRY-RUN MODE: every aws command is printed, nothing is deleted."
fi
echo

command -v aws >/dev/null 2>&1 \
  || fail "AWS CLI not found. Install it: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"

REGION="${ARG_REGION:-$DEFAULT_REGION}"
AWS_REGION_ARG=(--region "$REGION")

CALLER="$(det_clean "$(ro_aws sts get-caller-identity --query Arn --output text)")"
if [[ -z "$CALLER" ]]; then
  fail "AWS credentials are missing or expired. Check with: aws sts get-caller-identity"
fi
ACCOUNT="$(det_clean "$(ro_aws sts get-caller-identity --query Account --output text)")"
ok "AWS identity: ${CALLER}"
note "account ${ACCOUNT:-unknown}, region ${REGION}"

STACK_NAME="${ARG_STACK:-$STACK_NAME}"
if [[ -z "$STACK_NAME" ]]; then
  if [[ "$INTERACTIVE" == "true" ]]; then
    echo
    echo "Stacks in ${REGION}:"
    ro_aws cloudformation list-stacks \
      --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE ROLLBACK_COMPLETE CREATE_FAILED DELETE_FAILED \
      --query 'StackSummaries[].[StackName,StackStatus]' --output text | sed 's/^/  /'
    echo
    STACK_NAME="$(ask "Stack name to tear down: " "")"
  fi
fi
[[ -n "$STACK_NAME" ]] || fail "No stack name. Pass --stack-name NAME (see --help)."

state_locate

stack_status_now() {
  local out
  # stderr is merged in on purpose: "does not exist" IS the answer we want,
  # and anything else is an error we must not read as "already gone".
  out="$(PROBE_MERGE_STDERR=1 ro_aws cloudformation describe-stacks \
          --stack-name "$STACK_NAME" --query 'Stacks[0].StackStatus' --output text)"
  out="$(first_line "$out")"
  if [[ "$out" =~ ^[A-Z][A-Z_]*$ ]]; then printf '%s' "$out"
  elif [[ "$out" == *"does not exist"* ]]; then printf 'MISSING'
  else printf 'unknown'
  fi
}

STACK_STATUS="$(stack_status_now)"
case "$STACK_STATUS" in
  MISSING)
    warn "Stack '${STACK_NAME}' does not exist in ${REGION}."
    note "Nothing to delete. Continuing to the sweep: a stack that is already"
    note "gone is exactly the case that leaves volumes behind."
    SWEEP_ONLY=true
    ;;
  unknown)
    fail "Could not read the status of '${STACK_NAME}' in ${REGION}. Refusing to
  guess: this script deletes things, so an unreadable stack is a stop, never an
  assumption. Check with:
    aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${REGION}"
    ;;
  *)
    ok "Stack '${STACK_NAME}' is ${STACK_STATUS}"
    ;;
esac

# =====================================================================
# Phase 2: build the attribution manifest (read-only)
#
# This runs BEFORE anything is deleted, and that ordering is the whole point.
# A volume that is still attached names its instance; a volume that has been
# detached names nothing at all, and no API will ever tell you which stack it
# came from. Capture the evidence while it exists, write it to the state file,
# and the sweep can be run days later and still be exact.
# =====================================================================
step "Phase 2: Build the attribution manifest (read-only)"

stack_resource_ids() {
  det_clean "$(ro_aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" \
    --query "StackResources[?ResourceType=='${1}'].PhysicalResourceId" --output text)"
}

# The traffic mirror sessions of this stack are attributed by TARGET alone:
# a session is ours iff it sends to one of SWEEP_MIRROR_TARGETS, the targets
# on this stack's collectors' ENIs (evidence class 4 below). Nothing else
# about a session is evidence:
#   - its filter is not. KVO names it cloudlens.mirror_filter.<vpc>, per VPC
#     and not per stack, so a second deploy tapping the same VPC has a filter
#     of that name too, or reuses ours. Counting its sessions as ours would
#     delete them, and the filter-in-use refusal that protects its filter
#     would then be gone as well;
#   - nor is where it sits or how it is tagged: the VPC holds the customer's
#     own sessions (to a GWLB endpoint or an NLB, their own filter), on the
#     very workload ENIs KVO taps;
#   - nor is a session id recorded by an earlier run. It may be gone since,
#     or repointed (ModifyTrafficMirrorSession) at somebody else's target, so
#     the list is rebuilt from AWS before it is shown and again just before
#     anything is deleted, and a recorded id that no longer sends to one of
#     our targets is named and left alone.
MIRROR_SESSION_Q='TrafficMirrorSessions[].[TrafficMirrorSessionId,NetworkInterfaceId,TrafficMirrorTargetId,TrafficMirrorFilterId]'

# mirror_sessions_for "TARGETS": the sessions that send to one of TARGETS,
# one line each: session-id<TAB>source-eni<TAB>target-id<TAB>filter-id.
# Every row AWS returns is checked again here against TARGETS before it
# counts. Read-only, and like every probe an empty answer means "none found
# or could not tell".
mirror_sessions_for() {
  local targets="$1" raw="" seen="" sid seni stgt sflt
  [[ -n "$targets" ]] || return 0
  raw="$(ro_aws ec2 describe-traffic-mirror-sessions \
    --filters "Name=traffic-mirror-target-id,Values=$(printf '%s' "$targets" | tr ' ' ',')" \
    --query "$MIRROR_SESSION_Q" --output text)"
  while IFS=$'\t' read -r sid seni stgt sflt; do
    [[ "${sid:-}" == tms-* ]] || continue
    in_list "$sid" "$seen" && continue
    [[ -n "${stgt:-}" ]] && in_list "$stgt" "$targets" || continue
    seen="${seen}${seen:+ }${sid}"
    printf '%s\t%s\t%s\t%s\n' "$sid" "${seni:-}" "${stgt:-}" "${sflt:-}"
  done <<< "$raw"
  return 0
}

# shared_filter_sessions "FILTERS" "TARGETS": the sessions that use one of
# FILTERS but send to a target outside TARGETS, same line format. Those are
# NOT ours; they are found only so the report can say why KVO's filter may
# have to stay. Read-only.
shared_filter_sessions() {
  local filters="$1" targets="$2" raw="" sid seni stgt sflt
  [[ -n "$filters" ]] || return 0
  raw="$(ro_aws ec2 describe-traffic-mirror-sessions \
    --filters "Name=traffic-mirror-filter-id,Values=$(printf '%s' "$filters" | tr ' ' ',')" \
    --query "$MIRROR_SESSION_Q" --output text)"
  while IFS=$'\t' read -r sid seni stgt sflt; do
    [[ "${sid:-}" == tms-* ]] || continue
    [[ -n "${stgt:-}" ]] && in_list "$stgt" "$targets" && continue
    printf '%s\t%s\t%s\t%s\n' "$sid" "${seni:-}" "${stgt:-}" "${sflt:-}"
  done <<< "$raw"
  return 0
}

# collect_mirror_sessions: REBUILD SWEEP_MIRROR_SESSIONS from what AWS says
# sends to one of SWEEP_MIRROR_TARGETS right now (details kept in
# MIRROR_SESSION_LINES for the report). Whatever the list held before, from a
# manifest or from an earlier call, and that AWS no longer attributes goes to
# MIRROR_SESSIONS_DROPPED: named, never deleted. Read-only.
collect_mirror_sessions() {
  local _ms _rest _before="$SWEEP_MIRROR_SESSIONS"
  SWEEP_MIRROR_SESSIONS=""; MIRROR_SESSIONS_DROPPED=""; MIRROR_SESSION_LINES=""; MIRROR_SHARED_LINES=""
  MIRROR_SESSION_LINES="$(mirror_sessions_for "$SWEEP_MIRROR_TARGETS")"
  while IFS=$'\t' read -r _ms _rest; do
    [[ -n "${_ms:-}" ]] || continue
    in_list "$_ms" "$SWEEP_MIRROR_SESSIONS" || SWEEP_MIRROR_SESSIONS="${SWEEP_MIRROR_SESSIONS}${SWEEP_MIRROR_SESSIONS:+ }${_ms}"
  done <<< "$MIRROR_SESSION_LINES"
  for _ms in $_before; do
    in_list "$_ms" "$SWEEP_MIRROR_SESSIONS" || MIRROR_SESSIONS_DROPPED="${MIRROR_SESSIONS_DROPPED}${MIRROR_SESSIONS_DROPPED:+ }${_ms}"
  done
  MIRROR_SHARED_LINES="$(shared_filter_sessions "$SWEEP_MIRROR_FILTERS" "$SWEEP_MIRROR_TARGETS")"
  return 0
}

# explain_dropped_sessions: one line per MIRROR_SESSIONS_DROPPED id saying
# what became of it, from AWS. Read-only; never a reason to delete anything.
explain_dropped_sessions() {
  local _ms _out _row _a _b _c _d
  for _ms in $MIRROR_SESSIONS_DROPPED; do
    _out="$(PROBE_MERGE_STDERR=1 ro_aws ec2 describe-traffic-mirror-sessions \
      --traffic-mirror-session-ids "$_ms" --query "$MIRROR_SESSION_Q" --output text)"
    _row="$(printf '%s\n' "$_out" | grep "^${_ms}[[:space:]]" | head -1 || true)"
    if [[ -n "$_row" ]]; then
      IFS=$'\t' read -r _a _b _c _d <<< "$_row"
      note "recorded mirror session ${_ms} now sends to ${_c:-?}, not to this stack's collectors: left alone"
    elif [[ "$_out" == *"InvalidTrafficMirrorSessionId.NotFound"* ]]; then
      note "recorded mirror session ${_ms} is already gone"
    else
      note "recorded mirror session ${_ms} no longer sends to this stack's collectors, or AWS could not be read: left alone"
    fi
  done
  return 0
}

if [[ "$STACK_STATUS" != "MISSING" ]]; then
  STACK_INSTANCE_IDS="$(tokens "$(stack_resource_ids 'AWS::EC2::Instance')")"
  STACK_SG_IDS="$(tokens "$(stack_resource_ids 'AWS::EC2::SecurityGroup')")"
  # Evidence class 2 only applies when the STACK created the VPC. A brownfield
  # deploy (--existing-vpc-id) points at a VPC the customer owns, and the other
  # things living in it are not ours to sweep.
  STACK_VPC_ID="$(tokens "$(stack_resource_ids 'AWS::EC2::VPC')")"
  STACK_VPC_ID="${STACK_VPC_ID%% *}"
  if [[ -n "$STACK_VPC_ID" ]]; then
    STACK_OWNS_VPC=true
  else
    # Brownfield. Record the VPC for reporting, but never for deleting.
    if [[ -n "$STACK_INSTANCE_IDS" ]]; then
      STACK_VPC_ID="$(det_clean "$(ro_aws ec2 describe-instances \
        --instance-ids ${STACK_INSTANCE_IDS} \
        --query 'Reservations[0].Instances[0].VpcId' --output text)")"
    fi
    STACK_OWNS_VPC=false
  fi
else
  # The stack is gone: fall back to what a previous run recorded. This is the
  # only source of attribution left, which is why phase 2 writes it early.
  STACK_INSTANCE_IDS="$(state_get SWEEP_INSTANCES 2>/dev/null || true)"
  STACK_VPC_ID="$(state_get SWEEP_VPC_ID 2>/dev/null || true)"
  [[ "$(state_get SWEEP_STACK_OWNS_VPC 2>/dev/null || true)" == "true" ]] && STACK_OWNS_VPC=true
  SWEEP_VOLUMES="$(state_get SWEEP_VOLUMES 2>/dev/null || true)"
  SWEEP_OWN_INSTANCES="$(state_get SWEEP_OWN_INSTANCES 2>/dev/null || true)"
  SWEEP_MIRROR_TARGETS="$(state_get SWEEP_MIRROR_TARGETS 2>/dev/null || true)"
  SWEEP_MIRROR_FILTERS="$(state_get SWEEP_MIRROR_FILTERS 2>/dev/null || true)"
  # Absent from a manifest written before sessions were recorded, and never
  # trusted as it stands when present. So AWS is asked now for the sessions
  # still sending to the recorded targets (the rule above the helpers), for
  # the report and the confirmation, and asked again in Phase 7 just before
  # the delete. A recorded id AWS no longer attributes is named, not deleted.
  SWEEP_MIRROR_SESSIONS="$(state_get SWEEP_MIRROR_SESSIONS 2>/dev/null || true)"
  collect_mirror_sessions
  [[ -z "$SWEEP_SGS" ]] && SWEEP_SGS="$(state_get SWEEP_SGS 2>/dev/null || true)"
  if [[ -n "$SWEEP_VOLUMES" || -n "$STACK_VPC_ID" ]]; then
    note "Using the manifest recorded by an earlier run: ${STATE_FILE}"
  else
    warn "No manifest for '${STACK_NAME}' and the stack is gone."
    note "Volumes are reported but NOT deletable: nothing in AWS ties a detached"
    note "volume back to a stack that no longer exists, and guessing is exactly"
    note "what this script refuses to do."
  fi
fi

if [[ -n "$STACK_VPC_ID" ]]; then
  if [[ "$STACK_OWNS_VPC" == "true" ]]; then
    ok "Stack VPC: ${STACK_VPC_ID} (created by the stack: in scope)"
  else
    warn "Stack VPC: ${STACK_VPC_ID} (pre-existing / brownfield: the VPC stays)"
    note "Only what this deploy and its KVO stamped in it is swept (evidence class 4"
    note "below); anything else in the VPC is reported, never deleted."
  fi
fi
[[ -n "$STACK_INSTANCE_IDS" ]] && ok "Stack instances: $(printf '%s' "$STACK_INSTANCE_IDS" | wc -w | tr -d ' ')"

# ---- volumes: recorded while still attached -------------------------
if [[ "$STACK_STATUS" != "MISSING" && -n "$STACK_INSTANCE_IDS" ]]; then
  SWEEP_VOLUMES="$(tokens "$(ro_aws ec2 describe-instances --instance-ids ${STACK_INSTANCE_IDS} \
    --query 'Reservations[].Instances[].BlockDeviceMappings[].Ebs.VolumeId' --output text)")"
fi
# Volumes that DID carry a stack tag (some resources do propagate) count as
# evidence class 3 and are merged in.
if [[ -n "$STACK_NAME" && "$STACK_STATUS" != "MISSING" ]]; then
  _tagged="$(tokens "$(ro_aws ec2 describe-volumes \
    --filters "Name=tag:aws:cloudformation:stack-name,Values=${STACK_NAME}" \
    --query 'Volumes[].VolumeId' --output text)")"
  for _v in $_tagged; do
    in_list "$_v" "$SWEEP_VOLUMES" || SWEEP_VOLUMES="${SWEEP_VOLUMES}${SWEEP_VOLUMES:+ }${_v}"
  done
fi

if [[ -n "$SWEEP_VOLUMES" ]]; then
  VOLUME_LINES="$(ro_aws ec2 describe-volumes --volume-ids ${SWEEP_VOLUMES} \
    --query 'Volumes[].[VolumeId,Size,VolumeType,State]' --output text)"
fi

# ---- security groups in the stack's own VPC -------------------------
if [[ "$STACK_OWNS_VPC" == "true" && -n "$STACK_VPC_ID" ]]; then
  _all_sgs="$(ro_aws ec2 describe-security-groups --filters "Name=vpc-id,Values=${STACK_VPC_ID}" \
    --query 'SecurityGroups[?GroupName!=`default`].[GroupId,GroupName]' --output text)"
  while IFS=$'\t' read -r _sgid _sgname; do
    [[ -n "${_sgid:-}" ]] || continue
    # SGs the stack owns are CloudFormation's to delete, not ours.
    in_list "$_sgid" "$STACK_SG_IDS" && continue
    SWEEP_SGS="${SWEEP_SGS}${SWEEP_SGS:+ }${_sgid}"
    eval "SGNAME_${_sgid//-/_}=\"\$_sgname\""
  done <<< "$_all_sgs"
fi

# ---- collector ASGs + launch templates ------------------------------
# KVO names its zone-tapping collector group cloudlens.collector.<vpc-id>.<az>,
# so the VPC id is carried in the ASG name itself: attribution is exact. The
# subnet check is the belt to that braces, for any group placed in this VPC
# under a different name.
if [[ "$STACK_OWNS_VPC" == "true" && -n "$STACK_VPC_ID" ]]; then
  _stack_subnets="$(tokens "$(ro_aws ec2 describe-subnets --filters "Name=vpc-id,Values=${STACK_VPC_ID}" \
    --query 'Subnets[].SubnetId' --output text)")"
  _asg_raw="$(ro_aws autoscaling describe-auto-scaling-groups \
    --query 'AutoScalingGroups[].[AutoScalingGroupName,VPCZoneIdentifier]' --output text)"
  while IFS=$'\t' read -r _asgname _asgsubnets; do
    [[ -n "${_asgname:-}" ]] || continue
    _match=false
    case "$_asgname" in *"$STACK_VPC_ID"*) _match=true ;; esac
    if [[ "$_match" != "true" && -n "${_asgsubnets:-}" && "$_asgsubnets" != "None" ]]; then
      for _s in $(printf '%s' "$_asgsubnets" | tr ',' ' '); do
        in_list "$_s" "$_stack_subnets" && { _match=true; break; }
      done
    fi
    [[ "$_match" == "true" ]] || continue
    SWEEP_ASGS="${SWEEP_ASGS}${SWEEP_ASGS:+ }${_asgname}"
    _lt="$(det_clean "$(ro_aws autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "$_asgname" \
      --query 'AutoScalingGroups[0].[LaunchTemplate.LaunchTemplateId,MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateId]' \
      --output text)")"
    for _id in $(tokens "$_lt"); do
      in_list "$_id" "$SWEEP_LTS" || SWEEP_LTS="${SWEEP_LTS}${SWEEP_LTS:+ }${_id}"
    done
  done <<< "$_asg_raw"

  # Launch templates whose NAME carries the VPC id, left behind by a group
  # that has already been deleted.
  _lt_named="$(ro_aws ec2 describe-launch-templates \
    --query 'LaunchTemplates[].[LaunchTemplateId,LaunchTemplateName]' --output text)"
  while IFS=$'\t' read -r _ltid _ltname; do
    [[ -n "${_ltid:-}" ]] || continue
    case "${_ltname:-}" in
      *"$STACK_VPC_ID"*) in_list "$_ltid" "$SWEEP_LTS" || SWEEP_LTS="${SWEEP_LTS}${SWEEP_LTS:+ }${_ltid}" ;;
    esac
  done <<< "$_lt_named"
fi

# ---- evidence class 4: what the deploy and its KVO stamped themselves ----
# Applies in ANY VPC, a customer's included. A brownfield teardown on
# 2026-10-07 left the capture host, two KVO collectors (ASG, launch template,
# mirror targets, filter), four security groups and two volumes behind in the
# customer VPC, because everything in that VPC had been treated as the
# customer's. These things carry the deploy's own marks, so they are
# attributed by mark, never by location:
#   instances tagged cloudlens:stack=<stack>     the capture host, test VMs
#   security groups named cloudlens-*-<stack>    collector and receiver groups
#   collector ASGs tagged cloudlens:ip=<THIS stack's vController private IP>
#   mirror targets on those collectors' ENIs, and the filter KVO named
#   cloudlens.mirror_filter.<vpc>, only when the collectors were ours
#   mirror sessions that send to one of those targets (never by filter)
if [[ "$STACK_STATUS" != "MISSING" ]]; then
  _rail_ids="$(tokens "$(ro_aws ec2 describe-instances \
      --filters "Name=tag:cloudlens:stack,Values=${STACK_NAME}" "Name=tag-key,Values=cloudlens:vpb-rail" \
      --query 'Reservations[].Instances[].InstanceId' --output text)")"
  for _i in $(tokens "$(ro_aws ec2 describe-instances \
      --filters "Name=tag:cloudlens:stack,Values=${STACK_NAME}" \
                "Name=instance-state-name,Values=running,pending,stopping,stopped" \
      --query 'Reservations[].Instances[].InstanceId' --output text)"); do
    in_list "$_i" "$STACK_INSTANCE_IDS" && continue
    in_list "$_i" "$_rail_ids" && continue          # the rail vPBs have their own sweep
    SWEEP_OWN_INSTANCES="${SWEEP_OWN_INSTANCES}${SWEEP_OWN_INSTANCES:+ }${_i}"
  done
  if [[ -n "$STACK_VPC_ID" ]]; then
    while IFS=$'\t' read -r _sgid _sgname; do
      [[ -n "${_sgid:-}" ]] || continue
      in_list "$_sgid" "$STACK_SG_IDS" && continue
      in_list "$_sgid" "$SWEEP_SGS" && continue
      SWEEP_SGS="${SWEEP_SGS}${SWEEP_SGS:+ }${_sgid}"
      eval "SGNAME_${_sgid//-/_}=\"\$_sgname\""
    done <<< "$(ro_aws ec2 describe-security-groups \
      --filters "Name=vpc-id,Values=${STACK_VPC_ID}" "Name=group-name,Values=cloudlens-*-${STACK_NAME}" \
      --query 'SecurityGroups[].[GroupId,GroupName]' --output text)"
  fi
  _vc_id="$(det_clean "$(ro_aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" \
    --query "StackResources[?LogicalResourceId=='VcontrollerInstance'].PhysicalResourceId | [0]" --output text)")"
  _vc_ip=""
  [[ -n "$_vc_id" ]] && _vc_ip="$(det_clean "$(ro_aws ec2 describe-instances --instance-ids "$_vc_id" \
    --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)")"
  _coll_ids=""
  if [[ -n "$_vc_ip" ]]; then
    while IFS=$'\t' read -r _asgname _lt1 _lt2 _members; do
      [[ -n "${_asgname:-}" ]] || continue
      in_list "$_asgname" "$SWEEP_ASGS" || SWEEP_ASGS="${SWEEP_ASGS}${SWEEP_ASGS:+ }${_asgname}"
      for _id in $(tokens "$_lt1 $_lt2"); do
        in_list "$_id" "$SWEEP_LTS" || SWEEP_LTS="${SWEEP_LTS}${SWEEP_LTS:+ }${_id}"
      done
      for _m in $(printf '%s' "${_members:-}" | tr ',' ' '); do
        [[ "$_m" == "None" ]] && continue
        _coll_ids="${_coll_ids}${_coll_ids:+ }${_m}"
      done
    done <<< "$(ro_aws autoscaling describe-auto-scaling-groups \
      --query "AutoScalingGroups[?Tags[?Key=='cloudlens:ip' && Value=='${_vc_ip}']].[AutoScalingGroupName,LaunchTemplate.LaunchTemplateId,MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateId,join(',',Instances[].InstanceId)]" \
      --output text)"
  fi
  if [[ -n "$_coll_ids" || -n "$SWEEP_OWN_INSTANCES" ]]; then
    for _v in $(tokens "$(ro_aws ec2 describe-instances --instance-ids ${_coll_ids} ${SWEEP_OWN_INSTANCES} \
        --query 'Reservations[].Instances[].BlockDeviceMappings[].Ebs.VolumeId' --output text)"); do
      in_list "$_v" "$SWEEP_VOLUMES" || SWEEP_VOLUMES="${SWEEP_VOLUMES}${SWEEP_VOLUMES:+ }${_v}"
    done
  fi
  if [[ -n "$_coll_ids" ]]; then
    _coll_enis="$(tokens "$(ro_aws ec2 describe-instances --instance-ids ${_coll_ids} \
        --query 'Reservations[].Instances[].NetworkInterfaces[].NetworkInterfaceId' --output text)")"
    while IFS=$'\t' read -r _tmt _teni; do
      [[ -n "${_tmt:-}" ]] || continue
      in_list "${_teni:-}" "$_coll_enis" || continue
      SWEEP_MIRROR_TARGETS="${SWEEP_MIRROR_TARGETS}${SWEEP_MIRROR_TARGETS:+ }${_tmt}"
    done <<< "$(ro_aws ec2 describe-traffic-mirror-targets \
      --query 'TrafficMirrorTargets[].[TrafficMirrorTargetId,NetworkInterfaceId]' --output text)"
    [[ -n "$STACK_VPC_ID" ]] && SWEEP_MIRROR_FILTERS="$(tokens "$(ro_aws ec2 describe-traffic-mirror-filters \
      --filters "Name=tag:Name,Values=cloudlens.mirror_filter.${STACK_VPC_ID}" \
      --query 'TrafficMirrorFilters[].TrafficMirrorFilterId' --output text)")"
    # The sessions KVO cut to those targets. They do NOT go with the
    # collectors: KVO cuts one per tapped WORKLOAD ENI (source = the
    # workload's ENI, target = the collector's target, filter = KVO's), the
    # workload ENIs outlive the stack, and the KVO that would remove the
    # sessions is deleted with it. Left in place they hold the target and the
    # filter (AWS refuses both deletes, TrafficMirrorTargetInUse, seen live
    # 2026-10-16), and worse: AWS mirrors a packet to the first matching
    # session on an ENI only, so a stale session to a dead target swallows
    # the copies a redeployed KVO's new sessions should get. Attributed by
    # target only: the filter's name is per VPC, not per stack.
    collect_mirror_sessions
  fi
  _n_own="$(printf '%s' "$SWEEP_OWN_INSTANCES" | wc -w | tr -d ' ')"
  _n_coll="$(printf '%s' "$_coll_ids" | wc -w | tr -d ' ')"
  if (( _n_own + _n_coll > 0 )); then
    ok "Stamped by this deploy: ${_n_own} instance(s), ${_n_coll} collector(s), $(printf '%s' "$SWEEP_MIRROR_TARGETS" | wc -w | tr -d ' ') mirror target(s), $(printf '%s' "$SWEEP_MIRROR_SESSIONS" | wc -w | tr -d ' ') mirror session(s)"
  fi
fi

# ---- is there a KVO in this stack? ----------------------------------
if [[ "$STACK_STATUS" != "MISSING" ]]; then
  _kvo_param="$(det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='DeployKVO'].ParameterValue | [0]" --output text)")"
  # This decides whether the LICENCE gate runs. Getting it wrong in the "no"
  # direction strands the activation quantity permanently: the counts can be
  # released only from a LIVE KVO (Product Licensing > Deactivate, or the
  # licensing API's deactivate operation), and the gate is the last moment
  # that is possible. So every branch below fails CLOSED, assuming a KVO is
  # present whenever it cannot prove otherwise.
  case "$(to_lower "${_kvo_param:-}")" in
    yes) HAS_KVO=true ;;
    no)  HAS_KVO=false ;;   # the stack itself says so; KvoInstance is conditional
    *)
      # Empty means the probe FAILED or the template predates the parameter,
      # and probe cannot tell those apart: it returns "" for a timeout, a
      # throttle and expired credentials alike. Ask the stack's own manifest by
      # LOGICAL id instead. A *kvo* Name filter would be wrong twice over: tag
      # filters are case sensitive, and KvoName is operator settable, so
      # KvoName=KVO-prod matches nothing and yields a confident false.
      _kvo_res="$(det_clean "$(ro_aws cloudformation describe-stack-resources \
        --stack-name "$STACK_NAME" \
        --query "StackResources[?LogicalResourceId=='KvoInstance'].PhysicalResourceId | [0]" \
        --output text)")"
      _kvo_n="$(det_clean "$(ro_aws cloudformation describe-stack-resources \
        --stack-name "$STACK_NAME" --query 'length(StackResources)' --output text)")"
      if [[ ! "$_kvo_n" =~ ^[0-9]+$ ]]; then
        warn "Could not read ${STACK_NAME}'s resources to check for a KVO."
        note "Assuming there IS one: a licence warning nobody needed costs a"
        note "confirmation, a missed one strands the licence for good."
        HAS_KVO=true
      elif [[ -n "$_kvo_res" ]]; then
        HAS_KVO=true
      fi ;;
  esac
else
  [[ "$(state_get DEPLOY_KVO 2>/dev/null || true)" == "true" ]] && HAS_KVO=true
fi

# ---- record the manifest BEFORE anything is deleted -----------------
if [[ "$AUDIT_ONLY" != "true" && "$DRY_RUN" != "true" && "$STACK_STATUS" != "MISSING" ]]; then
  if [[ ! -f "$STATE_FILE" ]]; then
    ( umask 077; : > "$STATE_FILE" ) 2>/dev/null || STATE_FILE=""
  fi
  state_set SWEEP_RECORDED_AT "$(date -u +%FT%TZ)"
  state_set SWEEP_VPC_ID "$STACK_VPC_ID"
  state_set SWEEP_STACK_OWNS_VPC "$STACK_OWNS_VPC"
  state_set SWEEP_INSTANCES "$STACK_INSTANCE_IDS"
  state_set SWEEP_VOLUMES "$SWEEP_VOLUMES"
  state_set SWEEP_ASGS "$SWEEP_ASGS"
  state_set SWEEP_LTS "$SWEEP_LTS"
  state_set SWEEP_SGS "$SWEEP_SGS"
  state_set SWEEP_OWN_INSTANCES "$SWEEP_OWN_INSTANCES"
  state_set SWEEP_MIRROR_TARGETS "$SWEEP_MIRROR_TARGETS"
  state_set SWEEP_MIRROR_FILTERS "$SWEEP_MIRROR_FILTERS"
  state_set SWEEP_MIRROR_SESSIONS "$SWEEP_MIRROR_SESSIONS"
  [[ -n "$STATE_FILE" ]] && note "Manifest recorded: ${STATE_FILE}"
elif [[ "$DRY_RUN" == "true" && "$STACK_STATUS" != "MISSING" ]]; then
  dryrun_say "would record the attribution manifest to ${STATE_FILE}"
fi

# =====================================================================
# Phase 3: report what was found, with GB and monthly cost
# =====================================================================
step "Phase 3: What this stack has left loose"

_gb_attributed=0
_cost_attributed=0
if [[ -n "$VOLUME_LINES" ]]; then
  echo "  EBS volumes belonging to this stack:"
  while IFS=$'\t' read -r _vid _vsize _vtype _vstate; do
    [[ -n "${_vid:-}" ]] || continue
    printf '    %-24s %6s GB  %-9s %s\n' "$_vid" "$_vsize" "$_vtype" "$_vstate"
    if [[ "${_vstate:-}" == "available" || "$STACK_STATUS" != "MISSING" ]]; then
      _gb_attributed=$(( _gb_attributed + ${_vsize:-0} ))
      _cost_attributed="$(awk -v c="$_cost_attributed" -v s="${_vsize:-0}" -v r="$(ebs_rate "${_vtype:-gp2}")" \
        'BEGIN { printf "%.4f", c + (s * r) }')"
    fi
  done <<< "$VOLUME_LINES"
fi
SWEEP_GB="$_gb_attributed"
SWEEP_COST="$(money "$_cost_attributed")"

if (( SWEEP_GB > 0 )); then
  echo
  echo -e "  ${C_BOLD}${SWEEP_GB} GB${C_RESET} of EBS, about ${C_BOLD}\$${SWEEP_COST}/month${C_RESET} if it is left behind."
  note "Deleting the stack does NOT delete these: the Marketplace AMIs set"
  note "DeleteOnTermination=false on their root volumes."
else
  ok "No EBS volumes attributable to this stack."
fi

if [[ -n "$SWEEP_SGS" ]]; then
  echo
  echo "  Non-stack security groups inside ${STACK_VPC_ID}:"
  for _sg in $SWEEP_SGS; do
    eval "_n=\"\${SGNAME_${_sg//-/_}:-}\""
    printf '    %-24s %s\n' "$_sg" "${_n:-}"
  done
  note "Each one of these blocks the VPC delete, which is what puts the stack"
  note "into DELETE_FAILED with an error that names nothing useful."
fi

if [[ -n "$SWEEP_ASGS" ]]; then
  echo
  echo "  Auto Scaling groups placed in this stack's VPC:"
  for _a in $SWEEP_ASGS; do echo "    $_a"; done
  note "KVO creates these outside CloudFormation. Left behind, the group"
  note "rebuilds its instance every time someone terminates it."
fi
if [[ -n "$SWEEP_LTS" ]]; then
  echo
  echo "  Launch templates for those groups:"
  for _l in $SWEEP_LTS; do echo "    $_l"; done
fi

if [[ -n "$SWEEP_MIRROR_SESSIONS" ]]; then
  echo
  echo "  Traffic mirror sessions to this stack's collectors (one per tapped workload ENI):"
  for _ms in $SWEEP_MIRROR_SESSIONS; do
    _meni=""; _mtgt=""
    while IFS=$'\t' read -r _a _b _c _d; do
      [[ "${_a:-}" == "$_ms" ]] && { _meni="${_b:-}"; _mtgt="${_c:-}"; break; }
    done <<< "$MIRROR_SESSION_LINES"
    if [[ -n "$_meni" ]]; then
      printf '    %-26s source %s, target %s\n' "$_ms" "$_meni" "${_mtgt:-?}"
    else
      printf '    %s\n' "$_ms"
    fi
  done
  note "Deleting a session stops only the mirror COPIES sent to the collector."
  note "The workload's production traffic never passes through a session and is"
  note "not affected. Left in place, a session blocks the delete of its target and"
  note "filter, and since AWS sends each packet only to the first matching session"
  note "on an ENI, it keeps the copies a redeployed KVO's new sessions should get."
fi
if [[ -n "$MIRROR_SESSIONS_DROPPED" ]]; then
  echo
  echo "  Mirror sessions recorded by an earlier run that no longer send to this stack's collectors:"
  explain_dropped_sessions
fi
if [[ -n "$MIRROR_SHARED_LINES" ]]; then
  echo
  echo "  Traffic mirror sessions that use this VPC's KVO filter but send to a target"
  echo "  this stack does not own (another deployment's or the customer's), left alone:"
  while IFS=$'\t' read -r _a _b _c _d; do
    [[ -n "${_a:-}" ]] || continue
    printf '    %-26s source %s, target %s, filter %s\n' "$_a" "${_b:-?}" "${_c:-?}" "${_d:-?}"
  done <<< "$MIRROR_SHARED_LINES"
  note "A filter that one of them still uses stays: it is shared, not ours alone."
fi

# In audit mode also surface volumes that are loose in the region but NOT
# attributable to this stack. Reported so the cost is visible, never deletable:
# in a shared account "available" is not ownership.
if [[ "$AUDIT_ONLY" == "true" ]]; then
  _loose="$(ro_aws ec2 describe-volumes --filters "Name=status,Values=available" \
    --query 'Volumes[].[VolumeId,Size,VolumeType]' --output text)"
  _lg=0; _lc=0; _ln=0
  if [[ -n "$_loose" ]]; then
    while IFS=$'\t' read -r _vid _vsize _vtype; do
      [[ -n "${_vid:-}" ]] || continue
      in_list "$_vid" "$SWEEP_VOLUMES" && continue
      _ln=$(( _ln + 1 )); _lg=$(( _lg + ${_vsize:-0} ))
      _lc="$(awk -v c="$_lc" -v s="${_vsize:-0}" -v r="$(ebs_rate "${_vtype:-gp2}")" \
        'BEGIN { printf "%.4f", c + (s * r) }')"
    done <<< "$_loose"
  fi
  UNATTRIBUTED_COUNT="$_ln"; UNATTRIBUTED_GB="$_lg"; UNATTRIBUTED_COST="$(money "$_lc")"
  echo
  if (( UNATTRIBUTED_COUNT > 0 )); then
    warn "${UNATTRIBUTED_COUNT} other unattached volumes in ${REGION}: ${UNATTRIBUTED_GB} GB, about \$${UNATTRIBUTED_COST}/month."
    note "NOT attributable to '${STACK_NAME}', so this script will never delete"
    note "them. Reported because that number is the reason you are reading this."
    note "List them:  aws ec2 describe-volumes --region ${REGION} \\"
    note "              --filters Name=status,Values=available \\"
    note "              --query 'Volumes[].[VolumeId,Size,CreateTime]' --output table"
  else
    ok "No other unattached volumes in ${REGION}."
  fi
fi

if [[ "$AUDIT_ONLY" == "true" ]]; then
  echo
  step "Audit complete"
  echo "  Stack:                 ${STACK_NAME} (${STACK_STATUS})"
  echo "  Attributable EBS:      ${SWEEP_GB} GB, about \$${SWEEP_COST}/month"
  echo "  Non-stack SGs in VPC:  $(printf '%s' "$SWEEP_SGS" | wc -w | tr -d ' ')"
  echo "  Orphan ASGs:           $(printf '%s' "$SWEEP_ASGS" | wc -w | tr -d ' ')"
  echo "  Orphan launch temps:   $(printf '%s' "$SWEEP_LTS" | wc -w | tr -d ' ')"
  echo "  Mirror sessions:       $(printf '%s' "$SWEEP_MIRROR_SESSIONS" | wc -w | tr -d ' ') (to this stack's collectors)"
  echo "  Unattributed EBS:      ${UNATTRIBUTED_GB} GB, about \$${UNATTRIBUTED_COST}/month (reported only)"
  echo
  echo "  Nothing was deleted. To actually tear this down:"
  echo "    bash deploy/teardown-stack.sh --stack-name ${STACK_NAME} --region ${REGION}"
  SCRIPT_DONE=true
  exit 0
fi

# =====================================================================
# Phase 4a: release the KVO's licences, while there is still a KVO
#
# Activation codes are bound to the KVO host they were activated on. While
# that host is alive the counts can be returned (the licensing API's
# deactivate operation: proven, 20 counts recovered); once the stack is
# deleted they cannot, ever. This phase is the last moment that is possible,
# so it lists what the KVO holds and offers to release all of it, with
# scripts/kvo_license.py doing the API work.
#
# It runs AFTER the teardown itself has been confirmed (Phase 4), so the
# licences are only ever stripped from a KVO the operator has already
# committed to destroying: a release followed by a "no" at the delete used
# to leave a running KVO with nothing on it.
#
# Fail CLOSED. The only thing that waives the licence gate in Phase 4b is
# kvo_license.py exiting 0, which it does only when every deactivate
# reported SUCCESS and the KVO then reports no licence left (or the KVO
# answered the list with nothing on it to begin with). An unreachable KVO,
# a refused password, a pending EULA, an operation that failed or ran out
# of time, a list that could not be read: every one of those is reported
# with its reason and the gate runs unchanged.
#
# Every call here is bounded: the list by lic_run's kill timer, the release
# by kvo_license.py's own --timeout. Nothing in this phase can hang the
# teardown, and nothing in it can delete anything.
# =====================================================================

# The KVO's address, where the stack can say. The KvoAddress output is the
# stack's own statement of how the KVO is reached: the Elastic IP when the
# stack has one, else the private IP. When the output cannot be read (an
# older template, a throttled probe) the KvoInstance resource itself is
# asked, public address first, and last the instance carrying the Name tag
# the template writes: the KvoName parameter, else <stack>-kvo. Prints the
# address or nothing; never fails.
kvo_address() {
  local a="" iid="" nm=""
  if [[ -n "$ARG_KVO_ADDRESS" ]]; then printf '%s' "$ARG_KVO_ADDRESS"; return 0; fi
  a="$(det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='KvoAddress'].OutputValue | [0]" --output text)" || true)"
  if [[ -z "$a" ]]; then
    iid="$(det_clean "$(ro_aws cloudformation describe-stack-resources --stack-name "$STACK_NAME" \
            --query "StackResources[?LogicalResourceId=='KvoInstance'].PhysicalResourceId | [0]" \
            --output text)" || true)"
    if [[ -n "$iid" ]]; then
      a="$(tokens "$(ro_aws ec2 describe-instances --instance-ids "$iid" \
            --query 'Reservations[0].Instances[0].[PublicIpAddress,PrivateIpAddress]' --output text)" || true)"
      a="${a%% *}"
    fi
  fi
  if [[ -z "$a" ]]; then
    nm="$(det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
            --query "Stacks[0].Parameters[?ParameterKey=='KvoName'].ParameterValue | [0]" --output text)" || true)"
    [[ -n "$nm" ]] || nm="${STACK_NAME}-kvo"
    a="$(tokens "$(ro_aws ec2 describe-instances \
            --filters "Name=tag:Name,Values=${nm}" "Name=instance-state-name,Values=running" \
            --query 'Reservations[0].Instances[0].[PublicIpAddress,PrivateIpAddress]' --output text)" || true)"
    a="${a%% *}"
  fi
  [[ "$a" =~ ^[A-Za-z0-9.:-]+$ ]] || a=""
  printf '%s' "$a"
  return 0
}

# lic_py_has_modes FILE: whether FILE is a whole kvo_license.py that carries
# the release modes. Two checks: the --release-all marker (argparse would
# refuse --list without it and the run would read that as an unreachable
# KVO), and the entry point in the last three lines, so a download cut
# short cannot pass as the script: a truncated copy can still parse, and
# then stops somewhere in the middle of a release.
lic_py_has_modes() {
  grep -q -- '--release-all' "$1" 2>/dev/null \
    && tail -n 3 "$1" 2>/dev/null | grep -q 'sys.exit(main())'
}

# scripts/kvo_license.py, wherever this run can get it: a checkout next to
# this script, the clone deploy-stack.sh makes under $HOME, the current
# directory, and failing all of those the raw file from the repo, fetched
# over TLS into a temp dir and kept for this run. A copy that predates the
# release modes is skipped and said so. A fetched copy is checked to be
# whole, to carry the modes and to parse as Python before it is used, and
# LIC_FIND_WHY says which of those it failed. Sets LIC_PY, or leaves it
# empty. Never fails.
LIC_FIND_WHY=""
find_kvo_license_py() {
  local cand="" tmp=""
  LIC_PY=""
  LIC_FIND_WHY=""
  if [[ -n "${CLOUDLENS_KVO_LICENSE_PY:-}" ]]; then
    # An explicit override is honoured or reported, never quietly replaced
    # by some other copy.
    if [[ -f "$CLOUDLENS_KVO_LICENSE_PY" ]]; then
      LIC_PY="$CLOUDLENS_KVO_LICENSE_PY"
    else
      LIC_FIND_WHY="CLOUDLENS_KVO_LICENSE_PY names a file that does not exist"
    fi
    return 0
  fi
  for cand in "$SCRIPT_DIR/../scripts/kvo_license.py" "$HOME/${REPO_NAME}/scripts/kvo_license.py" \
              "$PWD/scripts/kvo_license.py"; do
    [[ -f "$cand" ]] || continue
    if ! lic_py_has_modes "$cand"; then
      note "skipping ${cand}: it predates the release modes (or is not the whole script)"
      continue
    fi
    LIC_PY="$cand"
    return 0
  done
  if ! command -v curl >/dev/null 2>&1; then
    LIC_FIND_WHY="curl is not available to fetch it"
    return 0
  fi
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/cloudlens-teardown-lic.XXXXXX" 2>/dev/null || true)"
  if [[ -z "$tmp" ]]; then
    LIC_FIND_WHY="no temp dir could be made to fetch it into"
    return 0
  fi
  LIC_CACHE_DIR="$tmp"
  note "scripts/kvo_license.py is not on this machine; fetching it from ${REPO_RAW}"
  if ! curl -fsSL --proto '=https' --max-time 30 -o "$tmp/kvo_license.py" "${REPO_RAW}/scripts/kvo_license.py" 2>/dev/null; then
    LIC_FIND_WHY="it could not be fetched from ${REPO_RAW}"
  elif ! grep -q -- '--release-all' "$tmp/kvo_license.py" 2>/dev/null; then
    LIC_FIND_WHY="the copy at ${REPO_RAW} predates the release modes (no --release-all)"
  elif ! tail -n 3 "$tmp/kvo_license.py" 2>/dev/null | grep -q 'sys.exit(main())'; then
    LIC_FIND_WHY="the fetched copy is not the whole script (its last lines are not the entry point)"
  elif ! python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$tmp/kvo_license.py" 2>/dev/null; then
    LIC_FIND_WHY="the fetched copy does not parse as Python"
  else
    LIC_PY="$tmp/kvo_license.py"
  fi
  return 0
}

# lic_run LIMIT OUTFILE cmd...: run cmd with stdout and stderr in OUTFILE and
# stdin closed, and kill it after LIMIT seconds. LIC_RC is the command's exit
# status, or 124 when it was killed at the bound. Same contract as probe: it
# never fails and never hangs, so a KVO that stopped answering costs a bounded
# wait and a warning, never the run.
lic_run() {
  local limit="$1" out="$2" pid="" ticks=0
  shift 2
  LIC_RC=0
  "$@" >"$out" 2>&1 </dev/null &
  pid=$!
  while kill -0 "$pid" 2>/dev/null && (( ticks < limit )); do
    sleep 1
    ticks=$((ticks+1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    LIC_RC=124
    return 0
  fi
  wait "$pid" 2>/dev/null && LIC_RC=0 || LIC_RC=$?
  return 0
}

# What to do when the release did not happen or did not finish clean: the
# gate in Phase 4 runs, and the operator is told the two ways back.
lic_fallback_note() {
  note "Nothing was released. The licence gate below runs as it always has."
  note "To release first: re-run this teardown once the cause is fixed and"
  note "answer yes to the release (or pass --release-licences), or deactivate"
  note "each code in the KVO UI > Settings > Product Licensing, then re-run."
}

# =====================================================================
# Phase 4: confirm the teardown
#
# Asked BEFORE the licences are touched, on purpose. Phase 4a strips the
# KVO of its licences, and a release followed by a "no" here would leave
# a running KVO with nothing on it. So the operator commits to destroying
# the stack first; only then is the KVO in it emptied, and the one
# question left after that is the licence-loss gate (Phase 4b), which
# runs only when the release did not leave the KVO clear.
# =====================================================================
step "Phase 4: Confirm the teardown"
echo
echo "  About to delete, in ${REGION}:"
[[ "$SWEEP_ONLY" != "true" ]] && echo "    - CloudFormation stack ${STACK_NAME} (${STACK_STATUS})"
(( SWEEP_GB > 0 )) && echo "    - $(printf '%s' "$SWEEP_VOLUMES" | wc -w | tr -d ' ') EBS volumes, ${SWEEP_GB} GB, about \$${SWEEP_COST}/month"
[[ -n "$SWEEP_SGS" ]]  && echo "    - $(printf '%s' "$SWEEP_SGS" | wc -w | tr -d ' ') non-stack security groups in ${STACK_VPC_ID}"
[[ -n "$SWEEP_ASGS" ]] && echo "    - $(printf '%s' "$SWEEP_ASGS" | wc -w | tr -d ' ') Auto Scaling groups"
[[ -n "$SWEEP_LTS" ]]  && echo "    - $(printf '%s' "$SWEEP_LTS" | wc -w | tr -d ' ') launch templates"
if [[ -n "$SWEEP_MIRROR_SESSIONS$SWEEP_MIRROR_TARGETS$SWEEP_MIRROR_FILTERS" ]]; then
  echo "    - traffic mirroring: $(printf '%s' "$SWEEP_MIRROR_SESSIONS" | wc -w | tr -d ' ') session(s) (${SWEEP_MIRROR_SESSIONS:-none found}), then"
  echo "      $(printf '%s' "$SWEEP_MIRROR_TARGETS" | wc -w | tr -d ' ') target(s) and $(printf '%s' "$SWEEP_MIRROR_FILTERS" | wc -w | tr -d ' ') filter(s). The sessions sending to those targets"
  echo "      are looked up again just before the delete, and only those go; a"
  echo "      filter another session still uses is left in place. Deleting a"
  echo "      session stops only the mirror copies; the workloads' production"
  echo "      traffic is unaffected."
fi
if [[ "$HAS_KVO" == "true" && "$SWEEP_ONLY" != "true" ]]; then
  echo "    - the KVO in that stack, with every licence still activated on it. The"
  echo "      next step offers to release them first; whatever is not released is"
  echo "      stranded for good when the KVO is deleted."
fi
echo

if [[ "$DRY_RUN" == "true" ]]; then
  dryrun_say "a real run would ask for confirmation here (or require --yes)"
elif [[ "$INTERACTIVE" == "true" ]]; then
  ask_yn "  Proceed with the teardown? [y/N]: " "n" || fail "Aborted. Nothing was deleted, and no licence was released."
elif [[ "$ASSUME_YES" == "true" ]]; then
  ok "Proceeding (--yes)."
else
  fail "No terminal to confirm on and --yes was not given, so nothing was deleted.
  Re-run with --yes to confirm, or with --orphans to see what is loose without
  deleting anything."
fi

if [[ "$HAS_KVO" == "true" && "$SWEEP_ONLY" != "true" ]]; then
  step "Phase 4a: Release KVO licences"
  echo "  This stack has a KVO. The licence counts activated on it can be returned"
  echo "  to your entitlement NOW, while the KVO is alive, and never after it is"
  echo "  deleted. This script can release them for you (the licensing API's"
  echo "  deactivate operation, what Settings > Product Licensing > Deactivate"
  echo "  licenses does), and asks before it does."
  echo
  KVO_ADDR="$(kvo_address)"
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "KVO address: ${KVO_ADDR:-not found (KvoAddress output, KvoInstance, Name tag all empty)}"
    dryrun_say "would run scripts/kvo_license.py --list against it, then offer to release every"
    dryrun_say "licence it holds with --release-all (asked on a terminal, --release-licences without one)"
    dryrun_say "a real run skips the licence-loss gate below only when the release leaves the KVO clear"
    dryrun_say "nothing is called on the KVO in a dry run"
  elif [[ -z "$KVO_ADDR" ]]; then
    warn "Could not find the KVO's address: the stack's KvoAddress output, its"
    warn "KvoInstance resource and the instance Name tag all came back empty."
    note "Pass it with --kvo-address ADDR (its public IP, or private from inside the VPC)."
    lic_fallback_note
  elif ! command -v python3 >/dev/null 2>&1; then
    warn "python3 is not available here, and the release runs through scripts/kvo_license.py."
    lic_fallback_note
  else
    find_kvo_license_py
    if [[ -z "$LIC_PY" ]]; then
      warn "No usable scripts/kvo_license.py: ${LIC_FIND_WHY:-none found on this machine}."
      lic_fallback_note
    else
      # Credentials: flags, then the deploy's env names, then a prompt with the
      # KVO's defaults offered. The password is read without echo, passed to
      # the script through the environment (never a command line), and never
      # written anywhere: not the state file, not a log.
      KVO_USER="${ARG_KVO_USER:-${CLOUDLENS_KVO_ADMIN_USER:-}}"
      KVO_PASS="${ARG_KVO_PASS:-${CLOUDLENS_KVO_ADMIN_PASS:-}}"
      if [[ "$INTERACTIVE" == "true" ]]; then
        echo "  KVO at ${KVO_ADDR}. Its admin login is needed to list and release the licences."
        if [[ -z "$KVO_USER" ]]; then
          KVO_USER="$(ask "  KVO admin user [admin]: " "admin")"
        fi
        if [[ -z "$KVO_PASS" ]]; then
          read -rsp "  KVO admin password [admin]: " KVO_PASS || true
          echo
          [[ -n "$KVO_PASS" ]] || KVO_PASS="admin"
        fi
      fi
      [[ -n "$KVO_USER" ]] || KVO_USER="admin"
      [[ -n "$KVO_PASS" ]] || KVO_PASS="admin"
      export CLOUDLENS_KVO_ADMIN_PASS="$KVO_PASS"
      LIC_ARGS=(--kvo "$KVO_ADDR" --user "$KVO_USER" --password-env CLOUDLENS_KVO_ADMIN_PASS
                --insecure --http-timeout "$KVO_HTTP_TIMEOUT")

      _lic_out="$(mktemp "${TMPDIR:-/tmp}/cloudlens-teardown-lic.XXXXXX" 2>/dev/null || true)"
      if [[ -z "$_lic_out" ]]; then
        warn "Could not create a temp file for the licence list."
        lic_fallback_note
      else
        note "Listing the licences on KVO ${KVO_ADDR} (bounded: ${KVO_HTTP_TIMEOUT}s per call)..."
        # token + list, each bounded by the script, and the whole thing by
        # the kill timer in case the KVO answers nothing at all
        lic_run $(( KVO_HTTP_TIMEOUT * 3 + 5 )) "$_lic_out" python3 "$LIC_PY" "${LIC_ARGS[@]}" --list --json
        # The count is the JSON object on the last line (see LIC_COUNT_PARSE).
        # Anything but a whole number from that parse is "could not tell",
        # fail closed: nothing is released on it and the gate runs. The
        # lines before it are the script's own report, shown as they are.
        _lic_n="$(tail -n 1 "$_lic_out" 2>/dev/null | python3 -c "$LIC_COUNT_PARSE" 2>/dev/null || true)"
        if [[ "$_lic_n" =~ ^[0-9]+$ ]]; then
          sed '$d' "$_lic_out" 2>/dev/null | sed 's/^/    /' || true
        else
          _lic_n=""
          sed 's/^/    /' "$_lic_out" 2>/dev/null || true
        fi
        rm -f "$_lic_out" 2>/dev/null || true
        _lic_out=""

        case "$LIC_RC" in
          0)
            if [[ "${_lic_n:-}" == "0" ]]; then
              ok "The KVO holds no licences. Nothing to release, nothing will be stranded."
              LIC_RELEASED=true
              LIC_WHY="held nothing"
            elif [[ -z "${_lic_n:-}" ]]; then
              warn "Could not tell how many licences the KVO holds: kvo_license.py's summary"
              warn "line did not parse (its output is above). Not releasing on a guess."
              lic_fallback_note
            else
              _go=false
              if [[ "$INTERACTIVE" == "true" ]]; then
                # Default yes: releasing is the safe direction. The counts go
                # back to the entitlement and can be activated again anywhere.
                ask_yn "  Release all ${_lic_n} licences from this KVO now? [Y/n]: " "y" && _go=true
              elif [[ "$RELEASE_LICENCES" == "true" ]]; then
                note "Releasing all ${_lic_n} licences (--release-licences)."
                _go=true
              else
                note "No terminal to ask on and no --release-licences: not releasing."
                note "Pass --release-licences to release them without a prompt."
              fi
              if [[ "$_go" == "true" ]]; then
                note "Each deactivate goes to the Keysight licensing backend and can take up"
                note "to a minute; progress is printed per licence. Bounded: ${KVO_RELEASE_TIMEOUT}s overall."
                _rel_rc=0
                python3 "$LIC_PY" "${LIC_ARGS[@]}" --release-all --timeout "$KVO_RELEASE_TIMEOUT" || _rel_rc=$?
                case "$_rel_rc" in
                  0)
                    ok "All ${_lic_n} licences released. The KVO reports none left: nothing will be stranded."
                    LIC_RELEASED=true
                    LIC_WHY="released" ;;
                  3)
                    warn "The release did not leave the KVO clear (the reasons are above): an"
                    warn "operation failed or its outcome is unknown, or licences remain."
                    lic_fallback_note ;;
                  6)
                    warn "The KVO refused the login or stopped answering during the release."
                    lic_fallback_note ;;
                  *)
                    warn "kvo_license.py exited ${_rel_rc} during the release."
                    lic_fallback_note ;;
                esac
              else
                lic_fallback_note
              fi
            fi ;;
          6)
            warn "KVO ${KVO_ADDR} could not be used: unreachable within ${KVO_HTTP_TIMEOUT}s, the"
            warn "password was refused, or its EULA is pending (the reason is above)."
            note "Check the address (--kvo-address) and the login (--kvo-admin-user / --kvo-admin-pass,"
            note "or CLOUDLENS_KVO_ADMIN_USER / CLOUDLENS_KVO_ADMIN_PASS)."
            lic_fallback_note ;;
          124)
            warn "KVO ${KVO_ADDR} did not answer within $(( KVO_HTTP_TIMEOUT * 3 + 5 ))s; gave up listing its licences."
            lic_fallback_note ;;
          3)
            warn "The KVO's licence list could not be read (the reason is above)."
            lic_fallback_note ;;
          *)
            warn "kvo_license.py exited ${LIC_RC} while listing the licences."
            lic_fallback_note ;;
        esac
      fi
      unset CLOUDLENS_KVO_ADMIN_PASS KVO_PASS
      ARG_KVO_PASS=""
    fi
  fi
fi

# =====================================================================
# Phase 4b: the licence-loss gate
#
# Only when the stack has a KVO, and always before the first delete.
# Phase 4a can waive it, and only by reporting the KVO clear; every other
# outcome leaves it exactly as it was: the red warning and the typed stack
# name, or --accept-licence-loss with no terminal.
# =====================================================================
if [[ "$HAS_KVO" == "true" && "$SWEEP_ONLY" != "true" ]]; then
  step "Phase 4b: Confirm the licence loss"
fi
if [[ "$HAS_KVO" == "true" && "$SWEEP_ONLY" != "true" && "$LIC_RELEASED" == "true" ]]; then
  case "$LIC_WHY" in
    released) ok "Phase 4a released every licence, and the KVO reports none left: nothing will" ;;
    *)        ok "Phase 4a found the KVO held nothing, and it reports none left: nothing will" ;;
  esac
  ok "be stranded, so no licence-loss confirmation is needed."
elif [[ "$HAS_KVO" == "true" && "$SWEEP_ONLY" != "true" ]]; then
  echo
  echo -e "${C_RED}${C_BOLD}  LICENCES ARE ABOUT TO BE STRANDED, PERMANENTLY.${C_RESET}"
  echo
  echo "  This stack contains a KVO. KVO activation codes are bound to the KVO"
  echo "  host they were activated on. While that host is alive the counts CAN"
  echo "  be released: this script offers to do it (Phase 4a above, or"
  echo "  --release-licences with no terminal), and the KVO UI can too"
  echo "  (Settings > Product Licensing > Deactivate licenses). Both drive the"
  echo "  licensing API's deactivate operation: proven, 20 counts recovered."
  echo "  Deleting the stack destroys that host, and the quantity still activated"
  echo "  on it is NOT returned afterwards: it is stranded for good."
  echo
  echo "  This is not theoretical. Three activation codes worth 500 counts each"
  echo "  came back availableQuantity=0 after the KVO they were activated on was"
  echo "  deleted: 1500 counts lost, with nothing anywhere warning it would happen."
  echo
  echo "  Get them back FIRST, if you ever want them:"
  echo "    re-run this teardown and answer yes to the release in Phase 4a"
  echo "    (or pass --release-licences), or deactivate each code in the"
  echo "    KVO UI > Settings > Product Licensing, then re-run this teardown."
  echo "  The release above did not happen or did not leave the KVO clear; the"
  echo "  reason is printed in Phase 4a."
  echo
  echo "  Continuing destroys the KVO and every licence count activated on it."
  echo
  if [[ "$INTERACTIVE" == "true" ]]; then
    _typed="$(ask "  Type the stack name '${STACK_NAME}' to accept the licence loss: " "")"
    [[ "$_typed" == "$STACK_NAME" ]] \
      || fail "Not confirmed (got '${_typed}'). Nothing was deleted."
    ok "Licence loss accepted."
  elif [[ "$ACCEPT_LICENCE_LOSS" == "true" ]]; then
    ok "Licence loss accepted (--accept-licence-loss)."
  elif [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "a real run would stop here and require --accept-licence-loss"
  else
    fail "This stack has a KVO and there is no terminal to confirm on.
  Deleting it strands the licence quantity activated on it, permanently.
  Release the licences first, with --yes --release-licences (this script
  does it; a release that leaves the KVO clear needs no other flag) or in
  the KVO UI, or accept the loss with:
    --yes --accept-licence-loss"
  fi
fi

# =====================================================================
# Phase 5: delete the stack, wait for a terminal state
# =====================================================================
DRYRUN_FIRST_WAIT=true
wait_for_delete() {
  local waited=0 interval=15 st last_report=0
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "would wait up to $(( DELETE_TIMEOUT / 60 )) minutes for stack-delete-complete"
    # A stack that is ALREADY DELETE_FAILED keeps that status through the first
    # rehearsed wait, so --dry-run walks the blocker path instead of pretending
    # the delete succeeded. That path is the one worth rehearsing.
    if [[ "$DRYRUN_FIRST_WAIT" == "true" && "$STACK_STATUS" == "DELETE_FAILED" ]]; then
      DRYRUN_FIRST_WAIT=false
      return 1
    fi
    DRYRUN_FIRST_WAIT=false
    STACK_STATUS="DELETE_COMPLETE"
    return 0
  fi
  while (( waited < DELETE_TIMEOUT )); do
    st="$(stack_status_now)"
    case "$st" in
      MISSING|DELETE_COMPLETE) STACK_STATUS="DELETE_COMPLETE"; return 0 ;;
      DELETE_FAILED)           STACK_STATUS="DELETE_FAILED";   return 1 ;;
    esac
    if (( waited - last_report >= 120 )); then
      note "still ${st} ($(( waited / 60 ))m elapsed)"
      last_report="$waited"
    fi
    sleep "$interval"
    waited=$(( waited + interval ))
  done
  STACK_STATUS="$(stack_status_now)"
  return 2
}

# Extra vPBs and their Elastic IPs. deploy-stack.sh launches one vPB per
# further area (--vpb-rails) outside CloudFormation, tagged
# cloudlens:stack=<stack> + cloudlens:vpb-rail=<area>, in whatever VPC the
# stack used, and gives each an Elastic IP with the same tags. They share the
# stack's security group, so they must go BEFORE the stack delete: with one
# still running, CloudFormation cannot remove the group and the stack lands in
# DELETE_FAILED (seen 2026-10-07 on a brownfield stack, where Phase 6 then
# treats the blocker as out of scope because the VPC is the customer's).
# Called again from the sweep for anything that appeared in between.
sweep_rail_vpbs() {
  local _rail_ids _li _rail_eips _al
  _rail_ids=$(ro_aws ec2 describe-instances \
      --filters "Name=tag:cloudlens:stack,Values=${STACK_NAME}" "Name=tag-key,Values=cloudlens:vpb-rail" \
                "Name=instance-state-name,Values=running,pending,stopping,stopped" \
      --query 'Reservations[].Instances[].InstanceId' --output text | tr '\t' '\n')
  for _li in $_rail_ids; do
    if del_aws ec2 terminate-instances --instance-ids "$_li"; then
      ok "$(did) extra vPB ${_li}"
    else
      warn "could not terminate extra vPB ${_li}: ${DEL_ERR}"
    fi
  done
  [[ -n "$_rail_ids" && "$DRY_RUN" != "true" ]] && ro_aws ec2 wait instance-terminated --instance-ids $_rail_ids 2>/dev/null || true
  _rail_eips=$(ro_aws ec2 describe-addresses \
      --filters "Name=tag:cloudlens:stack,Values=${STACK_NAME}" "Name=tag-key,Values=cloudlens:vpb-rail" \
      --query 'Addresses[].AllocationId' --output text | tr '\t' '\n')
  for _al in $_rail_eips; do
    if del_aws ec2 release-address --allocation-id "$_al"; then
      ok "$(did) Elastic IP ${_al} (extra vPB)"
    else
      warn "could not release Elastic IP ${_al}: ${DEL_ERR}"
    fi
  done
  return 0
}

# The CyPerf add-on stack. deploy-stack.sh --with-cyperf builds the traffic
# generator (controller + client agent + server agent, their security group
# and Elastic IP) as its own CloudFormation stack, <stack>-cyperf, inside the
# main stack's VPC. It goes BEFORE the main stack: its interfaces sit in the
# main stack's subnets, so a greenfield VPC cannot be deleted around them.
# Evidence of ownership: the stack tag cloudlens:stack names THIS stack, which
# only deploy-stack.sh writes. A stack that merely has the same name prefix
# is left alone.
# The controller's licence is released first, for the same reason as KVO's:
# an activation is returned to the entitlement only while the controller that
# holds it is alive.
sweep_cyperf_stack() {
  local cy="${STACK_NAME}-cyperf" st owner addr py waited=0
  st="$(det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$cy" \
        --query 'Stacks[0].StackStatus' --output text | first_line)")"
  [[ -z "$st" ]] && return 0
  owner="$(det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$cy" \
        --query "Stacks[0].Tags[?Key=='cloudlens:stack'].Value | [0]" --output text | first_line)")"
  if [[ "$owner" != "$STACK_NAME" ]]; then
    warn "A stack named ${cy} exists but is not tagged cloudlens:stack=${STACK_NAME}; leaving it alone."
    return 0
  fi
  note "CyPerf add-on stack ${cy} (${st}) belongs to this deployment."
  addr="$(det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$cy" \
        --query "Stacks[0].Outputs[?OutputKey=='ControllerAddress'].OutputValue | [0]" --output text | first_line)")"
  py=""
  for _c in "$SCRIPT_DIR/../scripts/cyperf_setup.py" "$HOME/${REPO_NAME}/scripts/cyperf_setup.py" "$PWD/scripts/cyperf_setup.py"; do
    [[ -f "$_c" ]] && { py="$_c"; break; }
  done
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "would release the CyPerf licence on ${addr:-the controller} (cyperf_setup.py --release-license), then delete stack ${cy}"
    return 0
  fi
  if [[ "$AUDIT_ONLY" == "true" ]]; then
    note "--orphans: ${cy} is reported, not deleted."
    return 0
  fi
  if [[ -n "$addr" && -n "$py" ]] && command -v python3 >/dev/null 2>&1; then
    if python3 "$py" --controller "$addr" --insecure --release-license; then
      ok "CyPerf licence released (or none was installed): the activation returns to your entitlement."
    else
      warn "The CyPerf licence could NOT be released (controller unreachable or it refused)."
      warn "Its activation stays consumed until Keysight support resets it. Continuing."
    fi
  else
    warn "CyPerf licence not released: $([[ -z "$addr" ]] && echo "no controller address" || echo "scripts/cyperf_setup.py or python3 not found")."
  fi
  if del_aws cloudformation delete-stack --stack-name "$cy"; then
    ok "Delete requested for ${cy}"
  else
    warn "delete-stack was rejected for ${cy}: ${DEL_ERR}"
    return 0
  fi
  while (( waited < 900 )); do
    st="$(det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$cy" \
          --query 'Stacks[0].StackStatus' --output text | first_line)")"
    case "$st" in
      ""|DELETE_COMPLETE) ok "Stack ${cy} deleted."; return 0 ;;
      DELETE_FAILED) warn "Stack ${cy} is DELETE_FAILED; the main stack's delete may fail on its VPC."; return 0 ;;
    esac
    sleep 15; waited=$(( waited + 15 ))
  done
  warn "Stack ${cy} is still ${st} after 15 minutes; continuing."
  return 0
}

if [[ "$SWEEP_ONLY" != "true" ]]; then
  step "Phase 5: Delete the stack"
  # The rail vPBs first (they hold the stack security group open).
  # The CyPerf add-on stack before that: it lives in this stack's subnets.
  sweep_cyperf_stack
  sweep_rail_vpbs
  if del_aws cloudformation delete-stack --stack-name "$STACK_NAME"; then
    ok "Delete requested for ${STACK_NAME}"
  else
    fail "delete-stack was rejected: ${DEL_ERR}"
  fi
  note "Waiting for a terminal state (this takes several minutes)."
  _rc=0; wait_for_delete || _rc=$?
  case "$_rc" in
    0) ok "Stack ${STACK_NAME} deleted." ;;
    1) warn "Stack ${STACK_NAME} is DELETE_FAILED." ;;
    2) warn "Stack ${STACK_NAME} is still ${STACK_STATUS} after $(( DELETE_TIMEOUT / 60 )) minutes."
       note "Not waiting further. Re-run with --sweep-only once it settles." ;;
  esac

  # ===================================================================
  # Phase 6: DELETE_FAILED. The common case, not the exception.
  # ===================================================================
  if [[ "$STACK_STATUS" == "DELETE_FAILED" ]]; then
    step "Phase 6: What blocked the delete"
    _blockers="$(ro_aws cloudformation describe-stack-events --stack-name "$STACK_NAME" \
      --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceType,ResourceStatusReason]" \
      --output text)"
    if [[ -n "$_blockers" ]]; then
      echo "  CloudFormation could not delete:"
      printf '%s\n' "$_blockers" | head -20 | sed 's/^/    /'
    else
      warn "CloudFormation reported no blocking resource, which usually means the"
      warn "events have aged out. Check the console for stack ${STACK_NAME}."
    fi

    # The known blocker: a security group somebody else created inside the
    # stack's VPC. CloudFormation owns the VPC and cannot delete it while a
    # group it does not own is still in there.
    if [[ -n "$SWEEP_SGS" && "$STACK_OWNS_VPC" == "true" ]]; then
      echo
      warn "Non-stack security groups in ${STACK_VPC_ID} are the usual cause:"
      for _sg in $SWEEP_SGS; do
        eval "_n=\"\${SGNAME_${_sg//-/_}:-}\""
        echo "    ${_sg}  ${_n:-}"
      done
      note "These are in scope: they sit in a VPC this stack created."
      _go=false
      if [[ "$DRY_RUN" == "true" ]]; then
        _go=true
        dryrun_say "a real run would ask before removing these"
      elif [[ "$INTERACTIVE" == "true" ]]; then
        ask_yn "  Remove them and retry the stack delete? [y/N]: " "n" && _go=true
      elif [[ "$ASSUME_YES" == "true" ]]; then
        _go=true
      else
        warn "No terminal and no --yes: leaving them alone."
      fi
      if [[ "$_go" == "true" ]]; then
        _remaining=""
        for _sg in $SWEEP_SGS; do
          if del_aws ec2 delete-security-group --group-id "$_sg"; then
            ok "$(did) ${_sg}"
            DELETED_SGS=$(( DELETED_SGS + 1 ))
          else
            warn "could not delete ${_sg}: ${DEL_ERR}"
            _remaining="${_remaining}${_remaining:+ }${_sg}"
            # DependencyViolation means something is still using it. Name what,
            # rather than leaving the operator to find it in the console.
            _users="$(ro_aws ec2 describe-network-interfaces --filters "Name=group-id,Values=${_sg}" \
              --query 'NetworkInterfaces[].[NetworkInterfaceId,Attachment.InstanceId,Description]' --output text)"
            if [[ -n "$_users" ]]; then
              note "still in use by:"
              printf '%s\n' "$_users" | sed 's/^/      /'
              _owner="$(det_clean "$(ro_aws ec2 describe-security-groups --group-ids "$_sg" \
                --query "SecurityGroups[0].Tags[?Key=='${TESTVM_OWNER_TAG_KEY}'].Value | [0]" --output text)")"
              if [[ "$_owner" == "$TESTVM_OWNER_TAG_VALUE" ]]; then
                note "This group belongs to scripts/deploy-test-workload-vms.sh."
                note "Remove its instances and the group with:"
                note "  bash scripts/deploy-test-workload-vms.sh --region ${REGION} --cleanup"
              fi
            fi
            record_failure "$_sg" "$DEL_ERR"
          fi
        done
        # Every group here has now been attempted, so the phase 7 sweep must
        # not try them again and report the identical failure a second time.
        SWEEP_SGS=""
        if [[ -z "$_remaining" ]]; then
          note "Retrying the stack delete."
          if del_aws cloudformation delete-stack --stack-name "$STACK_NAME"; then
            _rc=0; wait_for_delete || _rc=$?
            if (( _rc == 0 )); then ok "Stack ${STACK_NAME} deleted on the retry."
            else warn "Stack is ${STACK_STATUS} after the retry."; fi
          else
            warn "Retry of delete-stack was rejected: ${DEL_ERR}"
          fi
        else
          warn "Some groups are still in use, so the stack delete was not retried."
        fi
      fi
    fi
  fi
fi

# =====================================================================
# Phase 7: sweep what the stack left behind
# =====================================================================
if [[ "$NO_SWEEP" == "true" ]]; then
  step "Sweep skipped (--no-sweep)"
  note "${SWEEP_GB} GB of EBS is still there, about \$${SWEEP_COST}/month."
  note "Sweep it later with: bash deploy/teardown-stack.sh --stack-name ${STACK_NAME} --sweep-only"
else
  step "Phase 7: Sweep"

  # ---- Sample EKS cluster first: it owns ENIs in the VPC (control plane +
  # nodes) that block subnet and VPC deletion below, and its nodegroup is an
  # ASG the generic ASG pass does not know is EKS-managed. Only the SAMPLE
  # cluster this deploy created is touched: matched strictly by its name
  # (${STACK_NAME}-eks) AND its cloudlens:stack tag; a customer's own cluster
  # never carries both. Order per AWS: nodegroup, then cluster, then roles.
  _eks_name="${STACK_NAME}-eks"
  _eks_tag=$(ro_aws eks describe-cluster --name "$_eks_name"       --query 'cluster.tags."cloudlens:stack"' --output text 2>/dev/null)
  if [[ "$_eks_tag" == "$STACK_NAME" ]]; then
    note "Sample EKS cluster ${_eks_name} belongs to this stack; removing it."
    for _ng in $(ro_aws eks list-nodegroups --cluster-name "$_eks_name"                    --query 'nodegroups[]' --output text 2>/dev/null); do
      if del_aws eks delete-nodegroup --cluster-name "$_eks_name" --nodegroup-name "$_ng"; then
        ok "$(did) EKS nodegroup ${_ng} (delete started)"
      fi
    done
    for _ng in $(ro_aws eks list-nodegroups --cluster-name "$_eks_name"                    --query 'nodegroups[]' --output text 2>/dev/null); do
      note "waiting for nodegroup ${_ng} to go (2-5 min)..."
      ro_aws eks wait nodegroup-deleted --cluster-name "$_eks_name" --nodegroup-name "$_ng" || true
    done
    # delete-cluster is refused (ResourceInUseException) while the nodegroup
    # is still draining, and `eks wait nodegroup-deleted` can return before
    # it is truly gone. Left unhandled, the control plane survives, its two
    # ENIs keep the subnets and security groups, and the stack retry fails
    # again (seen live 2026-09-29). Keep asking for up to ten minutes.
    _eks_gone=false
    for _try in $(seq 1 20); do
      if del_aws eks delete-cluster --name "$_eks_name"; then
        note "waiting for the control plane to go (a few minutes)..."
        ro_aws eks wait cluster-deleted --name "$_eks_name" || true
        ok "$(did) EKS cluster ${_eks_name}"
        _eks_gone=true
        break
      fi
      [[ "$_try" == "1" ]] && note "EKS refused the cluster delete (${DEL_ERR:-nodegroup still draining}); retrying every 30 s for up to 10 min"
      sleep 30
    done
    [[ "$_eks_gone" == "true" ]] || warn "EKS cluster ${_eks_name} is still there; its ENIs will block the subnets. Delete it, then re-run with --sweep-only."
    # The sample cluster's second-AZ subnet is created by deploy-eks-tapping.sh
    # outside CloudFormation and associated with the stack's route table; left
    # behind, the route table (and so the stack) cannot delete (live
    # 2026-09-29). Only subnets in the stack VPC that carry no CloudFormation
    # stack-name tag are touched, after their route-table associations.
    if [[ -n "${STACK_VPC_ID:-}" ]]; then
      for _sn in $(ro_aws ec2 describe-subnets --filters "Name=vpc-id,Values=${STACK_VPC_ID}" \
                     --query 'Subnets[?!(Tags[?Key==`aws:cloudformation:stack-name`])].SubnetId' --output text 2>/dev/null); do
        for _as in $(ro_aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=${_sn}" \
                       --query "RouteTables[].Associations[?SubnetId=='${_sn}'].RouteTableAssociationId" --output text 2>/dev/null); do
          del_aws ec2 disassociate-route-table --association-id "$_as" || true
        done
        if del_aws ec2 delete-subnet --subnet-id "$_sn"; then
          ok "$(did) subnet ${_sn} (sample cluster, outside CloudFormation)"
        else
          warn "could not delete subnet ${_sn}: ${DEL_ERR}"
        fi
      done
    fi
    for _r in "${STACK_NAME}-eks-cluster-role" "${STACK_NAME}-eks-node-role"; do
      for _pa in $(ro_aws iam list-attached-role-policies --role-name "$_r"                      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
        del_aws iam detach-role-policy --role-name "$_r" --policy-arn "$_pa" || true
      done
      del_aws iam delete-role --role-name "$_r" && ok "$(did) IAM role ${_r}"
    done
    _ecr_tag=$(ro_aws ecr list-tags-for-resource         --resource-arn "arn:aws:ecr:${REGION}:$(ro_aws sts get-caller-identity --query Account --output text):repository/cloudlens-sensor"         --query 'tags[?Key==`cloudlens:stack`].Value | [0]' --output text 2>/dev/null)
    if [[ "$_ecr_tag" == "$STACK_NAME" ]]; then
      del_aws ecr delete-repository --repository-name cloudlens-sensor --force         && ok "$(did) ECR repository cloudlens-sensor (this stack created it)"
    fi
  fi

  # ---- Loose instances in the stack's own VPC. Everything KVO launches
  # outside CloudFormation (mirror collectors via their ASG, and the capture
  # receiver with NO asg) lives here, holds an ENI, and blocks every subnet
  # below it. Only when the VPC is the stack's own (the same rule that scopes
  # SGs and ASGs): in a customer VPC, other people's instances are none of
  # this script's business and STACK_OWNS_VPC is false.
  if [[ "${STACK_OWNS_VPC:-false}" == "true" && -n "${STACK_VPC_ID:-}" ]]; then
    _loose=$(ro_aws ec2 describe-instances \
        --filters "Name=vpc-id,Values=${STACK_VPC_ID}" \
                  "Name=instance-state-name,Values=running,pending,stopping,stopped" \
        --query 'Reservations[].Instances[].InstanceId' --output text | tr '\t' '\n')
    _loose_ids=()
    while IFS= read -r _li; do [[ -n "$_li" ]] && _loose_ids+=("$_li"); done <<< "$_loose"
    if [[ ${#_loose_ids[@]} -gt 0 ]]; then
      note "${#_loose_ids[@]} instance(s) still alive in the stack's VPC (launched"
      note "outside CloudFormation, e.g. the mirror capture receiver); terminating"
      note "them so the subnets can go."
      for _li in "${_loose_ids[@]}"; do
        if del_aws ec2 terminate-instances --instance-ids "$_li"; then
          ok "$(did) instance ${_li}"
        else
          warn "could not terminate ${_li}: ${DEL_ERR}"
        fi
      done
      ro_aws ec2 wait instance-terminated --instance-ids "${_loose_ids[@]}" 2>/dev/null || true
    fi
  fi

  sweep_rail_vpbs

  # ---- instances the deploy stamped (evidence class 4), in any VPC
  if [[ -n "$SWEEP_OWN_INSTANCES" ]]; then
    for _li in $SWEEP_OWN_INSTANCES; do
      if del_aws ec2 terminate-instances --instance-ids "$_li"; then
        ok "$(did) instance ${_li} (tagged cloudlens:stack=${STACK_NAME})"
      else
        warn "could not terminate ${_li}: ${DEL_ERR}"
      fi
    done
    [[ "$DRY_RUN" != "true" ]] && ro_aws ec2 wait instance-terminated --instance-ids $SWEEP_OWN_INSTANCES 2>/dev/null || true
  fi

  # ---- Auto Scaling groups first: a live group re-creates its instances,
  # which re-creates volumes and holds ENIs on the groups below.
  for _a in $SWEEP_ASGS; do
    if del_aws autoscaling delete-auto-scaling-group --auto-scaling-group-name "$_a" --force-delete; then
      ok "$(did) ASG ${_a}"
      DELETED_ASGS=$(( DELETED_ASGS + 1 ))
    else
      warn "could not delete ASG ${_a}: ${DEL_ERR}"
      record_failure "$_a" "$DEL_ERR"
    fi
  done

  for _l in $SWEEP_LTS; do
    if del_aws ec2 delete-launch-template --launch-template-id "$_l"; then
      ok "$(did) launch template ${_l}"
      DELETED_LTS=$(( DELETED_LTS + 1 ))
    else
      warn "could not delete launch template ${_l}: ${DEL_ERR}"
      record_failure "$_l" "$DEL_ERR"
    fi
  done

  # ---- traffic mirroring: sessions, then targets, then the filter.
  # The targets sit on the collectors' ENIs. The sessions do NOT go with the
  # collectors: KVO cuts one per tapped WORKLOAD ENI (source = the workload's
  # ENI, target = the collector's target, filter = KVO's filter), and the KVO
  # that would remove them is deleted with the stack. A session left behind
  # holds its target and filter (AWS refuses both deletes with ...InUse, seen
  # live 2026-10-16) and keeps taking the copies on its ENI. Deleting one stops
  # only those copies; the workload's own traffic is never touched.
  if [[ -n "$SWEEP_MIRROR_TARGETS$SWEEP_MIRROR_FILTERS$SWEEP_MIRROR_SESSIONS" && "$DRY_RUN" != "true" ]]; then
    for _a in $SWEEP_ASGS; do
      for _try in 1 2 3 4 5 6 7 8 9 10 11 12; do
        [[ "$(ro_aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$_a" --query 'length(AutoScalingGroups)' --output text 2>/dev/null)" == "0" ]] && break
        sleep 10
      done
    done
  fi
  # Ask AWS again, now, which sessions send to our targets, and delete those
  # and only those. The list from Phase 2 (or the manifest) is not reused as
  # it stands: KVO was alive until the stack delete and may have cut another
  # session since, a --sweep-only run with a manifest written before sessions
  # were recorded has the targets but no session list, and a recorded session
  # may have been repointed at somebody else's target since. The rule is the
  # one above the helpers: by target, never by filter.
  collect_mirror_sessions
  explain_dropped_sessions
  for _ms in $SWEEP_MIRROR_SESSIONS; do
    if del_aws ec2 delete-traffic-mirror-session --traffic-mirror-session-id "$_ms"; then
      ok "$(did) traffic mirror session ${_ms} (mirror copies only: production traffic is unaffected)"
      DELETED_SESSIONS=$(( DELETED_SESSIONS + 1 ))
    else
      case "$DEL_ERR" in
        # Recorded by an earlier run that already removed it: nothing to do,
        # and not a failure to send the operator chasing.
        *InvalidTrafficMirrorSessionId.NotFound*) note "traffic mirror session ${_ms} is already gone" ;;
        *) warn "could not delete mirror session ${_ms}: ${DEL_ERR}"
           record_failure "$_ms" "$DEL_ERR" ;;
      esac
    fi
  done
  for _t in $SWEEP_MIRROR_TARGETS; do
    if del_aws ec2 delete-traffic-mirror-target --traffic-mirror-target-id "$_t"; then
      ok "$(did) traffic mirror target ${_t}"
    else
      warn "could not delete mirror target ${_t}: ${DEL_ERR}"
      record_failure "$_t" "$DEL_ERR"
    fi
  done
  # The filter is attributed by KVO's per-VPC name, so it can be shared with
  # another deployment tapping the same VPC. AWS refuses the delete while any
  # session uses it, and that refusal is what protects a shared filter: when
  # the session it names is not one of ours, the filter stays and that is
  # the right outcome, not a failure to chase.
  for _f in $SWEEP_MIRROR_FILTERS; do
    if del_aws ec2 delete-traffic-mirror-filter --traffic-mirror-filter-id "$_f"; then
      ok "$(did) traffic mirror filter ${_f}"
    else
      _by=""
      [[ "$DEL_ERR" == *TrafficMirrorFilterInUse* ]] && _by="$(printf '%s' "$DEL_ERR" | sed -n 's/.*in use by \(tms-[0-9a-zA-Z]*\).*/\1/p')"
      if [[ -n "$_by" ]] && ! in_list "$_by" "$SWEEP_MIRROR_SESSIONS"; then
        warn "mirror filter ${_f} left in place: still used by session ${_by}, which was not found sending to this stack's collectors"
        note "the filter is shared with whoever owns that session, and goes when they remove it"
      else
        warn "could not delete mirror filter ${_f}: ${DEL_ERR}"
        record_failure "$_f" "$DEL_ERR"
      fi
    fi
  done

  # ---- security groups still standing (stack was fine, group was not)
  for _sg in $SWEEP_SGS; do
    if del_aws ec2 delete-security-group --group-id "$_sg"; then
      ok "$(did) security group ${_sg}"
      DELETED_SGS=$(( DELETED_SGS + 1 ))
    else
      warn "could not delete security group ${_sg}: ${DEL_ERR}"
      record_failure "$_sg" "$DEL_ERR"
    fi
  done

  # ---- volumes LAST, and only the ones now 'available'.
  # A volume still 'in-use' is attached to something that survived the
  # teardown, which means either the delete did not finish or the volume was
  # never ours. Either way, not a thing to force.
  if [[ -n "$SWEEP_VOLUMES" ]]; then
    if [[ "$DRY_RUN" == "true" ]]; then
      _now="$VOLUME_LINES"
    else
      _now="$(ro_aws ec2 describe-volumes --volume-ids ${SWEEP_VOLUMES} \
        --query 'Volumes[].[VolumeId,Size,VolumeType,State]' --output text)"
    fi
    if [[ -n "$_now" ]]; then
      while IFS=$'\t' read -r _vid _vsize _vtype _vstate; do
        [[ -n "${_vid:-}" ]] || continue
        if [[ "${_vstate:-}" != "available" && "$DRY_RUN" != "true" ]]; then
          warn "skipping ${_vid}: state is ${_vstate}, not available"
          note "an attached volume is still in use by something. Re-run with"
          note "--sweep-only once the instances are really gone."
          continue
        fi
        if del_aws ec2 delete-volume --volume-id "$_vid"; then
          ok "$(did) ${_vid} (${_vsize} GB ${_vtype})"
          DELETED_VOLUMES=$(( DELETED_VOLUMES + 1 ))
          DELETED_GB=$(( DELETED_GB + ${_vsize:-0} ))
          DELETED_COST="$(awk -v c="$DELETED_COST" -v s="${_vsize:-0}" -v r="$(ebs_rate "${_vtype:-gp2}")" \
            'BEGIN { printf "%.4f", c + (s * r) }')"
        else
          warn "could not delete ${_vid}: ${DEL_ERR}"
          record_failure "$_vid" "$DEL_ERR"
        fi
      done <<< "$_now"
    fi
  fi

  if (( DELETED_VOLUMES == 0 && DELETED_SGS == 0 && DELETED_ASGS == 0 && DELETED_LTS == 0 && DELETED_SESSIONS == 0 )); then
    ok "Nothing left to sweep."
  fi
fi

# =====================================================================
# Phase 8: summary
# =====================================================================
step "Teardown summary"
_saved="$(money "$DELETED_COST")"
_lbl="deleted"; _tail="no longer billing"
if [[ "$DRY_RUN" == "true" ]]; then _lbl="to delete"; _tail="would stop billing"; fi
echo "  Stack:              ${STACK_NAME} (${STACK_STATUS})"
echo "  Region:             ${REGION}"
echo "  Volumes ${_lbl}:    ${DELETED_VOLUMES} (${DELETED_GB} GB, about \$${_saved}/month ${_tail})"
echo "  Security groups:    ${DELETED_SGS}"
echo "  Auto Scaling grps:  ${DELETED_ASGS}"
echo "  Launch templates:   ${DELETED_LTS}"
echo "  Mirror sessions:    ${DELETED_SESSIONS}"
if [[ -n "$FAILED_ITEMS" ]]; then
  echo
  warn "Some resources could not be removed:"
  printf '%s\n' "$FAILED_ITEMS"
  note "Re-run with --sweep-only after fixing the cause, or inspect them by hand."
fi
if [[ "$DRY_RUN" == "true" ]]; then
  echo
  warn "DRY RUN: nothing above was actually deleted."
fi
echo
note "Sensors are NOT removed by this script: they live on the workload VMs, not"
note "in the stack. Remove them with: bash scripts/cleanup.sh customer_input.yaml"
echo
SCRIPT_DONE=true
exit 0

}   # End of brace-wrap for curl|bash safety
