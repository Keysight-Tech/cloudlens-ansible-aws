#!/usr/bin/env bash
# deploy/tests/test_windows_sensor.sh: when the Windows sensor is installed,
# and that the host pulls the installer from the vController.
#
# Why this exists. The Windows sensor used to be off unless an environment
# variable was set, so a run that asked for Windows test VMs and sensor
# tapping installed Linux sensors and silently skipped Windows. The default
# now follows what was asked for, and the installer is never copied from the
# machine running the deploy when the vController can serve it.
#
# Hermetic: dry runs and static checks only.
#
# Usage: bash deploy/tests/test_windows_sensor.sh
set -u
cd "$(dirname "$0")/../.."
SCRIPT="deploy/deploy-stack.sh"; S=$(mktemp -d); trap 'rm -rf "$S"' EXIT
PASS=0; FAIL=0
ok_()  { echo "PASS $*"; PASS=$((PASS+1)); }
bad_() { echo "FAIL $*"; FAIL=$((FAIL+1)); }
HERE="$PWD"
mkdir -p "$S/bin"; printf '#!/bin/sh\nexit 1\n' > "$S/bin/aws"; chmod +x "$S/bin/aws"
dry() { local out="$1"; shift
  ( cd "$S" && rm -f .cloudlens-deploy-* deploy-profile-* 2>/dev/null
    PATH="$S/bin:$PATH" /bin/bash "$HERE/$SCRIPT" --dry-run --region us-east-1 --stack-name wintest \
      --key-name k --admin-cidr 198.51.100.7/32 --no-eks --no-kvo --no-vpb "$@" </dev/null >"$out" 2>&1 ); }
line() { grep 'Windows sensor:' "$1" | head -1; }

dry "$S/a.log" --tapping sensors --test-vms ubuntu:1,windows:1
case "$(line "$S/a.log")" in *"yes"*) ok_ "sensors + Windows test VMs: the Windows sensor is on by default" ;;
  *) bad_ "sensors + Windows test VMs did not turn the Windows sensor on: $(line "$S/a.log")" ;; esac
dry "$S/b.log" --tapping sensors --test-vms ubuntu:1
case "$(line "$S/b.log")" in *"no ("*) ok_ "sensors without Windows VMs: off" ;;
  *) bad_ "no Windows VMs but: $(line "$S/b.log")" ;; esac
dry "$S/c.log" --tapping sensors --test-vms ubuntu:1,windows:1 --no-windows-sensor
case "$(line "$S/c.log")" in *"no ("*) ok_ "--no-windows-sensor wins over the default" ;;
  *) bad_ "--no-windows-sensor ignored: $(line "$S/c.log")" ;; esac
dry "$S/d.log" --tapping sensors --test-vms ubuntu:1 --windows-sensor
case "$(line "$S/d.log")" in *"yes"*) ok_ "--windows-sensor turns it on without Windows test VMs" ;;
  *) bad_ "--windows-sensor ignored: $(line "$S/d.log")" ;; esac
dry "$S/e.log" --tapping mirror --test-vms windows:1
if grep -q 'Windows sensor:' "$S/e.log"; then bad_ "a mirror-only run talks about the Windows sensor"
else ok_ "a mirror-only run does not mention the Windows sensor"; fi

grep -q 'export CLOUDLENS_WINDOWS_SENSOR="\$WINDOWS_SENSOR"' "$SCRIPT" \
  && ok_ "the decision is exported for quickstart.sh and the SSM bucket step" || bad_ "CLOUDLENS_WINDOWS_SENSOR is not exported"
grep -q 'cloudlens/static/agent-update/windows/latest/' "$SCRIPT" \
  && ok_ "the installer is discovered on the vController's static path" || bad_ "installer discovery path missing"
grep -q 'local host="\${CLMS_PRIVATE_IP:-\$CLMS_PUBLIC_IP}"' "$SCRIPT" \
  && ok_ "the download URL uses the private address the sensor registers on" || bad_ "download URL does not prefer the private address"
P=playbooks/windows.yaml
l_get="$(grep -n 'win_get_url:' "$P" | head -1 | cut -d: -f1)"
l_copy="$(grep -n 'Transfer CloudLens installer from a local file' "$P" | head -1 | cut -d: -f1)"
[[ -n "$l_get" && -n "$l_copy" && "$l_get" -lt "$l_copy" ]] \
  && ok_ "the playbook downloads on the host first; the local copy is the fallback" \
  || bad_ "playbook order: download line ${l_get:-none}, local copy line ${l_copy:-none}"
grep -A5 'Transfer CloudLens installer from a local file' "$P" | grep -q 'cloudlens_installer_url | length == 0' \
  && ok_ "the local copy runs only when there is no URL" || bad_ "the local copy is not conditional on a missing URL"
W=scripts/deploy-test-workload-vms.sh
grep -q 'UBUNTU_NAME\|RHEL_NAME\|WIN_NAME' "$W" && bad_ "the workload script still prints variables that are never set" \
  || ok_ "the workload script prints counts, not unset names"

echo; echo "${PASS} PASS, ${FAIL} FAIL"; [[ "$FAIL" -eq 0 ]]
