#!/usr/bin/env bash
# =====================================================================
# CloudLens Stack Deployment: vController + KVO + vPB + Sensors (AWS)
# (vController is the new name for what used to be called CLMS)
# =====================================================================
# IMPORTANT: the entire script is wrapped in a { ... } brace block so
# bash buffers the WHOLE thing from stdin before executing any of it.
# Without this, when invoked via `curl ... | bash`, bash would start
# executing partially-read content. The first `exec < /dev/tty` would
# then close the pipe while curl was still writing to it, producing:
#   curl: (56) Failure writing output to destination, passed N returned 0
# The closing brace lives on the last line of the file.
{
# One paste, full stack. Detects AWS CloudShell vs local, subscribes to
# the Marketplace AMIs, deploys vController, waits for init, optionally
# adds KVO + vPB, and chains into the sensor playbook via quickstart.sh.
#
# Infrastructure is provisioned with CloudFormation by default (calls
# deploy/cloudformation/stack.yaml). Pass --iac terraform to use the
# Terraform stack under deploy/terraform instead. Both build the same
# resources: a VPC, public subnet(s), security groups, and the CloudLens
# appliance EC2 instances with Elastic IPs.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/deploy/deploy-stack.sh | bash
#   OR
#   bash deploy/deploy-stack.sh [--dry-run]
#
# Flags:
#   --dry-run        Walk the prompts, print every aws command, touch nothing
#   --region         AWS region (e.g. us-east-1)
#   --stack-name     CloudFormation stack / Terraform workspace name
#   --iac cfn|terraform   Which infra engine to use (default: cfn)
#   --no-kvo         Skip KVO deployment (default: prompt)
#   --with-kvo       Deploy KVO without prompting
#   --no-vpb         Skip vPB deployment
#   --no-sensors     Skip sensor chain at the end
#   --sensor-mode    standalone | kvo | none (which project key sensors use)
#   --kvo-codes      KVO activation code (CODE or CODE,QTY), repeatable
#   --cloud-config   KVO Cloud Config name to provision
#   --bootstrap-vpb  Run scripts/bootstrap-vpb.sh on the vPB over SSH
#   --adopt-vpb      Adopt the vPB into KVO
#   --wire-vpb-path  Wire the vPB traffic path + monitoring policy
#   --with-mirror    Also attempt the AWS mirror session (default: no)
#   --key-name NAME  EC2 key pair to attach (created if missing)
#   --rollback       Delete the stack we created on any failure
#   --resume         Continue from the first unfinished phase (default when
#                    there is no terminal). Phases are skipped only when the
#                    REAL system says the work is already done.
#   --fresh          Run every phase again (still deletes nothing)
#   --from PHASE     Start at PHASE: stack|wait|bootstrap|key|license|adopt|
#                    sensors|vpb|path|mirror
#   --only PHASE     Run exactly one phase
#   -h | --help      Show this banner and exit
# =====================================================================
set -euo pipefail
# NOTE on error reporting: the ERR trap below is NOT inherited by shell
# functions (that would need `set -E`), so a command failing inside a function
# under `set -e` kills the run with no message at all. That is exactly how a
# failing read-only probe once ended a deploy with a bare exit 254 and nothing
# printed. `set -E` is the wrong cure: with errtrace on, the trap also fires
# inside every guarded x="$(cmd || fallback)" and cries failure for normal
# things like a grep that matched nothing. The cure used here is the EXIT trap
# (on_exit), which fires once, in the real shell, and can never be silent.

# ---------------------------------------------------------------------
# Re-attach stdin to the terminal when invoked via `curl ... | bash`.
# Without this, every `read` would try to consume bytes from the curl
# pipe (which is the script itself), so the very first prompt fails.
# ---------------------------------------------------------------------
if [[ ! -t 0 ]] && [[ -r /dev/tty ]]; then
  # Non-fatal: in a headless environment /dev/tty may exist but not be
  # usable. Probe it on a spare fd (stderr silenced) before reattaching;
  # otherwise fall through (reads then come from the original stdin, which
  # is fine for fully non-interactive runs).
  if { exec 3</dev/tty; } 2>/dev/null; then
    exec <&3 3<&-
  fi
fi

# ---------------------------------------------------------------------
# Config + globals
# ---------------------------------------------------------------------
REPO_OWNER="Keysight-Tech"
REPO_NAME="cloudlens-ansible-aws"
REPO_RAW="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/main"
# BASH_SOURCE is unset when piped via curl | bash; fall back to $0/PWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$PWD")"

# CloudFormation stack names must match [a-zA-Z][-a-zA-Z0-9]* (no spaces).
valid_stack_name() { [[ "$1" =~ ^[a-zA-Z][-a-zA-Z0-9]*$ ]]; }
sanitize_stack_name() {
  local s="$1"
  s="${s// /-}"; s="$(printf '%s' "$s" | tr -cd 'a-zA-Z0-9-')"
  [[ "$s" =~ ^[a-zA-Z] ]] || s="cloudlens-${s}"
  printf '%s' "$s"
}

# CloudFormation template + Terraform dir (relative to repo root)
CFN_TEMPLATE_REL="deploy/cloudformation/stack.yaml"
TF_DIR_REL="deploy/terraform"

# ---------------------------------------------------------------------
# CloudLens Marketplace AMIs (us-east-1 defaults, all env/flag override).
# Marketplace AMIs require a one-time subscription (accept terms) on the
# AWS account before they can launch. Phase 4 handles that.
# ---------------------------------------------------------------------
DEFAULT_REGION="${CLOUDLENS_REGION:-us-east-1}"

# Regions the CloudFormation RegionMap ships AMIs for. Keep in lock-step with
# the RegionMap blocks in deploy/cloudformation/*.yaml.
SUPPORTED_CFN_REGIONS="us-east-1 us-east-2 us-west-1 us-west-2 ca-central-1 eu-west-1 eu-west-2 eu-central-1 ap-southeast-1 ap-southeast-2 ap-northeast-1 ap-south-1"

# CloudLens Marketplace product owner + AMI names (stable product-code suffix
# across every region). We resolve the region-correct AMI id from these names
# at runtime so Phase 4's subscription probe and the Terraform path work in any
# supported region. The template itself resolves AMIs from its own RegionMap.
MARKETPLACE_OWNER="679593333241"
CLMS_AMI_NAME="CLMS_6.12.1-32-prod-m4ufj3qrd4shm"
KVO_AMI_NAME="kvo-2.13.0-prod-ol4ektflnyxn2"
VPB_AMI_NAME="cloudlens-vpb-kvo-3.13.0-7-security-update-1-prod-ltzj3zafoveok"

# us-east-1 fallback ids (used if name resolution fails, e.g. offline dry-run).
# An explicit CLOUDLENS_*_AMI env var always wins and disables auto-resolution.
CLMS_AMI_OVERRIDE="${CLOUDLENS_VCONTROLLER_AMI:-}"
KVO_AMI_OVERRIDE="${CLOUDLENS_KVO_AMI:-}"
VPB_AMI_OVERRIDE="${CLOUDLENS_VPB_AMI:-}"
# vController (formerly CLMS)
CLMS_AMI="${CLMS_AMI_OVERRIDE:-ami-0bebd5e730315337e}"
CLMS_TYPE="${CLOUDLENS_VCONTROLLER_TYPE:-t3.xlarge}"
# KVO (Keysight Vision Orchestrator)
KVO_AMI="${KVO_AMI_OVERRIDE:-ami-017c0db8981569380}"
KVO_TYPE="${CLOUDLENS_KVO_TYPE:-c5.2xlarge}"
# vPB (Virtual Packet Broker) - management SSH is on port 9022, not 22
VPB_AMI="${VPB_AMI_OVERRIDE:-ami-0d00b42a9748d580c}"
VPB_TYPE="${CLOUDLENS_VPB_TYPE:-t3.xlarge}"
# Collector SVM (informational; not deployed by this stack directly)
COLLECTOR_AMI="${CLOUDLENS_COLLECTOR_AMI:-ami-0c22ade3667f8d35a}"

VPB_SSH_PORT="${CLOUDLENS_VPB_SSH_PORT:-9022}"

# Brownfield / access (all optional; blank keeps greenfield behavior). These
# map 1:1 onto the CloudFormation stack parameters of the same intent.
# Per-VM Name tag overrides. Blank keeps <stack>-vcontroller / -kvo / -vpb.
VCONTROLLER_NAME="${CLOUDLENS_VCONTROLLER_NAME:-}"
KVO_NAME="${CLOUDLENS_KVO_NAME:-}"
VPB_NAME="${CLOUDLENS_VPB_NAME:-}"

EXISTING_VPC_ID="${CLOUDLENS_EXISTING_VPC_ID:-}"
EXISTING_SUBNET_ID="${CLOUDLENS_EXISTING_SUBNET_ID:-}"
EXISTING_DATA_SUBNET_ID="${CLOUDLENS_EXISTING_DATA_SUBNET_ID:-}"
EXISTING_SG_ID="${CLOUDLENS_EXISTING_SG_ID:-}"
ASSIGN_PUBLIC_IP="${CLOUDLENS_ASSIGN_PUBLIC_IP:-yes}"   # yes | no

# Optional test workloads deployed INTO the stack's own subnet, so sensors reach
# vController privately and traffic mirroring between them and the vPB is
# possible. Comma list: ubuntu,rhel,windows (or "none").
TEST_VMS="${CLOUDLENS_TEST_VMS:-}"

# Naming
DEFAULT_STACK_NAME="${CLOUDLENS_STACK_NAME:-cloudlens-stack}"
DEFAULT_ADMIN_USER="${CLOUDLENS_ADMIN_USER:-admin}"   # KVO/vPB KCOS images log in as admin
KEY_NAME="${CLOUDLENS_KEY_NAME:-}"

# vPB multi-NIC counts (management NIC is always present, these are extra)
VPB_INGRESS_NICS="${CLOUDLENS_VPB_INGRESS_NICS:-1}"
VPB_EGRESS_NICS="${CLOUDLENS_VPB_EGRESS_NICS:-1}"

# Who is allowed to reach the mgmt/UI ports. Default open; narrow for prod.
ADMIN_CIDR="${CLOUDLENS_ADMIN_CIDR:-0.0.0.0/0}"

# Workload discovery tag: which AWS tag marks EC2s that should get the
# CloudLens sensor. Default cloudlens=yes (the canonical convention).
DISCOVERY_TAG_KEY="${CLOUDLENS_DISCOVERY_TAG_KEY:-cloudlens}"
DISCOVERY_TAG_VALUE="${CLOUDLENS_DISCOVERY_TAG_VALUE:-yes}"

# Rollback: when true AND we created the stack this run, on_error deletes it.
ROLLBACK_ON_FAIL="${CLOUDLENS_ROLLBACK_ON_FAIL:-false}"

WINDOWS_INSTANCE_PROFILE="${CLOUDLENS_WINDOWS_INSTANCE_PROFILE:-}"
# The Windows sensor is a native .exe served by the CloudLens Manager. Unlike
# Linux (a container pull) it needs the installer URL from the CLMS "Launch
# Agent -> Start New Agents -> Windows agents" link. Supply it here and the
# Windows host downloads it directly; otherwise the playbook asserts early with
# the fix rather than failing silently at the copy step.
WINDOWS_INSTALLER_URL="${CLOUDLENS_WINDOWS_INSTALLER_URL:-}"
SUMMARY_FILE="cloudlens-deploy-summary.txt"
LOG_FILE="cloudlens-deploy-stack.log"

# Flag defaults (set by argument parser)
DRY_RUN=false
IAC="cfn"                 # cfn | terraform
DEPLOY_KVO=""
DEPLOY_VPB=""
CHAIN_SENSORS=""
ARG_REGION=""
ARG_STACK=""

# ---------------------------------------------------------------------
# Re-run control.
#
# A re-run must never start from zero. Every phase that can be skipped has a
# read-only detect_* that asks the REAL system whether the work is already
# done. The state file below is a convenience only: it remembers inputs so a
# resume does not re-prompt for them. When the state file and reality disagree,
# reality wins and we say so out loud.
# ---------------------------------------------------------------------
RESUME_MODE=""            # resume | fresh | "" = ask (non-interactive: resume)
FROM_PHASE=""             # --from PHASE: start at this phase
ONLY_PHASE=""             # --only PHASE: run just this phase
RESUME_ACTIVE=false       # true once we decide to skip already-done phases
PHASE_SKIP_REASON=""
STATE_FILE=""

# Where phase 9 stores the working vController login + project key (mode 600).
VC_CREDS_FILE="${CLOUDLENS_VC_CREDS_FILE:-$HOME/.cloudlens-vcontroller-creds.json}"

# KVO admin used by the read-only probes. Same defaults the scripts/ use.
KVO_ADMIN_USER="${CLOUDLENS_KVO_ADMIN_USER:-admin}"
KVO_ADMIN_PASS="${CLOUDLENS_KVO_ADMIN_PASS:-admin}"

# Hard wall-clock limit for any single detection probe. Detection is meant to
# save time, so it is never allowed to cost more than a few seconds per check.
PROBE_TIMEOUT="${CLOUDLENS_PROBE_TIMEOUT:-25}"

# Can this shell talk to AWS at all? Answered once, in phase 2, and then used
# instead of letting nine probes fail one after another.
AWS_USABLE=false
AWS_UNUSABLE_WHY=""

# Detection results. "unknown" is a real answer and never means "missing":
# a probe that failed makes us ask, it does not make us re-create anything.
DET_STACK="unknown";     DET_STACK_ERR=""
DET_VC="unknown";        DET_VC_OK=false
DET_KEY="unknown";       DET_KEY_OK=false
DET_LICENSE="unknown";   DET_LICENSE_OK=false
DET_ADOPT="unknown";     DET_ADOPT_OK=false
DET_CC="unknown";        DET_CC_OK=false
DET_SENSORS="unknown";   DET_SENSORS_N=""
DET_VPB_KVO="unknown";   DET_VPB_KVO_OK=false
DET_MIRROR="unknown";    DET_MIRROR_OK=false
FOUND_DEPLOYMENT=false
VPB_IN_KVO=false

SKIP_STACK=false;   REASON_STACK=""
SKIP_WAIT=false;    REASON_WAIT=""
# The vPB bootstrap has no reliable read-only "already done" probe, so it is
# never auto-skipped. It stays in the phase list only so --from / --only can
# select it, and it is prompt-gated as before.
SKIP_BOOTSTRAP=false; REASON_BOOTSTRAP=""
SKIP_KEY=false;     REASON_KEY=""
SKIP_LICENSE=false; REASON_LICENSE=""
SKIP_ADOPT=false;   REASON_ADOPT=""
SKIP_SENSORS=false; REASON_SENSORS=""
SKIP_VPB=false;     REASON_VPB=""
SKIP_PATH=false;    REASON_PATH=""
SKIP_MIRROR=false;  REASON_MIRROR=""

# Post-deploy orchestration (phases 10-16). Blank = prompt, and every prompt
# falls back to the documented default when there is no terminal to ask on.
SENSOR_MODE=""                # standalone | kvo | none
KVO_CODES=()                  # --kvo-codes, repeatable, passed straight through
CLOUD_CONFIG_NAME="${CLOUDLENS_CLOUD_CONFIG:-cloudlens-aws}"
CLM_NAME_IN_KVO="${CLOUDLENS_CLM_NAME:-cloudlens-manager}"
VPB_DEVICE_NAME="${CLOUDLENS_VPB_DEVICE_NAME:-cloudlens-vpb}"
VPB_COLLECTION="${CLOUDLENS_VPB_COLLECTION:-}"
BOOTSTRAP_VPB=""
ADOPT_VPB=""
WIRE_VPB_PATH=""
WITH_MIRROR=""
MIRROR_ACCESS_KEY="${CLOUDLENS_MIRROR_ACCESS_KEY:-}"
MIRROR_SECRET_KEY="${CLOUDLENS_MIRROR_SECRET_KEY:-}"

# State trackers (filled as we go) for trap reporting
# SCRIPT_DONE means "this exit has already been explained to the operator",
# so the EXIT trap knows to stay quiet.
SCRIPT_DONE=false
PHASE_NAME="init"
CREATED_STACK=false
CLMS_PUBLIC_IP=""
KVO_PUBLIC_IP=""
VPB_PUBLIC_IP=""

# Facts discovered after the deploy (private addressing the appliances use to
# talk to each other) and the two project keys the sensor step chooses between.
CLMS_PRIVATE_IP=""
KVO_PRIVATE_IP=""
VPB_PRIVATE_IP=""
VPB_INGRESS_IP=""
STACK_VPC_ID=""
MGMT_SUBNET_ID=""
INGRESS_SUBNET_ID=""
EGRESS_SUBNET_ID=""
STACK_ZONE=""
VC_PROJECT_KEY=""             # minted directly on the vController (Phase 9)
KVO_PROJECT_KEY=""            # provisioned by the KVO Cloud Config (Phase 12)
SENSOR_PROJECT_KEY=""         # whichever of the two the chosen mode uses
KVO_CHAIN_OK=true             # licensing gates adoption; false stops the branch

# ---------------------------------------------------------------------
# Pretty output
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
# Logins, printed the moment a component can accept one.
#
# Phases 11-16 (licensing, adoption, sensors, vPB) take many minutes and every
# one of them shows up in a UI, so the operator is told how to sign in BEFORE
# the waiting starts rather than in a summary twenty five minutes later.
#
# Nothing below is invented. The vController password is whatever
# scripts/vcontroller_project_key.py established (read back from the mode-600
# creds file it writes); KVO uses the admin account every KVO script here
# authenticates with; the vPB is reached with the EC2 key pair on port 9022 and
# is managed by KVO with the device login scripts/vpb_kvo_adopt.py sends.
# ---------------------------------------------------------------------
VC_ADMIN_USER="${CLOUDLENS_VC_ADMIN_USER:-admin}"
# The AWS Marketplace vController ships with this and forces a change on the
# first login, which is exactly what phase 9 completes to a known value.
VC_FACTORY_PASS='Cl0udLens@dm!n'
PROJECT_NAME="${CLOUDLENS_PROJECT_NAME:-cloudlens-autopilot}"
# What scripts/vpb_kvo_adopt.py hands KVO as the device login. Not a knob:
# it is stated here so the operator can use the same login by hand.
VPB_DEVICE_USER="admin"
VPB_DEVICE_PASS="ixia"

# The vController admin password phase 9 actually set, or empty when phase 9
# has not run yet (then the factory default is still the live one).
vc_password_now() {
  local p="${CLOUDLENS_VC_PASSWORD:-}"
  if [[ -z "$p" && -f "$VC_CREDS_FILE" ]] && command -v python3 >/dev/null 2>&1; then
    p="$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("password",""))
except Exception: pass' "$VC_CREDS_FILE" 2>/dev/null)" || p=""
  fi
  printf '%s' "$p"
}

watch_header() {
  echo
  echo -e "${C_BOLD}  Log in now and watch the rest happen:${C_RESET}"
}

announce_vcontroller_login() {
  local ip="${CLMS_PUBLIC_IP:-}" pw
  [[ -n "$ip" && "$ip" != "None" ]] || return 0
  pw="$(vc_password_now)"
  watch_header
  echo "    https://${ip}/cloudlens/login"
  if [[ -n "$pw" ]]; then
    echo "    ${VC_ADMIN_USER} / ${pw}"
  else
    echo "    ${VC_ADMIN_USER} / ${VC_FACTORY_PASS}   (the first login forces a change)"
  fi
  # Which project the sensors land in depends on the sensor mode, and this line
  # used to name the standalone one unconditionally. In KVO mode the sensors
  # register with the key the Cloud Config provisioned, into KVO_<cloud-config>,
  # so pointing the operator at 'cloudlens-autopilot' sent them to a project that
  # stays permanently empty while they wonder where their sensors went.
  local _proj="$PROJECT_NAME"
  if [[ "$SENSOR_MODE" == "kvo" && -n "${CLOUD_CONFIG_NAME:-}" ]]; then
    _proj="KVO_${CLOUD_CONFIG_NAME}"
  fi
  echo "  Sensors will appear under project '${_proj}' as they register."
  echo
}

announce_kvo_login() {
  local ip="${KVO_PUBLIC_IP:-}"
  [[ -n "$ip" && "$ip" != "None" ]] || return 0
  watch_header
  echo "    https://${ip}/"
  echo "    ${KVO_ADMIN_USER} / ${KVO_ADMIN_PASS}"
  echo "  Licensing, the adopted vController and the Visibility Fabric appear"
  echo "  here as the phases below build them."
  echo "  A freshly booted KVO shows its EULA first: accept it to reach the login."
  echo
}

announce_vpb_login() {
  local ip="${VPB_PUBLIC_IP:-}"
  [[ -n "$ip" && "$ip" != "None" ]] || return 0
  watch_header
  echo "    ssh -i ${KEY_PEM:-~/.ssh/${KEY_NAME}.pem} -p ${VPB_SSH_PORT} ${ADMIN_USERNAME:-admin}@${ip}"
  echo "    key-pair auth (no password), then 'sudo vpb' for the CLI"
  echo "    device login KVO manages it with: ${VPB_DEVICE_USER} / ${VPB_DEVICE_PASS}"
  echo "  It appears in KVO as device '${VPB_DEVICE_NAME}' once phase 14 adopts it."
  echo
}

# The one block an engineer screenshots or pastes to a customer. Printed to the
# terminal AND into the summary file, so the two can never drift.
login_block() {
  local pw; pw="$(vc_password_now)"
  echo "--- Logins (everything you need to sign in) ---"
  echo "vController UI:     https://${CLMS_PUBLIC_IP}/cloudlens/login"
  echo "  username:         ${VC_ADMIN_USER}"
  if [[ -n "$pw" ]]; then
    echo "  password:         ${pw}"
  else
    echo "  password:         ${VC_FACTORY_PASS}   (factory default, changes on first login)"
  fi
  echo "  project:          ${PROJECT_NAME}   (sensors register into it)"
  if [[ "$DEPLOY_KVO" == "true" ]]; then
    echo "KVO UI:             https://${KVO_PUBLIC_IP}/"
    echo "  username:         ${KVO_ADMIN_USER}"
    echo "  password:         ${KVO_ADMIN_PASS}"
    echo "  cloud config:     ${CLOUD_CONFIG_NAME}"
  fi
  if [[ "$DEPLOY_VPB" == "true" ]]; then
    echo "vPB SSH:            ssh -i ${KEY_PEM:-~/.ssh/${KEY_NAME}.pem} -p ${VPB_SSH_PORT} ${ADMIN_USERNAME:-admin}@${VPB_PUBLIC_IP}"
    echo "  auth:             EC2 key pair ${KEY_NAME} (no password), then 'sudo vpb'"
    echo "  device login:     ${VPB_DEVICE_USER} / ${VPB_DEVICE_PASS}   (what KVO manages it with)"
    echo "  device name:      ${VPB_DEVICE_NAME}"
  fi
  echo "Credentials file:   ${VC_CREDS_FILE} (mode 600)"
  echo "Sensor config:      customer_input.yaml (mode 600, holds the project key)"
  echo
  # Learned the expensive way: three activation codes worth 500 counts each
  # came back availableQuantity=0 because the KVO they were activated on had
  # been deleted. Activation binds the quantity to a host, and deleting the
  # host does not give it back. Nothing warned about this anywhere.
  if [[ "$DEPLOY_KVO" == "true" ]]; then
    echo
    echo "BEFORE YOU DELETE THIS STACK: KVO licences are bound to the KVO host."
    echo "Deleting the stack destroys the KVO and STRANDS whatever quantity was"
    echo "activated on it. The entitlement is not returned and cannot be"
    echo "reactivated elsewhere without Keysight rehosting it. Deactivate in"
    echo "Settings > Product Licensing first, or accept the loss knowingly."
    echo
  fi
  echo "THESE FILES CONTAIN CREDENTIALS: ${LOG_FILE} and ${SUMMARY_FILE} record"
  echo "everything printed above, including the passwords. Both are mode 600 and"
  echo "git-ignored. Treat them like a password file: do not paste them wholesale."
}

# ---------------------------------------------------------------------
# Prompting that can never block.
#
# The /dev/tty re-attach at the top of this file is what makes `curl | bash`
# interactive at all: without it stdin is the SCRIPT, not the terminal. When
# that re-attach did not happen (headless CI, `echo "" | bash ...`, a container
# with no controlling terminal) stdin is not a TTY and there is nobody to
# answer, so every prompt below skips the read entirely and takes its default.
# That is the single check: -t 0 AFTER the re-attach.
# ---------------------------------------------------------------------
INTERACTIVE=false
[[ -t 0 ]] && INTERACTIVE=true

# ask "prompt" "default" -> echoes the answer (default when not interactive)
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
# Phase names, and the decision of what to run.
#
# Short, STABLE names, not numbers: numbers shift every time a phase is added
# and this script has to survive years of that.
#   stack=6  wait=7  bootstrap=8  key=9  license=11  adopt=12  sensors=13
#   vpb=14  path=15  mirror=16
# ---------------------------------------------------------------------
PHASE_ORDER="stack wait bootstrap key license adopt sensors vpb mirror path"

phase_index() {
  local want="$1" i=1 p
  for p in $PHASE_ORDER; do
    [[ "$p" == "$want" ]] && { printf '%s' "$i"; return 0; }
    i=$((i+1))
  done
  return 1
}

phase_label() {
  case "$1" in
    stack)   printf '%s' "Deploy the stack" ;;
    wait)    printf '%s' "Wait for the vController API" ;;
    bootstrap) printf '%s' "vPB bootstrap over SSH" ;;
    key)     printf '%s' "vController project key" ;;
    license) printf '%s' "KVO licensing" ;;
    adopt)   printf '%s' "Adopt CLMS into KVO" ;;
    sensors) printf '%s' "Sensors" ;;
    vpb)     printf '%s' "vPB in KVO" ;;
    path)    printf '%s' "vPB traffic path" ;;
    mirror)  printf '%s' "AWS mirror" ;;
    *)       printf '%s' "$1" ;;
  esac
}

upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

# run_phase NAME -> 0 run it, 1 skip it (why: PHASE_SKIP_REASON)
run_phase() {
  local name="$1" idx want skipvar reasonvar
  PHASE_SKIP_REASON=""
  if [[ -n "$ONLY_PHASE" ]]; then
    [[ "$ONLY_PHASE" == "$name" ]] && return 0
    PHASE_SKIP_REASON="--only ${ONLY_PHASE}"
    return 1
  fi
  if [[ -n "$FROM_PHASE" ]]; then
    idx="$(phase_index "$name")"; want="$(phase_index "$FROM_PHASE")"
    if (( idx < want )); then PHASE_SKIP_REASON="--from ${FROM_PHASE}"; return 1; fi
  fi
  # --fresh (RESUME_ACTIVE=false) stops skipping. It never deletes anything:
  # it just runs every phase again.
  [[ "$RESUME_ACTIVE" == "true" ]] || return 0
  skipvar="SKIP_$(upper "$name")"; reasonvar="REASON_$(upper "$name")"
  if [[ "${!skipvar:-false}" == "true" ]]; then
    PHASE_SKIP_REASON="${!reasonvar:-already done}"
    return 1
  fi
  return 0
}

skip_note() { note "Skipping $1: ${PHASE_SKIP_REASON}."; }

# Is this phase even part of this run? A stack with no KVO never licenses one.
# Blank toggles mean "not decided yet", which counts as applicable.
phase_applicable() {
  case "$1" in
    bootstrap|vpb|path) [[ "$DEPLOY_VPB" == "false" ]] && return 1 ;;
  esac
  case "$1" in
    license|adopt|mirror|vpb|path) [[ "$DEPLOY_KVO" == "false" ]] && return 1 ;;
  esac
  case "$1" in
    sensors) [[ "$CHAIN_SENSORS" == "false" || "$SENSOR_MODE" == "none" ]] && return 1 ;;
  esac
  return 0
}

first_pending_phase() {
  local p skipvar
  for p in $PHASE_ORDER; do
    skipvar="SKIP_$(upper "$p")"
    [[ "${!skipvar:-false}" == "true" ]] && continue
    phase_applicable "$p" || continue
    phase_label "$p"; return 0
  done
  printf '%s' "the final summary"
}

# ---------------------------------------------------------------------
# State file: inputs and outcomes only, NEVER a secret.
#
# Named per stack + region so two deployments in one directory cannot collide.
# It is parsed line by line and never sourced, so a corrupt file is a non-event:
# unreadable lines are ignored and detection still works from reality alone.
# ---------------------------------------------------------------------
state_init() {
  [[ -n "${STACK_NAME:-}" && -n "${REGION:-}" ]] || return 0
  STATE_FILE=".cloudlens-deploy-${STACK_NAME}-${REGION}.state"
  if [[ ! -f "$STATE_FILE" ]]; then
    if ! ( umask 077; : > "$STATE_FILE" ) 2>/dev/null; then
      warn "Could not create ${STATE_FILE}; continuing without a state file."
      STATE_FILE=""
      return 0
    fi
  fi
  chmod 600 "$STATE_FILE" 2>/dev/null || true
}

# state_get KEY -> value on stdout, exit 1 when absent/unreadable
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
  # Secrets do not live here. The project key stays in the mode-600 creds file
  # and is referenced, never copied. This guard is the backstop for that rule.
  case "$k" in
    *PASSWORD*|*SECRET*|*PROJECT_KEY*|*ACCESS_KEY*|*TOKEN*|*CREDENTIAL*)
      warn "Refusing to write '${k}' to the state file: that name looks like a secret."
      return 0 ;;
  esac
  [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || return 0
  v="${v//$'\n'/ }"
  tmp="${STATE_FILE}.tmp.$$"
  ( umask 077; { grep -v "^${k}=" "$STATE_FILE" 2>/dev/null || true
                 printf '%s=%s\n' "$k" "$v"; } > "$tmp" ) 2>/dev/null || return 0
  chmod 600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$STATE_FILE" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
}

# state_phase NAME done|failed|skipped [detail]
# A dry run records itself as a dry run: it did not actually do the work.
state_phase() {
  local outcome="$2"
  [[ "$DRY_RUN" == "true" ]] && outcome="dry-run ${outcome}"
  state_set "PHASE_$(upper "$1")" "${outcome} at $(date -u +%FT%TZ)${3:+ (${3})}"
}

# Printed whenever an SSH step cannot find its private key. Every SSH-based
# phase (vPB bootstrap, sensor install, vPB adoption) needs the .pem ON THIS
# machine; AWS never returns it. Give the exact fix and remind them re-running
# resumes and retries the step, so a missing key is recoverable, not fatal.
pem_help() {
  note "SSH steps need ${KEY_NAME}.pem on THIS machine (AWS never returns a private key)."
  note "Fix it, then re-run this script from here: it resumes and retries this step."
  note "  1) put the file here  (CloudShell: Actions > Upload file drops it in ~/)"
  note "  2) mkdir -p ~/.ssh && mv ~/${KEY_NAME}.pem ~/.ssh/ && chmod 400 ~/.ssh/${KEY_NAME}.pem"
  note "  3) re-run:  bash <(curl -sSL ${REPO_RAW}/deploy/deploy-stack.sh)"
  note "Or start a fresh run and pick a NEW key pair, so the .pem is written locally."
}

# ---------------------------------------------------------------------
# Detection: read-only, side-effect free, and it runs even under --dry-run.
#
# Every function here only READS. None of them creates, modifies or deletes
# anything, which is exactly why they are safe to run in a dry run: detection
# that lied in dry-run would defeat the point of dry-run.
# ---------------------------------------------------------------------
# probe CMD... -> stdout of CMD. ALWAYS exits 0, ALWAYS returns within
# PROBE_TIMEOUT seconds.
#
# Two hard rules live in this one function:
#
#   1. A probe may never abort the run. Under `set -e` an assignment like
#      x="$(aws ...)" carries the command's exit status, so a single expired
#      SSO token used to kill the whole script (silently, because the ERR trap
#      is not inherited into functions without -E). Routing every probe through
#      here makes that structurally impossible instead of relying on remembering
#      `|| true` at each call site.
#   2. A probe may never hang the run. `timeout(1)` is not on macOS, so the
#      watchdog is done by hand.
#
# Anything that fails, times out, or is killed yields EMPTY output, and every
# caller reads empty (or unparseable) as "unknown": never as "missing", and
# never as "already done".
#
# Detection must also be unable to trip the failure traps. A non-zero exit
# inside a read-only probe is NORMAL: a stack that does not exist, a grep that
# matched nothing, an appliance that is not up yet. errexit and the ERR trap
# are lifted for the length of the checks and restored exactly as found.
PROBE_ERREXIT_WAS=false
probe_guard_begin() {
  PROBE_ERREXIT_WAS=false
  case "$-" in *e*) PROBE_ERREXIT_WAS=true ;; esac
  set +e
  trap - ERR
  return 0
}
probe_guard_end() {
  trap on_error ERR
  [[ "$PROBE_ERREXIT_WAS" == "true" ]] && set -e
  return 0
}

PROBE_TICK=""
probe() {
  local out="" tmp pid ticks=0 limit
  if [[ -z "$PROBE_TICK" ]]; then
    # Sub-second sleeps are not universal (busybox). Establish which we have.
    if sleep 0.2 >/dev/null 2>&1; then PROBE_TICK="0.2"; else PROBE_TICK="1"; fi
  fi
  if [[ "$PROBE_TICK" == "0.2" ]]; then limit=$(( PROBE_TIMEOUT * 5 )); else limit="$PROBE_TIMEOUT"; fi

  tmp="$(mktemp "${TMPDIR:-/tmp}/cloudlens-probe.XXXXXX" 2>/dev/null)" || tmp=""
  if [[ -z "$tmp" ]]; then
    # No temp file: run it inline. Still cannot fail the script.
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

ro_aws() { probe aws "${AWS_REGION_ARG[@]}" "$@"; }

det_clean() { local v="$1"; [[ "$v" == "None" ]] && v=""; printf '%s' "$v"; }

# First NON-BLANK line. The AWS CLI v2 leads its error output with an empty
# line, so a plain `head -1` reports "no answer" for an error that is sitting
# right there on line 2.
first_line() {
  printf '%s\n' "$1" | grep -v '^[[:space:]]*$' 2>/dev/null | head -1 || true
}

det_cfn_output() {
  det_clean "$(ro_aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='${1}'].OutputValue | [0]" --output text)"
}

# Public IP if there is one, otherwise the private IP (--no-public-ip deploys).
det_ec2_addr() {
  local v
  v="$(det_clean "$(ro_aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=${1}" "Name=instance-state-name,Values=running" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)")"
  [[ -z "$v" ]] && v="$(det_clean "$(ro_aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=${1}" "Name=instance-state-name,Values=running" \
        --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)")"
  printf '%s' "$v"
}

# Phase 6. Statuses are NOT all the same thing, so each is handled distinctly.
# A probe that cannot answer leaves DET_STACK as "unknown", which is neither
# "missing" (would re-create) nor "complete" (would skip). Unknown asks.
detect_stack() {
  local out
  DET_STACK="unknown"; DET_STACK_ERR=""
  if [[ "$AWS_USABLE" != "true" ]]; then
    DET_STACK_ERR="${AWS_UNUSABLE_WHY:-AWS is not reachable from this shell}"
    return 0
  fi
  if [[ "$IAC" == "terraform" ]]; then
    # There is no CloudFormation stack to ask about. The closest read-only
    # truth is whether the vController this workspace names is running.
    if [[ -n "$(det_ec2_addr "${VCONTROLLER_NAME:-${STACK_NAME}-vcontroller}")" ]]; then
      DET_STACK="APPLIED"
    else
      DET_STACK="MISSING"
    fi
    return 0
  fi
  # stderr is merged in on purpose: "does not exist" is the answer we want, and
  # anything else is an error we must NOT read as "no stack here".
  out="$(PROBE_MERGE_STDERR=1 ro_aws cloudformation describe-stacks \
          --stack-name "$STACK_NAME" --query 'Stacks[0].StackStatus' --output text)"
  out="$(first_line "$out")"
  if [[ "$out" =~ ^[A-Z][A-Z_]*$ ]]; then
    DET_STACK="$out"
  elif [[ "$out" == *"does not exist"* ]]; then
    DET_STACK="MISSING"
  else
    DET_STACK="unknown"
    DET_STACK_ERR="${out:-the AWS CLI did not answer (timeout or no network)}"
  fi
  return 0
}

stack_usable() {
  case "$DET_STACK" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE|APPLIED) return 0 ;;
  esac
  return 1
}
# CloudFormation can never update a stack in these states: it has to be deleted
# first. We print the command and let the human run it. We never delete.
stack_needs_delete() {
  case "$DET_STACK" in
    ROLLBACK_COMPLETE|ROLLBACK_FAILED|CREATE_FAILED|DELETE_FAILED) return 0 ;;
  esac
  return 1
}
stack_in_flight() {
  case "$DET_STACK" in *_IN_PROGRESS) return 0 ;; esac
  return 1
}

detect_addresses() {
  if [[ "$IAC" == "terraform" ]]; then
    [[ -z "$CLMS_PUBLIC_IP" ]] && CLMS_PUBLIC_IP="$(det_ec2_addr "${VCONTROLLER_NAME:-${STACK_NAME}-vcontroller}")"
    [[ -z "$KVO_PUBLIC_IP"  ]] && KVO_PUBLIC_IP="$(det_ec2_addr "${KVO_NAME:-${STACK_NAME}-kvo}")"
    [[ -z "$VPB_PUBLIC_IP"  ]] && VPB_PUBLIC_IP="$(det_ec2_addr "${VPB_NAME:-${STACK_NAME}-vpb}")"
    return 0
  fi
  [[ -z "$CLMS_PUBLIC_IP" ]] && CLMS_PUBLIC_IP="$(det_cfn_output VcontrollerAddress)"
  [[ -z "$KVO_PUBLIC_IP"  ]] && KVO_PUBLIC_IP="$(det_cfn_output KvoAddress)"
  [[ -z "$VPB_PUBLIC_IP"  ]] && VPB_PUBLIC_IP="$(det_cfn_output VpbAddress)"
  [[ -z "$CLMS_PUBLIC_IP" ]] && CLMS_PUBLIC_IP="$(det_ec2_addr "${VCONTROLLER_NAME:-${STACK_NAME}-vcontroller}")"
  [[ -z "$KVO_PUBLIC_IP"  ]] && KVO_PUBLIC_IP="$(det_ec2_addr "${KVO_NAME:-${STACK_NAME}-kvo}")"
  [[ -z "$VPB_PUBLIC_IP"  ]] && VPB_PUBLIC_IP="$(det_ec2_addr "${VPB_NAME:-${STACK_NAME}-vpb}")"
  return 0
}

# Phase 7. Same probe the wait loop uses: POST to the real login route. nginx
# serves the SPA long before the API answers, so only these codes mean serving.
vc_api_probe() {
  local code
  code="$(probe curl -sk -o /dev/null -w '%{http_code}' \
            --connect-timeout 5 --max-time 8 -X POST \
            -H 'Content-Type: application/json' \
            -d '{"username":"__probe__","password":"__probe__"}' \
            "https://${1}/cloudlens/api/v1/identity/login")"
  [[ "$code" =~ ^[0-9]{3}$ ]] || code="000"
  printf '%s' "$code"
}

detect_vcontroller() {
  DET_VC="unknown"; DET_VC_OK=false
  [[ -n "$CLMS_PUBLIC_IP" ]] || { DET_VC="no address yet"; return 0; }
  local code; code="$(vc_api_probe "$CLMS_PUBLIC_IP")"
  case "$code" in
    200|400|401|403|422) DET_VC="serving at ${CLMS_PUBLIC_IP}"; DET_VC_OK=true ;;
    000) DET_VC="unknown (probe did not answer)" ;;
    *)   DET_VC="not serving yet (HTTP ${code})" ;;
  esac
}

# Phase 9. Re-running the key step re-rotates an already-rotated admin password,
# which fails, so reusing an existing key for THIS vController matters.
vc_key_from_creds() {
  local k=""
  [[ -f "$VC_CREDS_FILE" ]] || return 1
  if command -v python3 >/dev/null 2>&1; then
    k="$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("project_key",""))
except Exception: pass' "$VC_CREDS_FILE" 2>/dev/null)" || k=""
  fi
  [[ -z "$k" ]] && { k="$(grep -o '"project_key"[^,}]*' "$VC_CREDS_FILE" 2>/dev/null | cut -d'"' -f4)" || k=""; }
  [[ -n "$k" ]] || return 1
  printf '%s' "$k"
}

detect_project_key() {
  DET_KEY="unknown"; DET_KEY_OK=false
  if [[ ! -f "$VC_CREDS_FILE" ]]; then DET_KEY="not present"; return 0; fi
  if ! vc_key_from_creds >/dev/null 2>&1; then DET_KEY="not present"; return 0; fi
  # The creds file records the UI url, so it can be matched to this vController.
  if [[ -n "$CLMS_PUBLIC_IP" ]] && ! grep -q "$CLMS_PUBLIC_IP" "$VC_CREDS_FILE" 2>/dev/null; then
    DET_KEY="present, but for a different vController"
    return 0
  fi
  DET_KEY="present"; DET_KEY_OK=true
}

# ---- KVO probes ------------------------------------------------------
# A freshly booted KVO 302-redirects everything to /eula/static/ until the EULA
# is accepted. That is "not ready", not an error, and it is never "licensed".
KVO_TOKEN_CACHE=""
KVO_TOKEN_STATE=""    # "" untried | ok | eula | fail

# kvo_auth sets KVO_TOKEN_CACHE / KVO_TOKEN_STATE as GLOBALS, so it must be
# called as a plain statement, never inside $( ). A command substitution runs
# in a subshell and the state would be lost the moment it returned.
kvo_auth() {
  local host="$1" body code tok
  [[ "$KVO_TOKEN_STATE" == "ok" ]] && return 0
  [[ -n "$KVO_TOKEN_STATE" ]] && return 1
  if ! command -v python3 >/dev/null 2>&1; then KVO_TOKEN_STATE="fail"; return 1; fi
  body="$(probe curl -sk --connect-timeout 5 --max-time 15 -w $'\n%{http_code}' -X POST \
           -H 'Content-Type: application/x-www-form-urlencoded' \
           --data "grant_type=password&client_id=vision-orchestrator&username=${KVO_ADMIN_USER}&password=${KVO_ADMIN_PASS}" \
           "https://${host}/auth/realms/keysight/protocol/openid-connect/token")" || body=""
  code="${body##*$'\n'}"; body="${body%$'\n'*}"
  case "$code" in
    30[0-9]) KVO_TOKEN_STATE="eula"; return 1 ;;
    200) ;;
    *)   KVO_TOKEN_STATE="fail"; return 1 ;;
  esac
  tok="$(printf '%s' "$body" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("access_token",""))
except Exception: pass' 2>/dev/null)" || tok=""
  [[ -n "$tok" ]] || { KVO_TOKEN_STATE="fail"; return 1; }
  KVO_TOKEN_CACHE="$tok"; KVO_TOKEN_STATE="ok"
  return 0
}

# Why the KVO could not be talked to, in English.
kvo_auth_why() {
  case "$KVO_TOKEN_STATE" in
    eula) printf '%s' "KVO is still on the EULA page" ;;
    *)    printf '%s' "could not authenticate to KVO" ;;
  esac
}

# kvo_gql HOST QUERY -> raw JSON. Read-only, and it only READS the cached
# token, so it is safe to call from inside $( ). Queries here carry no double
# quotes on purpose: names are matched in the parser, not inlined.
kvo_gql() {
  local host="$1" query="$2"
  [[ -n "$KVO_TOKEN_CACHE" ]] || return 1
  probe curl -sk --connect-timeout 5 --max-time 20 -X POST \
       -H "Authorization: Bearer ${KVO_TOKEN_CACHE}" \
       -H 'Content-Type: application/json' \
       --data "{\"query\":\"${query}\"}" \
       "https://${host}/public/graphql"
}

# Phase 11. REST, /api/v2/licensing/licenses, exactly what kvo_license.py reads.
# Re-activating a consumed code spends real entitlement, so "unknown" here has
# to stay "unknown" and become a question, never an assumption.
detect_license() {
  DET_LICENSE="unknown"; DET_LICENSE_OK=false
  [[ -n "$KVO_PUBLIC_IP" ]] || { DET_LICENSE="n/a (no KVO)"; return 0; }
  local body n
  if ! kvo_auth "$KVO_PUBLIC_IP"; then
    case "$KVO_TOKEN_STATE" in
      eula) DET_LICENSE="not licensed (KVO is still on the EULA page)" ;;
      *)    DET_LICENSE="unknown ($(kvo_auth_why))" ;;
    esac
    return 0
  fi
  body="$(probe curl -sk --connect-timeout 5 --max-time 20 \
           -H "Authorization: Bearer ${KVO_TOKEN_CACHE}" \
           "https://${KVO_PUBLIC_IP}/api/v2/licensing/licenses")" || body=""
  n="$(printf '%s' "$body" | python3 -c 'import json,sys
try: d = json.load(sys.stdin)
except Exception: print("?"); raise SystemExit
print(len(d) if isinstance(d, list) else "?")' 2>/dev/null)" || n=""
  case "$n" in
    ""|"?") DET_LICENSE="unknown (licensing API answer not understood)" ;;
    0)      DET_LICENSE="not licensed" ;;
    *)      DET_LICENSE="licensed (${n} installed)"; DET_LICENSE_OK=true ;;
  esac
}

# Phase 12a. Re-adopting an adopted manager errors, so CONNECTED means skip.
detect_adopt() {
  DET_ADOPT="unknown"; DET_ADOPT_OK=false
  [[ -n "$KVO_PUBLIC_IP" ]] || { DET_ADOPT="n/a (no KVO)"; return 0; }
  kvo_auth "$KVO_PUBLIC_IP" || { DET_ADOPT="unknown ($(kvo_auth_why))"; return 0; }
  local body status
  body="$(kvo_gql "$KVO_PUBLIC_IP" "{ cloudLensManagersFeed { cloudLensManagers { name ip status } } }")" \
    || { DET_ADOPT="unknown (KVO query failed)"; return 0; }
  status="$(printf '%s' "$body" | CLM="$CLM_NAME_IN_KVO" python3 -c '
import json,os,sys
try: d = json.load(sys.stdin)
except Exception: print("ERR"); raise SystemExit
if d.get("errors"): print("ERR"); raise SystemExit
rows = ((d.get("data") or {}).get("cloudLensManagersFeed") or {}).get("cloudLensManagers") or []
want = os.environ["CLM"]
for r in rows:
    if r.get("name") == want:
        print(r.get("status") or "UNKNOWN"); break
else:
    print("MISSING")' 2>/dev/null)" || status=""
  case "$status" in
    CONNECTED) DET_ADOPT="adopted and CONNECTED"; DET_ADOPT_OK=true ;;
    MISSING)   DET_ADOPT="not done" ;;
    ""|ERR)    DET_ADOPT="unknown (KVO answer not understood)" ;;
    *)         DET_ADOPT="adopted, status ${status}" ;;
  esac
}

# Phase 12b. Exists by name means reuse it, and reuse its project key.
detect_cloud_config() {
  DET_CC="unknown"; DET_CC_OK=false
  [[ -n "$KVO_PUBLIC_IP" ]] || { DET_CC="n/a (no KVO)"; return 0; }
  kvo_auth "$KVO_PUBLIC_IP" || { DET_CC="unknown ($(kvo_auth_why))"; return 0; }
  local body found
  body="$(kvo_gql "$KVO_PUBLIC_IP" "{ cloudConfigs { name } }")" \
    || { DET_CC="unknown (KVO query failed)"; return 0; }
  found="$(printf '%s' "$body" | CC="$CLOUD_CONFIG_NAME" python3 -c '
import json,os,sys
try: d = json.load(sys.stdin)
except Exception: print("ERR"); raise SystemExit
if d.get("errors"): print("ERR"); raise SystemExit
names = [c.get("name") for c in ((d.get("data") or {}).get("cloudConfigs") or [])]
print("YES" if os.environ["CC"] in names else "NO")' 2>/dev/null)" || found=""
  case "$found" in
    YES) DET_CC="present (${CLOUD_CONFIG_NAME})"; DET_CC_OK=true ;;
    NO)  DET_CC="not done" ;;
    *)   DET_CC="unknown (KVO answer not understood)" ;;
  esac
}

# The key the Cloud Config provisioned. Read back so a resume does not have to
# re-create anything to get it. Printed nowhere, written to no file but the
# existing mode-600 customer_input.yaml.
# Call kvo_auth BEFORE this (it is used inside $( ), so it cannot authenticate
# for itself: the cached token would not survive the subshell).
kvo_cloud_config_key() {
  local body key
  body="$(kvo_gql "$1" "{ customClouds { name clmsProjectId clmsProjectApiKey } }")" || return 1
  key="$(printf '%s' "$body" | CC="$CLOUD_CONFIG_NAME" python3 -c '
import json,os,sys
try: d = json.load(sys.stdin)
except Exception: raise SystemExit
for c in ((d.get("data") or {}).get("customClouds") or []):
    if c.get("name") == os.environ["CC"]:
        print(c.get("clmsProjectApiKey") or ""); break' 2>/dev/null)" || key=""
  [[ -n "$key" ]] || return 1
  printf '%s' "$key"
}

# Phase 13. Best effort: sensorCount comes from KVO, so with no KVO (or an
# unreadable one) this stays unknown and the sensor step is OFFERED, not
# skipped. Sensor install is idempotent enough to re-run.
detect_sensors() {
  DET_SENSORS="unknown"; DET_SENSORS_N=""
  [[ -n "$KVO_PUBLIC_IP" ]] || { DET_SENSORS="unknown (no KVO to ask)"; return 0; }
  kvo_auth "$KVO_PUBLIC_IP" || { DET_SENSORS="unknown ($(kvo_auth_why))"; return 0; }
  local body n
  body="$(kvo_gql "$KVO_PUBLIC_IP" "{ cloudLensManagersFeed { cloudLensManagers { name sensorCount } } }")" \
    || { DET_SENSORS="unknown (KVO query failed)"; return 0; }
  n="$(printf '%s' "$body" | CLM="$CLM_NAME_IN_KVO" python3 -c '
import json,os,sys
try: d = json.load(sys.stdin)
except Exception: raise SystemExit
if d.get("errors"): raise SystemExit
rows = ((d.get("data") or {}).get("cloudLensManagersFeed") or {}).get("cloudLensManagers") or []
want = os.environ["CLM"]
tot = 0
for r in rows:
    if r.get("name") == want:
        tot = r.get("sensorCount") or 0; break
print(int(tot))' 2>/dev/null)" || n=""
  if [[ "$n" =~ ^[0-9]+$ ]]; then
    DET_SENSORS="${n} registered"; DET_SENSORS_N="$n"
  else
    DET_SENSORS="unknown (KVO does not report a sensor count here)"
  fi
}

# Phase 14. Device present in KVO means the adoption already happened.
detect_vpb_in_kvo() {
  DET_VPB_KVO="unknown"; DET_VPB_KVO_OK=false
  [[ -n "$KVO_PUBLIC_IP" ]] || { DET_VPB_KVO="n/a (no KVO)"; return 0; }
  kvo_auth "$KVO_PUBLIC_IP" || { DET_VPB_KVO="unknown ($(kvo_auth_why))"; return 0; }
  local body found
  body="$(kvo_gql "$KVO_PUBLIC_IP" "{ devices { uid name } }")" \
    || { DET_VPB_KVO="unknown (KVO query failed)"; return 0; }
  found="$(printf '%s' "$body" | DEV="$VPB_DEVICE_NAME" python3 -c '
import json,os,sys
try: d = json.load(sys.stdin)
except Exception: print("ERR"); raise SystemExit
if d.get("errors"): print("ERR"); raise SystemExit
names = [x.get("name") for x in ((d.get("data") or {}).get("devices") or [])]
print("YES" if os.environ["DEV"] in names else "NO")' 2>/dev/null)" || found=""
  case "$found" in
    YES) DET_VPB_KVO="present (${VPB_DEVICE_NAME})"; DET_VPB_KVO_OK=true; VPB_IN_KVO=true ;;
    NO)  DET_VPB_KVO="not done" ;;
    *)   DET_VPB_KVO="unknown (KVO answer not understood)" ;;
  esac
}

# Phase 16. Plain EC2 reads: targets, filters, sessions. Any answer that is not
# three plain integers is "unknown", never zero.
detect_mirror() {
  DET_MIRROR="unknown"; DET_MIRROR_OK=false
  local t f s_
  if [[ "$AWS_USABLE" != "true" ]]; then
    DET_MIRROR="unknown (could not check: ${AWS_UNUSABLE_WHY:-AWS is not reachable})"
    return 0
  fi
  t="$(ro_aws ec2 describe-traffic-mirror-targets  --query 'length(TrafficMirrorTargets)'  --output text)"
  f="$(ro_aws ec2 describe-traffic-mirror-filters  --query 'length(TrafficMirrorFilters)'  --output text)"
  s_="$(ro_aws ec2 describe-traffic-mirror-sessions --query 'length(TrafficMirrorSessions)' --output text)"
  if [[ "$t" =~ ^[0-9]+$ && "$f" =~ ^[0-9]+$ && "$s_" =~ ^[0-9]+$ ]]; then
    DET_MIRROR="${t} target(s), ${f} filter(s), ${s_} session(s)"
    [[ "$s_" != "0" ]] && DET_MIRROR_OK=true
  else
    DET_MIRROR="unknown (could not read the traffic mirror resources)"
  fi
  return 0
}

detect_all() {
  # Belt and braces. Every probe below is individually guarded, but detection
  # must NEVER be the thing that ends a deploy, including after some future
  # edit adds a tenth probe and forgets a guard.
  probe_guard_begin

  detect_stack
  if stack_usable || stack_in_flight; then
    detect_addresses
  fi
  detect_vcontroller
  detect_project_key
  detect_license
  detect_adopt
  detect_cloud_config
  detect_sensors
  detect_vpb_in_kvo
  detect_mirror

  probe_guard_end
  return 0
}

# ---------------------------------------------------------------------
# The resume banner: what reality says, and where we would continue from.
# ---------------------------------------------------------------------
print_resume_banner() {
  local last
  echo
  echo "Found an existing deployment: ${STACK_NAME} in ${REGION}"
  echo
  local stack_row="$DET_STACK"
  [[ "$DET_STACK" == "unknown" && -n "$DET_STACK_ERR" ]] \
    && stack_row="unknown (could not check: ${DET_STACK_ERR})"
  printf "  %-22s %s\n" "Stack" "$stack_row"
  printf "  %-22s %s\n" "vController" "$DET_VC"
  printf "  %-22s %s\n" "Project key" "$DET_KEY"
  printf "  %-22s %s\n" "KVO licensing" "$DET_LICENSE"
  printf "  %-22s %s\n" "Adopt CLMS into KVO" "$DET_ADOPT"
  printf "  %-22s %s\n" "KVO Cloud Config" "$DET_CC"
  printf "  %-22s %s\n" "Sensors" "$DET_SENSORS"
  printf "  %-22s %s\n" "vPB in KVO" "$DET_VPB_KVO"
  printf "  %-22s %s\n" "AWS mirror" "$DET_MIRROR"
  echo
  if last="$(state_get LAST_FAILURE)" && [[ -n "$last" && "$last" != "none" ]]; then
    warn "Last run stopped at ${last}"
    echo
  fi
}

# Turn the detected reality into skip decisions. Anything not proven done runs.
resume_decide() {
  # THE INVARIANT: a phase is skipped only on a positive, specific answer from
  # the real system. "unknown" (probe failed, timed out, or returned something
  # unparseable) never skips anything, because skipping is the dangerous
  # direction: it silently drops real work. Unknown falls through to running
  # the phase, or to a question where the answer costs money (licensing).
  if stack_usable; then
    SKIP_STACK=true;  REASON_STACK="the stack is ${DET_STACK}"
  fi
  if [[ "$DET_VC_OK" == "true" ]]; then
    SKIP_WAIT=true;   REASON_WAIT="the vController API is already serving"
  fi
  if [[ "$DET_KEY_OK" == "true" ]]; then
    SKIP_KEY=true
    REASON_KEY="a project key for this vController is already in ${VC_CREDS_FILE}"
  fi
  if [[ "$DET_LICENSE_OK" == "true" ]]; then
    SKIP_LICENSE=true; REASON_LICENSE="KVO is already ${DET_LICENSE}"
  fi
  # Phase 12 does BOTH the adoption and the Cloud Config, so it is only skipped
  # when both are already there. The adopt script is idempotent on an adopted
  # manager, so running it again to add a missing Cloud Config is safe.
  if [[ "$DET_ADOPT_OK" == "true" && "$DET_CC_OK" == "true" ]]; then
    SKIP_ADOPT=true;  REASON_ADOPT="${CLM_NAME_IN_KVO} is ${DET_ADOPT} and Cloud Config ${DET_CC}"
  fi
  if [[ "$DET_VPB_KVO_OK" == "true" ]]; then
    SKIP_VPB=true;    REASON_VPB="the vPB is already a device in KVO (${VPB_DEVICE_NAME})"
  fi
  if [[ "$DET_MIRROR_OK" == "true" ]]; then
    SKIP_MIRROR=true; REASON_MIRROR="the mirror fabric already exists: ${DET_MIRROR}"
  fi
  # Sensors are OFFERED, never silently skipped: see resume_ask_ambiguous.
  # The traffic path has no reliable "already wired" probe, so it always runs
  # (it is prompt-gated and defaults to no anyway).
  return 0
}

# Questions that only make sense once the operator has said "continue".
resume_ask_ambiguous() {
  # Licensing is the one place where guessing costs money: re-activating a
  # consumed code spends entitlement quantity. So when it cannot be determined,
  # ASK, and default to skipping.
  # This is the ONE place an "unknown" leads to a skip, and it is a question
  # rather than an assumption. It is here because the two mistakes are not
  # symmetric: skipping costs a re-run, re-activating a consumed code spends
  # entitlement quantity that does not come back.
  if [[ "$DET_LICENSE_OK" != "true" && "$DET_LICENSE" == unknown* ]]; then
    echo
    warn "KVO licensing state could not be determined: ${DET_LICENSE}."
    echo "  Re-activating a code that was already consumed spends entitlement"
    echo "  quantity, which is real money, so this is not something to guess at."
    echo "  Force it anyway with: bash deploy/deploy-stack.sh --only license"
    if ask_yn "  Skip KVO licensing this run (safe default)? [Y/n]: " y; then
      SKIP_LICENSE=true
      if [[ "$INTERACTIVE" == "true" ]]; then
        REASON_LICENSE="licensing state unknown and you chose to skip"
      else
        REASON_LICENSE="licensing state unknown, and skipping is the safe default with nobody to ask"
      fi
    else
      SKIP_LICENSE=false
    fi
  fi

  # Sensors: show the count, then offer. Default follows the count.
  if [[ -n "$DET_SENSORS_N" ]] && (( DET_SENSORS_N > 0 )); then
    echo
    echo "  ${DET_SENSORS_N} sensor(s) are already registered."
    if ask_yn "  Run the sensor install again anyway? [y/N]: " n; then
      SKIP_SENSORS=false
    else
      SKIP_SENSORS=true; REASON_SENSORS="${DET_SENSORS_N} sensor(s) already registered"
    fi
  fi
  return 0
}

# The whole resume decision, run once region + stack name are known.
resume_check() {
  step "Phase 3b: Existing deployment check"

  state_init
  if [[ -n "$STATE_FILE" ]]; then
    note "State file: ${STATE_FILE} (inputs only, never secrets)"
  fi

  note "Checking the real system, not the state file: every probe below is read-only."
  detect_all

  # Reality beats the state file, loudly.
  local remembered
  if remembered="$(state_get STACK_STATUS)" && [[ -n "$remembered" ]]; then
    if [[ "$DET_STACK" == "MISSING" && "$remembered" != "MISSING" ]]; then
      warn "The state file remembers this stack as ${remembered}, but ${REGION} says it does not exist."
      note "Reality wins: this run deploys from scratch."
    fi
  fi
  state_set STACK_STATUS "$DET_STACK"

  # A stack in one of these states can never be updated: CloudFormation needs
  # it deleted first. We print the command. We do not run it.
  if stack_needs_delete; then
    echo
    warn "Stack ${STACK_NAME} is ${DET_STACK}."
    echo "  CloudFormation can never update a stack in this state: it has to be"
    echo "  deleted before anything can be deployed under this name again."
    echo
    echo "  Delete it yourself (this script will not delete anything):"
    echo "    aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${REGION}"
    echo "    aws cloudformation wait stack-delete-complete --stack-name ${STACK_NAME} --region ${REGION}"
    echo
    echo "  Then re-run this script. Or deploy under a different --stack-name."
    fail "Stopping: ${STACK_NAME} is ${DET_STACK} and must be deleted by hand first."
  fi

  # Someone else may be mid-deploy. Never redeploy on top of that.
  if stack_in_flight; then
    echo
    warn "Stack ${STACK_NAME} is ${DET_STACK}: another deploy is already running."
    echo "  Redeploying on top of that would collide with it, so this run will not."
    if [[ "$INTERACTIVE" == "true" ]] && [[ "$DET_STACK" != "DELETE_IN_PROGRESS" ]] \
       && ask_yn "  Wait for it to finish (read-only polling)? [y/N]: " n; then
      note "Waiting: aws cloudformation wait stack-create-complete --stack-name ${STACK_NAME}"
      if aws "${AWS_REGION_ARG[@]}" cloudformation wait stack-create-complete \
           --stack-name "$STACK_NAME" 2>/dev/null; then
        ok "The other deploy finished."
        detect_stack
        detect_addresses
      else
        fail "The stack did not reach a complete state. Inspect it, then re-run."
      fi
    else
      echo "  Watch it with:"
      echo "    aws cloudformation describe-stacks --stack-name ${STACK_NAME} --region ${REGION}"
      fail "Stopping while ${STACK_NAME} is ${DET_STACK}."
    fi
  fi

  # Nothing there: a normal first deploy.
  if [[ "$DET_STACK" == "MISSING" ]]; then
    ok "No existing deployment for ${STACK_NAME} in ${REGION}: deploying everything."
    return 0
  fi

  if [[ "$DET_STACK" == "unknown" ]]; then
    warn "Could not check whether ${STACK_NAME} exists${DET_STACK_ERR:+: ${DET_STACK_ERR}}."
    [[ "$AWS_USABLE" != "true" ]] && aws_credentials_advice
    echo "  A failed probe is NOT proof that the stack is missing, so nothing is"
    echo "  assumed and NO phase is skipped on it. 'cloudformation deploy' updates"
    echo "  an existing stack rather than replacing it, so continuing is safe, but"
    echo "  you should know that is what is happening."
    if [[ "$INTERACTIVE" == "true" ]] && ! ask_yn "  Continue anyway? [Y/n]: " y; then
      fail "Stopping at your request: stack state could not be determined."
    fi
    return 0
  fi

  FOUND_DEPLOYMENT=true
  resume_decide
  print_resume_banner
  echo "Everything above the first \"not done\" will be skipped."

  if [[ "$RESUME_MODE" == "fresh" ]]; then
    RESUME_ACTIVE=false
    note "--fresh: every phase runs again. Nothing is deleted; phases just stop being skipped."
  elif [[ "$RESUME_MODE" == "resume" ]]; then
    RESUME_ACTIVE=true
    ok "--resume: continuing from '$(first_pending_phase)'."
  elif [[ "$INTERACTIVE" != "true" ]]; then
    RESUME_ACTIVE=true
    ok "Non-interactive: resuming from '$(first_pending_phase)'. Skipping finished work cannot destroy anything; redoing it can."
  elif ask_yn "  Continue from: $(first_pending_phase)?  [Y/n]: " y; then
    RESUME_ACTIVE=true
  else
    RESUME_ACTIVE=false
    note "Running every phase again. Nothing is deleted; the finished phases simply run once more."
  fi

  [[ "$RESUME_ACTIVE" == "true" ]] && resume_ask_ambiguous
  return 0
}

# Pre-fill this run's inputs from the previous one so a resume does not have to
# re-ask for everything. Flags and env vars still win: this only fills blanks.
resume_load_inputs() {
  local v
  [[ -n "$STATE_FILE" ]] || return 0
  if [[ -z "$KEY_NAME" ]] && v="$(state_get EC2_KEY_PAIR)" && [[ -n "$v" ]]; then
    KEY_NAME="$v"; note "Key pair from the previous run: ${KEY_NAME}"
  fi
  if [[ -z "$SENSOR_MODE" ]] && v="$(state_get SENSOR_MODE)" && [[ -n "$v" ]]; then
    case "$v" in standalone|kvo|none) SENSOR_MODE="$v"; note "Sensor mode from the previous run: ${SENSOR_MODE}" ;; esac
  fi
  if v="$(state_get CLOUD_CONFIG)" && [[ -n "$v" ]] && [[ "$CLOUD_CONFIG_NAME" == "cloudlens-aws" ]]; then
    CLOUD_CONFIG_NAME="$v"
  fi
  return 0
}

# ---------------------------------------------------------------------
# Logging: tee everything to log file.
#
# EVERYTHING printed from here on lands in $LOG_FILE, and that includes the
# working vController login. The summary file carries the same secrets. So both
# are created exactly the way customer_input.yaml and the state file are:
# umask 077 first, chmod 600 after, and a file left over from an older run
# (they were created -rw-r--r--, world readable) is tightened BEFORE the first
# new byte is appended to it.
# ---------------------------------------------------------------------
secure_run_file() {
  local f="$1"
  if [[ ! -e "$f" ]]; then
    ( umask 077; : >> "$f" ) 2>/dev/null || return 0
  fi
  chmod 600 "$f" 2>/dev/null || true
}
secure_run_file "$LOG_FILE"
secure_run_file "$SUMMARY_FILE"

exec > >(tee -a "$LOG_FILE") 2>&1

# ---------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------
show_help() {
  cat <<'HLP'
CloudLens Stack Deployment (AWS)

Usage:
  bash deploy/deploy-stack.sh [options]

Options:
  --dry-run                 Walk through prompts and print would-be aws
                            commands without touching AWS. Safe anywhere.
                            Detection still runs (it only reads), so a dry run
                            also shows what it found and what would be skipped.

Re-running a deploy (resume):
  A re-run never starts from zero. Before anything is created, every phase that
  can be skipped is checked against the REAL system: CloudFormation for the
  stack, the vController API, the KVO licensing API and GraphQL, and EC2 for
  the mirror resources. A state file (.cloudlens-deploy-<stack>-<region>.state,
  mode 600) only remembers inputs so a resume does not re-prompt for them; it
  holds no secrets. If the state file and reality disagree, reality wins.

  Nothing in this script is destructive, including under --fresh. Where a
  delete is genuinely required (a ROLLBACK_COMPLETE stack, which CloudFormation
  can never update), the exact command is printed for you to run.

  --resume                  Continue from the first unfinished phase without
                            asking. This is the default when there is no
                            terminal: skipping finished work cannot destroy
                            anything, whereas redoing it can.
  --fresh                   Run every phase again, skipping nothing. Still
                            deletes nothing.
  --from PHASE              Start at PHASE and run everything after it.
  --only PHASE              Run exactly one phase.
                            PHASE is one of the stable short names:
                              stack    deploy the stack            (phase 6)
                              wait     wait for the vController    (phase 7)
                              bootstrap vPB bootstrap over SSH     (phase 8)
                              key      vController project key     (phase 9)
                              license  KVO licensing               (phase 11)
                              adopt    adopt CLMS + Cloud Config   (phase 12)
                              sensors  sensor install              (phase 13)
                              vpb      adopt the vPB into KVO      (phase 14)
                              path     vPB traffic path            (phase 15)
                              mirror   AWS mirror session          (phase 16)
                            Names, not numbers: numbers shift when phases are
                            added, these do not.

Region + naming (all have sensible defaults, all overridable):
  --region REGION           AWS region             (default: us-east-1)
  --stack-name NAME         Stack / workspace name (default: cloudlens-stack)
  --admin-user NAME         OS login user for SSH  (default: admin)
  --key-name NAME           EC2 key pair to attach (created if missing)

Infrastructure engine:
  --iac cfn|terraform       Provision with CloudFormation (default) or
                            Terraform. CloudFormation calls
                            deploy/cloudformation/stack.yaml; Terraform
                            uses deploy/terraform.

Instance types (constrained by what each Marketplace AMI permits):
  --vcontroller-type TYPE   t3.xlarge (default) or m5.xlarge. m5 gives
                            dedicated CPU instead of burstable: prefer it
                            for production.
  --kvo-type TYPE           c5.2xlarge only (default)
  --vpb-type TYPE           t3.xlarge only (default)
                            Any other size is rejected by AWS at launch.

SSH key pair:
  --key-name NAME           Use this key pair, creating it if missing.
                            Omit it and the script lists the key pairs in the
                            region so you can pick one or create a new one.

vPB multi-NIC (fan-in / fan-out for prod):
  --vpb-ingress-nics N      Extra ingress NICs      (default: 1)
  --vpb-egress-nics N       Extra egress NICs       (default: 1)

Access control:
  --admin-cidr CIDR         Source CIDR allowed to reach UI/SSH ports
                            (default: 0.0.0.0/0 - narrow this for prod)
  --no-public-ip            Deploy with private IPs only (no Elastic IPs).
                            For private subnets or no-public-IP orgs; reach
                            the stack over VPN / Direct Connect / peering.
  --public-ip               Force public Elastic IPs (this is the default).

VM names (optional, default <stack-name>-vcontroller / -kvo / -vpb):
  --vcontroller-name NAME   Name tag for the vController instance
  --kvo-name NAME           Name tag for the KVO instance
  --vpb-name NAME           Name tag for the vPB instance

Test workloads (optional, deployed into the stack's own subnet):
  --windows-instance-profile NAME
                            EC2 instance profile for the Windows test VM.
                            Ansible reaches Windows over SSM, which needs
                            AmazonSSMManagedInstanceCore. Without it the VM
                            deploys but the sensor step cannot reach it.
  --test-vms LIST           Comma list of throwaway VMs to deploy alongside the
                            stack so you can prove the sensor path immediately:
                            ubuntu, rhel, windows, all, or none (default none).
                            They are tagged with the discovery tag below, so the
                            sensor step finds them with no extra work.

Deploy into existing infrastructure (brownfield):
  --existing-vpc-id ID      Deploy into a VPC you already own (needs subnet).
  --existing-subnet-id ID   Subnet to place the instances in.
  --existing-data-subnet-id ID
                            Separate subnet for the vPB data plane (optional).
  --existing-sg-id ID       Attach a pre-approved security group instead of
                            creating one (then --admin-cidr is ignored).

Toggles:
  --no-kvo                  Skip KVO deployment
  --with-kvo                Deploy KVO (skip interactive prompt)
  --with-vpb                Deploy vPB (skip interactive prompt)
  --no-vpb                  Skip vPB deployment
  --no-sensors              Skip sensor playbook chain at the end
  --rollback                On any failure, delete the stack we created
                            (never touches a pre-existing stack).
  --no-rollback             Force keep-partial behavior (default).
  -h, --help                Show this help

Post-deploy chain (phases 10-16). Every one of these is prompted for when it
is not given, and every prompt falls back to the default shown when there is
no terminal to ask on, so curl | bash stays fully non-interactive:
  --sensor-mode MODE        Which project key the sensors register with:
                              standalone  the key minted directly on the
                                          vController in phase 9 (default)
                              kvo         the key the KVO Cloud Config
                                          provisions in phase 12. Needs KVO.
                              none        skip sensors entirely
                            KVO mode runs licensing + adoption BEFORE sensors,
                            because the key does not exist until then.
  --kvo-codes CODE[,QTY]    KVO activation code, repeatable. Passed straight to
                            scripts/kvo_license.py. Omit it on a terminal and
                            that script prompts for code and quantity itself.
  --cloud-config NAME       KVO Cloud Config to create (default: cloudlens-aws).
                            Creating it is what provisions the CLM project and
                            its sensor key.
  --clm-name NAME           Name of the adopted vController inside KVO
                            (default: cloudlens-manager)
  --bootstrap-vpb           Run scripts/bootstrap-vpb.sh on the vPB over SSH
  --no-bootstrap-vpb        Only print the bootstrap instructions
  --adopt-vpb               Adopt the vPB into KVO (needs vPB + KVO)
  --no-adopt-vpb            Skip vPB adoption
  --wire-vpb-path           Wire the vPB traffic path + monitoring policy.
                            NOTE: this cannot complete on a fresh vPB. The
                            device config has no ports until eth1/eth2 are
                            brought up as DPDK data ports on the vPB itself,
                            and that bring-up is not automated.
  --no-wire-vpb-path        Skip the traffic path step
  --with-mirror             Also build the AWS mirror fabric (default: no).
                            Builds presence, cloud config, collection, the
                            collector SVM (mirror target), tool, policy and the
                            traffic mirror sessions. Proven live end to end.
  --no-mirror               Skip the AWS mirror session (default)
  --mirror-access-key KEY   AWS access key KVO uses for mirroring. Required:
  --mirror-secret-key KEY   an instance role is not enough, createAwsPresence
                            fails with "accessKeyId cannot be empty".

Multi-region: CloudFormation ships AMIs for us-east-1, us-east-2, us-west-1,
  us-west-2, ca-central-1, eu-west-1, eu-west-2, eu-central-1, ap-southeast-1,
  ap-southeast-2, ap-northeast-1, ap-south-1. Just pass --region; the script
  resolves the region-correct AMIs. Subscribe to the Marketplace AMIs in that
  region first.

Env-var overrides (alternative to flags, useful for curl | bash):
  CLOUDLENS_REGION, CLOUDLENS_STACK_NAME, CLOUDLENS_ADMIN_USER,
  CLOUDLENS_KEY_NAME, CLOUDLENS_ADMIN_CIDR, CLOUDLENS_ASSIGN_PUBLIC_IP,
  CLOUDLENS_EXISTING_VPC_ID, CLOUDLENS_EXISTING_SUBNET_ID,
  CLOUDLENS_EXISTING_DATA_SUBNET_ID, CLOUDLENS_EXISTING_SG_ID,
  CLOUDLENS_VCONTROLLER_AMI / _TYPE,
  CLOUDLENS_KVO_AMI / _TYPE,
  CLOUDLENS_VPB_AMI / _TYPE,
  CLOUDLENS_VPB_INGRESS_NICS, CLOUDLENS_VPB_EGRESS_NICS,
  CLOUDLENS_DISCOVERY_TAG_KEY, CLOUDLENS_DISCOVERY_TAG_VALUE,
  CLOUDLENS_CLOUD_CONFIG, CLOUDLENS_CLM_NAME, CLOUDLENS_VPB_DEVICE_NAME,
  CLOUDLENS_VPB_COLLECTION,
  CLOUDLENS_MIRROR_ACCESS_KEY, CLOUDLENS_MIRROR_SECRET_KEY

Example (full prod-style invocation):
  curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/deploy/deploy-stack.sh \
  | bash -s -- --region us-west-2 --with-kvo --vpb-ingress-nics 2 \
                --admin-cidr 203.0.113.0/24 --key-name my-ec2-key

Example (into existing private infra, no public IPs):
  curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-aws/main/deploy/deploy-stack.sh \
  | bash -s -- --region eu-central-1 --existing-vpc-id vpc-0abc123 \
                --existing-subnet-id subnet-0def456 --existing-sg-id sg-0aaa111 \
                --no-public-ip --key-name my-ec2-key

What it does (phases):
  1. Banner + environment detection (CloudShell vs local)
  2. Pre-flight checks (aws CLI, caller identity)
  3. Customer input (region, stack name, key pair, toggles)
 3b. Existing deployment check: read-only probes of the real system, then a
     status table and an offer to continue from the first unfinished phase
     (--resume / --fresh / --from / --only)
  4. Marketplace AMI subscription check (vController + KVO + vPB)
  5. Infra engine selection (CloudFormation or Terraform)
  6. Deploy the stack (vController + optional KVO + optional vPB)
  7. Wait for vController to initialize (~15 minutes)
  8. vPB post-deploy bootstrap over SSH (--bootstrap-vpb)
  9. vController project key + working UI login
 10. Sensor mode: standalone, KVO-managed, or none (--sensor-mode)
 11. KVO product licensing (KVO mode only, --kvo-codes)
 12. Adopt the vController into KVO + create the Cloud Config, which
     provisions the project key KVO-managed sensors use (--cloud-config)
 13. Sensor chain (optional, runs quickstart.sh with the key phase 10 chose)
 14. Adopt the vPB into KVO (--adopt-vpb)
 15. vPB traffic path + monitoring policy (--wire-vpb-path)
 16. AWS mirror session (--with-mirror, off by default)
 17. Final summary written to cloudlens-deploy-summary.txt

Phases 11 and 12 deliberately run BEFORE the sensors: in KVO-managed mode the
project key does not exist until the Cloud Config provisions it, and a sensor
installed with the phase 9 key registers to the wrong project.

Any phase from 6 onwards is skipped on a re-run when the real system says it is
already done. Re-run this script as often as you like: it picks up where it
stopped, and it never deletes anything.
HLP
}

# ---------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;

    --iac) IAC="$(to_lower "$2")"; shift 2 ;;

    --no-kvo) DEPLOY_KVO=false; shift ;;
    --with-kvo) DEPLOY_KVO=true; shift ;;
    --with-vpb) DEPLOY_VPB=true; shift ;;
    --no-vpb) DEPLOY_VPB=false; shift ;;
    --no-sensors) CHAIN_SENSORS=false; shift ;;

    --sensor-mode) SENSOR_MODE="$(to_lower "$2")"; shift 2 ;;
    --kvo-codes) KVO_CODES+=("$2"); shift 2 ;;
    --cloud-config) CLOUD_CONFIG_NAME="$2"; shift 2 ;;
    --clm-name) CLM_NAME_IN_KVO="$2"; shift 2 ;;
    --vpb-device-name) VPB_DEVICE_NAME="$2"; shift 2 ;;
    --vpb-collection) VPB_COLLECTION="$2"; shift 2 ;;
    --bootstrap-vpb) BOOTSTRAP_VPB=true; shift ;;
    --no-bootstrap-vpb) BOOTSTRAP_VPB=false; shift ;;
    --adopt-vpb) ADOPT_VPB=true; shift ;;
    --no-adopt-vpb) ADOPT_VPB=false; shift ;;
    --wire-vpb-path) WIRE_VPB_PATH=true; shift ;;
    --no-wire-vpb-path) WIRE_VPB_PATH=false; shift ;;
    --with-mirror) WITH_MIRROR=true; shift ;;
    --no-mirror) WITH_MIRROR=false; shift ;;
    --mirror-access-key) MIRROR_ACCESS_KEY="$2"; shift 2 ;;
    --mirror-secret-key) MIRROR_SECRET_KEY="$2"; shift 2 ;;

    --rollback) ROLLBACK_ON_FAIL=true; shift ;;
    --no-rollback) ROLLBACK_ON_FAIL=false; shift ;;

    --windows-instance-profile) WINDOWS_INSTANCE_PROFILE="$2"; shift 2 ;;
    --discovery-tag-key) DISCOVERY_TAG_KEY="$2"; DISCOVERY_TAG_EXPLICIT=true; shift 2 ;;
    --discovery-tag-value) DISCOVERY_TAG_VALUE="$2"; DISCOVERY_TAG_EXPLICIT=true; shift 2 ;;

    --resume) RESUME_MODE="resume"; shift ;;
    --fresh) RESUME_MODE="fresh"; shift ;;
    --from) FROM_PHASE="$(to_lower "$2")"; shift 2 ;;
    --only) ONLY_PHASE="$(to_lower "$2")"; shift 2 ;;

    --region) ARG_REGION="$2"; shift 2 ;;
    --stack-name) ARG_STACK="$2"; shift 2 ;;
    --admin-user) DEFAULT_ADMIN_USER="$2"; shift 2 ;;
    --key-name) KEY_NAME="$2"; shift 2 ;;
    --admin-cidr) ADMIN_CIDR="$2"; shift 2 ;;

    # Per-VM Name tag overrides
    --vcontroller-name) VCONTROLLER_NAME="$2"; shift 2 ;;
    --kvo-name) KVO_NAME="$2"; shift 2 ;;
    --vpb-name) VPB_NAME="$2"; shift 2 ;;

    # Brownfield: deploy into existing infrastructure
    --existing-vpc-id) EXISTING_VPC_ID="$2"; shift 2 ;;
    --existing-subnet-id) EXISTING_SUBNET_ID="$2"; shift 2 ;;
    --existing-data-subnet-id) EXISTING_DATA_SUBNET_ID="$2"; shift 2 ;;
    --existing-sg-id) EXISTING_SG_ID="$2"; shift 2 ;;
    --no-public-ip) ASSIGN_PUBLIC_IP="no"; shift ;;
    --test-vms) TEST_VMS="$2"; shift 2 ;;
    --public-ip) ASSIGN_PUBLIC_IP="yes"; shift ;;

    --vcontroller-type) CLMS_TYPE="$2"; shift 2 ;;
    --kvo-type) KVO_TYPE="$2"; shift 2 ;;
    --vpb-type) VPB_TYPE="$2"; shift 2 ;;

    --vpb-ingress-nics) VPB_INGRESS_NICS="$2"; shift 2 ;;
    --vpb-egress-nics) VPB_EGRESS_NICS="$2"; shift 2 ;;

    -h|--help) show_help; exit 0 ;;
    *) warn "Unknown argument: $1"; show_help; exit 1 ;;
  esac
done

if [[ "$IAC" != "cfn" && "$IAC" != "terraform" ]]; then
  fail "--iac must be 'cfn' or 'terraform' (got '$IAC')."
fi

# Phase selectors use the stable short names, not numbers.
if [[ -n "$FROM_PHASE" && -n "$ONLY_PHASE" ]]; then
  fail "--from and --only are mutually exclusive."
fi
for _p in "$FROM_PHASE" "$ONLY_PHASE"; do
  [[ -z "$_p" ]] && continue
  phase_index "$_p" >/dev/null \
    || fail "Unknown phase '${_p}'. Valid phases: ${PHASE_ORDER}."
done
if [[ "$RESUME_MODE" == "fresh" && ( -n "$FROM_PHASE" || -n "$ONLY_PHASE" ) ]]; then
  note "--fresh with --from/--only: the selector still limits which phases run."
fi

case "$SENSOR_MODE" in
  ""|standalone|kvo|none) ;;
  *) fail "--sensor-mode must be 'standalone', 'kvo', or 'none' (got '$SENSOR_MODE')." ;;
esac
# --sensor-mode none is just another spelling of --no-sensors, and naming a real
# mode means sensors are wanted, so neither one has to be asked about twice.
[[ "$SENSOR_MODE" == "none" ]] && CHAIN_SENSORS=false
[[ "$SENSOR_MODE" == "standalone" || "$SENSOR_MODE" == "kvo" ]] \
  && [[ -z "$CHAIN_SENSORS" ]] && CHAIN_SENSORS=true

# Validate NIC count bounds
for v in VPB_INGRESS_NICS:1:3 VPB_EGRESS_NICS:1:3; do
  name="${v%%:*}"; rest="${v#*:}"; lo="${rest%%:*}"; hi="${rest##*:}"
  val="${!name}"
  if ! [[ "$val" =~ ^[0-9]+$ ]] || (( val < lo || val > hi )); then
    fail "$name must be an integer between $lo and $hi (got '$val'). Run with -h for usage."
  fi
done

# Validate instance types against what each Marketplace AMI actually permits.
# Anything else is rejected by AWS at launch with UnsupportedOperation
# ("instance configuration for this AWS Marketplace product is not supported"),
# so fail here rather than 3 phases later.
#
# Probed with run-instances --dry-run across ALL 12 RegionMap regions (84
# checks): the permitted set is identical in every region, so these lists are
# region-independent. Re-probe with deploy/scripts/probe-instance-types.sh if
# Keysight publishes new AMI versions.
VCONTROLLER_ALLOWED="t3.xlarge m5.xlarge"
KVO_ALLOWED="c5.2xlarge"
VPB_ALLOWED="t3.xlarge"
check_type() {
  local label="$1" value="$2" allowed="$3"
  case " $allowed " in
    *" $value "*) return 0 ;;
    *) fail "$label instance type '$value' is not supported by the Marketplace AMI. Allowed: ${allowed}." ;;
  esac
}
check_type "vController" "$CLMS_TYPE" "$VCONTROLLER_ALLOWED"
check_type "KVO"         "$KVO_TYPE"  "$KVO_ALLOWED"
check_type "vPB"         "$VPB_TYPE"  "$VPB_ALLOWED"

# Parse --test-vms into per-OS toggles the template understands.
TEST_UBUNTU=no; TEST_RHEL=no; TEST_WINDOWS=no
if [[ -n "$TEST_VMS" && "$(to_lower "$TEST_VMS")" != "none" ]]; then
  IFS=',' read -ra _tv <<< "$(to_lower "$TEST_VMS")"
  for v in "${_tv[@]}"; do
    case "${v// /}" in
      ubuntu)  TEST_UBUNTU=yes ;;
      rhel|redhat) TEST_RHEL=yes ;;
      windows) TEST_WINDOWS=yes ;;
      all)     TEST_UBUNTU=yes; TEST_RHEL=yes; TEST_WINDOWS=yes ;;
      "")      ;;
      *) fail "--test-vms: unknown value '$v'. Use ubuntu, rhel, windows, all, or none (comma separated)." ;;
    esac
  done
fi

# Validate AssignPublicIp
case "$(to_lower "$ASSIGN_PUBLIC_IP")" in
  yes|no) ASSIGN_PUBLIC_IP="$(to_lower "$ASSIGN_PUBLIC_IP")" ;;
  *) fail "--no-public-ip/--public-ip: ASSIGN_PUBLIC_IP must be 'yes' or 'no' (got '$ASSIGN_PUBLIC_IP')." ;;
esac

# Brownfield sanity: an existing subnet needs an existing VPC, and vice versa.
if [[ -n "$EXISTING_VPC_ID" && -z "$EXISTING_SUBNET_ID" ]]; then
  fail "--existing-vpc-id requires --existing-subnet-id (which subnet to place instances in)."
fi
if [[ -z "$EXISTING_VPC_ID" && -n "$EXISTING_SUBNET_ID" ]]; then
  fail "--existing-subnet-id requires --existing-vpc-id."
fi
if [[ -n "$EXISTING_DATA_SUBNET_ID" && -z "$EXISTING_VPC_ID" ]]; then
  fail "--existing-data-subnet-id only applies with --existing-vpc-id / --existing-subnet-id."
fi

ADMIN_USERNAME="$DEFAULT_ADMIN_USER"

# ---------------------------------------------------------------------
# aws wrappers (echo in dry-run)
# ---------------------------------------------------------------------
AWS_REGION_ARG=()   # populated once region is known
run_aws() {
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "aws $*"
    return 0
  fi
  aws "${AWS_REGION_ARG[@]}" "$@"
}
run_aws_capture() {
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "aws $* (would capture stdout)"
    echo "DRY_RUN_VALUE"
    return 0
  fi
  aws "${AWS_REGION_ARG[@]}" "$@"
}

# ---------------------------------------------------------------------
# Error / interrupt traps
# ---------------------------------------------------------------------
on_error() {
  local exit_code=$?
  echo
  echo -e "${C_RED}[x] FAILED in phase: ${PHASE_NAME}${C_RESET}"
  echo -e "${C_RED}    exit code: ${exit_code}${C_RESET}"
  # Remember where this run stopped so the next one can say so up front. The
  # next run still re-checks reality: this is a hint, not an authority.
  state_set LAST_FAILURE "${PHASE_NAME} (exit ${exit_code}) at $(date -u +%FT%TZ)"

  echo
  echo "What was created so far:"
  [[ "$CREATED_STACK" == "true" ]] && echo "  - Stack: ${STACK_NAME:-unknown} (engine: ${IAC})"
  echo

  # If CloudFormation rejected the launch because Marketplace terms are not
  # accepted, say so plainly with the subscribe links: that error is otherwise
  # buried in the stack events.
  if [[ "$IAC" == "cfn" ]] && [[ -n "${STACK_NAME:-}" ]] && declare -f report_marketplace_failure >/dev/null 2>&1; then
    report_marketplace_failure || true
  fi

  if [[ "$ROLLBACK_ON_FAIL" == "true" ]] && [[ "$CREATED_STACK" == "true" ]] && [[ -n "${STACK_NAME:-}" ]]; then
    echo -e "${C_YELLOW}--rollback is ON. Tearing down ${STACK_NAME} in 5 seconds (Ctrl+C to abort)...${C_RESET}"
    for i in 5 4 3 2 1; do printf "  %d... " "$i"; sleep 1; done
    echo
    if [[ "$IAC" == "terraform" ]]; then
      ( cd "$REPO_DIR/$TF_DIR_REL" && terraform destroy -auto-approve ) \
        && ok "Terraform destroy initiated" \
        || warn "terraform destroy failed - inspect $REPO_DIR/$TF_DIR_REL"
    else
      if aws "${AWS_REGION_ARG[@]}" cloudformation delete-stack --stack-name "$STACK_NAME" >/dev/null 2>&1; then
        ok "Stack delete initiated: aws cloudformation delete-stack --stack-name ${STACK_NAME}"
      else
        warn "Stack delete failed - inspect: aws cloudformation describe-stacks --stack-name ${STACK_NAME}"
      fi
    fi
    return
  fi

  echo "Cleanup options:"
  if [[ "$CREATED_STACK" == "true" ]] && [[ -n "${STACK_NAME:-}" ]]; then
    echo "  Auto-rollback next time: re-run with --rollback"
    if [[ "$IAC" == "terraform" ]]; then
      echo "  Delete everything now:   (cd $REPO_DIR/$TF_DIR_REL && terraform destroy)"
    else
      echo "  Delete everything now:   aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${REGION}"
    fi
  fi
  echo "  Re-run this script:      bash deploy/deploy-stack.sh --region ${REGION:-REGION} --stack-name ${STACK_NAME:-NAME}"
  echo "                           It resumes: every finished phase is detected"
  echo "                           against the real system and skipped."
  echo "  Inspect log:             ${LOG_FILE}"
  SCRIPT_DONE=true
}
trap on_error ERR

# ---------------------------------------------------------------------
# Last line of defence: no exit may ever be silent.
#
# `set -e` inside a shell function does not run the ERR trap, so before this
# existed a single failed AWS call inside a detect_* ended the whole run with a
# bare exit code and not one word of explanation. This fires once, in the real
# shell (never in a $( ) subshell), for any non-zero exit that nothing else has
# already explained.
# ---------------------------------------------------------------------
on_exit() {
  local code=$?
  [[ "${BASHPID:-$$}" == "$$" ]] || return 0
  [[ "$SCRIPT_DONE" == "true" ]] && return 0
  (( code == 0 )) && return 0
  echo
  echo -e "${C_RED}[x] Stopped in phase: ${PHASE_NAME} (exit ${code})${C_RESET}" >&2
  if (( code == 130 )); then
    echo "    Interrupted (Ctrl-C). Nothing is lost: finished phases are recorded." >&2
  else
    echo "    Nothing above explained this, which means a command failed inside a" >&2
    echo "    function under 'set -e'. The usual cause is an AWS call: expired or" >&2
    echo "    invalid credentials, no network, or a missing permission." >&2
    echo "    Check with: aws sts get-caller-identity" >&2
  fi
  echo "    Full log:   ${LOG_FILE}" >&2
  echo >&2
  echo "    TO RESUME: re-run the same command from THIS directory. It reads the" >&2
  echo "    state file and skips every phase already done, continuing from here:" >&2
  echo "      bash <(curl -sSL ${REPO_RAW}/deploy/deploy-stack.sh)" >&2
  if [[ -n "${STATE_FILE:-}" ]]; then
    echo "    (progress is in ${STATE_FILE}; run from the directory that holds it)" >&2
  fi
  state_set LAST_FAILURE "${PHASE_NAME} (exit ${code}, unexplained) at $(date -u +%FT%TZ)" 2>/dev/null || true
}
trap on_exit EXIT

on_interrupt() {
  echo
  state_set LAST_FAILURE "interrupted in ${PHASE_NAME} at $(date -u +%FT%TZ)"
  SCRIPT_DONE=true
  warn "Interrupted in phase: ${PHASE_NAME}"
  warn "Re-run the same command to resume: finished phases are detected and skipped."
  [[ "$CREATED_STACK" == "true" ]] && [[ -n "${STACK_NAME:-}" ]] \
    && warn "To remove what got created: see cleanup options in ${LOG_FILE}"
  exit 130
}
trap on_interrupt INT TERM

# =====================================================================
# Phase 1: Banner + environment detection
# =====================================================================
step "Phase 1: Banner + environment detection"
banner "CloudLens Stack (AWS): vController + KVO + vPB + Sensors"
echo
[[ "$DRY_RUN" == "true" ]] && warn "DRY-RUN MODE: no AWS resources will be created"
echo

IN_CLOUD_SHELL=false
IN_WSL=false
IS_NATIVE_WINDOWS=false
KERNEL="$(uname -s 2>/dev/null || echo unknown)"

# AWS CloudShell exports AWS_EXECUTION_ENV=CloudShell and CLOUDSHELL_USER.
if [[ "${AWS_EXECUTION_ENV:-}" == *CloudShell* ]] \
   || [[ -n "${CLOUDSHELL_USER:-}" ]] \
   || [[ -d /usr/local/cloudshell ]]; then
  IN_CLOUD_SHELL=true
  # Pre-authenticated, yes. Ansible-ready, no: the CloudShell pre-installed
  # software list (Amazon Linux 2023) has python3 and pip3 and no Ansible. The
  # infra phases run fine here; the sensor phase installs it with one pip
  # command, which phase 9 prints if ansible-playbook turns out to be missing.
  ok "Detected: AWS CloudShell (pre-authenticated; Ansible is a pip install away)"
elif [[ "$KERNEL" == "Linux" ]] && grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; then
  IN_WSL=true
  ok "Detected: WSL on Windows"
elif [[ "$KERNEL" == MINGW* ]] || [[ "$KERNEL" == MSYS* ]] || [[ "$KERNEL" == CYGWIN* ]]; then
  IS_NATIVE_WINDOWS=true
  warn "Detected: Windows shell ($KERNEL) - not fully supported"
  echo
  echo "  The infra-deploy phases MAY work in Git Bash/Cygwin but the sensor"
  echo "  install (Ansible) does NOT - Ansible's control node cannot run"
  echo "  natively on Windows."
  echo
  echo "  Better paths for Windows users:"
  echo "    1. AWS CloudShell       (browser, https://console.aws.amazon.com/cloudshell)"
  echo "    2. WSL                  (Windows Subsystem for Linux: 'wsl --install')"
  echo "    3. Linux jumpbox EC2    (small EC2 you SSH into)"
  echo
  read -rp "Continue anyway in this Windows shell? [y/N]: " yn || true
  yn_lc=$(to_lower "${yn:-n}")
  if [[ "$yn_lc" != "y" && "$yn_lc" != "yes" ]]; then
    fail "Aborted. Open AWS CloudShell and rerun the curl line there for the smoothest experience."
  fi
  warn "Continuing on Windows. Expect the sensor chain to fail; infra will still deploy."
else
  ok "Detected: Local machine ($KERNEL)"
fi

# =====================================================================
# Phase 2: Pre-flight checks
# =====================================================================
step "Phase 2: Pre-flight checks"

install_aws_cli() {
  local os; os="$(uname -s)"
  echo "Detecting OS to install AWS CLI v2..."
  if [[ "$os" == "Darwin" ]]; then
    if command -v brew >/dev/null 2>&1; then
      note "Installing via Homebrew: brew install awscli"
      brew install awscli
    else
      note "Installing via the official AWS pkg installer"
      curl -sSL "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o /tmp/AWSCLIV2.pkg
      sudo installer -pkg /tmp/AWSCLIV2.pkg -target /
    fi
  elif [[ "$os" == "Linux" ]]; then
    local arch; arch="$(uname -m)"
    local url="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
    [[ "$arch" == "aarch64" || "$arch" == "arm64" ]] && url="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"
    note "Installing via the official AWS bundle ($arch)"
    curl -sSL "$url" -o /tmp/awscliv2.zip
    ( cd /tmp && unzip -q -o awscliv2.zip && sudo ./aws/install --update )
  elif [[ "$os" == MINGW* || "$os" == MSYS* || "$os" == CYGWIN* ]]; then
    # Git Bash / MSYS / Cygwin: the Linux bundle does not apply. Point at the
    # MSI and offer to run it through Windows' own installer if msiexec is
    # reachable from this shell.
    note "Windows shell detected ($os). AWS CLI v2 ships as an MSI."
    echo "    Download: https://awscli.amazonaws.com/AWSCLIV2.msi"
    if command -v msiexec >/dev/null 2>&1 || command -v powershell.exe >/dev/null 2>&1; then
      read -rp "    Download and run the MSI installer now? [Y/n]: " yn || true
      if [[ "$(to_lower "${yn:-y}")" != "n" ]]; then
        curl -sSL "https://awscli.amazonaws.com/AWSCLIV2.msi" -o "$TMPDIR/AWSCLIV2.msi" 2>/dev/null \
          || curl -sSL "https://awscli.amazonaws.com/AWSCLIV2.msi" -o "./AWSCLIV2.msi"
        if command -v msiexec >/dev/null 2>&1; then
          msiexec //i "AWSCLIV2.msi" //qb || warn "msiexec did not complete: run the MSI by hand."
        else
          powershell.exe -Command "Start-Process msiexec.exe -Wait -ArgumentList '/i AWSCLIV2.msi /qb'" \
            || warn "Installer did not complete: run AWSCLIV2.msi by hand."
        fi
        note "Close and reopen this shell afterwards so PATH picks up 'aws'."
      fi
    else
      echo "    Run that MSI, reopen this shell, then re-run this installer."
    fi
    command -v aws >/dev/null 2>&1 \
      || fail "AWS CLI still not on PATH. Install the MSI, reopen the shell, then re-run."
  else
    fail "Unsupported OS ($os). Install AWS CLI v2 manually from https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html then re-run."
  fi
}

if ! command -v aws >/dev/null 2>&1; then
  if [[ "$DRY_RUN" == "true" ]]; then
    warn "aws CLI not installed (dry-run continues)"
  else
    warn "AWS CLI not installed."
    read -rp "Install it now? Pulls the official AWS CLI v2. [Y/n]: " yn || true
    yn_lc=$(to_lower "${yn:-y}")
    if [[ "$yn_lc" == "n" || "$yn_lc" == "no" ]]; then
      fail "AWS CLI required. Install it from https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html then re-run."
    fi
    install_aws_cli
    command -v aws >/dev/null 2>&1 || fail "AWS CLI installation did not succeed. Install manually and re-run."
    ok "AWS CLI installed"
  fi
else
  ok "AWS CLI present ($(aws --version 2>&1 | awk '{print $1}' | cut -d/ -f2 2>/dev/null || echo unknown))"
fi

# ---------------------------------------------------------------------
# Verify caller identity (credentials present + valid).
#
# This is answered ONCE, here, and the answer is reused by every read-only
# probe in phase 3b. Nine "unknown" rows with no explanation is a terrible way
# to tell somebody their SSO token expired, and an expiring token mid-session
# is routine, not hypothetical.
# ---------------------------------------------------------------------
aws_identity_check() {
  local out
  AWS_USABLE=false; AWS_UNUSABLE_WHY=""
  if ! command -v aws >/dev/null 2>&1; then
    AWS_UNUSABLE_WHY="the AWS CLI is not on PATH"
    return 0
  fi
  out="$(PROBE_MERGE_STDERR=1 probe aws sts get-caller-identity --query Account --output text)"
  out="$(first_line "$out")"
  if [[ "$out" =~ ^[0-9]{12}$ ]]; then
    ACCOUNT_ID="$out"; AWS_USABLE=true
    return 0
  fi
  case "$out" in
    *ExpiredToken*|*TokenRefreshRequired*|*"security token included in the request is expired"*)
      AWS_UNUSABLE_WHY="your AWS credentials have expired" ;;
    *InvalidClientTokenId*|*SignatureDoesNotMatch*|*UnrecognizedClientException*|*AuthFailure*|*AccessDenied*)
      AWS_UNUSABLE_WHY="AWS rejected these credentials as invalid" ;;
    *"Unable to locate credentials"*|*NoCredentialProviders*|*"You must specify a region"*)
      AWS_UNUSABLE_WHY="this shell has no AWS credentials configured" ;;
    "")
      AWS_UNUSABLE_WHY="the AWS CLI did not answer within ${PROBE_TIMEOUT}s (timeout or no network)" ;;
    *)
      AWS_UNUSABLE_WHY="the AWS identity check failed: ${out:0:160}" ;;
  esac
  return 0
}

aws_credentials_advice() {
  echo "  Fix it with ONE of these, then re-run the same command:"
  if [[ -n "${AWS_PROFILE:-}" ]]; then
    echo "    aws sso login --profile ${AWS_PROFILE}    # this shell has AWS_PROFILE=${AWS_PROFILE}"
  else
    echo "    aws sso login --profile NAME              # IAM Identity Center / SSO"
  fi
  echo "    aws configure                             # static access key + secret"
  echo "    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...   # fresh keys in this shell"
  echo "  Check it worked with: aws sts get-caller-identity"
}

probe_guard_begin
aws_identity_check
probe_guard_end

if [[ "$AWS_USABLE" == "true" ]]; then
  CALLER_ARN="$(probe aws sts get-caller-identity --query Arn --output text)"
  [[ -n "$CALLER_ARN" ]] || CALLER_ARN="unknown"
  ok "AWS account: ${ACCOUNT_ID}"
  note "Caller: ${CALLER_ARN}"
elif [[ "$DRY_RUN" == "true" ]]; then
  # Dry-run is meant to work anywhere, including offline, so this is a warning
  # and the run continues. Say plainly what it means for the checks that follow.
  warn "Cannot talk to AWS: ${AWS_UNUSABLE_WHY}."
  aws_credentials_advice
  note "--dry-run continues anyway. Every AWS check below will read 'unknown',"
  note "which means 'not checked', not 'not there': nothing is skipped on it."
  ACCOUNT_ID="000000000000"
  CALLER_ARN="arn:aws:iam::000000000000:user/dry-run"
else
  warn "Cannot talk to AWS: ${AWS_UNUSABLE_WHY}."
  if [[ "$INTERACTIVE" == "true" ]]; then
    echo
    echo "  How do you sign in to AWS?"
    echo "    1) IAM Identity Center / SSO   (runs: aws sso login)"
    echo "    2) Access key + secret         (runs: aws configure)"
    echo "    3) I will sort it out myself   (exit)"
    echo
    read -rp "  Choose 1-3 [1]: " auth_choice || true
    case "${auth_choice:-1}" in
      1)
        read -rp "  AWS profile name (blank = default): " auth_profile || true
        if [[ -n "$auth_profile" ]]; then
          export AWS_PROFILE="$auth_profile"
          aws sso login --profile "$auth_profile" || true
        else
          aws sso login || true
        fi
        ;;
      2)
        note "aws configure will prompt for Access Key, Secret, region, and output format."
        aws configure || true
        ;;
      *)
        aws_credentials_advice
        fail "Cannot proceed without valid AWS credentials."
        ;;
    esac
    probe_guard_begin
    aws_identity_check
    probe_guard_end
  fi
  if [[ "$AWS_USABLE" != "true" ]]; then
    # Nothing downstream can work: every phase either creates or inspects AWS
    # resources. Stop here, with the reason and the fix, rather than failing
    # nine probes and then a deploy.
    echo
    aws_credentials_advice
    fail "Cannot proceed: ${AWS_UNUSABLE_WHY}."
  fi
  ok "Signed in."
  CALLER_ARN="$(probe aws sts get-caller-identity --query Arn --output text)"
  [[ -n "$CALLER_ARN" ]] || CALLER_ARN="unknown"
  ok "AWS account: ${ACCOUNT_ID}"
  note "Caller: ${CALLER_ARN}"
fi

# Locate the repo root so we can find the CFN template / Terraform dir.
# When curl|bash'd we may not be inside the repo; clone lazily if needed.
if [[ -f "$SCRIPT_DIR/../$CFN_TEMPLATE_REL" ]] || [[ -d "$SCRIPT_DIR/../$TF_DIR_REL" ]]; then
  REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [[ -f "$PWD/$CFN_TEMPLATE_REL" ]]; then
  REPO_DIR="$PWD"
else
  REPO_DIR="$HOME/${REPO_NAME}"
  # We created this clone, so we own keeping it current. A checkout the operator
  # is working in is never touched.
  REPO_DIR_IS_OURS=true
fi
REPO_DIR_IS_OURS="${REPO_DIR_IS_OURS:-false}"

# =====================================================================
# Phase 3: Customer input
# =====================================================================
step "Phase 3: Customer input"

# Region
echo
echo "AWS region (e.g. us-east-1, us-west-2, eu-west-1):"
if [[ -n "$ARG_REGION" ]]; then
  REGION="$ARG_REGION"; ok "Region: ${REGION} (from --region)"
else
  input_region="$(ask "AWS region [${DEFAULT_REGION}]: " "$DEFAULT_REGION")"; echo
  REGION="${input_region:-$DEFAULT_REGION}"; ok "Region: ${REGION}"
fi
AWS_REGION_ARG=(--region "$REGION")
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

# Warn early if the region is outside what the CFN RegionMap covers. The
# Terraform path takes AMI ids directly, so it can still work if we resolve
# them below.
region_supported() {
  case " $SUPPORTED_CFN_REGIONS " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}
if ! region_supported "$REGION"; then
  warn "Region '${REGION}' is not in the CloudFormation RegionMap."
  echo "  CFN-supported regions: ${SUPPORTED_CFN_REGIONS}"
  echo "  You can still use --iac terraform (AMIs are resolved by name below),"
  echo "  or pick a supported region."
fi

# Resolve the region-correct Marketplace AMI id from its stable product name,
# unless the user pinned an explicit id via CLOUDLENS_*_AMI. This keeps Phase 4's
# subscription probe and the Terraform path correct in every region. The CFN
# template resolves its own AMIs from RegionMap, so this does not feed CFN.
resolve_ami_by_name() {
  local ami_name="$1"
  aws "${AWS_REGION_ARG[@]}" ec2 describe-images --owners "$MARKETPLACE_OWNER" \
    --filters "Name=name,Values=${ami_name}" \
    --query 'Images[0].ImageId' --output text 2>/dev/null
}
if [[ "$DRY_RUN" != "true" ]]; then
  if [[ -z "$CLMS_AMI_OVERRIDE" ]]; then
    r="$(resolve_ami_by_name "$CLMS_AMI_NAME")"; [[ -n "$r" && "$r" != "None" ]] && CLMS_AMI="$r"
  fi
  if [[ -z "$KVO_AMI_OVERRIDE" ]]; then
    r="$(resolve_ami_by_name "$KVO_AMI_NAME")"; [[ -n "$r" && "$r" != "None" ]] && KVO_AMI="$r"
  fi
  if [[ -z "$VPB_AMI_OVERRIDE" ]]; then
    r="$(resolve_ami_by_name "$VPB_AMI_NAME")"; [[ -n "$r" && "$r" != "None" ]] && VPB_AMI="$r"
  fi
  note "Resolved AMIs for ${REGION}: vController=${CLMS_AMI} KVO=${KVO_AMI} vPB=${VPB_AMI}"
fi

# Stack name
echo
echo "Stack name is the CloudFormation stack (and Terraform workspace) that"
echo "owns every VPC, subnet, security group, and EC2 in this deploy."
if [[ -n "$ARG_STACK" ]]; then
  STACK_NAME="$ARG_STACK"
  if ! valid_stack_name "$STACK_NAME"; then
    fail "Invalid stack name '${STACK_NAME}'. CloudFormation names must start with a letter and use only letters, digits, and hyphens (try: $(sanitize_stack_name "$STACK_NAME"))."
  fi
  ok "Stack name: ${STACK_NAME} (from --stack-name)"
else
  while true; do
    input_stack="$(ask "Stack name [${DEFAULT_STACK_NAME}]: " "$DEFAULT_STACK_NAME")"; echo
    STACK_NAME="${input_stack:-$DEFAULT_STACK_NAME}"
    if valid_stack_name "$STACK_NAME"; then break; fi
    suggestion="$(sanitize_stack_name "$STACK_NAME")"
    warn "Invalid name '${STACK_NAME}': CloudFormation names must start with a letter and use only letters, digits, and hyphens (no spaces)."
    use_sugg="$(ask "Use '${suggestion}' instead? [Y/n]: " y)"; echo
    if [[ "$(to_lower "$use_sugg")" != "n" ]]; then STACK_NAME="$suggestion"; break; fi
  done
  ok "Stack name: ${STACK_NAME}"
fi

# =====================================================================
# Phase 3b: Existing deployment check (resume)
# =====================================================================
# Runs the moment stack name + region are known, which is the earliest point at
# which "does this deployment already exist" is a question that can be asked.
# Everything it does is read-only.
resume_check
resume_load_inputs

# An appliance that is already in this stack is not worth prompting about.
if [[ "$FOUND_DEPLOYMENT" == "true" ]]; then
  [[ -z "$DEPLOY_KVO" && -n "$KVO_PUBLIC_IP" ]] \
    && { DEPLOY_KVO=true; note "KVO is already part of this stack (${KVO_PUBLIC_IP})."; }
  [[ -z "$DEPLOY_VPB" && -n "$VPB_PUBLIC_IP" ]] \
    && { DEPLOY_VPB=true; note "vPB is already part of this stack (${VPB_PUBLIC_IP})."; }
fi

# EC2 key pair. AWS appliances use SSH key auth, not passwords. Create one
# on demand if the customer does not already have a key pair.
ensure_key_pair() {
  local name="$1"
  local pem="$HOME/.ssh/${name}.pem"
  [[ "$DRY_RUN" == "true" ]] && { note "would ensure key pair $name exists"; return 0; }
  if aws "${AWS_REGION_ARG[@]}" ec2 describe-key-pairs --key-names "$name" >/dev/null 2>&1; then
    ok "Key pair '${name}' exists in ${REGION}"
    # Existing in AWS is only half of it. AWS keeps the public half; the private
    # half exists once, wherever it was created. Deploying against a key pair
    # whose .pem is not on THIS machine builds the whole stack and then fails
    # every SSH step at the end: sensors go UNREACHABLE and vPB adoption is
    # skipped, 25 minutes after the choice that caused it. Say it now, while
    # nothing has been spent.
    if [[ ! -f "$pem" ]]; then
      # Look where people actually leave a downloaded .pem before concluding it
      # is missing. A key downloaded from the console lands in ~/Downloads far
      # more often than in ~/.ssh.
      local found=""
      local cand
      for cand in "$HOME/Downloads/${name}.pem" "$HOME/${name}.pem" "$PWD/${name}.pem" \
                  "$HOME/Downloads/${name}.cer" "$HOME/.ssh/${name}"; do
        [[ -f "$cand" ]] && { found="$cand"; break; }
      done
      if [[ -n "$found" ]]; then
        note "Found the private key at ${found}"
        mkdir -p "$HOME/.ssh" 2>/dev/null || true
        if cp "$found" "$pem" 2>/dev/null && chmod 600 "$pem" 2>/dev/null; then
          ok "Copied it to ${pem} (mode 600)"
        else
          warn "Could not copy ${found} to ${pem}. Do it by hand, then re-run."
          KEY_PEM_MISSING=true
        fi
      else
        echo
        warn "The private key for '${name}' is not on this machine."
        note "Looked in ~/.ssh, ~/Downloads, and the current directory."
        note "AWS stores only the public half, so the .pem exists once, wherever"
        note "it was first downloaded. Without it this deploy builds fine and"
        note "then fails every step that needs SSH: sensor install and vPB"
        note "adoption, about 25 minutes from now."
        note "Fix it either way:"
        note "  copy the .pem to ${pem} and chmod 600, or"
        note "  re-run with --key-name <a-new-name> and a fresh pair is created"
        note "  for you, with the .pem saved where this script expects it."
        if [[ "$INTERACTIVE" == "true" ]]; then
          if ! ask_yn "  Continue anyway (every SSH step will be skipped)? [y/N]: " "n"; then
            fail "Stopped before deploying. Nothing was created, nothing was spent."
          fi
        else
          note "Non-interactive: continuing. The SSH steps will report this again."
        fi
        KEY_PEM_MISSING=true
      fi
    fi
    return 0
  fi
  note "Key pair '${name}' not found in ${REGION}. Creating it..."
  local pem="$HOME/.ssh/${name}.pem"
  mkdir -p "$HOME/.ssh"
  aws "${AWS_REGION_ARG[@]}" ec2 create-key-pair --key-name "$name" \
    --query 'KeyMaterial' --output text > "$pem"
  chmod 600 "$pem"
  ok "Created key pair '${name}', private key saved to ${pem}"
}

# Interactive key pair chooser. Unlike the CloudFormation console (where an
# AWS-typed dropdown parameter cannot be optional, so it must hard-require a
# pre-existing key), the CLI can list what the account actually has AND create
# one on demand. Shows existing keys so a typo cannot silently mint a duplicate.
select_key_pair() {
  local existing=() line pick default_new="cloudlens-key"

  if [[ "$DRY_RUN" == "true" ]]; then
    KEY_NAME="${KEY_NAME:-$default_new}"
    note "would select or create key pair '${KEY_NAME}'"
    return 0
  fi

  while IFS= read -r line; do
    [[ -n "$line" ]] && existing+=("$line")
  done < <(aws "${AWS_REGION_ARG[@]}" ec2 describe-key-pairs \
             --query 'KeyPairs[].KeyName' --output text 2>/dev/null | tr '\t' '\n' | sort)

  if [[ ${#existing[@]} -eq 0 ]]; then
    warn "No EC2 key pairs exist in ${REGION}."
    read -rp "Name for a new key pair to create [${default_new}]: " pick || true
    KEY_NAME="${pick:-$default_new}"
    ensure_key_pair "$KEY_NAME"
    return 0
  fi

  echo
  echo "EC2 key pairs in ${REGION}:"
  # Mark the ones whose private key is actually on THIS machine. AWS keeps only
  # the public half, so an existing pair is useless here unless its .pem came
  # along. Picking one blind builds the stack and then fails every SSH step.
  local i=1 usable_here=0
  for line in "${existing[@]}"; do
    if [[ -f "$HOME/.ssh/${line}.pem" ]]; then
      printf "  %2d) %-40s (private key present here)\n" "$i" "$line"
      usable_here=$((usable_here+1))
    else
      printf "  %2d) %-40s (no private key on this machine)\n" "$i" "$line"
    fi
    i=$((i+1))
  done
  printf "  %2d) Create a NEW key pair  <- recommended\n" "$i"
  echo
  if (( usable_here == 0 )); then
    note "None of the existing pairs has its private key here, so SSH steps"
    note "(sensor install, vPB adoption) would fail with any of them."
  fi
  # Default to creating a new pair. Defaulting to the FIRST existing pair meant
  # pressing Enter picked a key whose .pem was on a different machine entirely,
  # which is how a full deploy reached the sensor step and died UNREACHABLE.
  read -rp "Choose 1-${i}, or type a key pair name [${i}]: " pick || true
  pick="${pick:-$i}"

  if [[ "$pick" =~ ^[0-9]+$ ]] && (( pick >= 1 && pick < i )); then
    KEY_NAME="${existing[$((pick-1))]}"
    # An existing key pair is only usable from here if its .pem is ALSO on this
    # machine: AWS never returns a private key, and every SSH step (the vPB
    # bootstrap and the Linux sensor install) needs it. Check NOW, before the
    # stack is built, instead of discovering it at phase 8 with the stack already
    # tied to a key whose .pem lives on a different machine (a common CloudShell
    # trap: you pick a key from a past session whose .pem is on your laptop).
    local found=""
    for cand in "$HOME/.ssh/${KEY_NAME}.pem" "$HOME/Downloads/${KEY_NAME}.pem" \
                "$PWD/${KEY_NAME}.pem" "$HOME/${KEY_NAME}.pem"; do
      [[ -f "$cand" ]] && { found="$cand"; break; }
    done
    if [[ -n "$found" ]]; then
      ok "Using existing key pair '${KEY_NAME}' (private key found: ${found})"
      return 0
    fi
    warn "Key pair '${KEY_NAME}' exists in AWS, but its .pem is NOT on this machine."
    note "AWS cannot hand back a private key, and the vPB bootstrap and Linux"
    note "sensor install both need it. If you continue, those SSH steps fail here"
    note "until you upload ${KEY_NAME}.pem to ~/.ssh/ yourself."
    if ask_yn "  Create a NEW key pair instead, so the .pem is written here? [Y/n]: " "y"; then
      read -rp "Name for the new key pair [${default_new}]: " pick || true
      pick="${pick:-$default_new}"
      KEY_NAME="$pick"; ensure_key_pair "$KEY_NAME"; return 0
    fi
    warn "Continuing with '${KEY_NAME}'. Upload its .pem to ~/.ssh/ before the SSH steps."
    ok "Using existing key pair '${KEY_NAME}'"
    return 0
  fi

  if [[ "$pick" =~ ^[0-9]+$ ]] && (( pick == i )); then
    read -rp "Name for the new key pair [${default_new}]: " pick || true
    pick="${pick:-$default_new}"
  fi

  KEY_NAME="$pick"
  ensure_key_pair "$KEY_NAME"
}

echo
echo "KVO and vPB are reached over SSH with an EC2 key pair (no password):"
echo "  KVO / vPB: ssh -i <key>.pem -p ${VPB_SSH_PORT} ${ADMIN_USERNAME}@<ip>"
echo "vController is appliance-managed via its web UI (admin / Cl0udLens@dm!n)."
if [[ -n "$KEY_NAME" ]]; then
  # Explicit --key-name / CLOUDLENS_KEY_NAME: honor it, create if missing.
  ensure_key_pair "$KEY_NAME"
else
  select_key_pair
fi

# Deploy KVO?
if [[ -z "$DEPLOY_KVO" ]]; then
  read -rp "Deploy KVO (Keysight Vision Orchestrator) alongside vController? [y/N]: " yn || true
  yn_lc=$(to_lower "$yn")
  [[ "$yn_lc" == "y" || "$yn_lc" == "yes" ]] && DEPLOY_KVO=true || DEPLOY_KVO=false
fi
ok "Deploy KVO: ${DEPLOY_KVO}"

# Deploy vPB?
if [[ -z "$DEPLOY_VPB" ]]; then
  read -rp "Deploy vPB alongside vController? [y/N]: " yn || true
  yn_lc=$(to_lower "$yn")
  [[ "$yn_lc" == "y" || "$yn_lc" == "yes" ]] && DEPLOY_VPB=true || DEPLOY_VPB=false
fi
ok "Deploy vPB: ${DEPLOY_VPB}"

# Chain to sensor deployment?
if [[ -z "$CHAIN_SENSORS" ]]; then
  read -rp "Chain to sensor deployment after stack is up? [y/N]: " yn || true
  yn_lc=$(to_lower "$yn")
  [[ "$yn_lc" == "y" || "$yn_lc" == "yes" ]] && CHAIN_SENSORS=true || CHAIN_SENSORS=false
fi
ok "Chain sensors: ${CHAIN_SENSORS}"
ok "Discovery tag: ${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}"

echo
printf "${C_BOLD}Resolved configuration:${C_RESET}\n"
printf "  %-22s %s\n" "Region:" "$REGION"
printf "  %-22s %s\n" "Stack name:" "$STACK_NAME"
printf "  %-22s %s\n" "Infra engine:" "$IAC"
printf "  %-22s %s\n" "Key pair:" "$KEY_NAME"
printf "  %-22s %s (%s)\n" "vController:" "$CLMS_AMI" "$CLMS_TYPE"
[[ "$DEPLOY_KVO" == "true" ]] && printf "  %-22s %s (%s)\n" "KVO:" "$KVO_AMI" "$KVO_TYPE"
[[ "$DEPLOY_VPB" == "true" ]] && printf "  %-22s %s (%s, +%s ingress/%s egress NICs, SSH %s)\n" \
  "vPB:" "$VPB_AMI" "$VPB_TYPE" "$VPB_INGRESS_NICS" "$VPB_EGRESS_NICS" "$VPB_SSH_PORT"
printf "  %-22s %s\n" "vController name:" "${VCONTROLLER_NAME:-${STACK_NAME}-vcontroller}"
[[ "$DEPLOY_KVO" == "true" ]] && printf "  %-22s %s\n" "KVO name:" "${KVO_NAME:-${STACK_NAME}-kvo}"
[[ "$DEPLOY_VPB" == "true" ]] && printf "  %-22s %s\n" "vPB name:" "${VPB_NAME:-${STACK_NAME}-vpb}"
printf "  %-22s %s\n" "Admin CIDR:" "$ADMIN_CIDR"
printf "  %-22s %s\n" "Public IPs:" "$ASSIGN_PUBLIC_IP"
if [[ -n "$EXISTING_VPC_ID" ]]; then
  printf "  %-22s %s\n" "Existing VPC:" "$EXISTING_VPC_ID"
  printf "  %-22s %s\n" "Existing subnet:" "$EXISTING_SUBNET_ID"
  [[ -n "$EXISTING_DATA_SUBNET_ID" ]] && printf "  %-22s %s\n" "vPB data subnet:" "$EXISTING_DATA_SUBNET_ID"
else
  printf "  %-22s %s\n" "Network:" "new VPC (greenfield)"
fi
[[ -n "$EXISTING_SG_ID" ]] && printf "  %-22s %s\n" "Existing SG:" "$EXISTING_SG_ID"
printf "  %-22s %s\n" "Rollback on failure:" "$ROLLBACK_ON_FAIL"
echo

# Remember the inputs (never the secrets) so a resume does not re-ask for them.
state_set REGION "$REGION"
state_set STACK_NAME "$STACK_NAME"
state_set IAC "$IAC"
state_set EC2_KEY_PAIR "$KEY_NAME"
state_set DEPLOY_KVO "$DEPLOY_KVO"
state_set DEPLOY_VPB "$DEPLOY_VPB"
state_set ADMIN_USER "$ADMIN_USERNAME"
state_set CLOUD_CONFIG "$CLOUD_CONFIG_NAME"
state_set CLM_NAME "$CLM_NAME_IN_KVO"
state_set VPB_DEVICE_NAME "$VPB_DEVICE_NAME"
state_set DISCOVERY_TAG "${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}"
state_set LAST_RUN "$(date -u +%FT%TZ)"

# =====================================================================
# Phase 4: Marketplace AMI subscription check
# =====================================================================
step "Phase 4: Marketplace AMI subscriptions"

# AWS Marketplace AMIs must be subscribed (terms accepted) on the account
# before they can launch, and there is no CLI to accept terms: the subscribe
# click happens in the Marketplace console.
#
# There is also no reliable way to pre-check it. The CloudLens AMIs are public
# images, so describe-images succeeds regardless of subscription (an earlier
# version of this script treated that as proof and printed a false "reachable"
# all-clear). run-instances --dry-run does report OptInRequired, but it needs a
# subnet, which does not exist yet on a greenfield deploy.
#
# So: show the exact subscribe links, let the customer confirm, and if the
# terms really are missing, translate the deploy failure via
# report_marketplace_failure instead of pretending to have verified it.
# Print the Marketplace subscribe URL for an AMI. The CloudLens AMIs are PUBLIC
# images, so describe-images succeeds whether or not the account has accepted
# the terms: it cannot be used as a subscription test. What it does give us is
# the Marketplace ProductCode, which maps to the exact subscribe page.
ami_subscribe_url() {
  local ami="$1" code
  code=$(aws "${AWS_REGION_ARG[@]}" ec2 describe-images --image-ids "$ami" \
           --query 'Images[0].ProductCodes[0].ProductCodeId' --output text 2>/dev/null)
  if [[ -n "$code" && "$code" != "None" ]]; then
    echo "https://aws.amazon.com/marketplace/pp?sku=${code}"
  else
    echo "https://aws.amazon.com/marketplace/search/results?searchTerms=Keysight+CloudLens"
  fi
}

# Show the subscribe links and let the customer confirm. There is deliberately
# no "reachable = subscribed" claim here: AWS offers no pre-flight API for
# "is this account subscribed", and run-instances --dry-run needs a subnet that
# does not exist yet on a greenfield deploy. If terms really are missing, the
# deploy fails with OptInRequired and report_marketplace_failure explains it.
check_ami_subscription() {
  local label="$1" ami="$2"
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "would show the Marketplace subscribe link for ${label} (${ami})"
    return 0
  fi
  local url; url="$(ami_subscribe_url "$ami")"
  printf "  %-12s %s\n" "$label" "$url"
}

# Translate a Marketplace terms failure into something actionable. AWS reports
# OptInRequired on the failing resource and its message already carries the
# product URL, so surface it verbatim rather than guessing.
report_marketplace_failure() {
  local reasons
  reasons=$(aws "${AWS_REGION_ARG[@]}" cloudformation describe-stack-events \
              --stack-name "$STACK_NAME" \
              --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].ResourceStatusReason' \
              --output text 2>/dev/null | tr '\t' '\n' | grep -i "OptInRequired\|Marketplace" | head -3)
  [[ -z "$reasons" ]] && return 1
  echo
  warn "This failed because the AWS Marketplace terms are not accepted on this account."
  echo "$reasons" | sed 's/^/    /'
  echo
  echo "  Subscribe (free to subscribe: you pay only for EC2 hours), then re-run:"
  echo "    vController  $(ami_subscribe_url "$CLMS_AMI")"
  [[ "$DEPLOY_KVO" == "true" ]] && echo "    KVO          $(ami_subscribe_url "$KVO_AMI")"
  [[ "$DEPLOY_VPB" == "true" ]] && echo "    vPB          $(ami_subscribe_url "$VPB_AMI")"
  echo "  Each one: Continue to Subscribe -> Accept Terms. Takes about a minute."
  return 0
}

# A stack that is already CREATE_COMPLETE launched from these AMIs, which is
# proof the subscriptions exist. Asking about them on a resume, and blocking on
# an Enter to do it, is asking whether something already demonstrated is true.
if [[ "$SKIP_STACK" == "true" ]]; then
  note "Skipping the Marketplace check: the stack already launched from these AMIs."
elif [[ "$DRY_RUN" != "true" ]]; then
  echo "Each CloudLens product needs a one-time Marketplace subscription on this"
  echo "AWS account. Free to subscribe: you pay only for the EC2 hours."
  echo
fi
check_ami_subscription "vController" "$CLMS_AMI"
[[ "$DEPLOY_KVO" == "true" ]] && check_ami_subscription "KVO" "$KVO_AMI"
[[ "$DEPLOY_VPB" == "true" ]] && check_ami_subscription "vPB" "$VPB_AMI"

if [[ "$DRY_RUN" != "true" ]]; then
  echo
  echo "  On each page: Continue to Subscribe -> Accept Terms (about a minute each)."
  echo "  Already subscribed on this account? Nothing to do."
  echo "  Check anytime: https://console.aws.amazon.com/marketplace/home#/subscriptions"
  echo
  read -rp "Press Enter to continue (Ctrl+C to abort and subscribe first): " _ || true
fi

# ---------------------------------------------------------------------
# Elastic IP headroom.
#
# Each component with a public IP consumes one EIP, and the default quota is
# 5 per region. Orphaned EIPs are easy to accumulate: terminating a stack's
# instances from the EC2 console leaves the stack, and its EIPs, in place. When
# the quota is short the deploy still runs for several minutes, allocates what
# it can, then dies on "The maximum number of addresses has been reached" and
# rolls the whole stack back. Checking here turns a ten minute failure into an
# immediate, actionable message.
# ---------------------------------------------------------------------
check_eip_headroom() {
  [[ "$DRY_RUN" == "true" ]] && return 0
  [[ "$ASSIGN_PUBLIC_IP" != "yes" ]] && { note "No public IPs requested, skipping Elastic IP quota check."; return 0; }

  local need=1
  [[ "$DEPLOY_KVO" == "true" ]] && need=$((need+1))
  [[ "$DEPLOY_VPB" == "true" ]] && need=$((need+1))

  local used limit free
  used=$(aws "${AWS_REGION_ARG[@]}" ec2 describe-addresses --query 'length(Addresses)' --output text 2>/dev/null)
  [[ "$used" =~ ^[0-9]+$ ]] || { warn "Could not read Elastic IP usage; skipping quota check."; return 0; }

  limit=$(aws "${AWS_REGION_ARG[@]}" service-quotas get-service-quota \
            --service-code ec2 --quota-code L-0263D0A3 \
            --query 'Quota.Value' --output text 2>/dev/null | cut -d. -f1)
  [[ "$limit" =~ ^[0-9]+$ ]] || limit=5   # AWS default when the quota API is not permitted

  free=$(( limit - used ))
  if (( free >= need )); then
    ok "Elastic IPs: ${used}/${limit} in use, need ${need}, ${free} free"
    return 0
  fi

  warn "Not enough Elastic IPs in ${REGION}: need ${need}, only ${free} free (${used}/${limit} in use)."
  echo "    The deploy would run for several minutes and then roll back."
  echo

  # Unattached EIPs are pure waste: they bill hourly and block deploys.
  local orphans
  orphans=$(aws "${AWS_REGION_ARG[@]}" ec2 describe-addresses \
    --query 'Addresses[?AssociationId==`null`].[PublicIp,AllocationId,Tags[?Key==`Name`]|[0].Value]' \
    --output text 2>/dev/null)

  if [[ -n "$orphans" ]]; then
    echo "    Unattached Elastic IPs (billing hourly, attached to nothing):"
    echo "$orphans" | while read -r ip alloc name; do
      printf "      %-16s %-24s %s\n" "$ip" "$alloc" "${name:-untagged}"
    done
    echo
    echo "    Release them all with:"
    echo "      aws ec2 describe-addresses --region ${REGION} \\"
    echo "        --query 'Addresses[?AssociationId==\`null\`].AllocationId' --output text \\"
    echo "        | tr '\\t' '\\n' | xargs -I{} aws ec2 release-address --region ${REGION} --allocation-id {}"
    echo
    echo "    If they belong to a CloudFormation stack whose instances are gone,"
    echo "    delete that stack instead so it does not recreate them:"
    echo "      aws cloudformation delete-stack --stack-name <name> --region ${REGION}"
  else
    echo "    All Elastic IPs are in use by running instances. Request a quota increase:"
    echo "      https://console.aws.amazon.com/servicequotas/home/services/ec2/quotas/L-0263D0A3"
  fi
  echo
  echo "    Or deploy without public IPs and reach the stack privately:"
  echo "      re-run with --no-public-ip"
  echo
  read -rp "    Continue anyway? [y/N]: " yn || true
  [[ "$(to_lower "${yn:-n}")" == "y" ]] || fail "Aborted: free up Elastic IPs, then re-run."
}
# Only when we are actually going to create the stack. A resume against an
# existing CREATE_COMPLETE stack allocates no new Elastic IPs: its three are
# already attached and counted in "in use". Checking headroom anyway made a
# perfectly good resume abort on a quota it was never going to draw from,
# which is the exact thing resume exists to avoid. Seen live: a re-run of a
# finished stack refused to reach the sensor phase.
if [[ "$SKIP_STACK" == "true" ]]; then
  note "Skipping the Elastic IP quota check: ${REASON_STACK:-the stack already exists}, so no new addresses are needed."
else
  check_eip_headroom
fi

# =====================================================================
# Phase 5: Infra engine selection
# =====================================================================
step "Phase 5: Infrastructure engine (${IAC})"

# Make sure the repo (template + terraform dir) is on disk. If curl|bash'd
# outside the repo, clone it so both engines have their sources.
if [[ ! -f "$REPO_DIR/$CFN_TEMPLATE_REL" ]] && [[ "$DRY_RUN" != "true" ]]; then
  note "Repo sources not found locally; cloning to $REPO_DIR"
  if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone -q "https://github.com/${REPO_OWNER}/${REPO_NAME}.git" "$REPO_DIR" 2>/dev/null \
      || warn "Could not clone repo. Run this script from inside a checkout of ${REPO_NAME}."
  fi
fi

# Refresh a clone WE created. curl|bash always fetches the newest deploy-stack.sh,
# but the helper scripts it calls come off disk, so a clone left by an earlier run
# silently pins them to whatever commit that run saw. That is version skew between
# the orchestrator and its own helpers, and it surfaces as nonsense: a live run hit
#   kvo_license.py: error: unrecognized arguments: --accept-eula
# because the new deploy-stack.sh was calling a months-old kvo_license.py.
# Only a clone at $HOME/<repo> is touched. A checkout the operator works in is
# never modified, and a dirty tree is left alone even then.
if [[ "$REPO_DIR_IS_OURS" == "true" && -d "$REPO_DIR/.git" && "$DRY_RUN" != "true" ]]; then
  if [[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]]; then
    warn "Local changes in $REPO_DIR; leaving it as it is."
    note "Helper scripts may be older than this one. Commit or stash, or delete that directory."
  else
    _before="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if git -C "$REPO_DIR" fetch -q --depth 1 origin main 2>/dev/null \
       && git -C "$REPO_DIR" reset -q --hard FETCH_HEAD 2>/dev/null; then
      _after="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
      if [[ "$_before" != "$_after" ]]; then
        ok "Helper scripts updated: ${_before} -> ${_after}"
      fi
    else
      warn "Could not refresh $REPO_DIR; helper scripts may be out of date."
    fi
  fi
fi

if [[ "$IAC" == "terraform" ]]; then
  if ! command -v terraform >/dev/null 2>&1 && [[ "$DRY_RUN" != "true" ]]; then
    warn "Terraform not installed."
    echo "  Install from https://developer.hashicorp.com/terraform/install then re-run,"
    echo "  or re-run with --iac cfn to use CloudFormation (no extra tooling needed)."
    fail "Terraform selected but not found on PATH."
  fi
  ok "Using Terraform (deploy/terraform)"
else
  ok "Using CloudFormation (deploy/cloudformation/stack.yaml)"
fi

# =====================================================================
# Phase 6: Deploy the stack
# =====================================================================
step "Phase 6: Deploy stack (vController + optional KVO + optional vPB)"

# The CloudFormation template (deploy/cloudformation/stack.yaml) takes yes/no
# toggles and resolves the CloudLens AMIs from an in-template RegionMap, so we
# do NOT pass AMI ids to it. Instance types are constrained to the qualified
# size per appliance. deploy-stack.sh's boolean toggles map to yes/no here.
deploy_cfn() {
  local tpl="$REPO_DIR/$CFN_TEMPLATE_REL"
  local kvo_yn="no" vpb_yn="no"
  [[ "$DEPLOY_KVO" == "true" ]] && kvo_yn="yes"
  [[ "$DEPLOY_VPB" == "true" ]] && vpb_yn="yes"

  # A Windows test VM with no instance profile deploys fine and is then
  # unreachable: Ansible drives Windows over SSM, and SSM only manages an
  # instance carrying AmazonSSMManagedInstanceCore. The template supports the
  # profile and documents it; nothing was passing it, so --test-vms windows
  # always produced a VM the sensor step could not touch.
  if [[ "$TEST_WINDOWS" == "yes" && -z "$WINDOWS_INSTANCE_PROFILE" ]]; then
    warn "Deploying a Windows test VM with no instance profile."
    note "Ansible reaches Windows over SSM, which needs a profile granting"
    note "AmazonSSMManagedInstanceCore. Without one the VM comes up fine but"
    note "the sensor step cannot reach it. Pass an existing profile with:"
    note "  --windows-instance-profile NAME"
    note "The Linux test VMs are unaffected."
  fi
  if [[ "$TEST_WINDOWS" == "yes" && -z "$WINDOWS_INSTALLER_URL" ]]; then
    note "Windows sensors also need the installer URL from the CloudLens Manager"
    note "(Launch Agent -> Start New Agents -> the Windows agents link). Set it with"
    note "  CLOUDLENS_WINDOWS_INSTALLER_URL=... or windows.installer_url in customer_input.yaml."
    note "Without it the Windows sensor step asserts early with the fix. Windows is"
    note "already covered agentlessly by the AWS mirror, so the sensor is additive."
  fi

  # Build parameter-overrides. Always pass the core set; append brownfield /
  # access params only when the customer set them, so blank ones keep the
  # template defaults (greenfield, public IPs, template-managed SG).
  local -a params=(
    # The CLI already created/verified the key pair in ensure_key_pair, so tell
    # the template to use it. Without this the template's default action
    # ("Create a new key pair for me") would win and mint a second key,
    # stranding the private key we saved to ~/.ssh/<name>.pem.
    "KeyPairAction=Use an existing key pair"
    "KeyPairName=$KEY_NAME"
    "DeployKVO=$kvo_yn"
    "DeployVPB=$vpb_yn"
    "AdminIngressCidr=$ADMIN_CIDR"
    "AssignPublicIp=$ASSIGN_PUBLIC_IP"
    "DeployTestWorkloadUbuntu=$TEST_UBUNTU"
    "DeployTestWorkloadRhel=$TEST_RHEL"
    "DeployTestWorkloadWindows=$TEST_WINDOWS"
    "TestWindowsInstanceProfile=$WINDOWS_INSTANCE_PROFILE"
    "DiscoveryTagKey=$DISCOVERY_TAG_KEY"
    "DiscoveryTagValue=$DISCOVERY_TAG_VALUE"
    "VcontrollerInstanceType=$CLMS_TYPE"
    "KvoInstanceType=$KVO_TYPE"
    "VpbInstanceType=$VPB_TYPE"
  )
  [[ -n "$VCONTROLLER_NAME" ]]        && params+=("VcontrollerName=$VCONTROLLER_NAME")
  [[ -n "$KVO_NAME" ]]                && params+=("KvoName=$KVO_NAME")
  [[ -n "$VPB_NAME" ]]                && params+=("VpbName=$VPB_NAME")
  [[ -n "$EXISTING_VPC_ID" ]]         && params+=("ExistingVpcId=$EXISTING_VPC_ID")
  [[ -n "$EXISTING_SUBNET_ID" ]]      && params+=("ExistingSubnetId=$EXISTING_SUBNET_ID")
  [[ -n "$EXISTING_DATA_SUBNET_ID" ]] && params+=("ExistingDataSubnetId=$EXISTING_DATA_SUBNET_ID")
  [[ -n "$EXISTING_SG_ID" ]]          && params+=("ExistingSecurityGroupId=$EXISTING_SG_ID")

  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "aws cloudformation deploy --stack-name ${STACK_NAME} --template-file ${tpl} \\
      --capabilities CAPABILITY_NAMED_IAM \\
      --parameter-overrides ${params[*]}"
    return 0
  fi
  [[ -f "$tpl" ]] || fail "CloudFormation template not found: $tpl"
  if ! region_supported "$REGION"; then
    fail "Region ${REGION} has no RegionMap entry in ${CFN_TEMPLATE_REL}. Use a supported region (${SUPPORTED_CFN_REGIONS}) or --iac terraform."
  fi
  aws "${AWS_REGION_ARG[@]}" cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file "$tpl" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides "${params[@]}"
  CREATED_STACK=true
}

cfn_output() {
  local key="$1"
  [[ "$DRY_RUN" == "true" ]] && { echo "203.0.113.10"; return 0; }
  aws "${AWS_REGION_ARG[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue | [0]" --output text 2>/dev/null || echo ""
}

deploy_terraform() {
  local dir="$REPO_DIR/$TF_DIR_REL"
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "cd ${dir} && terraform init && terraform apply -auto-approve \\
      -var region=${REGION} -var stack_name=${STACK_NAME} -var key_name=${KEY_NAME} \\
      -var deploy_kvo=${DEPLOY_KVO} -var deploy_vpb=${DEPLOY_VPB} \\
      -var controller_ami=${CLMS_AMI} -var controller_type=${CLMS_TYPE} \\
      -var kvo_ami=${KVO_AMI} -var kvo_type=${KVO_TYPE} \\
      -var vpb_ami=${VPB_AMI} -var vpb_type=${VPB_TYPE} -var vpb_ssh_port=${VPB_SSH_PORT} \\
      -var vpb_ingress_nics=${VPB_INGRESS_NICS} -var vpb_egress_nics=${VPB_EGRESS_NICS} \\
      -var admin_cidr=${ADMIN_CIDR}"
    return 0
  fi
  [[ -d "$dir" ]] || fail "Terraform directory not found: $dir"
  ( cd "$dir" && terraform init -input=false >/dev/null && \
    terraform apply -auto-approve \
      -var "region=$REGION" -var "stack_name=$STACK_NAME" -var "key_name=$KEY_NAME" \
      -var "deploy_kvo=$DEPLOY_KVO" -var "deploy_vpb=$DEPLOY_VPB" \
      -var "controller_ami=$CLMS_AMI" -var "controller_type=$CLMS_TYPE" \
      -var "kvo_ami=$KVO_AMI" -var "kvo_type=$KVO_TYPE" \
      -var "vpb_ami=$VPB_AMI" -var "vpb_type=$VPB_TYPE" -var "vpb_ssh_port=$VPB_SSH_PORT" \
      -var "vpb_ingress_nics=$VPB_INGRESS_NICS" -var "vpb_egress_nics=$VPB_EGRESS_NICS" \
      -var "admin_cidr=$ADMIN_CIDR" )
  CREATED_STACK=true
}

tf_output() {
  local key="$1"
  [[ "$DRY_RUN" == "true" ]] && { echo "203.0.113.10"; return 0; }
  ( cd "$REPO_DIR/$TF_DIR_REL" && terraform output -raw "$key" 2>/dev/null ) || echo ""
}

if [[ "$IAC" == "terraform" ]]; then
  if run_phase stack; then
    # A real apply can move addresses, so re-read them rather than trusting
    # what detection saw before it ran.
    CLMS_PUBLIC_IP=""; KVO_PUBLIC_IP=""; VPB_PUBLIC_IP=""
    deploy_terraform
  else
    skip_note "the Terraform apply"
    note "Keeping the addresses detection already read from the existing workspace."
  fi
  [[ -z "$CLMS_PUBLIC_IP" ]] && CLMS_PUBLIC_IP="$(tf_output controller_public_ip)"
  [[ "$DEPLOY_KVO" == "true" && -z "$KVO_PUBLIC_IP" ]] && KVO_PUBLIC_IP="$(tf_output kvo_public_ip)"
  [[ "$DEPLOY_VPB" == "true" && -z "$VPB_PUBLIC_IP" ]] && VPB_PUBLIC_IP="$(tf_output vpb_public_ip)"
else
  if run_phase stack; then
    CLMS_PUBLIC_IP=""; KVO_PUBLIC_IP=""; VPB_PUBLIC_IP=""
    deploy_cfn
  else
    skip_note "the CloudFormation deploy"
    note "Keeping the addresses detection already read from the existing stack."
  fi
  # Outputs are named *Address (public EIP when AssignPublicIp=yes, else the
  # private IP). Same variable is used downstream for the wait + sensor chain.
  [[ -z "$CLMS_PUBLIC_IP" ]] && CLMS_PUBLIC_IP="$(cfn_output VcontrollerAddress)"
  [[ "$DEPLOY_KVO" == "true" && -z "$KVO_PUBLIC_IP" ]] && KVO_PUBLIC_IP="$(cfn_output KvoAddress)"
  [[ "$DEPLOY_VPB" == "true" && -z "$VPB_PUBLIC_IP" ]] && VPB_PUBLIC_IP="$(cfn_output VpbAddress)"
fi

ok "vController at ${CLMS_PUBLIC_IP:-unknown}"
[[ "$DEPLOY_KVO" == "true" ]] && ok "KVO at ${KVO_PUBLIC_IP:-unknown}"
[[ "$DEPLOY_VPB" == "true" ]] && ok "vPB at ${VPB_PUBLIC_IP:-unknown} (SSH on port ${VPB_SSH_PORT})"

state_set VCONTROLLER_ADDRESS "$CLMS_PUBLIC_IP"
state_set KVO_ADDRESS "$KVO_PUBLIC_IP"
state_set VPB_ADDRESS "$VPB_PUBLIC_IP"
state_phase stack done

# ---------------------------------------------------------------------
# Post-deploy helpers, shared by phases 8 and 10-16.
#
# The appliances talk to each other over PRIVATE addressing (KVO licenses the
# vPB on its private IP, collectors register to the vController privately), and
# the traffic-path / mirror steps need the data-plane ENIs. CloudFormation
# publishes some of that as outputs; Terraform does not, so both paths fall
# back to reading the instance by its Name tag.
# ---------------------------------------------------------------------
KEY_PEM="$HOME/.ssh/${KEY_NAME}.pem"

# Locate a repo script wherever this run is executing from (checkout, clone,
# or cwd). Echoes the path, returns 1 when it is nowhere to be found.
find_repo_script() {
  local rel="$1" cand
  for cand in "$SCRIPT_DIR/../${rel}" "$REPO_DIR/${rel}" "$PWD/${rel}"; do
    [[ -f "$cand" ]] && { echo "$cand"; return 0; }
  done
  return 1
}

# ec2_fact <name-tag> <jmespath under Reservations[0].Instances[0]>
ec2_fact() {
  local name="$1" q="$2" v
  v=$(aws "${AWS_REGION_ARG[@]}" ec2 describe-instances \
        --filters "Name=tag:Name,Values=${name}" "Name=instance-state-name,Values=running" \
        --query "Reservations[0].Instances[0].${q}" --output text 2>/dev/null) || v=""
  [[ "$v" == "None" ]] && v=""
  printf '%s' "$v"
}

discover_stack_facts() {
  local vc_tag="${VCONTROLLER_NAME:-${STACK_NAME}-vcontroller}"
  local kvo_tag="${KVO_NAME:-${STACK_NAME}-kvo}"
  local vpb_tag="${VPB_NAME:-${STACK_NAME}-vpb}"

  if [[ "$DRY_RUN" == "true" ]]; then
    CLMS_PRIVATE_IP="10.0.0.10"; KVO_PRIVATE_IP="10.0.0.11"
    VPB_PRIVATE_IP="10.0.0.12";  VPB_INGRESS_IP="10.0.1.12"
    STACK_VPC_ID="vpc-dryrun";   STACK_ZONE="${REGION}a"
    MGMT_SUBNET_ID="subnet-mgmt"; INGRESS_SUBNET_ID="subnet-ingress"
    EGRESS_SUBNET_ID="subnet-egress"
    dryrun_say "would read private IPs, VPC and subnets from the deployed stack"
    return 0
  fi

  if [[ "$IAC" == "terraform" ]]; then
    STACK_VPC_ID="$(tf_output vpc_id)"
  else
    STACK_VPC_ID="$(cfn_output SharedVpcId)"
    CLMS_PRIVATE_IP="$(cfn_output VcontrollerPrivateIp)"
    [[ "$DEPLOY_KVO" == "true" ]] && KVO_PRIVATE_IP="$(cfn_output KvoPrivateIp)"
    [[ "$DEPLOY_VPB" == "true" ]] && VPB_PRIVATE_IP="$(cfn_output VpbPrivateIp)"
  fi
  [[ -z "$CLMS_PRIVATE_IP" || "$CLMS_PRIVATE_IP" == "None" ]] && CLMS_PRIVATE_IP="$(ec2_fact "$vc_tag" PrivateIpAddress)"
  MGMT_SUBNET_ID="$(ec2_fact "$vc_tag" SubnetId)"
  STACK_ZONE="$(ec2_fact "$vc_tag" 'Placement.AvailabilityZone')"

  if [[ "$DEPLOY_KVO" == "true" ]]; then
    [[ -z "$KVO_PRIVATE_IP" || "$KVO_PRIVATE_IP" == "None" ]] && KVO_PRIVATE_IP="$(ec2_fact "$kvo_tag" PrivateIpAddress)"
  fi
  if [[ "$DEPLOY_VPB" == "true" ]]; then
    [[ -z "$VPB_PRIVATE_IP" || "$VPB_PRIVATE_IP" == "None" ]] && VPB_PRIVATE_IP="$(ec2_fact "$vpb_tag" PrivateIpAddress)"
    # Data-plane ENIs: device index 1 is the first ingress NIC, the first egress
    # NIC follows the ingress ones (see the attachment ordering in the template).
    local egress_idx=$(( 1 + VPB_INGRESS_NICS ))
    VPB_INGRESS_IP="$(ec2_fact "$vpb_tag" "NetworkInterfaces[?Attachment.DeviceIndex==\`1\`].PrivateIpAddress | [0]")"
    INGRESS_SUBNET_ID="$(ec2_fact "$vpb_tag" "NetworkInterfaces[?Attachment.DeviceIndex==\`1\`].SubnetId | [0]")"
    EGRESS_SUBNET_ID="$(ec2_fact "$vpb_tag" "NetworkInterfaces[?Attachment.DeviceIndex==\`${egress_idx}\`].SubnetId | [0]")"
  fi
  # Collectors can share the management subnet when there is no vPB data plane.
  [[ -z "$INGRESS_SUBNET_ID" ]] && INGRESS_SUBNET_ID="$MGMT_SUBNET_ID"
  [[ -z "$EGRESS_SUBNET_ID" ]]  && EGRESS_SUBNET_ID="$MGMT_SUBNET_ID"
  return 0
}

# python3 + requests are what every scripts/*.py needs. Probe once, quietly.
# Always call this as an `if` condition: it reports readiness by exit status.
PY_CHAIN_OK=""
python_chain_ready() {
  if [[ -z "$PY_CHAIN_OK" ]]; then
    PY_CHAIN_OK=true
    if ! command -v python3 >/dev/null 2>&1; then
      PY_CHAIN_OK=false
    elif ! python3 -c "import requests" 2>/dev/null; then
      python3 -m pip install --quiet --user requests 2>/dev/null \
        || python3 -m pip install --quiet --break-system-packages --user requests 2>/dev/null \
        || true
      python3 -c "import requests" 2>/dev/null || PY_CHAIN_OK=false
    fi
  fi
  [[ "$PY_CHAIN_OK" == "true" ]]
}

discover_stack_facts

# =====================================================================
# Phase 7: Wait for vController init
# =====================================================================
step "Phase 7: Wait for vController initialization"

if ! run_phase wait; then
  skip_note "the vController wait"
  ok "The vController API answered during detection, so there is nothing to wait for."
elif [[ "$DRY_RUN" == "true" ]]; then
  dryrun_say "would poll https://${CLMS_PUBLIC_IP}:443 every 15s for up to 17 minutes"
elif [[ -z "$CLMS_PUBLIC_IP" || "$CLMS_PUBLIC_IP" == "None" ]]; then
  warn "No address available, skipping wait"
elif [[ "$ASSIGN_PUBLIC_IP" == "no" ]]; then
  # Private-IP deploy: the address is a private IP not reachable from here
  # unless this host is inside the VPC (VPN / Direct Connect / peering).
  # Probing would just hang, so skip and let the operator verify over their
  # private path.
  warn "Deployed with private IPs (--no-public-ip). Skipping the port-443 probe."
  note "vController private IP: ${CLMS_PUBLIC_IP}"
  note "Reach https://${CLMS_PUBLIC_IP} over your VPN / Direct Connect / VPC peering."
  note "Allow ~15 minutes for full initialization before logging in."
else
  # Wait for the API, not just the port. nginx accepts connections on 443 and
  # serves the single-page app about ten minutes before the REST API is up,
  # and the SPA fallback answers 200 for every unmatched path. Treating an open
  # port (or any 200) as "ready" declares success far too early, and the next
  # step then fails against an API that is not listening yet.
  #
  # While only the SPA is up, POST to the login route is unroutable and nginx
  # answers 405. Once the API is live that route answers 400/401/422 for bad
  # credentials, which is the real readiness signal.
  #
  # The route is /cloudlens/api/v1/identity/login. This probed /api/v3/users/
  # login, which does not exist on this product: it answers 405 forever, so the
  # loop could never match and every deploy burned the full 20 minutes and then
  # warned that the API never came up. Phase 9 would immediately log in and
  # succeed against the real path, which is what gave the lie away.
  # scripts/vcontroller_project_key.py already documents the same mistake.
  echo "vController needs ~15 minutes to initialize. Waiting for its REST API"
  echo "(not just port 443, which opens much earlier)."
  deadline=$(( $(date +%s) + 20*60 ))
  api_ready=false
  while (( $(date +%s) < deadline )); do
    remaining=$(( deadline - $(date +%s) ))
    printf "\r  %s seconds remaining, probing the vController API..." "$remaining"
    code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 8 -X POST \
             -H "Content-Type: application/json" \
             -d '{"username":"__probe__","password":"__probe__"}' \
             "https://${CLMS_PUBLIC_IP}/cloudlens/api/v1/identity/login" 2>/dev/null || echo "000")
    case "$code" in
      200|400|401|403|422) echo; ok "vController API is serving (probe HTTP ${code})"; api_ready=true; break ;;
    esac
    sleep 15
  done
  echo
  if [[ "$api_ready" != "true" ]]; then
    warn "vController API did not come up within 20 minutes."
    note "The instance is running; it may just need longer, or the subnet may lack"
    note "the outbound internet access the appliance needs on first boot."
    note "Check manually: curl -sk -o /dev/null -w '%{http_code}\\n' \\"
    note "  -X POST -H 'Content-Type: application/json' -d '{}' https://${CLMS_PUBLIC_IP}/cloudlens/api/v1/identity/login"
  fi
fi
state_phase wait done

# =====================================================================
# Phase 8: vPB post-deploy bootstrap (KCOS wait + vpb CLI wrapper)
# =====================================================================
# scripts/bootstrap-vpb.sh runs ON the vPB. It waits for KCOS, sets KUBECONFIG
# system-wide and installs the `sudo vpb` wrapper. Nothing else on the vPB works
# before it has run, including the KVO adoption in phase 14, so run it here over
# SSH when we hold the key pair PEM and fall back to printing the two commands
# when we do not.
# =====================================================================
vpb_bootstrap_manual_note() {
  note "Run the vPB one-time bootstrap yourself (KCOS wait + vpb CLI wrapper):"
  note "  ssh -i ${KEY_PEM} -p ${VPB_SSH_PORT} ${ADMIN_USERNAME}@${VPB_PUBLIC_IP}"
  note "  curl -sSL ${REPO_RAW}/scripts/bootstrap-vpb.sh | sudo bash"
}

if [[ "$DEPLOY_VPB" == "true" ]]; then
  step "Phase 8: vPB post-deploy bootstrap"
  note "vPB management SSH is reachable on port ${VPB_SSH_PORT} within ~5 minutes."

  announce_vpb_login

  if ! run_phase bootstrap; then
    skip_note "the vPB bootstrap"
    BOOTSTRAP_VPB=false
  fi

  if [[ -z "$BOOTSTRAP_VPB" ]]; then
    if ask_yn "Run the vPB bootstrap over SSH now? [Y/n]: " y; then
      BOOTSTRAP_VPB=true
    else
      BOOTSTRAP_VPB=false
    fi
  fi
  ok "Bootstrap vPB: ${BOOTSTRAP_VPB}"

  VPB_SSH_OPTS=(-n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
                -o LogLevel=ERROR -o ConnectTimeout=10)

  if [[ "$BOOTSTRAP_VPB" != "true" ]]; then
    vpb_bootstrap_manual_note
  elif [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "ssh -i ${KEY_PEM} -p ${VPB_SSH_PORT} ${ADMIN_USERNAME}@${VPB_PUBLIC_IP} 'curl -sSL ${REPO_RAW}/scripts/bootstrap-vpb.sh | sudo bash'"
  elif [[ ! -f "$KEY_PEM" ]]; then
    warn "Private key ${KEY_PEM} not found, so the vPB bootstrap cannot run from here."
    pem_help
    vpb_bootstrap_manual_note
    BOOTSTRAP_VPB=false
  elif [[ -z "$VPB_PUBLIC_IP" || "$VPB_PUBLIC_IP" == "None" ]]; then
    warn "No vPB address available; skipping the bootstrap."
    BOOTSTRAP_VPB=false
  else
    note "Waiting for vPB SSH on ${VPB_PUBLIC_IP}:${VPB_SSH_PORT} (up to 10 minutes)..."
    vpb_ssh_up=false
    ssh_deadline=$(( $(date +%s) + 10*60 ))
    while (( $(date +%s) < ssh_deadline )); do
      if ssh "${VPB_SSH_OPTS[@]}" -i "$KEY_PEM" -p "$VPB_SSH_PORT" \
           "${ADMIN_USERNAME}@${VPB_PUBLIC_IP}" true 2>/dev/null; then
        vpb_ssh_up=true; break
      fi
      sleep 15
    done
    if [[ "$vpb_ssh_up" != "true" ]]; then
      warn "vPB SSH did not answer within 10 minutes; skipping the bootstrap."
      vpb_bootstrap_manual_note
      BOOTSTRAP_VPB=false
    elif ssh "${VPB_SSH_OPTS[@]}" -i "$KEY_PEM" -p "$VPB_SSH_PORT" \
           "${ADMIN_USERNAME}@${VPB_PUBLIC_IP}" \
           "curl -sSL ${REPO_RAW}/scripts/bootstrap-vpb.sh | sudo bash"; then
      ok "vPB bootstrap completed."
    else
      warn "vPB bootstrap exited non-zero. The vPB is up; finish it by hand:"
      vpb_bootstrap_manual_note
      BOOTSTRAP_VPB=false
    fi
  fi
fi

# =====================================================================
# Phase 9: Project key + working UI login (automated, manual fallback)
# =====================================================================
step "Phase 9: Project key + vController login"

# The appliance forces a password change on the first UI login, which
# invalidates every session. So automation MUST complete that change itself to
# a known value and then hand the operator the working login. Otherwise the
# first human in the UI silently locks out everyone else, including this
# script's own token. scripts/vcontroller_project_key.py does all of it:
# login -> set known password -> create project -> return key, and prints the
# full login (url / user / password / project / key) for the operator.
# (VC_CREDS_FILE is defined up top: detection reads it too.)

vc_key_script() { find_repo_script "scripts/vcontroller_project_key.py"; }

# Re-running this step against an already-rotated admin password FAILS, so a
# resume reuses the key already in the creds file rather than rotating again.
if ! run_phase key; then
  skip_note "the project key step"
  if VC_PROJECT_KEY="$(vc_key_from_creds)"; then
    ok "Reusing the project key already in ${VC_CREDS_FILE} (${#VC_PROJECT_KEY} characters)."
  else
    VC_PROJECT_KEY=""
    warn "Could not read a project key back from ${VC_CREDS_FILE}."
    note "Re-run with --only key to mint one, or paste it when asked below."
  fi
elif [[ "$DRY_RUN" == "true" ]]; then
  dryrun_say "would run vcontroller_project_key.py --host ${CLMS_PUBLIC_IP} to mint the key and set a known admin password"
elif [[ -z "$CLMS_PUBLIC_IP" || "$CLMS_PUBLIC_IP" == "None" ]]; then
  warn "No vController address available; skipping automated key retrieval."
elif [[ "$ASSIGN_PUBLIC_IP" == "no" ]]; then
  note "Private-IP deploy: run the key step from inside the VPC:"
  note "  python3 scripts/vcontroller_project_key.py --host ${CLMS_PUBLIC_IP} --insecure"
else
  KEY_SCRIPT="$(vc_key_script || true)"
  PY_OK=true
  if ! command -v python3 >/dev/null 2>&1; then
    PY_OK=false
    warn "python3 not found; cannot automate the project key."
  elif ! python3 -c "import requests" 2>/dev/null; then
    # The one dependency. Offer to install rather than silently degrading.
    warn "The python 'requests' module is missing (needed to talk to the vController API)."
    read -rp "  Install it now with pip? [Y/n]: " yn || true
    if [[ "$(to_lower "${yn:-y}")" != "n" ]]; then
      python3 -m pip install --quiet --user requests 2>/dev/null \
        || python3 -m pip install --quiet --break-system-packages --user requests 2>/dev/null \
        || true
    fi
    python3 -c "import requests" 2>/dev/null || PY_OK=false
  fi

  if [[ "$PY_OK" == "true" ]] && [[ -n "$KEY_SCRIPT" ]]; then
    note "Automating: login, set a known admin password, create project, fetch key..."
    # stdout = the key only; the login banner goes to stderr (visible + logged).
    VC_PROJECT_KEY=$(python3 "$KEY_SCRIPT" --host "$CLMS_PUBLIC_IP" \
        --project "${CLOUDLENS_PROJECT_NAME:-cloudlens-autopilot}" \
        ${CLOUDLENS_VC_PASSWORD:+--new-password "$CLOUDLENS_VC_PASSWORD"} \
        --creds-file "$VC_CREDS_FILE" \
        --insecure --wait 900) || VC_PROJECT_KEY=""
    if [[ -n "$VC_PROJECT_KEY" ]]; then
      ok "Project key retrieved automatically."
      note "Your working UI login is shown above and saved to ${VC_CREDS_FILE} (mode 600)."
      note "That login is where you see the project and every VM whose sensor registers."
    else
      warn "Automated key retrieval failed; falling back to the manual steps."
    fi
  fi
fi

if [[ -z "$VC_PROJECT_KEY" ]] && [[ "$DRY_RUN" != "true" ]]; then
  cat <<EOM

Manual path: to deploy sensors, you need a project key.

  1. Open the vController UI: https://${CLMS_PUBLIC_IP}
  2. Sign in: admin / Cl0udLens@dm!n (or the password you set earlier)
  3. You will be prompted to change the password on first login. The password
     you set THERE becomes the real one: record it, it invalidates the default.
  4. Go to Projects -> Add Project, give it a name, then open the
     project and copy the API key.

EOM
fi
state_phase key done

# The vController can take a login from here on, and everything below (KVO
# licensing, adoption, sensors, vPB) is visible in its UI while it happens.
announce_vcontroller_login

# ---------------------------------------------------------------------
# What discovery will ACTUALLY search for.
#
# customer_input.yaml, not --discovery-tag-key/value, is the source of truth
# once it exists: scripts/render_inventory.py builds the runtime inventory from
# aws.tag_filters (which can hold several tags, all ANDed), aws.instance_ids,
# or aws.inventory_file. A count taken from one tag alone would therefore
# describe a search nobody is going to run. Read the file when it is there, and
# say plainly when the count is only an approximation.
#
# Sets: DISCOVERY_MODE (tags|instance-ids|static|approx), DISCOVERY_DESC,
#       DISCOVERY_FILTER_ARGS (aws ec2 describe-instances --filters values).
# ---------------------------------------------------------------------
DISCOVERY_MODE="approx"
DISCOVERY_DESC="${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}"
DISCOVERY_FILTER_ARGS=()

discovery_scope() {
  DISCOVERY_MODE="approx"
  DISCOVERY_DESC="${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}"
  DISCOVERY_FILTER_ARGS=("Name=tag:${DISCOVERY_TAG_KEY},Values=${DISCOVERY_TAG_VALUE}")

  # A flag the operator typed on this command line beats a file left behind by
  # an earlier run. Preserving operator edits to customer_input.yaml must not
  # mean silently ignoring --discovery-tag-key/value, which is the same
  # "the flag does nothing" failure this file was changed to fix.
  if [[ "${DISCOVERY_TAG_EXPLICIT:-false}" == "true" ]]; then
    DISCOVERY_MODE="tags"
    return 0
  fi

  [[ -f customer_input.yaml ]] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local out=""
  out="$(python3 - customer_input.yaml <<'PY' 2>/dev/null
import sys
try:
    import yaml
except ImportError:
    sys.exit(1)
try:
    with open(sys.argv[1]) as fh:
        doc = yaml.safe_load(fh)
except Exception:
    sys.exit(1)
if not isinstance(doc, dict):
    sys.exit(1)
aws = doc.get("aws") if isinstance(doc.get("aws"), dict) else {}

def text(v):
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return "" if v is None else str(v)

if aws.get("inventory_file"):
    print("static")
    print("the inventory file %s" % text(aws["inventory_file"]))
    sys.exit(0)

ids = aws.get("instance_ids") or []
if isinstance(ids, (list, tuple)) and ids:
    ids = [text(i) for i in ids if text(i)]
    print("instance-ids")
    print("instance ids " + " ".join(ids))
    print("Name=instance-id,Values=" + ",".join(ids))
    sys.exit(0)

tags = aws.get("tag_filters")
if isinstance(tags, dict) and tags:
    print("tags")
    print(" ".join("%s=%s" % (k, text(v)) for k, v in tags.items()))
    for k, v in tags.items():
        print("Name=tag:%s,Values=%s" % (k, text(v)))
    sys.exit(0)
sys.exit(1)
PY
)" || out=""
  [[ -n "$out" ]] || return 0

  local line i=0
  DISCOVERY_FILTER_ARGS=()
  while IFS= read -r line; do
    case "$i" in
      0) DISCOVERY_MODE="$line" ;;
      1) DISCOVERY_DESC="$line" ;;
      *) DISCOVERY_FILTER_ARGS+=("$line") ;;
    esac
    i=$((i+1))
  done <<< "$out"
  return 0
}

# Pre-flight: count the EC2s the sensor step will actually discover.

# ---------------------------------------------------------------------
# Two things used to end a run with homework: no Ansible, and no workloads
# to install onto. Both are one command away, so offer to do them here
# rather than printing the command and stopping. Both return 1 when they
# decline or fail, and the caller keeps its original blocker message.
# ---------------------------------------------------------------------
install_ansible_now() {
  command -v ansible-playbook >/dev/null 2>&1 && return 0
  command -v pip3 >/dev/null 2>&1 || return 1
  [[ "$DRY_RUN" == "true" ]] && { dryrun_say "would pip3 install --user ansible boto3"; return 0; }
  ask_yn "  Install Ansible now with pip3 (into your home, ~1 min)? [Y/n]: " "y" || return 1
  step "Installing Ansible"
  # Show the real error. The first version of this sent stdout AND stderr to
  # /dev/null and printed "pip3 install failed", which told the operator
  # nothing: a live CloudShell run failed here and the cause was invisible.
  # Newer distros also refuse a --user install into a managed environment
  # (PEP 668), so retry once with the documented escape hatch before giving up.
  # --user is INVALID inside a virtualenv, and AWS CloudShell now runs one:
  #   ERROR: Can not perform a '--user' install. User site-packages are not
  #   visible in this virtualenv.
  # So pick the flag from the environment instead of assuming. In a venv the
  # plain install lands on PATH already; outside one, --user keeps it in $HOME,
  # which is the only part of CloudShell that survives the session.
  local _user_flag="--user"
  if [[ -n "${VIRTUAL_ENV:-}" ]] || python3 -c 'import sys; sys.exit(0 if sys.prefix!=sys.base_prefix else 1)' 2>/dev/null; then
    _user_flag=""
    note "Python here is a virtualenv, so installing into it rather than --user."
  fi
  local _pip_log; _pip_log="$(mktemp)"
  if ! pip3 install ${_user_flag} --quiet ansible boto3 >"$_pip_log" 2>&1; then
    if grep -qi "externally-managed-environment" "$_pip_log"; then
      note "Python here refuses --user installs (PEP 668). Retrying into a managed break-out."
      if ! pip3 install ${_user_flag} --quiet --break-system-packages ansible boto3 >>"$_pip_log" 2>&1; then
        warn "pip3 install failed. The error was:"
        sed -n '1,12p' "$_pip_log" | sed 's/^/      /'
        rm -f "$_pip_log"; return 1
      fi
    else
      warn "pip3 install failed. The error was:"
      sed -n '1,12p' "$_pip_log" | sed 's/^/      /'
      rm -f "$_pip_log"; return 1
    fi
  fi
  rm -f "$_pip_log"
  # Only a --user install needs the PATH help; a venv install is already on it.
  if [[ -n "$_user_flag" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
    if ! grep -qs 'HOME/.local/bin' "$HOME/.bashrc" 2>/dev/null; then
      echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc" 2>/dev/null || true
    fi
  fi
  command -v ansible-playbook >/dev/null 2>&1 || { warn "Ansible still not on PATH."; return 1; }
  ok "Ansible $(ansible --version 2>/dev/null | head -1 | awk '{print $2}') installed"
  return 0
}

deploy_test_workloads_now() {
  [[ "$DRY_RUN" == "true" ]] && { dryrun_say "would deploy Ubuntu + RHEL test workloads tagged ${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}"; return 0; }
  echo
  echo "  There is nothing tagged ${DISCOVERY_DESC} to install a sensor on."
  echo "  Three throwaway EC2s (Ubuntu + RHEL t3.small, Windows t3.medium) can be"
  echo "  created now, tagged so the sensor and mirror steps find them. They cost"
  echo "  about \$0.06/hour together and are yours to terminate afterward."
  ask_yn "  Create them? [y/N]: " "n" || return 1
  local wl_subnet="${EXISTING_SUBNET_ID:-}"
  if [[ -z "$wl_subnet" ]]; then
    wl_subnet=$(probe aws ec2 describe-subnets --region "$REGION" \
      --filters "Name=vpc-id,Values=${STACK_VPC_ID:-}" "Name=map-public-ip-on-launch,Values=true" \
      --query 'Subnets[0].SubnetId' --output text 2>/dev/null | head -1)
  fi
  [[ -z "$wl_subnet" || "$wl_subnet" == "None" ]] && wl_subnet=$(probe aws ec2 describe-subnets \
      --region "$REGION" --filters "Name=map-public-ip-on-launch,Values=true" \
      --query 'Subnets[0].SubnetId' --output text 2>/dev/null | head -1)
  [[ -z "$wl_subnet" || "$wl_subnet" == "None" ]] && { warn "No public subnet found for the workloads."; return 1; }
  local wl_script; wl_script="$(find_repo_script scripts/deploy-test-workload-vms.sh || true)"
  [[ -n "$wl_script" ]] || { warn "Could not locate deploy-test-workload-vms.sh."; return 1; }
  step "Creating test workloads in ${wl_subnet}"
  bash "$wl_script" --region "$REGION" --key-name "$KEY_NAME" --subnet-id "$wl_subnet" \
       --tag-key "$DISCOVERY_TAG_KEY" --tag-value "$DISCOVERY_TAG_VALUE" --yes \
    || { warn "Test workload creation failed."; return 1; }
  TAGGED_COUNT=$(probe aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:${DISCOVERY_TAG_KEY},Values=${DISCOVERY_TAG_VALUE}" \
                "Name=instance-state-name,Values=running,pending" \
      --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo 0)
  ok "Workloads created. Matching instances now: ${TAGGED_COUNT:-0}"
  return 0
}

# ---------------------------------------------------------------------
# Collector security groups. The AWS mirror Cloud Config requires THREE
# DISTINCT security groups (mgmt / ingress / egress), one per collector
# interface: _AwsConfigurationInput marks all three NON_NULL, so the mirror
# script cannot run without them. The KVO User Guide requires 443 and 22
# inbound on the management group. Nothing else created these, so the mirror
# step failed with "missing required security groups". Create them here,
# idempotent by name, and set MGMT_SG_ID / INGRESS_SG_ID / EGRESS_SG_ID.
# ---------------------------------------------------------------------
MGMT_SG_ID=""; INGRESS_SG_ID=""; EGRESS_SG_ID=""
ensure_collector_sgs() {
  [[ "$DRY_RUN" == "true" ]] && { dryrun_say "would create 3 distinct collector SGs (mgmt/ingress/egress)"; MGMT_SG_ID="sg-mgmt"; INGRESS_SG_ID="sg-ingress"; EGRESS_SG_ID="sg-egress"; return 0; }
  [[ -z "${STACK_VPC_ID:-}" ]] && { warn "No stack VPC id; cannot create collector SGs."; return 1; }
  local vpc_cidr
  vpc_cidr=$(probe aws ec2 describe-vpcs --region "$REGION" --vpc-ids "$STACK_VPC_ID" --query 'Vpcs[0].CidrBlock' --output text 2>/dev/null)
  [[ -z "$vpc_cidr" || "$vpc_cidr" == "None" ]] && vpc_cidr="10.0.0.0/8"
  # role: sg-name-suffix; returns the SG id (reused if it already exists)
  _mk_sg() {
    local role="$1" desc="$2" name="cloudlens-collector-${role}-${STACK_NAME}" id
    id=$(probe aws ec2 create-security-group --region "$REGION" --group-name "$name" \
         --description "$desc" --vpc-id "$STACK_VPC_ID" --query 'GroupId' --output text 2>/dev/null) \
      || id=$(probe aws ec2 describe-security-groups --region "$REGION" \
         --filters "Name=group-name,Values=${name}" "Name=vpc-id,Values=${STACK_VPC_ID}" \
         --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
    printf '%s' "$id"
  }
  MGMT_SG_ID="$(_mk_sg mgmt 'CloudLens collector management: KVO 443 + SSH 22')"
  INGRESS_SG_ID="$(_mk_sg ingress 'CloudLens collector ingress: mirrored traffic in')"
  EGRESS_SG_ID="$(_mk_sg egress 'CloudLens collector egress: to the vPB')"
  for id in "$MGMT_SG_ID" "$INGRESS_SG_ID" "$EGRESS_SG_ID"; do
    [[ -z "$id" || "$id" == "None" ]] && { warn "Failed to create a collector SG."; return 1; }
  done
  # mgmt: 443 + 22 (KVO UG). ingress/egress: intra-VPC incl. VXLAN and GRE.
  probe aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$MGMT_SG_ID" \
    --ip-permissions "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=${ADMIN_CIDR:-0.0.0.0/0}}]" \
                     "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${ADMIN_CIDR:-0.0.0.0/0}}]" >/dev/null 2>&1
  for id in "$INGRESS_SG_ID" "$EGRESS_SG_ID"; do
    probe aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$id" \
      --ip-permissions "IpProtocol=-1,IpRanges=[{CidrIp=${vpc_cidr}}]" >/dev/null 2>&1
  done
  ok "Collector SGs: mgmt ${MGMT_SG_ID}, ingress ${INGRESS_SG_ID}, egress ${EGRESS_SG_ID}"
  return 0
}

# ---------------------------------------------------------------------
# Capture host. The vPB egress tool is LOCAL, so the vPB emits raw frames
# onto the egress subnet that AWS will not deliver to any instance. To let
# an operator actually SEE the tapped traffic, we stand up a tiny host on
# the egress subnet running tcpdump, then (in the wire phase) attach a
# REMOTE tool at its IP to the same policy that feeds the vPB egress. Sets
# CAPTURE_HOST_IP on success. Idempotent: reuses an existing one by tag.
# ---------------------------------------------------------------------
# CAPTURE_HOST_IP is the PRIVATE address, used for the REMOTE tool because the
# vPB tunnels to it in-VPC. CAPTURE_HOST_PUBLIC_IP is what the operator SSHes to
# from outside the VPC (a laptop or CloudShell), so the summary shows that one.
CAPTURE_HOST_IP=""
CAPTURE_HOST_PUBLIC_IP=""
ensure_capture_host_now() {
  [[ "$DRY_RUN" == "true" ]] && { dryrun_say "would launch a tcpdump capture host on the egress subnet and set CAPTURE_HOST_IP"; CAPTURE_HOST_IP="10.0.0.250"; return 0; }
  # reuse if one is already running
  local existing_id
  # Scope the reuse to THIS stack's VPC. Without the vpc-id filter the fixed
  # cloudlens-role tag matches account-wide, so in a shared training account one
  # trainee's run would reuse another trainee's capture host.
  existing_id=$(probe aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:cloudlens-role,Values=tool-receiver" "Name=instance-state-name,Values=running,pending" \
                ${STACK_VPC_ID:+"Name=vpc-id,Values=${STACK_VPC_ID}"} \
      --query 'Reservations[].Instances[0].InstanceId' --output text 2>/dev/null | head -1)
  if [[ -n "$existing_id" && "$existing_id" != "None" ]]; then
    CAPTURE_HOST_IP=$(probe aws ec2 describe-instances --region "$REGION" --instance-ids "$existing_id" \
        --query 'Reservations[].Instances[].PrivateIpAddress' --output text 2>/dev/null)
    CAPTURE_HOST_PUBLIC_IP=$(probe aws ec2 describe-instances --region "$REGION" --instance-ids "$existing_id" \
        --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null)
    ok "Reusing capture host ${existing_id} at ${CAPTURE_HOST_IP} (public ${CAPTURE_HOST_PUBLIC_IP:-none})"; return 0
  fi
  local egress_subnet="${EGRESS_SUBNET_ID:-}"
  [[ -z "$egress_subnet" || "$egress_subnet" == "None" ]] && { warn "No egress subnet id known; cannot place a capture host."; return 1; }
  # SG: accept the encapsulated tap (GRE + VXLAN) from anywhere in the VPC
  local vpc_cidr sg
  vpc_cidr=$(probe aws ec2 describe-vpcs --region "$REGION" --vpc-ids "${STACK_VPC_ID}" --query 'Vpcs[0].CidrBlock' --output text 2>/dev/null)
  [[ -z "$vpc_cidr" || "$vpc_cidr" == "None" ]] && vpc_cidr="10.0.0.0/8"
  sg=$(probe aws ec2 create-security-group --region "$REGION" --group-name "cloudlens-tool-receiver-${STACK_NAME}" \
       --description "CloudLens tool receiver: mirrored traffic from vPB egress" --vpc-id "$STACK_VPC_ID" \
       --query 'GroupId' --output text 2>/dev/null) \
    || sg=$(probe aws ec2 describe-security-groups --region "$REGION" \
       --filters "Name=group-name,Values=cloudlens-tool-receiver-${STACK_NAME}" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
  [[ -z "$sg" || "$sg" == "None" ]] && { warn "Could not create the capture host security group."; return 1; }
  probe aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$sg" \
    --ip-permissions "IpProtocol=47,IpRanges=[{CidrIp=${vpc_cidr},Description=L2GRE tap}]" >/dev/null 2>&1
  probe aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$sg" \
    --ip-permissions "IpProtocol=udp,FromPort=4789,ToPort=4789,IpRanges=[{CidrIp=${vpc_cidr}}]" >/dev/null 2>&1
  probe aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$sg" \
    --ip-permissions "IpProtocol=udp,FromPort=10800,ToPort=10801,IpRanges=[{CidrIp=${vpc_cidr}}]" >/dev/null 2>&1
  # SSH from the same admin CIDR the rest of the stack uses, so the operator can log in and watch
  probe aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$sg" \
    --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${ADMIN_CIDR:-0.0.0.0/0}}]" >/dev/null 2>&1
  # Ubuntu 22.04 AMI + tcpdump-on-boot user-data
  local ami
  ami=$(probe aws ssm get-parameter --region "$REGION" \
      --name /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id \
      --query 'Parameter.Value' --output text 2>/dev/null)
  [[ -z "$ami" || "$ami" == "None" ]] && { warn "Could not resolve an Ubuntu AMI for the capture host."; return 1; }
  local ud; ud="$(mktemp)"
  cat > "$ud" <<'UD'
#!/bin/bash
apt-get update -y && apt-get install -y tcpdump
mkdir -p /var/log/cloudlens-tool
cat > /etc/systemd/system/cloudlens-tap.service <<'SVC'
[Unit]
Description=CloudLens tool receiver capture
After=network-online.target
[Service]
ExecStart=/usr/bin/tcpdump -i any -nn -s 0 -U -W 10 -C 100 -w /var/log/cloudlens-tool/tap.pcap proto 47 or udp port 4789 or udp portrange 10800-10801
Restart=always
[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload && systemctl enable --now cloudlens-tap.service
UD
  local prof_arg=()
  [[ -n "${WINDOWS_INSTANCE_PROFILE:-}" ]] && prof_arg=(--iam-instance-profile "Name=${WINDOWS_INSTANCE_PROFILE}")
  step "Launching capture host on ${egress_subnet}"
  local iid
  iid=$(probe aws ec2 run-instances --region "$REGION" --image-id "$ami" --instance-type t3.medium \
      --subnet-id "$egress_subnet" --security-group-ids "$sg" --associate-public-ip-address \
      ${KEY_NAME:+--key-name "$KEY_NAME"} "${prof_arg[@]}" \
      --user-data "file://$ud" \
      --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=cloudlens-tool-receiver},{Key=cloudlens-role,Value=tool-receiver}]' \
      --query 'Instances[0].InstanceId' --output text 2>/dev/null)
  rm -f "$ud"
  [[ -z "$iid" || "$iid" == "None" ]] && { warn "Capture host launch failed."; return 1; }
  # a REMOTE tool delivers frames not addressed to the ENI: turn off src/dst check
  local eni
  probe aws ec2 wait instance-running --region "$REGION" --instance-ids "$iid" 2>/dev/null
  eni=$(probe aws ec2 describe-instances --region "$REGION" --instance-ids "$iid" \
      --query 'Reservations[].Instances[].NetworkInterfaces[0].NetworkInterfaceId' --output text 2>/dev/null)
  [[ -n "$eni" && "$eni" != "None" ]] && probe aws ec2 modify-network-interface-attribute \
      --region "$REGION" --network-interface-id "$eni" --no-source-dest-check >/dev/null 2>&1
  CAPTURE_HOST_IP=$(probe aws ec2 describe-instances --region "$REGION" --instance-ids "$iid" \
      --query 'Reservations[].Instances[].PrivateIpAddress' --output text 2>/dev/null)
  CAPTURE_HOST_PUBLIC_IP=$(probe aws ec2 describe-instances --region "$REGION" --instance-ids "$iid" \
      --query 'Reservations[].Instances[].PublicIpAddress' --output text 2>/dev/null)
  ok "Capture host ${iid} at ${CAPTURE_HOST_IP} (public ${CAPTURE_HOST_PUBLIC_IP:-none}, tcpdump on boot, pcaps in /var/log/cloudlens-tool)"
  return 0
}

# ---------------------------------------------------------------------
# The Windows sensor is a native .exe the CLMS serves at a predictable
# static path, and the directory returns a JSON listing, so the URL can be
# discovered without the operator copying it from the UI. Proven live
# 2026-08-03: the sensor registered on Server 2022 with the URL this finds.
# Sets WINDOWS_INSTALLER_URL. Non-fatal: if discovery fails the playbook
# still asserts with the manual fix.
# ---------------------------------------------------------------------
discover_windows_installer_url() {
  [[ -n "$WINDOWS_INSTALLER_URL" ]] && return 0          # operator supplied one
  [[ -z "${CLMS_PUBLIC_IP:-}" ]] && return 1
  command -v curl >/dev/null 2>&1 || return 1
  local listing exe
  listing="$(curl -sk --max-time 15 \
    "https://${CLMS_PUBLIC_IP}/cloudlens/static/agent-update/windows/latest/" 2>/dev/null)"
  exe="$(printf '%s' "$listing" | python3 -c '
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
for f in (d if isinstance(d, list) else []):
    n = f.get("name", "")
    if n.endswith(".exe"): print(n); break' 2>/dev/null)"
  [[ -z "$exe" ]] && return 1
  # Sensors reach the CLMS in-VPC on its private address; fall back to public.
  local host="${CLMS_PRIVATE_IP:-$CLMS_PUBLIC_IP}"
  WINDOWS_INSTALLER_URL="https://${host}/cloudlens/static/agent-update/windows/latest/${exe}"
  ok "Discovered Windows installer: ${exe}"
  return 0
}

# ---------------------------------------------------------------------
# Windows sensors go over AWS SSM, and that plugin stages every file
# through an S3 bucket. Telling the operator to create one, wire an IAM
# policy, and edit a YAML file is three manual steps mid-run: exactly the
# homework this script exists to remove. Do it here instead.
# Returns 1 when it declines or cannot, and the caller keeps its message.
# ---------------------------------------------------------------------
ensure_ssm_bucket_now() {
  local have=""
  if [[ -f customer_input.yaml ]] && command -v python3 >/dev/null 2>&1; then
    have="$(python3 -c 'import sys,yaml
try: d=yaml.safe_load(open("customer_input.yaml")) or {}
except Exception: sys.exit(0)
print(((d.get("aws") or {}).get("ssm_bucket_name") or "").strip())' 2>/dev/null)"
  fi
  [[ -n "$have" ]] && { SSM_BUCKET_NAME="$have"; return 0; }

  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "would create a private S3 bucket for Windows SSM file transfer and grant the Windows instance role access"
    return 0
  fi

  echo
  echo "  Windows sensors install over AWS SSM, which stages files through an S3"
  echo "  bucket. None is configured, so the Windows host cannot be reached."
  echo "  A private bucket can be created now and wired to the Windows instance"
  echo "  role. Storage cost is a few cents a month."
  ask_yn "  Create it? [Y/n]: " "y" || return 1

  local acct bucket
  acct="$(probe aws sts get-caller-identity --query Account --output text)"
  [[ -z "$acct" || "$acct" == "None" ]] && { warn "Could not read the AWS account id."; return 1; }
  bucket="cloudlens-ssm-transfer-${acct}"

  step "S3 bucket for Windows SSM transfer"
  if probe aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    ok "Bucket ${bucket} already exists"
  else
    local mk=0
    if [[ "$REGION" == "us-east-1" ]]; then
      aws s3api create-bucket --bucket "$bucket" --region "$REGION" >/dev/null 2>&1 || mk=1
    else
      aws s3api create-bucket --bucket "$bucket" --region "$REGION" \
        --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null 2>&1 || mk=1
    fi
    [[ "$mk" -ne 0 ]] && { warn "Could not create ${bucket}."; return 1; }
    aws s3api put-public-access-block --bucket "$bucket" \
      --public-access-block-configuration \
      "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null 2>&1 || true
    ok "Created ${bucket} (private, nothing public)"
  fi

  # Grant the role the Windows hosts actually run under, read from the
  # instances themselves. A BYO Windows host will not be using the role this
  # repo's test-VM script creates, so assuming that name would be wrong.
  local prof_arn role
  prof_arn="$(probe aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:os,Values=windows" "Name=instance-state-name,Values=running" \
      --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn' --output text)"
  if [[ -z "$prof_arn" || "$prof_arn" == "None" ]]; then
    warn "The Windows host has no IAM instance profile, so SSM cannot manage it."
    note "Bucket ${bucket} is ready, but the instance needs a profile granting"
    note "AmazonSSMManagedInstanceCore before Windows is reachable at all."
    return 1
  fi
  role="$(probe aws iam get-instance-profile --instance-profile-name "${prof_arn##*/}" \
        --query 'InstanceProfile.Roles[0].RoleName' --output text)"
  if [[ -n "$role" && "$role" != "None" ]]; then
    local pol="/tmp/ssm-bucket-policy.$$.json"
    printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:ListBucket"],"Resource":["arn:aws:s3:::%s","arn:aws:s3:::%s/*"]}]}' "$bucket" "$bucket" > "$pol"
    if aws iam put-role-policy --role-name "$role" --policy-name SsmTransferBucket \
         --policy-document "file://$pol" >/dev/null 2>&1; then
      ok "Granted ${role} read and write on ${bucket}"
    else
      warn "Could not attach the S3 policy to ${role} (needs iam:PutRolePolicy)."
      rm -f "$pol"; return 1
    fi
    rm -f "$pol"
  fi

  SSM_BUCKET_NAME="$bucket"
  ok "Windows SSM transfer bucket ready: ${bucket}"
  return 0
}


if [[ "$CHAIN_SENSORS" == "true" ]] && [[ "$DRY_RUN" != "true" ]]; then
  discovery_scope
  if [[ "$DISCOVERY_MODE" == "static" ]]; then
    TAGGED_COUNT="?"
    echo "  Discovery source: ${DISCOVERY_DESC} (from customer_input.yaml)."
    echo "  AWS discovery is skipped entirely, so there is nothing to count here."
  else
    TAGGED_COUNT=$(aws "${AWS_REGION_ARG[@]}" ec2 describe-instances \
      --filters "${DISCOVERY_FILTER_ARGS[@]}" "Name=instance-state-name,Values=running" \
      --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo "?")
    case "$DISCOVERY_MODE" in
      tags)         echo "  Discovery (from customer_input.yaml, ALL must match): ${DISCOVERY_DESC}" ;;
      instance-ids) echo "  Discovery (from customer_input.yaml): ${DISCOVERY_DESC}" ;;
      *)            echo "  Discovery: ${DISCOVERY_DESC}"
                    echo "  Approximate: customer_input.yaml is not readable here, so this counts"
                    echo "  the single tag this run was given, not whatever that file ends up with." ;;
    esac
    if [[ "$TAGGED_COUNT" == "0" ]]; then
      echo "  Matching running EC2s: 0"
      echo "  Tag a running instance so the sensor installs on it:"
      echo "    aws ec2 create-tags --resources <instance-id> \\"
      echo "        --tags Key=${DISCOVERY_TAG_KEY},Value=${DISCOVERY_TAG_VALUE} Key=os,Value=ubuntu Key=env,Value=prod"
      echo "  (Tag os = ubuntu | rhel | windows ; env = prod | dev)"
    else
      echo "  Matching running EC2s: ${TAGGED_COUNT}"
      echo "  The sensor chain will install on those ${TAGGED_COUNT} instance(s)."
    fi
  fi
  echo
fi

if [[ "$CHAIN_SENSORS" == "true" ]] && [[ "$DRY_RUN" != "true" ]]; then
  # Pre-flight BEFORE asking for a secret. Two things make the sensor chain a
  # no-op, and both are cheaper to catch here than after the paste:
  #   - Ansible missing: quickstart.sh hard-fails on it.
  #   - Zero tagged instances: the playbook runs against nothing.
  sensor_blocker=""

  if ! command -v ansible-playbook >/dev/null 2>&1; then
    sensor_blocker="ansible"
    warn "Ansible is not installed on this machine, so the sensor step cannot run here."
    # Printing the pip line and stopping made the operator leave the run, install
    # by hand, and start over. It is one command and we are already here, so
    # offer to run it. --user keeps it under $HOME, the only part of a CloudShell
    # environment that survives the session.
    if install_ansible_now; then
      sensor_blocker=""
    elif [[ "$IN_CLOUD_SHELL" == "true" ]]; then
      # AWS CloudShell does NOT ship Ansible: the pre-installed software list in
      # the CloudShell user guide has python3 + pip3 and no Ansible at all. It
      # is one pip away, and --user keeps it under $HOME, which is the only part
      # of a CloudShell environment that survives the session.
      echo "    CloudShell does not ship Ansible. Install it into your persistent home:"
      echo "      pip3 install --user ansible boto3"
      echo "      export PATH=\"\$HOME/.local/bin:\$PATH\"    (add to ~/.bashrc to keep it)"
      echo "    Then re-run:"
      echo "      curl -sSL ${REPO_RAW}/quickstart.sh | bash"
    else
      echo "    Install it:   pip3 install --user ansible boto3"
      echo "    Or run the sensor step later from AWS CloudShell:"
      echo "      pip3 install --user ansible boto3 && export PATH=\"\$HOME/.local/bin:\$PATH\""
      echo "      curl -sSL ${REPO_RAW}/quickstart.sh | bash"
    fi
  fi

  if [[ "${TAGGED_COUNT:-0}" == "0" ]]; then
    [[ -n "$sensor_blocker" ]] && echo
    sensor_blocker="${sensor_blocker:+$sensor_blocker,}notags"
    warn "No running EC2 instances match ${DISCOVERY_DESC}, so there is nothing to install a sensor on."
    # Same reasoning as the Ansible branch: an empty account is the normal case
    # on a first run, and telling someone to go tag something by hand ends the
    # run. Offer to stand up throwaway workloads in the stack's own subnet.
    if deploy_test_workloads_now; then
      sensor_blocker="${sensor_blocker/notags/}"
      sensor_blocker="${sensor_blocker%,}"; sensor_blocker="${sensor_blocker#,}"
    else
      echo "    Tag your workloads first (see the command above), then run:"
      echo "      curl -sSL ${REPO_RAW}/quickstart.sh | bash"
    fi
  fi

  if [[ -n "$sensor_blocker" ]]; then
    echo
    read -rp "Skip the sensor step for now? [Y/n]: " skip_yn || true
    if [[ "$(to_lower "${skip_yn:-y}")" != "n" ]]; then
      warn "Skipping sensor deployment. Infrastructure is deployed and ready."
      # Do not end here without saying how to come back. The stack is built and
      # a re-run resumes in about a minute, but nobody can guess --resume: it
      # was in --help and in no user-facing doc at all.
      echo
      echo "  Nothing is lost. When you are ready, re-run this and it will detect"
      echo "  everything already built, skip it, and pick up at the sensor step:"
      echo
      echo "    curl -sSL ${REPO_RAW}/deploy/deploy-stack.sh | bash -s -- \\"
      echo "      --region ${REGION} --stack-name ${STACK_NAME} --resume"
      echo
      echo "  That takes about a minute, not another full deploy."
      CHAIN_SENSORS=false
    fi
  fi
fi

# =====================================================================
# Phase 10: Sensor mode (which project key the sensors register with)
# =====================================================================
# This is the fork in the road, and it decides the ORDER of everything below:
#   standalone  sensors use the key phase 9 minted straight on the vController
#   kvo         sensors use the key the KVO Cloud Config provisions, so KVO
#               licensing + adoption + cloud config must run FIRST. A sensor
#               installed with the phase 9 key in this mode registers to the
#               wrong project.
step "Phase 10: Sensor mode"

if [[ "$CHAIN_SENSORS" != "true" && "$CHAIN_SENSORS" != "write_yaml_only" ]]; then
  SENSOR_MODE="none"
  note "Sensors are switched off for this run."
elif [[ -z "$SENSOR_MODE" ]]; then
  if [[ "$DEPLOY_KVO" != "true" ]]; then
    # No KVO in this stack, so there is no second option to offer.
    SENSOR_MODE="standalone"
    note "No KVO in this stack, so standalone is the only sensor mode available."
  else
    echo
    echo "  Which management plane owns the sensors?"
    echo "    1) standalone   register straight to the vController project"
    echo "    2) KVO-managed  register to the project KVO provisions, so KVO is"
    echo "                    the single pane of glass. Runs KVO licensing and"
    echo "                    adoption BEFORE the sensors."
    echo
    case "$(ask "  Choose 1-2 [1]: " 1)" in
      2) SENSOR_MODE="kvo" ;;
      *) SENSOR_MODE="standalone" ;;
    esac
  fi
fi

# --sensor-mode kvo without a KVO cannot work: say so instead of failing later.
if [[ "$SENSOR_MODE" == "kvo" && "$DEPLOY_KVO" != "true" ]]; then
  warn "--sensor-mode kvo needs a KVO, and this stack was deployed without one."
  note "Falling back to standalone (the vController project key)."
  SENSOR_MODE="standalone"
fi
ok "Sensor mode: ${SENSOR_MODE}"

# =====================================================================
# Phase 11: KVO product licensing
# =====================================================================
# An unlicensed KVO refuses every write, so this gates adoption. scripts/
# kvo_license.py owns the prompting: with no --codes and a terminal it asks for
# the code and the per-entitlement quantity itself, so it is run with its stdin
# inherited and nothing is re-implemented here. With no codes and no terminal it
# exits 2, which is a clean, honest stop.
#
# This runs for any stack that has a KVO, not only in KVO-managed sensor mode:
# an unlicensed KVO also cannot adopt the vPB in phase 14.
if [[ "$DEPLOY_KVO" == "true" ]]; then
  step "Phase 11: KVO product licensing"

  # KVO is reachable by now, and its UI is where the licence, the adopted
  # vController and the Visibility Fabric show up as the next phases build them.
  announce_kvo_login

  LIC_SCRIPT="$(find_repo_script scripts/kvo_license.py || true)"

  # Activation codes are consumable: re-activating one that is already spent
  # burns entitlement quantity. An already-licensed KVO is therefore left alone.
  if ! run_phase license; then
    skip_note "KVO licensing"
    ok "No activation code is re-used, so no entitlement quantity is spent."
  elif [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "python3 scripts/kvo_license.py --kvo ${KVO_PUBLIC_IP} --insecure --accept-eula ${KVO_CODES[@]+--codes ${KVO_CODES[*]}}"
    [[ ${#KVO_CODES[@]} -eq 0 ]] && dryrun_say "no --kvo-codes given: on a terminal kvo_license.py would prompt for code + quantity"
  elif [[ -z "$LIC_SCRIPT" ]]; then
    warn "scripts/kvo_license.py not found; cannot license KVO."
    KVO_CHAIN_OK=false
  elif ! python_chain_ready; then
    warn "python3 is not usable here; cannot license KVO."
    KVO_CHAIN_OK=false
  elif [[ -z "$KVO_PUBLIC_IP" || "$KVO_PUBLIC_IP" == "None" ]]; then
    warn "No KVO address available; cannot license KVO."
    KVO_CHAIN_OK=false
  elif [[ ${#KVO_CODES[@]} -eq 0 && "$INTERACTIVE" != "true" ]]; then
    warn "No --kvo-codes given and no terminal to prompt on, so KVO stays unlicensed."
    KVO_CHAIN_OK=false
  else
    note "This accepts the KVO EULA on your behalf: a fresh KVO blocks all API"
    note "access, including login, until it is signed."
    note "Activating KVO licenses (kvo_license.py prompts for anything it needs)..."
    # --accept-eula is not optional here. A freshly booted KVO 302-redirects
    # EVERY request until its EULA is signed, including the Keycloak token
    # endpoint, so licensing cannot even authenticate. Licensing runs before
    # adoption, and adoption was the only step passing this, which meant a
    # fresh KVO could never be licensed however valid the code was. Seen live:
    # "auth failed: Expecting value: line 1 column 1" on an HTML redirect body.
    # Phase 12 already tells the operator this deploy accepts the KVO EULA.
    if python3 "$LIC_SCRIPT" --kvo "$KVO_PUBLIC_IP" --insecure --accept-eula \
         ${KVO_CODES[@]+--codes "${KVO_CODES[@]}"}; then
      ok "KVO licensing completed."
    else
      warn "KVO licensing did not complete."
      KVO_CHAIN_OK=false
    fi
  fi

  if [[ "$KVO_CHAIN_OK" != "true" ]]; then
    warn "Licensing gates every KVO write, so the KVO branch stops here."
    note "Nothing is adopted and no KVO project key exists."
    [[ "$SENSOR_MODE" == "kvo" ]] && \
      note "KVO-managed sensors therefore have no project key to register with."
    note "Fix licensing and re-run with:"
    note "  bash deploy/deploy-stack.sh --sensor-mode ${SENSOR_MODE} --kvo-codes CODE[,QTY]"
    note "The re-run resumes: everything already done above is detected and skipped."
    state_phase license failed "KVO licensing did not complete"
  else
    state_phase license done
  fi
fi

# =====================================================================
# Phase 12: Adopt the vController into KVO + create the Cloud Config
# =====================================================================
# The Cloud Config step is the one that matters: it provisions the real CLM
# project and returns the sensor key on stdout (the script's own log goes to
# stderr). Skipping it leaves a phantom key and an empty Cloud Configs page.
if [[ "$DEPLOY_KVO" == "true" && "$KVO_CHAIN_OK" == "true" ]]; then
  step "Phase 12: Adopt the vController into KVO + Cloud Config"
  ADOPT_SCRIPT="$(find_repo_script scripts/kvo_adopt_clms.py || true)"

  # The vController password is whatever phase 9 set. Read it back rather than
  # assuming the factory default, which phase 9 deliberately invalidates.
  VC_ADMIN_PASS="${CLOUDLENS_VC_PASSWORD:-}"
  if [[ -z "$VC_ADMIN_PASS" && -f "$VC_CREDS_FILE" ]]; then
    VC_ADMIN_PASS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("password",""))' \
                      "$VC_CREDS_FILE" 2>/dev/null || echo "")
  fi
  VC_ADMIN_PASS="${VC_ADMIN_PASS:-Cl0udLens@dm!n}"

  # The notice is only printed on the paths that are actually going to adopt.
  adopt_notice() {
    echo
    echo "  Adopting ${CLM_NAME_IN_KVO} (${CLMS_PUBLIC_IP}) into KVO and creating"
    echo "  Cloud Config '${CLOUD_CONFIG_NAME}'. This accepts the KVO EULA on your"
    echo "  behalf, which is a legal acceptance."
  }

  # Re-adopting an already-adopted manager errors, and the Cloud Config is
  # reusable by name, so a resume reads the existing key back instead.
  if ! run_phase adopt; then
    skip_note "the KVO adoption and Cloud Config"
    kvo_auth "$KVO_PUBLIC_IP" || true
    if KVO_PROJECT_KEY="$(kvo_cloud_config_key "$KVO_PUBLIC_IP")"; then
      ok "Reusing the project key from Cloud Config '${CLOUD_CONFIG_NAME}' (${#KVO_PROJECT_KEY} characters)."
    else
      KVO_PROJECT_KEY=""
      warn "Could not read the project key back from Cloud Config '${CLOUD_CONFIG_NAME}'."
      note "Re-run with --only adopt if KVO-managed sensors need it."
    fi
  elif [[ "$DRY_RUN" == "true" ]]; then
    adopt_notice
    dryrun_say "python3 scripts/kvo_adopt_clms.py --clms ${CLMS_PUBLIC_IP} --clms-admin-pass <hidden> --kvo ${KVO_PUBLIC_IP} --name ${CLM_NAME_IN_KVO} --cloud-config ${CLOUD_CONFIG_NAME} --accept-eula --insecure"
    if [[ "$SENSOR_MODE" == "kvo" ]]; then
      dryrun_say "would capture the provisioned project key from its stdout and use it for the sensors"
    else
      dryrun_say "would capture the provisioned project key from its stdout (unused in ${SENSOR_MODE} mode)"
    fi
  elif { adopt_notice; ! ask_yn "  Adopt now? [Y/n]: " y; }; then
    warn "Adoption skipped."
    [[ "$SENSOR_MODE" == "kvo" ]] && \
      note "KVO-managed sensors have no project key without it."
    KVO_CHAIN_OK=false
  elif [[ -z "$ADOPT_SCRIPT" ]]; then
    warn "scripts/kvo_adopt_clms.py not found; skipping adoption."
    KVO_CHAIN_OK=false
  elif ! python_chain_ready; then
    warn "python3 is not usable here; skipping adoption."
    KVO_CHAIN_OK=false
  else
    # stdout is the project key ONLY. Capture it; never echo it.
    KVO_PROJECT_KEY=$(python3 "$ADOPT_SCRIPT" \
        --clms "$CLMS_PUBLIC_IP" --clms-admin-pass "$VC_ADMIN_PASS" \
        --kvo "$KVO_PUBLIC_IP" --name "$CLM_NAME_IN_KVO" \
        --cloud-config "$CLOUD_CONFIG_NAME" --accept-eula --insecure) || KVO_PROJECT_KEY=""
    if [[ -n "$KVO_PROJECT_KEY" ]]; then
      ok "vController adopted into KVO; Cloud Config '${CLOUD_CONFIG_NAME}' provisioned its project key."
    else
      warn "Adoption or Cloud Config creation did not return a project key."
      KVO_CHAIN_OK=false
    fi
  fi
  if [[ "$KVO_CHAIN_OK" == "true" ]]; then
    state_phase adopt done
  else
    state_phase adopt failed "adoption or Cloud Config did not complete"
  fi
fi

# ---------------------------------------------------------------------
# Which key do the sensors actually use? Phase 10 already decided; this only
# resolves it, and falls back to a paste when the chosen source produced none.
# ---------------------------------------------------------------------
if [[ "$CHAIN_SENSORS" == "true" || "$CHAIN_SENSORS" == "write_yaml_only" ]]; then
  if [[ "$SENSOR_MODE" == "kvo" ]]; then
    SENSOR_PROJECT_KEY="$KVO_PROJECT_KEY"
    KEY_SOURCE="the KVO Cloud Config '${CLOUD_CONFIG_NAME}'"
  else
    SENSOR_PROJECT_KEY="$VC_PROJECT_KEY"
    KEY_SOURCE="the vController project"
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "sensors would register with the key from ${KEY_SOURCE}"
  elif [[ -n "$SENSOR_PROJECT_KEY" ]]; then
    ok "Sensors will register with the key from ${KEY_SOURCE} (${#SENSOR_PROJECT_KEY} characters)."
  else
    echo
    echo "No key came from ${KEY_SOURCE}."
    echo "The project key is a secret. It is written to customer_input.yaml"
    echo "(git-ignored, permissions 600) so Ansible can read it."
    if [[ "$INTERACTIVE" == "true" ]]; then
      # -s: do not echo the secret to the terminal or the log.
      read -rsp "Paste project key (or press Enter to skip sensor deployment): " SENSOR_PROJECT_KEY || true
      echo
    fi
    if [[ -z "$SENSOR_PROJECT_KEY" ]]; then
      warn "No project key supplied. Skipping sensor chain."
      CHAIN_SENSORS=false
    else
      ok "Project key received (${#SENSOR_PROJECT_KEY} characters)."
    fi
  fi
fi

# ---------------------------------------------------------------------
# customer_input.yaml: this script owns two keys in it, and the operator owns
# the rest.
#
# Since scripts/render_inventory.py landed, this file is the source of truth
# for host discovery: aws.tag_filters can hold SEVERAL tags, aws.instance_ids
# names exact hosts, aws.inventory_file skips AWS discovery altogether, and the
# ssh_user_* values are per site. Rewriting the whole file every run threw all
# of that away, including on a resumed run, which is the one run most likely to
# happen after the operator edited it.
#
# So: no file, write one. A file that is there, update ONLY
# cloudlens.manager_ip_or_fqdn and cloudlens.project_key and leave everything
# else exactly as found. A file that cannot be parsed is backed up with a
# timestamp and said so loudly: it is never silently discarded.
#
# The merge is done in python with PyYAML (already required by
# render_inventory.py) rather than with sed: hand-rolled YAML edits are how the
# discovery settings got lost in the first place.
# ---------------------------------------------------------------------
write_customer_input_fresh() {
  # Create the file with tight permissions BEFORE writing the project key into
  # it, so the secret is never briefly world-readable.
  ( umask 077; : > customer_input.yaml )
  chmod 600 customer_input.yaml 2>/dev/null || true
  cat > customer_input.yaml <<YAML
# Auto-generated by deploy-stack.sh on $(date -u +%FT%TZ)
# This file is yours to edit. A later run of deploy-stack.sh updates only
# cloudlens.manager_ip_or_fqdn and cloudlens.project_key and keeps everything
# else, so discovery settings you add here survive.
#
# Discovery keys that matter (see scripts/render_inventory.py):
#   aws.tag_filters     one or more tags, ALL of which must match
#   aws.instance_ids    exactly these instances, ignoring tags
#   aws.inventory_file  an inventory you already have; skips AWS discovery
#   aws.regions         which regions to scan

aws:
  profile: "${AWS_PROFILE:-default}"
  regions:
    - ${REGION}
  tag_filters:
    ${DISCOVERY_TAG_KEY}: "${DISCOVERY_TAG_VALUE}"
  windows_connection: "ssm"
  linux_connection:   "ssh"
  ${SSH_KEY_LINE}
  ssh_user_ubuntu: "ubuntu"
  ssh_user_rhel:   "ec2-user"

cloudlens:
  manager_ip_or_fqdn: "${CLMS_PUBLIC_IP}"
  project_key:        "${SENSOR_PROJECT_KEY}"
  custom_tags:        "DeployedBy=stack Region=${REGION}"
  registry_type:      "insecure"
  linux_runtime:      "auto"

# Windows sensors only. The Windows sensor is a native .exe, not a container.
# Set installer_url to the download link from the CloudLens Manager
# (Launch Agent -> Start New Agents -> the Windows agents link) and the host
# pulls it directly. Leave it blank if you are not installing Windows sensors,
# or set installer_path instead to a .exe you have already downloaded.
# Windows is also covered agentlessly by AWS VPC Traffic Mirroring, so the
# sensor is additive, not required.
windows:
  installer_url: "${WINDOWS_INSTALLER_URL}"

vpc_ids:    []
subnet_ids: []
YAML
  chmod 600 customer_input.yaml 2>/dev/null || true
}

# Update the two owned keys in an existing file.
#   0  merged
#   3  python3 or PyYAML unavailable
#   4  the file does not parse as a YAML mapping
# The project key is passed in the environment and never printed.
merge_customer_input() {
  command -v python3 >/dev/null 2>&1 || return 3
  CL_ADDR="$CLMS_PUBLIC_IP" CL_KEY="$SENSOR_PROJECT_KEY" CL_STAMP="$(date -u +%FT%TZ)" \
  CL_TAG_K="$DISCOVERY_TAG_KEY" CL_TAG_V="$DISCOVERY_TAG_VALUE" \
  CL_TAG_EXPLICIT="${DISCOVERY_TAG_EXPLICIT:-false}" CL_SSM_BUCKET="${SSM_BUCKET_NAME:-}" \
    python3 - customer_input.yaml <<'PY'
import os, sys
try:
    import yaml
except ImportError:
    sys.exit(3)

path = sys.argv[1]
try:
    with open(path) as fh:
        doc = yaml.safe_load(fh)
except Exception:
    sys.exit(4)
if doc is None:
    doc = {}
if not isinstance(doc, dict):
    sys.exit(4)

cl = doc.get("cloudlens")
if not isinstance(cl, dict):
    cl = {}
addr = os.environ.get("CL_ADDR", "")
key = os.environ.get("CL_KEY", "")
if addr:
    cl["manager_ip_or_fqdn"] = addr
if key:
    cl["project_key"] = key
# Only filled when absent: an operator who set them keeps their choice.
cl.setdefault("registry_type", "insecure")
cl.setdefault("linux_runtime", "auto")
doc["cloudlens"] = cl

# --discovery-tag-key/value typed on THIS command line replaces the tag
# filters, even though the file is otherwise the operator's to keep. Leaving
# a stale file to win made the flags do nothing at all, silently: a run asking
# for monitoring=enabled searched for cloudlens=yes and found no hosts.
# The SSM bucket the Windows play needs. Written here so the operator never
# has to hand-edit this file between phases.
_ssm = os.environ.get("CL_SSM_BUCKET", "")
if _ssm:
    aws_blk = doc.get("aws")
    if not isinstance(aws_blk, dict):
        aws_blk = {}
    aws_blk["ssm_bucket_name"] = _ssm
    doc["aws"] = aws_blk

if os.environ.get("CL_TAG_EXPLICIT") == "true":
    tk = os.environ.get("CL_TAG_K", "")
    tv = os.environ.get("CL_TAG_V", "")
    if tk:
        aws_blk = doc.get("aws")
        if not isinstance(aws_blk, dict):
            aws_blk = {}
        aws_blk["tag_filters"] = {tk: tv}
        doc["aws"] = aws_blk

tmp = path + ".tmp"
old_umask = os.umask(0o077)
try:
    with open(tmp, "w") as fh:
        fh.write(
            "# Updated by deploy-stack.sh on %s\n"
            "# Only cloudlens.manager_ip_or_fqdn and cloudlens.project_key were\n"
            "# rewritten. Everything under aws: is yours and was kept as found.\n"
            "# Comments elsewhere in the file are not carried through this update.\n"
            % os.environ.get("CL_STAMP", "")
        )
        yaml.safe_dump(doc, fh, default_flow_style=False, sort_keys=False, width=10000)
finally:
    os.umask(old_umask)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

# Back up a file we are about to replace, with a timestamp, mode 600.
backup_customer_input() {
  local bak="customer_input.yaml.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  if cp customer_input.yaml "$bak" 2>/dev/null; then
    chmod 600 "$bak" 2>/dev/null || true
    printf '%s' "$bak"
    return 0
  fi
  return 1
}

# =====================================================================
# Phase 13: Sensor chain (optional)
# =====================================================================
# The sensor install is idempotent enough to re-run, so this is the one phase
# that is offered rather than hard-skipped (see resume_ask_ambiguous).
SENSOR_SKIP_REASON=""
if [[ "$CHAIN_SENSORS" == "true" || "$CHAIN_SENSORS" == "write_yaml_only" ]] \
   && ! run_phase sensors; then
  SENSOR_SKIP_REASON="$PHASE_SKIP_REASON"
  CHAIN_SENSORS=false
fi

if [[ "$CHAIN_SENSORS" == "true" ]] && [[ "$IS_NATIVE_WINDOWS" == "true" ]]; then
  warn "Sensor chain disabled: Ansible cannot run on Windows shells."
  warn "Run sensors from AWS CloudShell, WSL, or a Linux EC2 with:"
  warn "  curl -sSL ${REPO_RAW}/quickstart.sh | bash"
  CHAIN_SENSORS=write_yaml_only
fi

if [[ "$CHAIN_SENSORS" == "true" || "$CHAIN_SENSORS" == "write_yaml_only" ]]; then
  step "Phase 13: Chain into sensor deployment"

  if [[ "$DRY_RUN" == "true" ]]; then
    if [[ -f customer_input.yaml ]]; then
      dryrun_say "customer_input.yaml exists: would keep your discovery settings and update only the vController address and the project key"
    else
      dryrun_say "would write a new customer_input.yaml (mode 600)"
    fi
    dryrun_say "would run bash quickstart.sh"
  else
    # Linux SSH auth: prefer the key pair PEM we know about.
    if [[ -f "$KEY_PEM" ]]; then
      SSH_KEY_LINE="ssh_key_path:    \"${KEY_PEM}\""
    else
      SSH_KEY_LINE="ssh_key_path:    \"~/.ssh/${KEY_NAME}.pem\""
    fi

    # Windows sensors need the installer .exe. Auto-discover its URL from the
    # CLMS so the operator does not have to copy it from the UI. Best effort:
    # if it fails the playbook still asserts with the manual fix.
    if [[ "$TEST_WINDOWS" == "yes" ]]; then
      discover_windows_installer_url || true
    fi

    if [[ ! -f customer_input.yaml ]]; then
      write_customer_input_fresh
      ok "Wrote a new customer_input.yaml (mode 600, contains the ${SENSOR_MODE} project key)"
    else
      merge_rc=0
      merge_customer_input || merge_rc=$?
      case "$merge_rc" in
        0)
          chmod 600 customer_input.yaml 2>/dev/null || true
          if [[ "${DISCOVERY_TAG_EXPLICIT:-false}" == "true" ]]; then
            ok "Updated customer_input.yaml: discovery set to ${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE} from the command line, everything else kept."
          else
            ok "Kept your existing discovery settings in customer_input.yaml."
          fi
          note "Only the vController address and the ${SENSOR_MODE} project key were updated."
          ;;
        3)
          CI_BAK="$(backup_customer_input || true)"
          warn "python3 with PyYAML is not available here, so customer_input.yaml cannot be merged."
          if [[ -n "$CI_BAK" ]]; then
            warn "YOUR FILE WAS NOT DISCARDED: it is saved as ${CI_BAK} (mode 600)."
            warn "Copy any aws: discovery settings from it back into the new file."
          else
            warn "The existing file could not even be backed up; leaving it untouched."
          fi
          if [[ -n "$CI_BAK" ]]; then
            write_customer_input_fresh
            ok "Wrote a fresh customer_input.yaml (mode 600, contains the ${SENSOR_MODE} project key)"
          fi
          ;;
        *)
          CI_BAK="$(backup_customer_input || true)"
          warn "customer_input.yaml does not parse as YAML, so it could not be merged."
          if [[ -n "$CI_BAK" ]]; then
            warn "YOUR FILE WAS NOT DISCARDED: it is saved as ${CI_BAK} (mode 600)."
            warn "Copy any aws: discovery settings from it back into the new file."
            write_customer_input_fresh
            ok "Wrote a fresh customer_input.yaml (mode 600, contains the ${SENSOR_MODE} project key)"
          else
            warn "The existing file could not be backed up either, so it is left exactly as it is."
            warn "Fix the YAML by hand, then re-run with --only sensors."
          fi
          ;;
      esac
    fi

    # Locate quickstart.sh (current dir, repo dir, or clone).
    QS_DIR=""
    if [[ -f quickstart.sh ]]; then
      QS_DIR="$PWD"
    elif [[ -f "$REPO_DIR/quickstart.sh" ]]; then
      QS_DIR="$REPO_DIR"
      ( cd "$REPO_DIR" && git pull -q origin main 2>/dev/null ) || true
    else
      note "Cloning repo to $REPO_DIR to run quickstart.sh"
      git clone -q "https://github.com/${REPO_OWNER}/${REPO_NAME}.git" "$REPO_DIR" 2>/dev/null \
        && QS_DIR="$REPO_DIR" || warn "Could not clone the repo."
    fi

    if [[ "$CHAIN_SENSORS" == "write_yaml_only" ]]; then
      ok "Saved customer_input.yaml for later use from CloudShell or WSL."
      QS_DIR=""
    fi

    if [[ -n "$QS_DIR" ]]; then
      # cp creates the copy under the current umask, and it holds the project
      # key, so the copy is tightened explicitly rather than by side effect.
      cp customer_input.yaml "$QS_DIR/customer_input.yaml" 2>/dev/null || true
      chmod 600 "$QS_DIR/customer_input.yaml" 2>/dev/null || true
      chmod +x "$QS_DIR/quickstart.sh" 2>/dev/null || true
      # Windows needs the SSM transfer bucket before the playbook runs, or its
      # play asserts and takes the whole run's exit code with it even when
      # Linux succeeded.
      if probe aws ec2 describe-instances --region "$REGION" \
           --filters "Name=tag:os,Values=windows" "Name=instance-state-name,Values=running" \
           --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null | grep -q '^i-'; then
        ensure_ssm_bucket_now || true
      fi
      ok "Launching quickstart.sh from $QS_DIR"
      if ( cd "$QS_DIR" && bash quickstart.sh ); then
        state_phase sensors done
      else
        warn "quickstart.sh exited non-zero (see $QS_DIR for logs)"
        # By far the most common cause is the SSH private key not being here:
        # the sensor install connects to each workload over SSH.
        [[ ! -f "$KEY_PEM" ]] && { warn "The likely cause: the SSH key is not on this machine."; pem_help; }
      fi
    fi
  fi
  state_phase sensors done
else
  step "Phase 13: Sensor chain (skipped)"
  if [[ -n "$SENSOR_SKIP_REASON" ]]; then
    note "Skipping the sensor install: ${SENSOR_SKIP_REASON}."
    note "Install them later with: curl -sSL ${REPO_RAW}/quickstart.sh | bash"
    note "Or force it now with:    bash deploy/deploy-stack.sh --only sensors"
    state_phase sensors skipped "$SENSOR_SKIP_REASON"
  fi
fi

# =====================================================================
# Phase 14: Adopt the vPB into KVO
# =====================================================================
# scripts/vpb_kvo_adopt.py accepts the vPB EULA, turns KVO on over SSH (the vPB
# points at the KVO PRIVATE ip for licensing and management) and adopts the
# device with autoBind, so KVO creates the Device Config and licenses it.
if [[ "$DEPLOY_VPB" == "true" && "$DEPLOY_KVO" == "true" ]]; then
  step "Phase 14: Adopt the vPB into KVO"

  # A device already in KVO must not be adopted twice.
  if ! run_phase vpb; then
    skip_note "the vPB adoption"
    ADOPT_VPB=false
  fi

  if [[ -z "$ADOPT_VPB" ]]; then
    if ask_yn "Adopt the vPB into KVO now (accepts the vPB EULA)? [Y/n]: " y; then
      ADOPT_VPB=true
    else
      ADOPT_VPB=false
    fi
  fi
  ok "Adopt vPB: ${ADOPT_VPB}"

  VPB_ADOPT_SCRIPT="$(find_repo_script scripts/vpb_kvo_adopt.py || true)"
  if [[ "$ADOPT_VPB" != "true" ]]; then
    note "Skipped. Run it later with scripts/vpb_kvo_adopt.py."
  elif [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "python3 scripts/vpb_kvo_adopt.py --vpb ${VPB_PUBLIC_IP} --vpb-port ${VPB_SSH_PORT} --vpb-user ${ADMIN_USERNAME} --key ${KEY_PEM} --kvo ${KVO_PUBLIC_IP} --kvo-internal-ip ${KVO_PRIVATE_IP} --vpb-mgmt-ip ${VPB_PRIVATE_IP} --device-name ${VPB_DEVICE_NAME} --wait-cli --accept-eula --insecure"
  elif [[ "$KVO_CHAIN_OK" != "true" ]]; then
    warn "KVO is not licensed/adopted, so the vPB adoption would fail. Skipping."
    ADOPT_VPB=false
  elif [[ -z "$VPB_ADOPT_SCRIPT" ]] || ! python_chain_ready; then
    warn "scripts/vpb_kvo_adopt.py or python3 is unavailable; skipping vPB adoption."
    ADOPT_VPB=false
  elif [[ ! -f "$KEY_PEM" ]]; then
    warn "Private key ${KEY_PEM} not found; the vPB adoption needs it for SSH. Skipping."
    pem_help
    ADOPT_VPB=false
  elif [[ -z "$KVO_PRIVATE_IP" ]]; then
    warn "Could not resolve the KVO private IP, which the vPB needs for licensing. Skipping."
    ADOPT_VPB=false
  else
    if python3 "$VPB_ADOPT_SCRIPT" \
         --vpb "$VPB_PUBLIC_IP" --vpb-port "$VPB_SSH_PORT" --vpb-user "$ADMIN_USERNAME" \
         --key "$KEY_PEM" --kvo "$KVO_PUBLIC_IP" --kvo-internal-ip "$KVO_PRIVATE_IP" \
         ${VPB_PRIVATE_IP:+--vpb-mgmt-ip "$VPB_PRIVATE_IP"} \
         --device-name "$VPB_DEVICE_NAME" --wait-cli --accept-eula --insecure; then
      ok "vPB adopted into KVO as '${VPB_DEVICE_NAME}'."
      VPB_IN_KVO=true
      state_phase vpb done
    else
      warn "vPB adoption did not complete; the rest of the run continues."
      ADOPT_VPB=false
      state_phase vpb failed "vpb_kvo_adopt.py exited non-zero"
    fi
  fi
fi

# =====================================================================
# Phase 15: AWS mirror session (agentless VPC Traffic Mirroring)
# =====================================================================
# Default NO, and the prompt says why BEFORE the customer opts in: KVO creates
# the mirror target and filter and launches the collector SVM, but never adopts
# the collector, so zero mirror sessions are created. That is an open Keysight
# product item, not something this script can work around.
if [[ "$DEPLOY_KVO" == "true" ]]; then
  step "Phase 15: AWS mirror session (optional)"

  if ! run_phase mirror; then
    skip_note "the AWS mirror step"
    note "Found in ${REGION}: ${DET_MIRROR}"
    WITH_MIRROR=false
  fi

  echo
  echo "  This builds the agentless AWS mirror fabric end to end: the AWS presence,"
  echo "  the Cloud Config, the collection, the collector Service VM (the mirror"
  echo "  target), the tool, the monitoring policy, and the traffic mirror sessions"
  echo "  themselves. Proven live: tapped workload traffic reaches the tool,"
  echo "  verified packet by packet. Windows is tapped agentlessly here too, with"
  echo "  no sensor. The vPB traffic path is wired next if the vPB is adopted."
  if [[ -z "$WITH_MIRROR" ]]; then
    if ask_yn "  Set up the AWS mirror fabric? [y/N]: " n; then
      WITH_MIRROR=true
    else
      WITH_MIRROR=false
    fi
  fi
  ok "AWS mirror session: ${WITH_MIRROR}"

  MIRROR_SCRIPT="$(find_repo_script scripts/kvo_aws_mirror.py || true)"
  if [[ "$WITH_MIRROR" != "true" ]]; then
    note "Skipped. Run scripts/kvo_aws_mirror.py later if you want the fabric anyway."
  elif [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "python3 scripts/kvo_aws_mirror.py --kvo ${KVO_PUBLIC_IP} --clm-name ${CLM_NAME_IN_KVO} --region ${REGION} --vpc-id ${STACK_VPC_ID} --source-tag ${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE} --ssh-key ${KEY_NAME} --cloudlens-ip ${CLMS_PRIVATE_IP} --zone ${STACK_ZONE} --mgmt-subnet ${MGMT_SUBNET_ID} --ingress-subnet ${INGRESS_SUBNET_ID} --egress-subnet ${EGRESS_SUBNET_ID} --tool-remote-ip ${VPB_INGRESS_IP} --tool-encap L2GRE --aws-access-key <hidden> --aws-secret-key <hidden> --accept-eula --insecure"
  elif [[ "$KVO_CHAIN_OK" != "true" ]]; then
    warn "KVO is not licensed/adopted, so the mirror fabric would fail. Skipping."
  elif [[ -z "$MIRROR_SCRIPT" ]] || ! python_chain_ready; then
    warn "scripts/kvo_aws_mirror.py or python3 is unavailable; skipping the mirror step."
  else
    # KVO's own instance role is NOT enough here: createAwsPresence fails with
    # "accessKeyId cannot be empty", so real keys have to be handed over.
    if [[ -z "$MIRROR_ACCESS_KEY" && "$INTERACTIVE" == "true" ]]; then
      echo "  KVO needs an AWS access key of its own for this (an instance role is"
      echo "  not enough: createAwsPresence fails with 'accessKeyId cannot be empty')."
      MIRROR_ACCESS_KEY="$(ask "  AWS access key id: " "")"
    fi
    if [[ -z "$MIRROR_SECRET_KEY" && "$INTERACTIVE" == "true" && -n "$MIRROR_ACCESS_KEY" ]]; then
      # -s: the secret must not reach the terminal or the log.
      read -rsp "  AWS secret access key: " MIRROR_SECRET_KEY || true
      echo
    fi

    # KVO requires three DISTINCT security groups for the collector interfaces.
    # Create them here (idempotent), or the mirror script stops with
    # "missing required security groups".
    if [[ -z "$MGMT_SG_ID" || -z "$INGRESS_SG_ID" || -z "$EGRESS_SG_ID" ]]; then
      ensure_collector_sgs || warn "Could not create the collector SGs; the mirror step will report them missing."
    fi
    if [[ -z "$MIRROR_ACCESS_KEY" || -z "$MIRROR_SECRET_KEY" ]]; then
      warn "No AWS access key / secret key supplied; skipping the mirror step."
      note "Supply them with --mirror-access-key / --mirror-secret-key."
    elif python3 "$MIRROR_SCRIPT" --kvo "$KVO_PUBLIC_IP" --clm-name "$CLM_NAME_IN_KVO" \
           --region "$REGION" --vpc-id "$STACK_VPC_ID" \
           --source-tag "${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}" \
           --ssh-key "$KEY_NAME" --cloudlens-ip "$CLMS_PRIVATE_IP" \
           ${STACK_ZONE:+--zone "$STACK_ZONE"} \
           ${MGMT_SUBNET_ID:+--mgmt-subnet "$MGMT_SUBNET_ID"} \
           ${INGRESS_SUBNET_ID:+--ingress-subnet "$INGRESS_SUBNET_ID"} \
           ${EGRESS_SUBNET_ID:+--egress-subnet "$EGRESS_SUBNET_ID"} \
           ${MGMT_SG_ID:+--mgmt-sg "$MGMT_SG_ID"} \
           ${INGRESS_SG_ID:+--ingress-sg "$INGRESS_SG_ID"} \
           ${EGRESS_SG_ID:+--egress-sg "$EGRESS_SG_ID"} \
           ${VPB_INGRESS_IP:+--tool-remote-ip "$VPB_INGRESS_IP"} \
           --tool-encap L2GRE \
           --aws-access-key "$MIRROR_ACCESS_KEY" --aws-secret-key "$MIRROR_SECRET_KEY" \
           --accept-eula --insecure; then
      state_phase mirror done
      ok "AWS mirror fabric committed in KVO; traffic mirror sessions created."
      note "Verify: aws ec2 describe-traffic-mirror-sessions --region ${REGION}"
    else
      warn "The AWS mirror step did not complete; the rest of the run continues."
    fi
  fi
fi

# =====================================================================
# Phase 16: vPB traffic path + monitoring policy
#
# MUST run after the mirror phase. Step 6 (updateCloudConfig deviceLinks)
# writes to awsConfiguration, which exists only on an AWS-type cloud config,
# and that config is created by the mirror phase. Run this first and step 6
# has nothing to attach the C2DL to.
# =====================================================================
# HONEST WARNING, do not soften it: this cannot complete on a fresh vPB.
# deviceConfig._portGroups stays EMPTY until eth1/eth2 are brought up as DPDK
# data ports ON the vPB, and that bring-up is not automated anywhere in this
# repo. With no ports there is nothing for the bind stage to bind, so the step
# is offered and then reported truthfully rather than claimed as a success.
# VPB_IN_KVO covers the resume case: the vPB was adopted by an earlier run, so
# phase 14 skipped, but the traffic path is still worth offering.
if [[ "$DEPLOY_VPB" == "true" && "$DEPLOY_KVO" == "true" ]] \
   && [[ "$ADOPT_VPB" == "true" || "$VPB_IN_KVO" == "true" ]]; then
  step "Phase 16: vPB traffic path + monitoring policy"

  if ! run_phase path; then
    skip_note "the vPB traffic path"
    WIRE_VPB_PATH=false
  fi

  echo
  echo "  Heads up before you choose: this step needs the vPB data interfaces"
  echo "  (eth1/eth2) up as DPDK data ports on the vPB itself. That bring-up is"
  echo "  not automated, so on a fresh vPB the device config has no ports and the"
  echo "  port-bind stage has nothing to bind."
  if [[ -z "$WIRE_VPB_PATH" ]]; then
    if ask_yn "  Try the traffic path anyway? [y/N]: " n; then
      WIRE_VPB_PATH=true
    else
      WIRE_VPB_PATH=false
    fi
  fi
  ok "Wire vPB path: ${WIRE_VPB_PATH}"

  WIRE_SCRIPT="$(find_repo_script scripts/vpb_wire_path.py || true)"
  WIRE_COLLECTION="${VPB_COLLECTION:-$CLOUD_CONFIG_NAME}"
  # Stand up a capture host so the tapped traffic is actually visible. Opt-out
  # with CLOUDLENS_CAPTURE_HOST=no. Failure here is non-fatal: the vPB path is
  # wired either way, the operator just would not have a tcpdump host.
  if [[ "$WIRE_VPB_PATH" == "true" && "${CLOUDLENS_CAPTURE_HOST:-yes}" != "no" && -z "$CAPTURE_HOST_IP" ]]; then
    ensure_capture_host_now || note "Continuing without a capture host; the vPB path is still wired."
  fi
  CAPTURE_ARG=(); [[ -n "$CAPTURE_HOST_IP" ]] && CAPTURE_ARG=(--capture-ip "$CAPTURE_HOST_IP")
  if [[ "$WIRE_VPB_PATH" != "true" ]]; then
    note "Skipped. Bring eth1/eth2 up on the vPB first, then run scripts/vpb_wire_path.py."
  elif [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "python3 scripts/vpb_wire_path.py --kvo ${KVO_PUBLIC_IP} --device ${VPB_DEVICE_NAME} --collection ${WIRE_COLLECTION} --cloud-config ${CLOUD_CONFIG_NAME} --ingress-ip ${VPB_INGRESS_IP} ${CAPTURE_ARG[*]} --insecure"
    dryrun_say "would report the port bind honestly: no ports found is NOT a success"
  elif [[ -z "$WIRE_SCRIPT" ]] || ! python_chain_ready; then
    warn "scripts/vpb_wire_path.py or python3 is unavailable; skipping the traffic path."
  elif [[ -z "$VPB_INGRESS_IP" ]]; then
    warn "No vPB ingress ENI found (--vpb-ingress-nics 0?), so there is no ingress IP for the C2DL. Skipping."
  else
    if python3 "$WIRE_SCRIPT" --kvo "$KVO_PUBLIC_IP" --device "$VPB_DEVICE_NAME" \
         --collection "$WIRE_COLLECTION" --cloud-config "$CLOUD_CONFIG_NAME" \
         --ingress-ip "$VPB_INGRESS_IP" "${CAPTURE_ARG[@]}" --insecure; then
      ok "vPB traffic path and monitoring policy committed."
      if [[ -n "$CAPTURE_HOST_IP" ]]; then
        ok "Capture host (tool VM) wired to the vPB egress: ${CAPTURE_HOST_IP} (private)"
        note "SSH to it from your laptop or CloudShell (public address, key-pair login):"
        note "  ssh ${KEY_NAME:+-i ~/.ssh/${KEY_NAME}.pem }ubuntu@${CAPTURE_HOST_PUBLIC_IP:-$CAPTURE_HOST_IP}"
        note "Then watch the live tapped traffic, decapsulated:"
        note "  sudo tcpdump -i any -nn 'proto 47' -v"
        note "  # or read the rolling pcaps in /var/log/cloudlens-tool/"
      fi
      state_phase path done
    else
      warn "The vPB traffic path did not finish every step."
      note "Read WHICH step stopped before assuming why. Seen live: steps 1 to 5"
      note "all committed (ports synced, C2DL created, eth1 and eth2 bound, tool"
      note "created) and only the deviceLinks step stopped, because the cloud"
      note "config was a Custom Cloud with no awsConfiguration. That is not a"
      note "failure of the vPB path."
      note "This warning used to blame the DPDK bring-up unconditionally. Only"
      note "believe that if the PORT BIND steps themselves failed. If they did,"
      note "bring eth1/eth2 up on the vPB and re-run:"
      note "  python3 scripts/vpb_wire_path.py --kvo ${KVO_PUBLIC_IP} --device ${VPB_DEVICE_NAME} \\"
      note "    --collection ${WIRE_COLLECTION} --cloud-config ${CLOUD_CONFIG_NAME} --ingress-ip ${VPB_INGRESS_IP} --insecure"
    fi
  fi
fi

# =====================================================================
# Phase 17: Final summary
# =====================================================================
step "Phase 17: Final summary"

write_summary() {
  cat <<SUMMARY
========================================================================
CloudLens Stack Deployment Summary (AWS)
Generated: $(date -u +%FT%TZ)
========================================================================

AWS account:        ${ACCOUNT_ID}
Region:             ${REGION}
Stack name:         ${STACK_NAME}
Infra engine:       ${IAC}
Key pair:           ${KEY_NAME}  (private key: ~/.ssh/${KEY_NAME}.pem)

--- vController (formerly CLMS) ---
Public IP:          ${CLMS_PUBLIC_IP}
Web UI:             https://${CLMS_PUBLIC_IP}
Access:             web UI (appliance shell uses password auth, not the key pair)
Default UI creds:   admin / Cl0udLens@dm!n  (change on first login)

SUMMARY

  if [[ "$DEPLOY_KVO" == "true" ]]; then
    cat <<KSUMMARY
--- KVO (Keysight Vision Orchestrator) ---
Public IP:          ${KVO_PUBLIC_IP}
Web UI:             https://${KVO_PUBLIC_IP}
SSH:                ssh -i ~/.ssh/${KEY_NAME}.pem -p ${VPB_SSH_PORT} ${ADMIN_USERNAME}@${KVO_PUBLIC_IP}
Default UI creds:   see Keysight KVO documentation
Purpose:            Centralized vPB fleet orchestration and configuration

KSUMMARY
  fi

  if [[ "$DEPLOY_VPB" == "true" ]]; then
    cat <<VSUMMARY
--- vPB ---
Public IP:          ${VPB_PUBLIC_IP}
Management SSH:     ssh -i ~/.ssh/${KEY_NAME}.pem -p ${VPB_SSH_PORT} ${ADMIN_USERNAME}@${VPB_PUBLIC_IP}
Bootstrap:          runs automatically at first boot (log: /var/log/cloudlens-bootstrap.log)
vPB CLI:            sudo vpb        (re-run bootstrap manually: curl -sSL ${REPO_RAW}/scripts/bootstrap-vpb.sh | sudo bash)
Note:               SSH is reachable ~5 minutes after deploy

VSUMMARY
  fi

  cat <<CSUMMARY
--- Post-deploy chain ---
Sensor mode:        ${SENSOR_MODE:-none}   (project key source: ${KEY_SOURCE:-none})
KVO cloud config:   ${CLOUD_CONFIG_NAME}   (created: $([[ -n "$KVO_PROJECT_KEY" ]] && echo yes || echo no))
vPB bootstrap:      ${BOOTSTRAP_VPB:-n/a}
vPB adopted in KVO: ${ADOPT_VPB:-n/a}      (device name: ${VPB_DEVICE_NAME})
vPB traffic path:   ${WIRE_VPB_PATH:-n/a}  (needs eth1/eth2 up as DPDK data ports on the vPB)
AWS mirror session: ${WITH_MIRROR:-n/a}    (known open item: the collector SVM is never adopted, so no sessions)

CSUMMARY

  # Every login in one place: URLs, users, passwords, the SSH line, the project
  # name and where the credential files live.
  login_block
  echo

  cat <<EOM
--- Next steps ---
1. Open https://${CLMS_PUBLIC_IP}/cloudlens/login and sign in with the login above
2. Create a project in the vController UI and copy the project key
3. To deploy sensors later:
     curl -sSL ${REPO_RAW}/quickstart.sh | bash

--- Cleanup ---
EOM
  if [[ "$IAC" == "terraform" ]]; then
    echo "Delete everything: (cd ${REPO_DIR}/${TF_DIR_REL} && terraform destroy)"
  else
    echo "Delete everything: aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${REGION}"
  fi
  cat <<EOM

--- Re-running ---
Safe to re-run at any time. Each phase is checked against the real system
first and skipped when it is already done:
  bash deploy/deploy-stack.sh --region ${REGION} --stack-name ${STACK_NAME} --resume
  --fresh          run every phase again (still deletes nothing)
  --from PHASE     start at stack|wait|bootstrap|key|license|adopt|sensors|vpb|path|mirror
  --only PHASE     run exactly one phase
State file (inputs only, no secrets): ${STATE_FILE:-none}

--- Log ---
Full deployment log: ${LOG_FILE}   (mode 600: it contains the credentials above)
========================================================================
EOM
}

# The run finished: clear the "stopped at" hint the next run would report.
state_set LAST_FAILURE "none"

# The summary carries the logins, so it is created tight and re-tightened after
# tee: tee truncates an existing file but never changes its mode.
secure_run_file "$SUMMARY_FILE"
write_summary | tee "$SUMMARY_FILE" >/dev/null
secure_run_file "$SUMMARY_FILE"

# Honest completion: "Done" must not imply every phase succeeded. Read the phase
# states and, if any post-stack phase is not 'done', list what is outstanding and
# the one command that finishes it. A run where SSH steps were skipped for a
# missing .pem used to end with a cheerful "Done" and no hint it was incomplete.
completion_report() {
  local labels="sensors:Linux/Windows sensors vpb:vPB adopted mirror:AWS mirror sessions path:vPB traffic path"
  local pending=() p name val
  for p in sensors vpb mirror path; do
    val="$(state_get "PHASE_$(upper "$p")" 2>/dev/null || echo '')"
    case "$val" in
      done*|"dry-run"*) : ;;                       # done (or dry-run) counts as covered
      *) name="$(printf '%s' "$labels" | tr ' ' '\n' | grep "^${p}:" | cut -d: -f2-)"
         pending+=("${name:-$p}") ;;
    esac
  done
  (( ${#pending[@]} == 0 )) && return 0
  echo
  warn "Not every phase finished. Outstanding: ${pending[*]}"
  if [[ ! -f "$KEY_PEM" ]]; then
    note "The usual cause is the SSH key not being on this machine:"
    pem_help
  else
    note "Re-run to retry the outstanding phases (it resumes and skips finished work):"
    note "  bash <(curl -sSL ${REPO_RAW}/deploy/deploy-stack.sh)"
  fi
}

banner "Stack deployment complete"
echo
echo "Summary saved to:    ${SUMMARY_FILE}   (mode 600, contains credentials)"
echo "Log saved to:        ${LOG_FILE}   (mode 600, contains credentials)"
echo
login_block
completion_report
echo
ok "Done."
SCRIPT_DONE=true
trap - ERR
exit 0

} # End of curl|bash brace-wrap (see top of file)
