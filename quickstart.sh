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

# === Run dynamic inventory probe ===
echo ""
echo "→ Probing AWS EC2 inventory..."
DISCOVERED=$(ansible-inventory --list -i inventory/aws_ec2.yaml 2>/dev/null | python3 -c "import sys, json; d=json.load(sys.stdin); print(sum(len(g.get('hosts', [])) for k,g in d.items() if k not in ('_meta','all')))" 2>/dev/null || echo "0")
ok "Discovered $DISCOVERED tagged EC2 instance(s)"

if [ "$DISCOVERED" = "0" ]; then
  warn "Zero instances discovered, so there is nothing to install a sensor on."
  echo "  Tag the workloads you want monitored, then re-run this script:"
  echo "    aws ec2 create-tags --resources <instance-id> \\"
  echo "        --tags Key=cloudlens,Value=yes Key=os,Value=ubuntu Key=env,Value=prod"
  echo "  (os = ubuntu | rhel | windows ; env = prod | dev)"
  echo "  Check what is currently tagged:"
  echo "    aws ec2 describe-instances --filters Name=tag:cloudlens,Values=yes \\"
  echo "        --query 'Reservations[].Instances[].InstanceId' --output text"
  echo ""
  # Running the playbook against an empty inventory installs nothing but still
  # exits 0, which previously printed a "Deploy complete" that was not true.
  fail "Nothing to deploy. Tag at least one running instance and re-run."
fi

# === Run the deploy ===
echo ""
echo "→ Running deploy.yaml against $DISCOVERED instance(s)..."
if ansible-playbook deploy.yaml --extra-vars "@customer_input.yaml"; then
  ok "Deploy complete on $DISCOVERED instance(s). Verify in the vController UI: https://$(grep manager_ip_or_fqdn customer_input.yaml | awk '{print $2}' | tr -d '\"')/"
else
  fail "ansible-playbook failed. The output above shows which task and host failed."
fi
