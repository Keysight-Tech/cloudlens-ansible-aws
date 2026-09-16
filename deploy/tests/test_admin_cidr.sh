#!/usr/bin/env bash
# deploy/tests/test_admin_cidr.sh: the interview asks who may reach the
# appliances, and refuses to accept anything that is not an IPv4 CIDR.
#
# Why this exists. ADMIN_CIDR has always had a flag and an environment form,
# but nothing asked for it, so every interactive run took the 0.0.0.0/0
# default and opened SSH to the internet. That is the first thing a corporate
# CIS scan reports (CIS 5.2) and the Launch Stack form has had the field all
# along, so the two paths disagreed.
#
# Hermetic: no AWS, no network. `ask` and `curl` are stubs per case, so the
# public-IP lookup never leaves the machine.
#
# Usage: bash deploy/tests/test_admin_cidr.sh
#        DEPLOY_STACK_SH=/path/to/deploy-stack.sh bash deploy/tests/test_admin_cidr.sh
set -u
cd "$(dirname "$0")/../.."
SCRIPT="${DEPLOY_STACK_SH:-deploy/deploy-stack.sh}"
S=$(mktemp -d)
trap 'rm -rf "$S"' EXIT

awk '
  /^(ok|warn|fail|step|note)\(\)/ { print; next }
  /^(valid_cidr|ask_admin_cidr)\(\)/ { p=1 }
  p { print }
  p && /^}/ { p=0 }
' "$SCRIPT" > "$S/helpers.sh"

PASS=0; FAIL=0
ok_()  { echo "PASS $*"; PASS=$((PASS+1)); }
bad_() { echo "FAIL $*"; FAIL=$((FAIL+1)); }

if ! grep -q '^ask_admin_cidr()' "$S/helpers.sh" || ! grep -q '^valid_cidr()' "$S/helpers.sh"; then
  echo "FAIL the interview has no admin CIDR question (ask_admin_cidr/valid_cidr missing)"
  echo; echo "0 PASS, 1 FAIL"; exit 1
fi

# run "<detected public ip or empty>" "<answer>[|<answer>...]"
# Echoes the chosen CIDR on stdout; the prompts and warnings land in $S/err.
run() {
  local detected="$1" answers="$2"
  /bin/bash -c '
    set -euo pipefail
    C_GREEN= C_YELLOW= C_BLUE= C_RED= C_GREY= C_BOLD= C_RESET=
    DETECTED="$1"; ANSWERS="$2"
    # AWS checkip, stubbed: an empty DETECTED plays the lookup failing.
    curl() { [ -n "$DETECTED" ] || return 1; printf "%s\n" "$DETECTED"; }
    # ask pops the next queued answer; an empty one means the operator pressed
    # Enter, which must yield the default the prompt offered.
    # The real ask reads the terminal afresh each call. Every call here is a
    # command substitution, so a shell variable would never advance: the queue
    # lives in a file.
    printf "%s" "$ANSWERS" > "$4"
    ask() {
      local def="${2:-}" q a
      q="$(cat "$QUEUE")"
      a="${q%%|*}"
      case "$q" in *"|"*) printf "%s" "${q#*|}" > "$QUEUE" ;; *) : > "$QUEUE" ;; esac
      printf "%s" "${a:-$def}"
    }
    QUEUE="$4"
    source "$3"
    ask_admin_cidr
  ' _ "$detected" "$answers" "$S/helpers.sh" "$S/queue" 2>"$S/err"
}

# 1. the operator types their corporate range
got="$(run "203.0.113.10" "10.0.0.0/8")"
if [ "$got" = "10.0.0.0/8" ]; then ok_ "1. a typed CIDR is used as given"; else bad_ "1. expected 10.0.0.0/8, got '$got'"; fi

# 2. the detected public address is offered as the default and Enter takes it
got="$(run "203.0.113.10" "")"
if [ "$got" = "203.0.113.10/32" ]; then ok_ "2. Enter takes this machine's address as a /32"; else bad_ "2. expected 203.0.113.10/32, got '$got'"; fi
if grep -q "203.0.113.10" "$S/err"; then ok_ "2b. the offered default is shown to the operator"; else bad_ "2b. the detected address was never shown"; fi

# 3. junk is refused and the question repeats
got="$(run "203.0.113.10" "not-a-cidr|10.1.2.0/24")"
if [ "$got" = "10.1.2.0/24" ]; then ok_ "3. an invalid answer is refused and the question repeats"; else bad_ "3. expected 10.1.2.0/24, got '$got'"; fi
if grep -q "is not an IPv4 CIDR" "$S/err"; then ok_ "3b. the operator is told why it was refused"; else bad_ "3b. no reason was given"; fi

# 4. a prefix out of range and a bad octet are both refused
got="$(run "" "10.0.0.0/33|300.1.1.1/24|172.16.0.0/12")"
if [ "$got" = "172.16.0.0/12" ]; then ok_ "4. /33 and a 300 octet are both refused"; else bad_ "4. expected 172.16.0.0/12, got '$got'"; fi

# 5. opening it to the world is allowed but warned about
got="$(run "203.0.113.10" "0.0.0.0/0")"
if [ "$got" = "0.0.0.0/0" ]; then ok_ "5. 0.0.0.0/0 is accepted when asked for"; else bad_ "5. expected 0.0.0.0/0, got '$got'"; fi
if grep -q "reachable from any address on the internet" "$S/err"; then ok_ "5b. choosing 0.0.0.0/0 warns"; else bad_ "5b. no warning for 0.0.0.0/0"; fi

# 6. no public address available: the default stays 0.0.0.0/0 and says so
got="$(run "" "")"
if [ "$got" = "0.0.0.0/0" ]; then ok_ "6. a failed lookup falls back to 0.0.0.0/0"; else bad_ "6. expected 0.0.0.0/0, got '$got'"; fi
if grep -q "could not be read" "$S/err"; then ok_ "6b. the operator is told the lookup failed"; else bad_ "6b. the failed lookup was silent"; fi

# 7. three bad answers stop the loop instead of asking forever
got="$(run "203.0.113.10" "x|y|z|10.0.0.0/8")"
if [ "$got" = "203.0.113.10/32" ]; then ok_ "7. three invalid answers fall back to the default, no endless loop"; else bad_ "7. expected the default after 3 tries, got '$got'"; fi

# 8. valid_cidr itself, the edges
edge() {
  /bin/bash -c 'set -uo pipefail; source "$1"; if valid_cidr "$2"; then echo yes; else echo no; fi' _ "$S/helpers.sh" "$1" 2>/dev/null
}
bad_edges=0
for c in "1.2.3.4" "10.0.0.0/8/8" "10.0.0/8" "" "10.0.0.0/-1" "abc/24"; do
  [ "$(edge "$c")" = "no" ] || { bad_edges=$((bad_edges+1)); echo "    ('$c' was accepted)"; }
done
for c in "0.0.0.0/0" "255.255.255.255/32" "10.0.0.0/8"; do
  [ "$(edge "$c")" = "yes" ] || { bad_edges=$((bad_edges+1)); echo "    ('$c' was refused)"; }
done
if [ "$bad_edges" -eq 0 ]; then ok_ "8. valid_cidr accepts real CIDRs and refuses malformed ones"; else bad_ "8. valid_cidr got $bad_edges edge cases wrong"; fi

echo
echo "$PASS PASS, $FAIL FAIL"
[ "$FAIL" -eq 0 ]
