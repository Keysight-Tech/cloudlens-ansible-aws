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
#   --key-name NAME  EC2 key pair to attach (created if missing)
#   --rollback       Delete the stack we created on any failure
#   -h | --help      Show this banner and exit
# =====================================================================
set -euo pipefail

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

# State trackers (filled as we go) for trap reporting
PHASE_NAME="init"
CREATED_STACK=false
CLMS_PUBLIC_IP=""
KVO_PUBLIC_IP=""
VPB_PUBLIC_IP=""

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
fail()  { echo -e "${C_RED}[x]${C_RESET} $1" >&2; exit 1; }
step()  { echo; echo -e "${C_BLUE}--- $1 ---${C_RESET}"; PHASE_NAME="$1"; }
note()  { echo -e "${C_GREY}  -> $1${C_RESET}"; }
dryrun_say() { echo -e "${C_YELLOW}[dry-run]${C_RESET} $1"; }

to_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# ---------------------------------------------------------------------
# Logging: tee everything to log file
# ---------------------------------------------------------------------
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
  CLOUDLENS_DISCOVERY_TAG_KEY, CLOUDLENS_DISCOVERY_TAG_VALUE

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
  4. Marketplace AMI subscription check (vController + KVO + vPB)
  5. Infra engine selection (CloudFormation or Terraform)
  6. Deploy the stack (vController + optional KVO + optional vPB)
  7. Wait for vController to initialize (~15 minutes)
  8. Read stack outputs (Elastic IPs)
  9. Manual project key step (from vController UI)
 10. Sensor chain (optional, runs quickstart.sh)
 11. Final summary written to cloudlens-deploy-summary.txt
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

    --rollback) ROLLBACK_ON_FAIL=true; shift ;;
    --no-rollback) ROLLBACK_ON_FAIL=false; shift ;;

    --discovery-tag-key) DISCOVERY_TAG_KEY="$2"; shift 2 ;;
    --discovery-tag-value) DISCOVERY_TAG_VALUE="$2"; shift 2 ;;

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

# Validate NIC count bounds
for v in VPB_INGRESS_NICS:1:3 VPB_EGRESS_NICS:1:3; do
  name="${v%%:*}"; rest="${v#*:}"; lo="${rest%%:*}"; hi="${rest##*:}"
  val="${!name}"
  if ! [[ "$val" =~ ^[0-9]+$ ]] || (( val < lo || val > hi )); then
    fail "$name must be an integer between $lo and $hi (got '$val'). Run with -h for usage."
  fi
done

# Validate instance types against what each Marketplace AMI actually permits.
# Verified with run-instances --dry-run: anything else is rejected by AWS at
# launch with UnsupportedOperation ("instance configuration for this AWS
# Marketplace product is not supported"). Fail here rather than 3 phases later.
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
  echo "  Re-run this script:      bash deploy/deploy-stack.sh    # idempotent"
  echo "  Inspect log:             ${LOG_FILE}"
}
trap on_error ERR

on_interrupt() {
  echo
  warn "Interrupted in phase: ${PHASE_NAME}"
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
  ok "Detected: AWS CloudShell (pre-authenticated, Ansible-ready)"
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

# Verify caller identity (credentials present + valid)
if [[ "$DRY_RUN" == "false" ]]; then
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    warn "No valid AWS credentials found in this shell."
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
        echo "  Sign in with one of these, then re-run this installer:"
        echo "    aws sso login --profile NAME  # IAM Identity Center / SSO"
        echo "    aws configure                 # static access key"
        fail "Cannot proceed without valid AWS credentials."
        ;;
    esac
    # Re-check after the assisted login.
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
      fail "Still no valid credentials. Sign in, then re-run this installer."
    fi
    ok "Signed in."
  fi
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text)
  ok "AWS account: ${ACCOUNT_ID}"
  note "Caller: ${CALLER_ARN}"
else
  ACCOUNT_ID="000000000000"
  CALLER_ARN="arn:aws:iam::000000000000:user/dry-run"
  ok "AWS account: ${ACCOUNT_ID}  [dry-run placeholder]"
fi

# Locate the repo root so we can find the CFN template / Terraform dir.
# When curl|bash'd we may not be inside the repo; clone lazily if needed.
if [[ -f "$SCRIPT_DIR/../$CFN_TEMPLATE_REL" ]] || [[ -d "$SCRIPT_DIR/../$TF_DIR_REL" ]]; then
  REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [[ -f "$PWD/$CFN_TEMPLATE_REL" ]]; then
  REPO_DIR="$PWD"
else
  REPO_DIR="$HOME/${REPO_NAME}"
fi

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
  read -rp "AWS region [${DEFAULT_REGION}]: " input_region; echo || true
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
    read -rp "Stack name [${DEFAULT_STACK_NAME}]: " input_stack; echo || true
    STACK_NAME="${input_stack:-$DEFAULT_STACK_NAME}"
    if valid_stack_name "$STACK_NAME"; then break; fi
    suggestion="$(sanitize_stack_name "$STACK_NAME")"
    warn "Invalid name '${STACK_NAME}': CloudFormation names must start with a letter and use only letters, digits, and hyphens (no spaces)."
    read -rp "Use '${suggestion}' instead? [Y/n]: " use_sugg; echo || true
    if [[ "${use_sugg,,}" != "n" ]]; then STACK_NAME="$suggestion"; break; fi
  done
  ok "Stack name: ${STACK_NAME}"
fi

# EC2 key pair. AWS appliances use SSH key auth, not passwords. Create one
# on demand if the customer does not already have a key pair.
ensure_key_pair() {
  local name="$1"
  [[ "$DRY_RUN" == "true" ]] && { note "would ensure key pair $name exists"; return 0; }
  if aws "${AWS_REGION_ARG[@]}" ec2 describe-key-pairs --key-names "$name" >/dev/null 2>&1; then
    ok "Key pair '${name}' exists in ${REGION}"
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
  local i=1
  for line in "${existing[@]}"; do
    printf "  %2d) %s\n" "$i" "$line"
    i=$((i+1))
  done
  printf "  %2d) Create a NEW key pair\n" "$i"
  echo
  read -rp "Choose 1-${i}, or type a key pair name [1]: " pick || true
  pick="${pick:-1}"

  if [[ "$pick" =~ ^[0-9]+$ ]] && (( pick >= 1 && pick < i )); then
    KEY_NAME="${existing[$((pick-1))]}"
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

if [[ "$DRY_RUN" != "true" ]]; then
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
  deploy_terraform
  CLMS_PUBLIC_IP="$(tf_output controller_public_ip)"
  [[ "$DEPLOY_KVO" == "true" ]] && KVO_PUBLIC_IP="$(tf_output kvo_public_ip)"
  [[ "$DEPLOY_VPB" == "true" ]] && VPB_PUBLIC_IP="$(tf_output vpb_public_ip)"
else
  deploy_cfn
  # Outputs are named *Address (public EIP when AssignPublicIp=yes, else the
  # private IP). Same variable is used downstream for the wait + sensor chain.
  CLMS_PUBLIC_IP="$(cfn_output VcontrollerAddress)"
  [[ "$DEPLOY_KVO" == "true" ]] && KVO_PUBLIC_IP="$(cfn_output KvoAddress)"
  [[ "$DEPLOY_VPB" == "true" ]] && VPB_PUBLIC_IP="$(cfn_output VpbAddress)"
fi

ok "vController at ${CLMS_PUBLIC_IP:-unknown}"
[[ "$DEPLOY_KVO" == "true" ]] && ok "KVO at ${KVO_PUBLIC_IP:-unknown}"
[[ "$DEPLOY_VPB" == "true" ]] && ok "vPB at ${VPB_PUBLIC_IP:-unknown} (SSH on port ${VPB_SSH_PORT})"

# =====================================================================
# Phase 7: Wait for vController init
# =====================================================================
step "Phase 7: Wait for vController initialization"

if [[ "$DRY_RUN" == "true" ]]; then
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
  echo "vController needs ~15 minutes to initialize. The 443 UI typically responds"
  echo "within 60 seconds, with a further settle for backend services."
  deadline=$(( $(date +%s) + 17*60 ))
  while (( $(date +%s) < deadline )); do
    remaining=$(( deadline - $(date +%s) ))
    printf "\r%s seconds remaining, probing port 443..." "$remaining"
    if (echo > /dev/tcp/"${CLMS_PUBLIC_IP}"/443) >/dev/null 2>&1; then
      echo; ok "vController port 443 is open"
      echo "Settling 2 more minutes for backend init..."; sleep 120; break
    fi
    sleep 15
  done
  echo
fi

# =====================================================================
# Phase 8: vPB bootstrap hint (KVO adoption + traffic config)
# =====================================================================
if [[ "$DEPLOY_VPB" == "true" ]]; then
  step "Phase 8: vPB post-deploy bootstrap"
  note "vPB management SSH is reachable on port ${VPB_SSH_PORT} within ~5 minutes."
  note "Finish the vPB one-time bootstrap (KCOS wait + vpb CLI wrapper) with:"
  note "  ssh -i ~/.ssh/${KEY_NAME}.pem -p ${VPB_SSH_PORT} ${ADMIN_USERNAME}@${VPB_PUBLIC_IP}"
  note "  curl -sSL ${REPO_RAW}/scripts/bootstrap-vpb.sh | sudo bash"
fi

# =====================================================================
# Phase 9: Manual project key step
# =====================================================================
step "Phase 9: Get project key from vController"

cat <<EOM

vController is now reachable. To deploy sensors, you need a project key.

  1. Open the vController UI: https://${CLMS_PUBLIC_IP}
  2. Sign in with the default credentials: admin / Cl0udLens@dm!n
  3. You will be prompted to change the password on first login.
  4. Go to Projects -> Add Project, give it a name, then open the
     project and copy the API key.

  (Or automate this with scripts/vcontroller_project_key.py --host ${CLMS_PUBLIC_IP} --insecure)

EOM

# Pre-flight: count already-tagged EC2s using the chosen discovery tag.
if [[ "$CHAIN_SENSORS" == "true" ]] && [[ "$DRY_RUN" != "true" ]]; then
  TAGGED_COUNT=$(aws "${AWS_REGION_ARG[@]}" ec2 describe-instances \
    --filters "Name=tag:${DISCOVERY_TAG_KEY},Values=${DISCOVERY_TAG_VALUE}" "Name=instance-state-name,Values=running" \
    --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo "?")
  if [[ "$TAGGED_COUNT" == "0" ]]; then
    echo "  Workload EC2s tagged ${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}: 0"
    echo "  Tag a running instance so the sensor installs on it:"
    echo "    aws ec2 create-tags --resources <instance-id> \\"
    echo "        --tags Key=${DISCOVERY_TAG_KEY},Value=${DISCOVERY_TAG_VALUE} Key=os,Value=ubuntu Key=env,Value=prod"
    echo "  (Tag os = ubuntu | rhel | windows ; env = prod | dev)"
  else
    echo "  Workload EC2s tagged ${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}: ${TAGGED_COUNT}"
    echo "  The sensor chain will install on those ${TAGGED_COUNT} instance(s)."
  fi
  echo
fi

PROJECT_KEY=""
if [[ "$CHAIN_SENSORS" == "true" ]]; then
  read -rp "Paste project key (or press Enter to skip sensor deployment): " PROJECT_KEY || true
  if [[ -z "$PROJECT_KEY" ]]; then
    warn "No project key supplied. Skipping sensor chain."
    CHAIN_SENSORS=false
  fi
fi

# =====================================================================
# Phase 10: Sensor chain (optional)
# =====================================================================
if [[ "$CHAIN_SENSORS" == "true" ]] && [[ "$IS_NATIVE_WINDOWS" == "true" ]]; then
  warn "Sensor chain disabled: Ansible cannot run on Windows shells."
  warn "Run sensors from AWS CloudShell, WSL, or a Linux EC2 with:"
  warn "  curl -sSL ${REPO_RAW}/quickstart.sh | bash"
  CHAIN_SENSORS=write_yaml_only
fi

if [[ "$CHAIN_SENSORS" == "true" || "$CHAIN_SENSORS" == "write_yaml_only" ]]; then
  step "Phase 10: Chain into sensor deployment"

  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "would generate customer_input.yaml and run bash quickstart.sh"
  else
    # Linux SSH auth: prefer the key pair PEM we know about.
    KEY_PEM="$HOME/.ssh/${KEY_NAME}.pem"
    if [[ -f "$KEY_PEM" ]]; then
      SSH_KEY_LINE="ssh_key_path:    \"${KEY_PEM}\""
    else
      SSH_KEY_LINE="ssh_key_path:    \"~/.ssh/${KEY_NAME}.pem\""
    fi

    cat > customer_input.yaml <<YAML
# Auto-generated by deploy-stack.sh on $(date -u +%FT%TZ)
# Edit BEFORE running quickstart.sh ONLY if:
#   - Your workload EC2s use a different ssh_user than the defaults
#   - You want to filter by more tags than just ${DISCOVERY_TAG_KEY}=${DISCOVERY_TAG_VALUE}

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
  project_key:        "${PROJECT_KEY}"
  custom_tags:        "DeployedBy=stack Region=${REGION}"
  registry_type:      "insecure"
  linux_runtime:      "auto"

vpc_ids:    []
subnet_ids: []
YAML
    ok "Wrote customer_input.yaml"

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
      cp customer_input.yaml "$QS_DIR/customer_input.yaml" 2>/dev/null || true
      chmod +x "$QS_DIR/quickstart.sh" 2>/dev/null || true
      ok "Launching quickstart.sh from $QS_DIR"
      ( cd "$QS_DIR" && bash quickstart.sh ) \
        || warn "quickstart.sh exited non-zero (see $QS_DIR for logs)"
    fi
  fi
else
  step "Phase 10: Sensor chain (skipped)"
fi

# =====================================================================
# Phase 11: Final summary
# =====================================================================
step "Phase 11: Final summary"

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

  cat <<EOM
--- Next steps ---
1. Open https://${CLMS_PUBLIC_IP} and change the default vController password
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

--- Log ---
Full deployment log: ${LOG_FILE}
========================================================================
EOM
}

write_summary | tee "$SUMMARY_FILE" >/dev/null

banner "Stack deployment complete"
echo
echo "Summary saved to:    ${SUMMARY_FILE}"
echo "Log saved to:        ${LOG_FILE}"
echo "vController UI:      https://${CLMS_PUBLIC_IP}"
[[ "$DEPLOY_KVO" == "true" ]] && echo "KVO UI:              https://${KVO_PUBLIC_IP}"
[[ "$DEPLOY_VPB" == "true" ]] && echo "vPB management:      ${VPB_PUBLIC_IP} (SSH port ${VPB_SSH_PORT})"
echo
ok "Done."
trap - ERR
exit 0

} # End of curl|bash brace-wrap (see top of file)
