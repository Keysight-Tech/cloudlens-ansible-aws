#!/usr/bin/env bash
# =====================================================================
# Sharded deployment for more than 2,000 instances
# =====================================================================
# Splits the discovered instances into shards of SHARD_SIZE and runs one
# ansible-playbook per shard in parallel. A failure in one shard does not stop
# the others, but any failed or empty shard makes the script exit non-zero.
#
# Every shard runs against the SAME inventory source with --limit, so groups
# (os_ubuntu, os_windows, ...), group_vars and the classify play stay intact.
# The previous version wrote each shard as a flat host list: every play
# matched no hosts, and each shard still printed "complete" and exited 0.
#
# Usage: bash deploy/shard.sh [COUNT_HINT] [FORKS_PER_SHARD]
#   COUNT_HINT is accepted for compatibility; the real count comes from the
#   inventory. Env: SHARD_SIZE (500), LOG_DIR (./logs/shards), PLAYBOOK
#   (deploy.yaml), INVENTORY (inventory/generated.aws_ec2.yaml when it exists,
#   else inventory/aws_ec2.yaml).
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

FORKS_PER_SHARD="${2:-200}"
SHARD_SIZE="${SHARD_SIZE:-500}"
LOG_DIR="${LOG_DIR:-./logs/shards}"
PLAYBOOK="${PLAYBOOK:-deploy.yaml}"
if [[ -z "${INVENTORY:-}" ]]; then
  INVENTORY=inventory/aws_ec2.yaml
  [[ -f inventory/generated.aws_ec2.yaml ]] && INVENTORY=inventory/generated.aws_ec2.yaml
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$LOG_DIR"

ansible-inventory -i "$INVENTORY" --list > "$WORK/inventory.json"

python3 - "$WORK" "$SHARD_SIZE" <<'PY'
import json, sys
work, size = sys.argv[1], int(sys.argv[2])
inv = json.load(open(work + "/inventory.json"))
hosts = sorted(inv.get("_meta", {}).get("hostvars", {}))
if not hosts:
    sys.exit("No instances discovered: nothing to deploy. Check the discovery filters.")
n = 0
for i in range(0, len(hosts), size):
    with open("%s/shard_%03d.limit" % (work, n), "w") as f:
        # localhost too: deploy.yaml opens with a localhost play (config and
        # vController checks) that --limit would otherwise skip.
        f.write("localhost\n" + "\n".join(hosts[i:i + size]) + "\n")
    n += 1
print("%d instances in %d shards of up to %d" % (len(hosts), n, size))
PY

pids=()
names=()
for limit in "$WORK"/shard_*.limit; do
  name="$(basename "$limit" .limit)"
  ansible-playbook -i "$INVENTORY" "$PLAYBOOK" -e "@customer_input.yaml" \
    --forks "$FORKS_PER_SHARD" --limit "@$limit" \
    > "$LOG_DIR/$name.log" 2>&1 &
  pids+=("$!")
  names+=("$name")
done
echo "Launched ${#pids[@]} shards, $FORKS_PER_SHARD forks each. Logs: $LOG_DIR/"

# kill -0 instead of ps: the container image ships no procps, and the old
# ps-based loop ended at once there.
while :; do
  running=0
  for p in "${pids[@]}"; do
    if kill -0 "$p" 2>/dev/null; then running=$((running + 1)); fi
  done
  [[ "$running" -eq 0 ]] && break
  echo "  $(date +%H:%M:%S): $running of ${#pids[@]} shards still running"
  sleep 30
done

echo
echo "SHARD SUMMARY"
bad=0
for i in "${!pids[@]}"; do
  name="${names[$i]}"
  log="$LOG_DIR/$name.log"
  if wait "${pids[$i]}"; then rc=0; else rc=$?; fi
  # grep exits 1 when nothing matches; under pipefail + set -e that would end
  # the script on a clean shard, hence || true on both counts.
  ran="$(grep -cE '^[^[:space:]].*[[:space:]]:[[:space:]]+ok=[0-9]+' "$log" || true)"
  failed="$(grep -cE 'failed=[1-9][0-9]*' "$log" || true)"
  if [[ "$rc" -ne 0 ]]; then
    echo "  $name: FAILED (rc=$rc, $failed host(s) failed), see $log"; bad=1
  elif [[ "$ran" -eq 0 ]]; then
    echo "  $name: FAILED, ran against no hosts, see $log"; bad=1
  else
    echo "  $name: ok, $ran hosts"
  fi
done
exit "$bad"
