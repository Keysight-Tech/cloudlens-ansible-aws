#!/usr/bin/env bash
# =====================================================================
# CloudLens Stack Deployment: CLMS + vPB + Sensors, end to end
# =====================================================================
# One paste, full stack. Detects Azure Cloud Shell vs local, accepts
# Marketplace terms, deploys CLMS, waits for init, optionally adds vPB,
# and chains into the sensor playbook via quickstart.sh.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Keysight-Tech/cloudlens-ansible-azure/main/deploy/deploy-stack.sh | bash
#   OR
#   bash deploy/deploy-stack.sh [--dry-run]
#
# Flags:
#   --dry-run        Walk the prompts, print every az command, touch nothing
#   --resource-group RG_NAME
#   --location       REGION (e.g. eastus2)
#   --no-vpb         Skip vPB deployment
#   --no-sensors     Skip sensor chain at the end
#   -h | --help      Show this banner and exit
# =====================================================================
set -euo pipefail

# ---------------------------------------------------------------------
# Config + globals
# ---------------------------------------------------------------------
REPO_OWNER="Keysight-Tech"
REPO_NAME="cloudlens-ansible-azure"
REPO_RAW="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/main"

CLMS_TEMPLATE_URL="${REPO_RAW}/deploy/clms-marketplace.json"
VPB_TEMPLATE_URL="${REPO_RAW}/deploy/vpb-marketplace.json"

CLMS_PUBLISHER="keysight-technologies-cloudlens"
CLMS_OFFER="keysight-cloudlens-manager-preview"
CLMS_PLAN="clms-6-13-0_76"

VPB_PUBLISHER="keysight-technologies-cloudlens"
VPB_OFFER="keysight-cloudlens-virtual-packet-broker"
VPB_PLAN="cloudlens-virtual-packet-broker-3-15-0_1"

DEFAULT_RG="cloudlens-rg"
DEFAULT_LOCATION="eastus2"
DEFAULT_CLMS_NAME="clms"
DEFAULT_VPB_NAME="vpb"
CLMS_VM_SIZE="Standard_D4s_v5"
VPB_VM_SIZE="Standard_D8s_v3"

SUMMARY_FILE="cloudlens-deploy-summary.txt"
LOG_FILE="cloudlens-deploy-stack.log"

# Flag defaults (set by argument parser)
DRY_RUN=false
DEPLOY_VPB=""
CHAIN_SENSORS=""
ARG_RG=""
ARG_LOCATION=""

# State trackers (filled as we go) for trap reporting
PHASE_NAME="init"
CREATED_RG=false
DEPLOYED_CLMS=false
DEPLOYED_VPB=false
CLMS_PUBLIC_IP=""
VPB_PUBLIC_IP=""
ADMIN_USERNAME="azureuser"
ADMIN_PASSWORD=""

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
  echo -e "${C_BLUE}╔══════════════════════════════════════════════════════════════╗${C_RESET}"
  printf "${C_BLUE}║${C_RESET}  ${C_BOLD}%-60s${C_RESET}${C_BLUE}║${C_RESET}\n" "$msg"
  echo -e "${C_BLUE}╚══════════════════════════════════════════════════════════════╝${C_RESET}"
}
ok()    { echo -e "${C_GREEN}\xE2\x9C\x93${C_RESET} $1"; }
warn()  { echo -e "${C_YELLOW}\xE2\x9A\xA0${C_RESET} $1"; }
fail()  { echo -e "${C_RED}\xE2\x9C\x97${C_RESET} $1" >&2; exit 1; }
step()  { echo; echo -e "${C_BLUE}\xE2\x94\x80\xE2\x94\x80\xE2\x94\x80 $1 \xE2\x94\x80\xE2\x94\x80\xE2\x94\x80${C_RESET}"; PHASE_NAME="$1"; }
note()  { echo -e "${C_GREY}\xE2\x86\x92 $1${C_RESET}"; }
dryrun_say() { echo -e "${C_YELLOW}[dry-run]${C_RESET} $1"; }

# POSIX-portable lowercase (bash 3.2 compatible)
to_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# ---------------------------------------------------------------------
# Logging: tee everything to log file
# ---------------------------------------------------------------------
exec > >(tee -a "$LOG_FILE") 2>&1

# ---------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------
show_help() {
  cat <<'HLP'
CloudLens Stack Deployment

Usage:
  bash deploy/deploy-stack.sh [options]

Options:
  --dry-run                 Walk through prompts and print would-be az commands
                            without touching Azure. Safe to run anywhere.
  --resource-group NAME     Override default resource group (cloudlens-rg)
  --location REGION         Override default Azure region (eastus2)
  --no-vpb                  Skip vPB deployment
  --no-sensors              Skip sensor playbook chain at the end
  -h, --help                Show this help

What it does (10 phases):
  1. Banner + environment detection (Cloud Shell vs local)
  2. Pre-flight checks (az CLI, subscription access, quota)
  3. Customer input (resource group, region, admin password)
  4. Marketplace terms acceptance
  5. Resource group creation (skipped if exists)
  6. CLMS deployment via ARM template
  7. Wait for CLMS to initialize (~15 minutes)
  8. vPB deployment (optional)
  9. Manual project key step
 10. Sensor chain (optional, runs quickstart.sh)
 11. Final summary written to cloudlens-deploy-summary.txt
HLP
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --no-vpb) DEPLOY_VPB=false; shift ;;
    --no-sensors) CHAIN_SENSORS=false; shift ;;
    --resource-group) ARG_RG="$2"; shift 2 ;;
    --location) ARG_LOCATION="$2"; shift 2 ;;
    -h|--help) show_help; exit 0 ;;
    *) warn "Unknown argument: $1"; show_help; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------
# Run az (or echo it in dry-run)
# ---------------------------------------------------------------------
run_az() {
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "az $*"
    return 0
  fi
  az "$@"
}

# Run az with stdout captured (used for outputs like IPs)
run_az_capture() {
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "az $* (would capture stdout)"
    echo "DRY_RUN_VALUE"
    return 0
  fi
  az "$@"
}

# ---------------------------------------------------------------------
# Error trap: explain partial state, point at cleanup
# ---------------------------------------------------------------------
on_error() {
  local exit_code=$?
  echo
  echo -e "${C_RED}\xE2\x9C\x97 FAILED in phase: ${PHASE_NAME}${C_RESET}"
  echo -e "${C_RED}  exit code: ${exit_code}${C_RESET}"
  echo
  echo "What was created so far:"
  [[ "$CREATED_RG" == "true" ]]    && echo "  - Resource group: ${RESOURCE_GROUP:-unknown}"
  [[ "$DEPLOYED_CLMS" == "true" ]] && echo "  - CLMS VM: ${DEFAULT_CLMS_NAME} (IP ${CLMS_PUBLIC_IP:-pending})"
  [[ "$DEPLOYED_VPB" == "true" ]]  && echo "  - vPB VM:  ${DEFAULT_VPB_NAME}  (IP ${VPB_PUBLIC_IP:-pending})"
  echo
  echo "Cleanup options:"
  if [[ "$CREATED_RG" == "true" ]] && [[ -n "${RESOURCE_GROUP:-}" ]]; then
    echo "  Delete everything:  az group delete -n ${RESOURCE_GROUP} --yes --no-wait"
  fi
  echo "  Re-run this script:  bash deploy/deploy-stack.sh    # idempotent, skips existing"
  echo "  Inspect log:          ${LOG_FILE}"
}
trap on_error ERR

on_interrupt() {
  echo
  warn "Interrupted in phase: ${PHASE_NAME}"
  if [[ "$CREATED_RG" == "true" ]] && [[ -n "${RESOURCE_GROUP:-}" ]]; then
    warn "To remove what got created: az group delete -n ${RESOURCE_GROUP} --yes --no-wait"
  fi
  exit 130
}
trap on_interrupt INT TERM

# =====================================================================
# Phase 1: Banner + environment detection
# =====================================================================
step "Phase 1: Banner + environment detection"
banner "CloudLens Stack Deployment: CLMS + vPB + Sensors"
echo
[[ "$DRY_RUN" == "true" ]] && warn "DRY-RUN MODE: no Azure resources will be created"
echo

IN_CLOUD_SHELL=false
if [[ -n "${AZUREPS_HOST_ENVIRONMENT:-}" ]] \
   || [[ "${ACC_CLOUD:-}" == "PROD" ]] \
   || [[ -d /usr/cloudshell ]]; then
  IN_CLOUD_SHELL=true
  ok "Detected: Azure Cloud Shell (pre-authenticated)"
else
  ok "Detected: Local machine ($(uname -s))"
fi

# =====================================================================
# Phase 2: Pre-flight checks
# =====================================================================
step "Phase 2: Pre-flight checks"

if ! command -v az >/dev/null 2>&1; then
  if [[ "$DRY_RUN" == "true" ]]; then
    warn "az CLI not installed (dry-run continues)"
  else
    fail "Azure CLI not installed. https://learn.microsoft.com/cli/azure/install-azure-cli"
  fi
else
  ok "Azure CLI present ($(az version --query \"\\\"azure-cli\\\"\" -o tsv 2>/dev/null || echo unknown))"
fi

# Check login
if [[ "$DRY_RUN" == "false" ]]; then
  if ! az account show >/dev/null 2>&1; then
    warn "Not logged in. Running az login..."
    az login --use-device-code
  fi
  SUB_ID=$(az account show --query id -o tsv)
  SUB_NAME=$(az account show --query name -o tsv)
  ok "Subscription: ${SUB_NAME} (${SUB_ID})"
else
  SUB_ID="00000000-0000-0000-0000-000000000000"
  SUB_NAME="DryRunSubscription"
  ok "Subscription: ${SUB_NAME} (${SUB_ID})  [dry-run placeholder]"
fi

# Quota probe is informational. We do not block the run on it.
check_quota_family() {
  local family="$1"
  local region="$2"
  local needed="$3"
  if [[ "$DRY_RUN" == "true" ]]; then
    note "Quota check skipped in dry-run for ${family} in ${region}"
    return 0
  fi
  local available
  available=$(az vm list-usage --location "$region" \
    --query "[?contains(name.value,'${family}')].{cur:currentValue,max:limit}" \
    -o tsv 2>/dev/null | head -1 || echo "")
  if [[ -z "$available" ]]; then
    note "Could not read quota for ${family} in ${region} (continuing)"
    return 0
  fi
  local cur max
  cur=$(echo "$available" | awk '{print $1}')
  max=$(echo "$available" | awk '{print $2}')
  local free=$((max - cur))
  if (( free < needed )); then
    warn "Quota tight: ${family} in ${region}: ${free} free, ${needed} needed"
  else
    ok "Quota OK: ${family} in ${region}: ${free} free (need ${needed})"
  fi
}

# =====================================================================
# Phase 3: Customer input
# =====================================================================
step "Phase 3: Customer input"

# Resource group
if [[ -n "$ARG_RG" ]]; then
  RESOURCE_GROUP="$ARG_RG"
  ok "Resource group: ${RESOURCE_GROUP} (from --resource-group)"
else
  read -rp "Resource group name [${DEFAULT_RG}]: " input_rg
  RESOURCE_GROUP="${input_rg:-$DEFAULT_RG}"
fi

# Location
if [[ -n "$ARG_LOCATION" ]]; then
  LOCATION="$ARG_LOCATION"
  ok "Region: ${LOCATION} (from --location)"
else
  read -rp "Azure region [${DEFAULT_LOCATION}]: " input_loc
  LOCATION="${input_loc:-$DEFAULT_LOCATION}"
fi

# Admin password: auto-generate or prompt
gen_password() {
  # 16 chars: upper, lower, digit, symbol guarantee + 12 random
  local alpha="ABCDEFGHJKLMNPQRSTUVWXYZ"
  local lower="abcdefghjkmnpqrstuvwxyz"
  local digit="23456789"
  local symbol='!@#%^*-_=+'
  local rand
  # SIGPIPE-safe: write urandom bytes via dd, then filter
  rand=$( (dd if=/dev/urandom bs=64 count=1 2>/dev/null || echo "fallbackRandomBytes" ) | LC_ALL=C tr -dc 'A-Za-z0-9' | cut -c1-12 )
  if [[ -z "$rand" ]]; then
    rand="Random12345Z"
  fi
  echo "${alpha:RANDOM%${#alpha}:1}${lower:RANDOM%${#lower}:1}${digit:RANDOM%${#digit}:1}${symbol:RANDOM%${#symbol}:1}${rand}"
}

read -rsp "Admin password (Enter to auto-generate): " input_pw; echo
if [[ -z "$input_pw" ]]; then
  ADMIN_PASSWORD=$(gen_password)
  ok "Generated 16-char password (saved in ${SUMMARY_FILE})"
else
  ADMIN_PASSWORD="$input_pw"
  ok "Using supplied password"
fi

# Deploy vPB?
if [[ -z "$DEPLOY_VPB" ]]; then
  read -rp "Deploy vPB alongside CLMS? [y/N]: " yn
  yn_lc=$(to_lower "$yn")
  if [[ "$yn_lc" == "y" ]] || [[ "$yn_lc" == "yes" ]]; then
    DEPLOY_VPB=true
  else
    DEPLOY_VPB=false
  fi
fi
ok "Deploy vPB: ${DEPLOY_VPB}"

# Chain to sensor deployment?
if [[ -z "$CHAIN_SENSORS" ]]; then
  read -rp "Chain to sensor deployment after stack is up? [y/N]: " yn
  yn_lc=$(to_lower "$yn")
  if [[ "$yn_lc" == "y" ]] || [[ "$yn_lc" == "yes" ]]; then
    CHAIN_SENSORS=true
  else
    CHAIN_SENSORS=false
  fi
fi
ok "Chain sensors: ${CHAIN_SENSORS}"

# Now we know enough to probe quotas
check_quota_family "standardDSv5Family" "$LOCATION" 4
if [[ "$DEPLOY_VPB" == "true" ]]; then
  check_quota_family "standardDSv3Family" "$LOCATION" 8
fi

# =====================================================================
# Phase 4: Marketplace terms acceptance
# =====================================================================
step "Phase 4: Marketplace terms acceptance"

accept_terms() {
  local publisher="$1" offer="$2" plan="$3"
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "az vm image terms accept --publisher ${publisher} --offer ${offer} --plan ${plan}"
    return 0
  fi
  if az vm image terms show --publisher "$publisher" --offer "$offer" --plan "$plan" \
        --query accepted -o tsv 2>/dev/null | grep -q true; then
    ok "Already accepted: ${offer}/${plan}"
  else
    az vm image terms accept --publisher "$publisher" --offer "$offer" --plan "$plan" >/dev/null
    ok "Accepted: ${offer}/${plan}"
  fi
}

accept_terms "$CLMS_PUBLISHER" "$CLMS_OFFER" "$CLMS_PLAN"
if [[ "$DEPLOY_VPB" == "true" ]]; then
  accept_terms "$VPB_PUBLISHER" "$VPB_OFFER" "$VPB_PLAN"
fi

# =====================================================================
# Phase 5: Resource group creation
# =====================================================================
step "Phase 5: Resource group"

rg_exists() {
  [[ "$DRY_RUN" == "true" ]] && return 1
  az group show -n "$RESOURCE_GROUP" >/dev/null 2>&1
}

if rg_exists; then
  ok "Resource group ${RESOURCE_GROUP} already exists (reusing)"
else
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "az group create --name ${RESOURCE_GROUP} --location ${LOCATION}"
  else
    az group create --name "$RESOURCE_GROUP" --location "$LOCATION" \
      --tags "deployedBy=cloudlens-stack" >/dev/null
  fi
  CREATED_RG=true
  ok "Created resource group ${RESOURCE_GROUP} in ${LOCATION}"
fi

# =====================================================================
# Phase 6: CLMS deployment
# =====================================================================
step "Phase 6: Deploy CLMS"

clms_exists() {
  [[ "$DRY_RUN" == "true" ]] && return 1
  az vm show -g "$RESOURCE_GROUP" -n "$DEFAULT_CLMS_NAME" >/dev/null 2>&1
}

if clms_exists; then
  warn "CLMS VM '${DEFAULT_CLMS_NAME}' already exists in ${RESOURCE_GROUP}"
  read -rp "Reuse existing CLMS? [Y/n]: " yn
  yn_lc=$(to_lower "$yn")
  if [[ "$yn_lc" == "n" ]] || [[ "$yn_lc" == "no" ]]; then
    fail "Refusing to overwrite existing CLMS. Delete it first or pick a different RG."
  fi
  CLMS_PUBLIC_IP=$(run_az_capture network public-ip show \
    -g "$RESOURCE_GROUP" -n "${DEFAULT_CLMS_NAME}-pip" --query ipAddress -o tsv 2>/dev/null \
    || echo "unknown")
  ok "Reusing CLMS at ${CLMS_PUBLIC_IP}"
else
  note "Deploying ARM template: ${CLMS_TEMPLATE_URL}"
  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "az deployment group create -g ${RESOURCE_GROUP} --template-uri ${CLMS_TEMPLATE_URL} --parameters adminPassword=<hidden> vmSize=${CLMS_VM_SIZE}"
    CLMS_PUBLIC_IP="203.0.113.10"
  else
    az deployment group create \
      -g "$RESOURCE_GROUP" \
      -n "clms-stack-$(date +%s)" \
      --template-uri "$CLMS_TEMPLATE_URL" \
      --parameters \
          vmName="$DEFAULT_CLMS_NAME" \
          adminUsername="$ADMIN_USERNAME" \
          adminPassword="$ADMIN_PASSWORD" \
          vmSize="$CLMS_VM_SIZE" \
      --query 'properties.outputs' -o json > /tmp/clms-outputs.json
    CLMS_PUBLIC_IP=$(python3 -c "import json; print(json.load(open('/tmp/clms-outputs.json'))['clmsPublicIp']['value'])" 2>/dev/null || echo "unknown")
  fi
  DEPLOYED_CLMS=true
  ok "CLMS deployed at ${CLMS_PUBLIC_IP}"
fi

# =====================================================================
# Phase 7: Wait for CLMS init
# =====================================================================
step "Phase 7: Wait for CLMS initialization"

if [[ "$DRY_RUN" == "true" ]]; then
  dryrun_say "would poll https://${CLMS_PUBLIC_IP}:443 every 15s for up to 17 minutes"
else
  echo "CLMS needs ~15 minutes to initialize. Web UI on 443 typically responds in 60 seconds,"
  echo "with a further 2-minute settle for backend services."
  echo

  if [[ "$CLMS_PUBLIC_IP" == "unknown" ]]; then
    warn "No public IP available, skipping wait"
  else
    deadline=$(( $(date +%s) + 17*60 ))
    while (( $(date +%s) < deadline )); do
      remaining=$(( deadline - $(date +%s) ))
      printf "\r%s seconds remaining, probing port 443..." "$remaining"
      if (echo > /dev/tcp/"${CLMS_PUBLIC_IP}"/443) >/dev/null 2>&1; then
        echo
        ok "CLMS port 443 is open"
        echo "Settling 2 more minutes for backend init..."
        sleep 120
        break
      fi
      sleep 15
    done
    echo
  fi
fi

# =====================================================================
# Phase 8: vPB deployment (optional)
# =====================================================================
if [[ "$DEPLOY_VPB" == "true" ]]; then
  step "Phase 8: Deploy vPB"

  vpb_exists() {
    [[ "$DRY_RUN" == "true" ]] && return 1
    az vm show -g "$RESOURCE_GROUP" -n "$DEFAULT_VPB_NAME" >/dev/null 2>&1
  }

  if vpb_exists; then
    warn "vPB VM already exists, reusing"
    VPB_PUBLIC_IP=$(run_az_capture network public-ip show \
      -g "$RESOURCE_GROUP" -n "${DEFAULT_VPB_NAME}-mgmt-pip" --query ipAddress -o tsv 2>/dev/null \
      || echo "unknown")
  else
    note "Deploying ARM template: ${VPB_TEMPLATE_URL}"
    if [[ "$DRY_RUN" == "true" ]]; then
      dryrun_say "az deployment group create -g ${RESOURCE_GROUP} --template-uri ${VPB_TEMPLATE_URL} --parameters adminPassword=<hidden> vmSize=${VPB_VM_SIZE}"
      VPB_PUBLIC_IP="203.0.113.20"
    else
      az deployment group create \
        -g "$RESOURCE_GROUP" \
        -n "vpb-stack-$(date +%s)" \
        --template-uri "$VPB_TEMPLATE_URL" \
        --parameters \
            vmName="$DEFAULT_VPB_NAME" \
            adminUsername="$ADMIN_USERNAME" \
            adminPassword="$ADMIN_PASSWORD" \
            vmSize="$VPB_VM_SIZE" \
        --query 'properties.outputs' -o json > /tmp/vpb-outputs.json
      VPB_PUBLIC_IP=$(python3 -c "import json; print(json.load(open('/tmp/vpb-outputs.json'))['vpbPublicIp']['value'])" 2>/dev/null || echo "unknown")
    fi
    DEPLOYED_VPB=true
    ok "vPB deployed at ${VPB_PUBLIC_IP}"
  fi

  note "vPB management SSH is reachable 10 to 15 minutes after deploy."
else
  step "Phase 8: vPB deployment (skipped)"
fi

# =====================================================================
# Phase 9: Manual project key step
# =====================================================================
step "Phase 9: Get project key from CLMS"

cat <<EOM

CLMS is now reachable. To deploy sensors, you need a project key.

  1. Open the CLMS UI: https://${CLMS_PUBLIC_IP}
  2. Sign in with the default credentials: admin / Cl0udLens@dm!n
  3. You will be prompted to change the password on first login.
  4. Go to Projects -> Add Project, give it a name, then open the
     project and copy the API key.

EOM

PROJECT_KEY=""
if [[ "$CHAIN_SENSORS" == "true" ]]; then
  read -rp "Paste project key (or press Enter to skip sensor deployment): " PROJECT_KEY
  if [[ -z "$PROJECT_KEY" ]]; then
    warn "No project key supplied. Skipping sensor chain."
    CHAIN_SENSORS=false
  fi
fi

# =====================================================================
# Phase 10: Sensor chain (optional)
# =====================================================================
if [[ "$CHAIN_SENSORS" == "true" ]]; then
  step "Phase 10: Chain into sensor deployment"

  if [[ "$DRY_RUN" == "true" ]]; then
    dryrun_say "would generate customer_input.yaml and run bash quickstart.sh"
  else
    cat > customer_input.yaml <<YAML
# Auto-generated by deploy-stack.sh on $(date -u +%FT%TZ)
azure:
  subscription_id: "${SUB_ID}"
  tag_filters:
    cloudlens: "yes"
cloudlens:
  manager_ip_or_fqdn: "${CLMS_PUBLIC_IP}"
  project_key: "${PROJECT_KEY}"
  custom_tags: "DeployedBy=stack Region=${LOCATION}"
  registry_type: "insecure"
  ssl_verify: "no"
  auto_update: "yes"
connection:
  mode: "direct_public"
linux:
  ansible_user: "azureuser"
  ssh_key_file: "~/.ssh/id_rsa"
YAML
    ok "Wrote customer_input.yaml"

    if [[ -x quickstart.sh ]]; then
      ok "Launching quickstart.sh"
      bash quickstart.sh || warn "quickstart.sh exited non-zero (see ${LOG_FILE})"
    else
      warn "quickstart.sh not found in $(pwd); skipping. Run from repo root to chain."
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
CloudLens Stack Deployment Summary
Generated: $(date -u +%FT%TZ)
========================================================================

Subscription:       ${SUB_NAME} (${SUB_ID})
Resource group:     ${RESOURCE_GROUP}
Region:             ${LOCATION}

--- CLMS ---
Name:               ${DEFAULT_CLMS_NAME}
Public IP:          ${CLMS_PUBLIC_IP}
Web UI:             https://${CLMS_PUBLIC_IP}
SSH:                ssh ${ADMIN_USERNAME}@${CLMS_PUBLIC_IP}
Default UI creds:   admin / Cl0udLens@dm!n  (change on first login)
OS-level user:      ${ADMIN_USERNAME}
OS-level password:  ${ADMIN_PASSWORD}

SUMMARY

  if [[ "$DEPLOY_VPB" == "true" ]]; then
    cat <<VSUMMARY
--- vPB ---
Name:               ${DEFAULT_VPB_NAME}
Public IP:          ${VPB_PUBLIC_IP}
Management SSH:     ssh ${ADMIN_USERNAME}@${VPB_PUBLIC_IP}
vPB CLI (two-hop):  ssh ${ADMIN_USERNAME}@${VPB_PUBLIC_IP}
                    then: ssh admin@localhost -p 2222 (password: ixia)
Note:               SSH is reachable 10 to 15 minutes after deploy

VSUMMARY
  fi

  cat <<EOM
--- Next steps ---
1. Open https://${CLMS_PUBLIC_IP} and change the default password
2. Create a project in the CLMS UI and copy the project key
3. To deploy sensors later:
     curl -sSL ${REPO_RAW}/quickstart.sh | bash

--- Cleanup ---
Delete everything: az group delete -n ${RESOURCE_GROUP} --yes --no-wait

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
echo "CLMS UI:             https://${CLMS_PUBLIC_IP}"
[[ "$DEPLOY_VPB" == "true" ]] && echo "vPB management:      ${VPB_PUBLIC_IP}"
echo
ok "Done."
trap - ERR
exit 0
