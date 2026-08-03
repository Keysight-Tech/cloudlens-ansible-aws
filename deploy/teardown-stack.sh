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
PROBE_TIMEOUT="${CLOUDLENS_PROBE_TIMEOUT:-25}"
DELETE_TIMEOUT="${CLOUDLENS_DELETE_TIMEOUT:-2700}"   # 45 minutes

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
SWEEP_GB=0
SWEEP_COST="0.00"
UNATTRIBUTED_GB=0
UNATTRIBUTED_COUNT=0
UNATTRIBUTED_COST="0.00"
VOLUME_LINES=""       # "vol-id<TAB>size<TAB>type<TAB>state" per line

DELETED_VOLUMES=0; DELETED_GB=0; DELETED_COST=0; DELETED_SGS=0; DELETED_ASGS=0; DELETED_LTS=0
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
unattached EBS volumes, non-stack security groups sitting in the stack's VPC,
and the collector Auto Scaling Group / launch templates KVO creates outside
CloudFormation. Deleting the stack alone leaves all of that billing.

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
                            contains a KVO. Deleting a KVO permanently strands
                            the licence quantity activated on it: there is an
                            activate API and no release or rehost API. Read the
                            warning this script prints before passing this.

Scoping (why this is safe to run in a shared account):
  A resource is deleted only when AWS itself ties it to this stack: it is a
  member of the stack, or it was attached to an instance that was, or it lives
  in the VPC the stack itself created (never a brownfield --existing-vpc-id
  VPC), or it carries a tag naming the stack. Anything else is reported and
  left alone. An unattached volume with no such evidence is somebody else's.

Env-var overrides:
  CLOUDLENS_REGION, CLOUDLENS_STACK_NAME, CLOUDLENS_PROBE_TIMEOUT,
  CLOUDLENS_DELETE_TIMEOUT

Examples:
  # What is this stack leaking, and what is it costing? Deletes nothing.
  bash deploy/teardown-stack.sh --stack-name cloudlens-stack --orphans

  # Rehearse the whole teardown, touch nothing.
  bash deploy/teardown-stack.sh --stack-name cloudlens-stack --dry-run

  # Real teardown, no terminal, stack contains a KVO.
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
  4. Warn about stranded KVO licences and take the confirmations.
  5. Delete the stack, then wait for a terminal state.
  6. On DELETE_FAILED, name the blocking resources, offer to remove the ones
     that are attributable, and retry the delete.
  7. Sweep the volumes, security groups, ASGs and launch templates.
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
    warn "Stack VPC: ${STACK_VPC_ID} (pre-existing / brownfield: OUT of scope)"
    note "Security groups and ASGs in this VPC will be reported, never deleted."
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

# ---- is there a KVO in this stack? ----------------------------------
if [[ "$STACK_STATUS" != "MISSING" ]]; then
  _kvo_param="$(det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='DeployKVO'].ParameterValue | [0]" --output text)")"
  [[ "$(to_lower "${_kvo_param:-}")" == "yes" ]] && HAS_KVO=true
  if [[ "$HAS_KVO" != "true" ]]; then
    _kvo_inst="$(det_clean "$(ro_aws ec2 describe-instances \
      --filters "Name=tag:Name,Values=*kvo*" "Name=instance-state-name,Values=running,stopped" \
      --query 'Reservations[0].Instances[0].InstanceId' --output text)")"
    if [[ -n "$_kvo_inst" ]] && in_list "$_kvo_inst" "$STACK_INSTANCE_IDS"; then HAS_KVO=true; fi
  fi
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
  echo "  Unattributed EBS:      ${UNATTRIBUTED_GB} GB, about \$${UNATTRIBUTED_COST}/month (reported only)"
  echo
  echo "  Nothing was deleted. To actually tear this down:"
  echo "    bash deploy/teardown-stack.sh --stack-name ${STACK_NAME} --region ${REGION}"
  SCRIPT_DONE=true
  exit 0
fi

# =====================================================================
# Phase 4: licence warning + confirmation
#
# This runs BEFORE the first delete, always, and it is the only reason the
# confirmation is two steps instead of one.
# =====================================================================
step "Phase 4: Confirm"

if [[ "$HAS_KVO" == "true" && "$SWEEP_ONLY" != "true" ]]; then
  echo
  echo -e "${C_RED}${C_BOLD}  LICENCES ARE ABOUT TO BE STRANDED, PERMANENTLY.${C_RESET}"
  echo
  echo "  This stack contains a KVO. KVO activation codes are bound to the KVO"
  echo "  host they were activated on. Deleting the stack destroys that host, and"
  echo "  the quantity activated on it is NOT returned: there is an activate API"
  echo "  and there is no release or rehost API. The entitlement cannot be used"
  echo "  again anywhere without Keysight rehosting it by hand."
  echo
  echo "  This is not theoretical. Three activation codes worth 500 counts each"
  echo "  came back availableQuantity=0 after the KVO they were activated on was"
  echo "  deleted: 1500 counts lost, with nothing anywhere warning it would happen."
  echo
  echo "  Get them back FIRST, if you ever want them:"
  echo "    KVO UI > Settings > Product Licensing > deactivate each code"
  echo "    (then re-run this teardown)"
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
  Deactivate the codes in KVO first, or re-run with:
    --yes --accept-licence-loss"
  fi
fi

echo
echo "  About to delete, in ${REGION}:"
[[ "$SWEEP_ONLY" != "true" ]] && echo "    - CloudFormation stack ${STACK_NAME} (${STACK_STATUS})"
(( SWEEP_GB > 0 )) && echo "    - $(printf '%s' "$SWEEP_VOLUMES" | wc -w | tr -d ' ') EBS volumes, ${SWEEP_GB} GB, about \$${SWEEP_COST}/month"
[[ -n "$SWEEP_SGS" ]]  && echo "    - $(printf '%s' "$SWEEP_SGS" | wc -w | tr -d ' ') non-stack security groups in ${STACK_VPC_ID}"
[[ -n "$SWEEP_ASGS" ]] && echo "    - $(printf '%s' "$SWEEP_ASGS" | wc -w | tr -d ' ') Auto Scaling groups"
[[ -n "$SWEEP_LTS" ]]  && echo "    - $(printf '%s' "$SWEEP_LTS" | wc -w | tr -d ' ') launch templates"
echo

if [[ "$DRY_RUN" == "true" ]]; then
  dryrun_say "a real run would ask for confirmation here (or require --yes)"
elif [[ "$INTERACTIVE" == "true" ]]; then
  ask_yn "  Proceed with the teardown? [y/N]: " "n" || fail "Aborted. Nothing was deleted."
elif [[ "$ASSUME_YES" == "true" ]]; then
  ok "Proceeding (--yes)."
else
  fail "No terminal to confirm on and --yes was not given, so nothing was deleted.
  Re-run with --yes to confirm, or with --orphans to see what is loose without
  deleting anything."
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

if [[ "$SWEEP_ONLY" != "true" ]]; then
  step "Phase 5: Delete the stack"
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

  if (( DELETED_VOLUMES == 0 && DELETED_SGS == 0 && DELETED_ASGS == 0 && DELETED_LTS == 0 )); then
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
