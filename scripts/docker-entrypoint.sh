#!/usr/bin/env bash
# Docker entrypoint: routes a mode to the right action.
#
# Its other job is to fail loudly. The image used to print "Deploying to 0 VMs"
# and exit 0 when the AWS credentials were wrong or no instance matched, so a
# CI job went green having deployed nothing. Every path that would deploy to
# nothing now stops with a non-zero exit and says why.
set -euo pipefail
cd /work

MOUNT_HINT='-v "$(pwd)/customer_input.yaml:/work/customer_input.yaml:ro"'

die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '==> %s\n' "$*"; }

# customer_input.yaml value by dotted path, empty when absent.
ci_get() {
  python3 - "$1" <<'PY'
import sys, yaml
d = yaml.safe_load(open("customer_input.yaml")) or {}
for k in sys.argv[1].split("."):
    d = d.get(k) if isinstance(d, dict) else None
print("" if d is None else d)
PY
}

require_customer_input() {
  if [[ -d customer_input.yaml ]]; then
    die "customer_input.yaml is a directory, not your file. Docker creates an empty folder when the file you mount does not exist. Delete that folder, cd to the folder that holds customer_input.yaml, and mount it with $MOUNT_HINT"
  fi
  [[ -f customer_input.yaml ]] || die "Mount customer_input.yaml: $MOUNT_HINT"
}

require_aws_credentials() {
  if [[ -z "${AWS_PROFILE:-}" \
     && ( -z "${AWS_ACCESS_KEY_ID:-}" || -z "${AWS_SECRET_ACCESS_KEY:-}" ) \
     && ! -f "$HOME/.aws/credentials" ]]; then
    die "Set AWS credentials: -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY (plus -e AWS_SESSION_TOKEN for temporary ones), or -e AWS_PROFILE with -v \$HOME/.aws:/root/.aws:ro."
  fi
}

# SSH from inside the container must not read the host's ~/.ssh/config when a
# whole ~/.ssh is mounted: macOS-only options (UseKeychain) abort a Linux ssh,
# and on a Linux host the file keeps the host uid, which ssh refuses as root
# ("Bad owner or permissions"). Either way every Linux instance went
# UNREACHABLE. ssh_args from ansible.cfg are kept; only the config is dropped.
setup_ssh() {
  [[ -n "${ANSIBLE_SSH_ARGS:-}" ]] && return 0
  local base
  base="$(python3 -c "import configparser; c = configparser.ConfigParser(interpolation=None); c.read('ansible.cfg'); print(c.get('ssh_connection', 'ssh_args', fallback='-C -o ControlMaster=auto -o ControlPersist=60s'))")"
  export ANSIBLE_SSH_ARGS="-F /dev/null $base"
  if [[ -f "$HOME/.ssh/config" ]]; then
    note "Ignoring the mounted ~/.ssh/config inside the container; keys are used as configured in customer_input.yaml"
  fi
}

# Keep a log on the host when the customer mounts a folder for it.
setup_logs() {
  if [[ -d /work/logs && -w /work/logs ]]; then
    export ANSIBLE_LOG_PATH="${ANSIBLE_LOG_PATH:-/work/logs/ansible.log}"
  elif [[ ! -w /work ]]; then
    export ANSIBLE_LOG_PATH="${ANSIBLE_LOG_PATH:-/tmp/ansible.log}"
  fi
}

# The same inventory quickstart.sh uses: inventory/aws_ec2.yaml with the
# discovery settings from customer_input.yaml (aws.tag_filters, regions, ...)
# applied by scripts/render_inventory.py. The image used to read
# inventory/aws_ec2.yaml directly and ignore customer_input.yaml. The copy
# lives in /tmp so a non-root user can write it; group_vars/ comes along
# because Ansible only loads it from beside the inventory file.
render_inventory() {
  local dir=/tmp/cloudlens-inventory summary
  rm -rf "$dir"
  cp -r inventory "$dir"
  rm -f "$dir"/generated.*
  summary="$(python3 scripts/render_inventory.py --input customer_input.yaml \
      --base inventory/aws_ec2.yaml --out "$dir/generated.aws_ec2.yaml")" \
    || die "customer_input.yaml could not be turned into an inventory (see above)."
  eval "$summary"
  INVENTORY="${CL_INV_PATH:-$dir/aws_ec2.yaml}"
  export INVENTORY
  note "Discovery: ${CL_INV_MODE:-tags} ${CL_INV_FILTERS:-} in ${CL_INV_REGIONS:-the default region}"
}

# Sets VM_COUNT. An inventory that fails to parse is an error
# (ANSIBLE_INVENTORY_UNPARSED_FAILED is set in the image), and so is finding
# no instance. Every discovered instance counts: classify.yaml sorts the
# untagged ones by OS during the run.
count_vms() {
  local out err
  out="$(mktemp)"; err="$(mktemp)"
  if ! ansible-inventory -i "$INVENTORY" --list >"$out" 2>"$err"; then
    grep -v '^\s*$' "$err" | tail -8 >&2
    rm -f "$out" "$err"
    die "AWS discovery failed. Check the credentials, the region and that they allow ec2:DescribeInstances."
  fi
  rm -f "$err"
  VM_COUNT="$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1])).get('_meta', {}).get('hostvars', {})))" "$out")"
  rm -f "$out"
  note "Discovered $VM_COUNT instances"
  if [[ "$VM_COUNT" == "0" ]]; then
    die "No instance matched ${CL_INV_FILTERS:-the discovery filters} in ${CL_INV_REGIONS:-the region}. Tag each instance cloudlens=yes, or set aws.tag_filters / aws.regions in customer_input.yaml."
  fi
}

# ANSIBLE_FORKS wins, then deploy.forks from customer_input.yaml, then a size
# based default. A --forks flag outranks ANSIBLE_FORKS inside Ansible, so the
# override has to be applied here or it silently does nothing.
pick_forks() {
  local f="${ANSIBLE_FORKS:-}"
  if [[ -z "$f" ]]; then
    f="$(ci_get deploy.forks)"
    [[ "$f" =~ ^[0-9]+$ && "$f" -gt 0 ]] || f=""
  fi
  if [[ -z "$f" ]]; then
    if   (( VM_COUNT <= 50 ));   then f=20
    elif (( VM_COUNT <= 500 ));  then f=50
    elif (( VM_COUNT <= 2000 )); then f=200
    else                              f=500
    fi
  fi
  FORKS="$f"
}

setup_ssh
setup_logs

mode="${1:-deploy}"
case "$mode" in
  deploy)
    require_customer_input
    require_aws_credentials
    render_inventory
    count_vms
    pick_forks
    if (( VM_COUNT > 2000 )); then
      shard_size="$(ci_get deploy.shard_size)"
      [[ "$shard_size" =~ ^[0-9]+$ && "$shard_size" -gt 0 ]] || shard_size=500
      note "More than 2000 instances: deploying in shards of $shard_size with $FORKS forks each"
      SHARD_SIZE="${SHARD_SIZE:-$shard_size}" exec bash deploy/shard.sh "$VM_COUNT" "$FORKS"
    fi
    note "Deploying to $VM_COUNT instances with $FORKS forks"
    exec ansible-playbook -i "$INVENTORY" deploy.yaml -e "@customer_input.yaml" --forks "$FORKS"
    ;;

  cleanup)
    require_customer_input
    require_aws_credentials
    render_inventory
    count_vms
    pick_forks
    exec ansible-playbook -i "$INVENTORY" cleanup.yaml -e "@customer_input.yaml" --forks "$FORKS"
    ;;

  inventory)
    if [[ -f customer_input.yaml ]]; then
      render_inventory
    else
      INVENTORY=inventory/aws_ec2.yaml
    fi
    exec ansible-inventory -i "$INVENTORY" --graph
    ;;

  shard)
    shift
    require_customer_input
    require_aws_credentials
    render_inventory
    exec bash deploy/shard.sh "$@"
    ;;

  shell)
    shift
    exec bash "$@"
    ;;

  *)
    exec "$@"
    ;;
esac
