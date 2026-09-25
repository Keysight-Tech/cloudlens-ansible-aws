#!/usr/bin/env bash
# Checks on the image the "Pull Docker Image" tier serves from GHCR.
#
# The image is the sensor rollout: its entrypoint runs the amazon.aws.aws_ec2
# inventory and deploy.yaml / cleanup.yaml. It shipped for two months unable
# to load a single collection: ansible.cfg pins collections_path to
# ./collections for laptop runs, the Dockerfile installed them elsewhere, and
# a local build hid the gap because the builder's own collections/ directory
# was copied in. Nothing here needs a cloud account.
#
# Static checks always run. The runtime checks need a built image:
#   IMAGE=cloudlens-ansible-aws:local bash deploy/tests/test_docker_image.sh
# REPO_DIR points the static checks at another checkout, which is how they are
# proven to go red against the version before the fix.
set -uo pipefail
cd "${REPO_DIR:-$(dirname "${BASH_SOURCE[0]}")/../..}" || exit 1

PASS=0
FAIL=0
pass() { printf 'PASS %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }
skip() { printf 'SKIP %s\n' "$1"; }

# ---- 1. The image tells Ansible where its collections are --------------------
# ansible.cfg's relative collections_path is right on a laptop and wrong in
# the image. The environment variable outranks the file.
env_path="$(grep -oE 'ANSIBLE_COLLECTIONS_PATH=[^ \\]+' Dockerfile | head -1 | cut -d= -f2)"
if [[ -n "$env_path" ]]; then
  pass "Dockerfile sets ANSIBLE_COLLECTIONS_PATH ($env_path)"
else
  fail "Dockerfile does not set ANSIBLE_COLLECTIONS_PATH, so ansible.cfg's ./collections applies and no collection loads"
fi

# ---- 2. The collections are installed into that same path -------------------
if tr '\n' ' ' < Dockerfile | grep -qE 'ansible-galaxy collection install[^&]*-p "\$ANSIBLE_COLLECTIONS_PATH"'; then
  pass "ansible-galaxy installs into \$ANSIBLE_COLLECTIONS_PATH"
else
  fail "ansible-galaxy does not install into \$ANSIBLE_COLLECTIONS_PATH"
fi

# ---- 3. A build never copies in local-only or secret files ------------------
if [[ -f .dockerignore ]]; then
  for p in .git collections/ customer_input.yaml; do
    if grep -qxF "$p" .dockerignore; then
      pass ".dockerignore excludes $p"
    else
      fail ".dockerignore does not exclude $p"
    fi
  done
else
  fail "no .dockerignore: a local build copies collections/ (hiding check 1) and customer_input.yaml (secrets) into a public image"
fi

# ---- Runtime checks against a built image ------------------------------------
IMAGE="${IMAGE:-}"
if [[ -z "$IMAGE" ]]; then
  skip "runtime checks: set IMAGE=<image ref> to run them"
elif ! docker info >/dev/null 2>&1; then
  skip "runtime checks: docker is not available"
else
  in_image() { docker run --rm --platform linux/amd64 --entrypoint bash "$IMAGE" -c "$1" 2>&1; }

  # 4. Every collection the playbooks need is visible to Ansible.
  out="$(in_image 'ansible-galaxy collection list')"
  for c in amazon.aws community.aws ansible.windows community.windows; do
    if grep -qE "^$c +[0-9]" <<<"$out"; then
      pass "image: $c is on the collections path"
    else
      fail "image: $c is not visible to Ansible"
    fi
  done

  # 5. The aws_ec2 inventory plugin loads. With dummy credentials the run must
  #    fail on AWS authentication, never on an unknown plugin.
  out="$(in_image 'AWS_ACCESS_KEY_ID=AKIAEXAMPLE AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=us-east-1 ansible-inventory -i inventory/aws_ec2.yaml --list')"
  if grep -qi "unknown plugin" <<<"$out"; then
    fail "image: amazon.aws.aws_ec2 inventory plugin does not load"
  else
    pass "image: amazon.aws.aws_ec2 inventory plugin loads"
  fi

  # 6. Both playbooks pass a syntax check, which resolves every module name.
  for pb in deploy.yaml cleanup.yaml; do
    if in_image "ansible-playbook --syntax-check -i localhost, $pb" >/dev/null; then
      pass "image: $pb passes --syntax-check"
    else
      fail "image: $pb fails --syntax-check"
    fi
  done

  # 7. The entrypoint refuses to deploy without customer_input.yaml.
  out="$(docker run --rm --platform linux/amd64 "$IMAGE" deploy 2>&1)"
  if grep -q "Mount customer_input.yaml" <<<"$out"; then
    pass "image: deploy without customer_input.yaml says what to mount"
  else
    fail "image: deploy without customer_input.yaml did not stop with the mount hint"
  fi
fi

printf '\n%d PASS, %d FAIL\n' "$PASS" "$FAIL"
[[ "$FAIL" == "0" ]]
