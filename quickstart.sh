#!/usr/bin/env bash
# CloudLens Ansible for AWS - quickstart
# Detects environment, installs collections, runs deploy.yaml

set -e

C_GREEN='\033[0;32m'
C_RED='\033[0;31m'
C_YELLOW='\033[1;33m'
C_RESET='\033[0m'

ok()   { echo -e "${C_GREEN}✓${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}⚠${C_RESET} $1"; }
fail() { echo -e "${C_RED}✗${C_RESET} $1"; exit 1; }

echo "================================================================"
echo " CloudLens Ansible for AWS - Quickstart"
echo " v1.0.0 · Keysight Technologies"
echo "================================================================"

# === Detect environment ===
if [ -n "${AWS_EXECUTION_ENV:-}" ] || [ -n "${CLOUDSHELL_USER:-}" ]; then
  ok "Detected AWS CloudShell environment"
elif [ -d "$HOME/.aws" ]; then
  ok "Detected local AWS CLI configuration ($HOME/.aws)"
else
  warn "No AWS profile detected. Run 'aws configure' or 'aws sso login' first."
fi

# === Verify customer_input.yaml exists ===
if [ ! -f customer_input.yaml ]; then
  warn "customer_input.yaml not found - creating from example"
  cp customer_input.yaml.example customer_input.yaml
  fail "Edit customer_input.yaml with your values and re-run: bash quickstart.sh"
fi
ok "customer_input.yaml present"

# =====================================================================
# Preflight: everything the sensor run needs, checked in one pass
# =====================================================================
# The old version checked only ansible-playbook and boto3, failed with a bare
# "install it yourself" message, and never mentioned session-manager-plugin at
# all, which Windows-over-SSM cannot work without. On a machine that already
# had the tooling this looked fine; on a fresh laptop it failed one item at a
# time, several minutes apart.
#
# Everything is checked up front, the missing set is shown once, and installing
# it is a single yes. Windows users are expected to be in WSL or CloudShell:
# Ansible has no native Windows control node.

detect_os() {
  case "$(uname -s)" in
    Darwin) OS_FAMILY=macos ;;
    Linux)
      if   [ -f /etc/debian_version ]; then OS_FAMILY=debian
      elif [ -f /etc/redhat-release ]; then OS_FAMILY=rhel
      else OS_FAMILY=linux; fi ;;
    MINGW*|MSYS*|CYGWIN*) OS_FAMILY=windows ;;
    *) OS_FAMILY=unknown ;;
  esac
}

# pip refuses to touch system Python on Homebrew and newer Debian (PEP 668).
# --user keeps us out of the system tree; --break-system-packages is the
# documented escape hatch when even that is refused.
pip_install() {
  python3 -m pip install --user --quiet "$@" 2>/dev/null \
    || python3 -m pip install --user --quiet --break-system-packages "$@" 2>/dev/null \
    || python3 -m pip install --quiet "$@"
}

install_session_manager_plugin() {
  case "$OS_FAMILY" in
    macos)
      if command -v brew >/dev/null 2>&1; then
        brew install --cask session-manager-plugin >/dev/null 2>&1 && return 0
      fi
      curl -s "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/mac/sessionmanager-bundle.zip" -o /tmp/smp.zip \
        && unzip -qo /tmp/smp.zip -d /tmp/smp \
        && sudo /tmp/smp/sessionmanager-bundle/install \
             -i /usr/local/sessionmanagerplugin -b /usr/local/bin/session-manager-plugin ;;
    debian)
      curl -s "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" -o /tmp/smp.deb \
        && sudo dpkg -i /tmp/smp.deb >/dev/null 2>&1 ;;
    rhel)
      sudo yum install -y -q "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/linux_64bit/session-manager-plugin.rpm" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

install_aws_cli() {
  case "$OS_FAMILY" in
    macos)
      command -v brew >/dev/null 2>&1 && { brew install awscli >/dev/null 2>&1 && return 0; }
      curl -s "https://awscliv2.amazonaws.com/AWSCLIV2.pkg" -o /tmp/awscli.pkg \
        && sudo installer -pkg /tmp/awscli.pkg -target / >/dev/null 2>&1 ;;
    debian|rhel|linux)
      arch=$(uname -m); url="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
      [ "$arch" = "aarch64" ] && url="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"
      curl -s "$url" -o /tmp/awscliv2.zip && unzip -qo /tmp/awscliv2.zip -d /tmp \
        && sudo /tmp/aws/install --update >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

detect_os
if [ "$OS_FAMILY" = "windows" ]; then
  fail "Ansible cannot run natively on Windows. Use WSL (wsl --install), AWS CloudShell, or a Linux jumpbox, then re-run this script there."
fi

echo ""
echo "→ Checking prerequisites ($OS_FAMILY)..."

MISSING_BIN=""      # things installed by package manager / installer
MISSING_PIP=""      # python packages

command -v python3            >/dev/null 2>&1 || MISSING_BIN="$MISSING_BIN python3"
command -v aws                >/dev/null 2>&1 || MISSING_BIN="$MISSING_BIN awscli"
command -v ansible-playbook   >/dev/null 2>&1 || MISSING_PIP="$MISSING_PIP ansible"
python3 -c "import boto3"     >/dev/null 2>&1 || MISSING_PIP="$MISSING_PIP boto3"
python3 -c "import botocore"  >/dev/null 2>&1 || MISSING_PIP="$MISSING_PIP botocore"
# PyYAML is a hard dependency of ansible-core, so it is present on every
# machine that can run this at all. It is checked anyway because the inventory
# renderer needs it before the first playbook ever runs, and "ImportError:
# yaml" from a python script is a worse message than a named prerequisite.
python3 -c "import yaml"      >/dev/null 2>&1 || MISSING_PIP="$MISSING_PIP pyyaml"

# Only needed for the Windows sensor path, and which one depends on the
# connection mode chosen in customer_input.yaml.
WIN_MODE=$(grep -E "^\s*windows_connection:" customer_input.yaml 2>/dev/null | awk -F'"' '{print $2}')
WIN_MODE=${WIN_MODE:-ssm}
if [ "$WIN_MODE" = "ssm" ]; then
  command -v session-manager-plugin >/dev/null 2>&1 || MISSING_BIN="$MISSING_BIN session-manager-plugin"
else
  python3 -c "import winrm" >/dev/null 2>&1 || MISSING_PIP="$MISSING_PIP pywinrm"
fi

if [ -n "$MISSING_BIN$MISSING_PIP" ]; then
  warn "Missing prerequisites:"
  for m in $MISSING_BIN $MISSING_PIP; do echo "    - $m"; done
  echo ""
  # Consent must be explicit. An earlier version fell back to "yes" when
  # /dev/tty was unavailable, which meant a piped or CI run silently installed
  # packages and ran sudo without anyone agreeing to it.
  if [ "${CLOUDLENS_ASSUME_YES:-}" = "true" ]; then
    yn=y
    echo "Install them now? [Y/n]: y   (CLOUDLENS_ASSUME_YES=true)"
  elif { : </dev/tty; } 2>/dev/null; then
    # The device node can exist yet not be openable (piped/CI shells), so probe
    # it rather than trusting a -c/-r test.
    printf "Install them now? [Y/n]: "
    read -r yn </dev/tty 2>/dev/null || yn=n
  elif [ -t 0 ]; then
    printf "Install them now? [Y/n]: "
    read -r yn || yn=n
  else
    echo ""
    fail "Missing prerequisites and no terminal to ask for consent.
    Install them yourself, or re-run with CLOUDLENS_ASSUME_YES=true to let this
    script install them (it uses sudo for the AWS CLI and session-manager-plugin)."
  fi
  case "${yn:-y}" in
    [Nn]*) fail "Cannot continue without: $MISSING_BIN $MISSING_PIP" ;;
  esac

  for m in $MISSING_BIN; do
    echo "  installing $m..."
    case "$m" in
      awscli)                 install_aws_cli ;;
      session-manager-plugin) install_session_manager_plugin ;;
      python3)
        case "$OS_FAMILY" in
          macos)  command -v brew >/dev/null 2>&1 && brew install python >/dev/null 2>&1 ;;
          debian) sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip ;;
          rhel)   sudo yum install -y -q python3 python3-pip ;;
        esac ;;
    esac
    command -v "$m" >/dev/null 2>&1 || command -v "${m%cli}" >/dev/null 2>&1 \
      || warn "  $m still not on PATH - you may need to open a new shell"
  done

  if [ -n "$MISSING_PIP" ]; then
    echo "  installing python packages:$MISSING_PIP"
    # shellcheck disable=SC2086
    pip_install $MISSING_PIP || fail "pip install failed for:$MISSING_PIP"
    # pip --user installs land outside PATH on some systems
    export PATH="$PATH:$HOME/.local/bin:$(python3 -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))' 2>/dev/null)"
  fi
  ok "Prerequisites installed"
fi

command -v ansible-playbook >/dev/null 2>&1 \
  || fail "ansible-playbook still not on PATH. Open a new shell and re-run, or add ~/.local/bin to PATH."
ok "Ansible $(ansible --version | head -1 | awk '{print $2}')"
ok "boto3 $(python3 -c 'import boto3; print(boto3.__version__)' 2>/dev/null)"
[ "$WIN_MODE" = "ssm" ] && command -v session-manager-plugin >/dev/null 2>&1 \
  && ok "session-manager-plugin present (Windows over SSM)"

# === Install required Ansible collections ===
echo ""
echo "→ Installing required Ansible collections..."
ansible-galaxy collection install -r requirements.yml --force >/dev/null 2>&1

# Fix the community.aws aws_ssm str/bytes crash on Windows-over-SSM. Some builds
# pass a BYTES marker to str.find()/str.rfind() in the SSM session reader, which
# aborts the Windows play at "Gathering Facts" with:
#   Task failed: find() argument 1 must be str, not bytes
# The collection is git-ignored and reinstalled from Galaxy on every run, so the
# fix cannot be vendored -- patch whatever version just got installed. The change
# only coerces a .find()/.rfind() argument to str when it is bytes; a no-op on
# already-fixed builds. Windows still works agentlessly via the mirror regardless.
python3 - <<'PYEOF' >/dev/null 2>&1 || true
import glob, os, re
bases = [os.path.expanduser("~/.ansible/collections"),
         os.path.join(os.getcwd(), "collections")]
pat = re.compile(r'\b([A-Za-z_]\w*)\.(r?find)\((\w+)\)')
def fix(m):
    obj, fn, arg = m.groups()
    return f'{obj}.{fn}({arg}.decode("surrogateescape") if isinstance({arg},(bytes,bytearray)) else {arg})'
n = 0
for base in bases:
    if not os.path.isdir(base):
        continue
    for p in glob.glob(os.path.join(base, "ansible_collections/community/aws/plugins/**/*.py"), recursive=True):
        try:
            src = open(p, encoding="utf-8").read()
        except Exception:
            continue
        new = pat.sub(fix, src)
        if new != src:
            open(p, "w", encoding="utf-8").write(new); n += 1
print("aws_ssm str/bytes patch applied to %d file(s)" % n)
PYEOF
ok "Collections installed (amazon.aws, community.aws, ansible.windows)"

# === Verify AWS auth works ===
echo ""
echo "→ Verifying AWS credentials..."
if aws sts get-caller-identity >/dev/null 2>&1; then
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
  ok "AWS auth verified - account $ACCOUNT"
else
  fail "AWS credentials invalid. Run: aws sso login   OR   aws configure"
fi

# =====================================================================
# Build the inventory discovery will actually use
# =====================================================================
# The tag and region an operator sets in customer_input.yaml (or passes to
# deploy-stack.sh, which writes them there) used to be ignored: discovery ran
# against inventory/aws_ec2.yaml, which hard-coded tag:cloudlens=yes and had
# regions commented out. A customer tagged Environment=prod got zero hosts, no
# error, and flags that appeared to do nothing.
#
# render_inventory.py merges customer_input.yaml onto the checked-in
# inventory/aws_ec2.yaml and writes inventory/generated.aws_ec2.yaml. Nobody
# hand-edits a file a script also writes, which is how the mismatch happened.
echo ""
echo "→ Building inventory from customer_input.yaml..."
[ -f scripts/render_inventory.py ] \
  || fail "scripts/render_inventory.py is missing. Run this from a checkout of the
    repository: git clone https://github.com/Keysight-Tech/cloudlens-ansible-aws
    && cd cloudlens-ansible-aws && bash quickstart.sh"
INV_SUMMARY=$(python3 scripts/render_inventory.py) \
  || fail "Could not build the inventory from customer_input.yaml. The error above says why."
eval "$INV_SUMMARY"
INVENTORY="${CL_INV_PATH:-inventory/aws_ec2.yaml}"

case "$CL_INV_MODE" in
  static)
    ok "Using the existing inventory you pointed at: $INVENTORY (AWS discovery skipped)"
    warn "Connection settings in inventory/group_vars do NOT apply to an external"
    warn "inventory file. Put ansible_user, ansible_ssh_private_key_file and"
    warn "ansible_connection in that file, or in a group_vars dir beside it."
    ;;
  instance-ids)
    ok "Discovery restricted to the instance ids in customer_input.yaml: $CL_INV_FILTERS"
    ;;
  *)
    if [ "$CL_INV_DEFAULT" = "true" ]; then
      ok "Discovery filters: $CL_INV_FILTERS (repository default)"
    else
      ok "Discovery filters: $CL_INV_FILTERS"
    fi
    ;;
esac
if [ -n "$CL_INV_REGIONS" ]; then
  ok "Regions: $CL_INV_REGIONS"
elif [ "$CL_INV_MODE" != "static" ]; then
  ok "Regions: every enabled region (set aws.regions in customer_input.yaml to narrow it)"
fi

# =====================================================================
# Zero-host diagnostics
# =====================================================================
# "Found 0 hosts" with no detail is what made the tag mismatch invisible for
# so long. On an empty result, say exactly what was searched and whether any
# running instances exist at all: instances present but unmatched means the
# tag is wrong, none present means the region or credentials are.
diagnose_zero_hosts() {
  DIAG_REGIONS="$CL_INV_REGIONS"
  DIAG_SCOPE_NOTE=""
  if [ -z "$DIAG_REGIONS" ]; then
    DIAG_REGIONS=$(aws configure get region 2>/dev/null || true)
    DIAG_REGIONS=${DIAG_REGIONS:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}
    DIAG_SCOPE_NOTE="customer_input.yaml sets no aws.regions, so every enabled region was scanned; the counts below are for $DIAG_REGIONS only"
  fi

  echo ""
  echo "  What was searched"
  echo "    inventory:  $INVENTORY"
  echo "    regions:    $DIAG_REGIONS"
  if [ -n "$DIAG_SCOPE_NOTE" ]; then
    echo "                ($DIAG_SCOPE_NOTE)"
  fi
  echo "    filters:    instance-state-name=running"
  for f in $CL_INV_FILTERS; do
    echo "                $f"
  done

  TOTAL_RUNNING=0
  echo ""
  echo "  Running instances present in those regions, ignoring every filter above"
  for r in $DIAG_REGIONS; do
    n=$(aws ec2 describe-instances --region "$r" \
          --filters Name=instance-state-name,Values=running \
          --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo 0)
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    echo "    $r: $n running instance(s)"
    TOTAL_RUNNING=$((TOTAL_RUNNING + n))
  done
  echo "    total: $TOTAL_RUNNING"

  echo ""
  if [ "$TOTAL_RUNNING" -eq 0 ]; then
    echo "  There are no running instances at all in those regions."
    echo "  So this is a region or credentials problem, not a tagging one:"
    echo "    - set aws.regions in customer_input.yaml to the region your workloads are in"
    echo "    - confirm the account: aws sts get-caller-identity"
    echo "    - confirm the instances are running, not stopped"
  else
    echo "  $TOTAL_RUNNING running instance(s) exist, but none carry every filter above."
    echo "  That means the discovery tag is wrong, not your credentials."
    echo ""
    echo "  Tags actually present on running instances (up to 25):"
    for r in $DIAG_REGIONS; do
      # Tag VALUES are printed so the operator can copy the right one into
      # tag_filters, but some tags hold secrets (cloudlens:projectKey is one
      # AWS itself writes onto collector instances), so anything that looks
      # like a credential shows its key only.
      aws ec2 describe-instances --region "$r" \
        --filters Name=instance-state-name,Values=running \
        --query 'Reservations[].Instances[].Tags[]' --output text 2>/dev/null \
        | awk 'NF>=1 {
                 k=$1; $1=""; sub(/^ /,""); v=$0; lk=tolower(k);
                 if (lk ~ /key|secret|token|password|passwd|credential/) v="<redacted>";
                 print "    tag:" k "=" v
               }' \
        | sort -u | head -25
    done
    echo ""
    echo "  Fix it either way:"
    echo "    - point customer_input.yaml at the tags you already use:"
    echo "        aws:"
    echo "          tag_filters:"
    echo "            Environment: \"prod\""
    echo "    - or name the instances outright:"
    echo "        aws:"
    echo "          instance_ids: [i-0123456789abcdef0]"
    echo "    - or tag the workloads to match what you asked for:"
    echo "        aws ec2 create-tags --resources <instance-id> \\"
    echo "            --tags Key=cloudlens,Value=yes Key=os,Value=ubuntu Key=env,Value=prod"
    echo "      (os = ubuntu | rhel | windows ; env = prod | dev | test)"
    echo "    - or skip AWS discovery entirely with an inventory you already have:"
    echo "        aws:"
    echo "          inventory_file: \"/path/to/hosts.ini\""
  fi
  echo ""
}

# === Run dynamic inventory probe ===
echo ""
if [ "$CL_INV_MODE" = "static" ]; then
  echo "→ Reading $INVENTORY..."
else
  echo "→ Probing AWS EC2 inventory..."
fi
# Count DISTINCT hosts. The old sum over every group counted each host once
# per group it landed in (aws_ec2 + os_* + platform_* + env_* + region_* +
# *_prod_vms), so one instance was reported as six and the numbers in the
# summary never matched what the customer had.
DISCOVERED=$(ansible-inventory --list -i "$INVENTORY" 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
hosts = set(d.get('_meta', {}).get('hostvars', {}))
for name, g in d.items():
    if name != '_meta' and isinstance(g, dict):
        hosts.update(g.get('hosts', []))
print(len(hosts))
" 2>/dev/null || echo "0")
ok "Discovered $DISCOVERED EC2 instance(s)"

if [ "$DISCOVERED" = "0" ]; then
  warn "Zero hosts discovered, so there is nothing to install a sensor on."
  if [ "$CL_INV_MODE" = "static" ]; then
    echo "  $INVENTORY parsed but produced no hosts. Check its contents with:"
    echo "    ansible-inventory --list -i $INVENTORY"
    echo ""
  else
    diagnose_zero_hosts
  fi
  # Running the playbook against an empty inventory installs nothing but still
  # exits 0, which previously printed a "Deploy complete" that was not true.
  fail "Nothing to deploy. Fix the discovery above and re-run."
fi

# === Run the deploy ===
echo ""
echo "→ Running deploy.yaml against $DISCOVERED instance(s)..."
if ansible-playbook deploy.yaml -i "$INVENTORY" --extra-vars "@customer_input.yaml"; then
  ok "Deploy complete on $DISCOVERED instance(s). Verify in the vController UI: https://$(grep manager_ip_or_fqdn customer_input.yaml | awk '{print $2}' | tr -d '\"')/"
else
  fail "ansible-playbook failed. The output above shows which task and host failed."
fi
