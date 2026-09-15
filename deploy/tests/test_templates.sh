#!/usr/bin/env bash
# Static checks on the CloudFormation templates the Launch Stack buttons serve.
#
# These run with no AWS account. They exist because a launch failure is
# expensive: CloudFormation rolls the whole stack back and deletes what it
# built, so a customer loses ten minutes and learns nothing except that it
# broke. Anything provable on a laptop should be caught here instead.
#
# Usage: bash deploy/tests/test_templates.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

PASS=0
FAIL=0
pass() { printf 'PASS %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }

TEMPLATES=(deploy/cloudformation/*.yaml)

# ---- 1. cfn-lint, errors only -------------------------------------------
# Warnings are tolerated (the existing ones are about parameters that default
# to an empty string on purpose). An E-level finding is a template AWS would
# refuse, so it fails the suite.
if command -v cfn-lint >/dev/null 2>&1; then
  for t in "${TEMPLATES[@]}"; do
    errs="$(cfn-lint "$t" 2>&1 | grep -c '^E[0-9]')"
    if [[ "$errs" == "0" ]]; then
      pass "cfn-lint: $(basename "$t") has no errors"
    else
      fail "cfn-lint: $(basename "$t") has $errs error(s)"
      cfn-lint "$t" 2>&1 | grep '^E[0-9]' | head -5
    fi
  done
else
  printf 'SKIP cfn-lint is not installed (pip install cfn-lint)\n'
fi

# ---- 2. An Elastic IP is never associated by instance id on a multi-NIC box --
# EC2 refuses "associate this address with that instance" as soon as the
# instance has more than one interface: "There are multiple interfaces attached
# to instance ... Please specify an interface ID for the operation instead."
# Nothing in CloudFormation orders an EIPAssociation against the interface
# attachments, so a template that does this is a race that rolls whole stacks
# back. Launching is the only way to see it, which is why it reached a customer.
python3 - <<'PY'
import glob, sys, yaml

class CfnLoader(yaml.SafeLoader):
    """CloudFormation YAML uses !Ref, !GetAtt, !If and friends. Their meaning
    does not matter here; keeping the argument readable does."""

def _tag(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return {"__tag__": node.tag, "value": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {"__tag__": node.tag, "value": loader.construct_sequence(node, deep=True)}
    return {"__tag__": node.tag, "value": loader.construct_mapping(node, deep=True)}

CfnLoader.add_multi_constructor("!", lambda l, s, n: _tag(l, n))

def ref_name(v):
    """The logical id a !Ref points at, or None for anything else."""
    if isinstance(v, dict):
        if v.get("__tag__") == "!Ref":
            return v.get("value")
        if list(v) == ["Ref"]:
            return v["Ref"]
    return None

bad = []
for path in sorted(glob.glob("deploy/cloudformation/*.yaml")):
    res = (yaml.load(open(path), Loader=CfnLoader) or {}).get("Resources") or {}

    # Instances that are given an extra interface by a separate attachment,
    # and instances that already name their own interfaces inline.
    multinic = set()
    for name, r in res.items():
        if r.get("Type") == "AWS::EC2::NetworkInterfaceAttachment":
            target = ref_name((r.get("Properties") or {}).get("InstanceId"))
            if target:
                multinic.add(target)
    for name, r in res.items():
        if r.get("Type") == "AWS::EC2::Instance":
            nics = (r.get("Properties") or {}).get("NetworkInterfaces") or []
            if len(nics) > 1:
                multinic.add(name)

    for name, r in res.items():
        if r.get("Type") != "AWS::EC2::EIPAssociation":
            continue
        props = r.get("Properties") or {}
        target = ref_name(props.get("InstanceId"))
        if target and target in multinic:
            bad.append("%s: %s associates by InstanceId with %s, which has more "
                       "than one interface" % (path, name, target))

if bad:
    for b in bad:
        print("  " + b)
    sys.exit(1)
PY
if [[ $? -eq 0 ]]; then
  pass "no Elastic IP is associated by instance id on a multi-NIC instance"
else
  fail "an Elastic IP is associated by instance id on a multi-NIC instance"
fi

printf '\n%d PASS, %d FAIL\n' "$PASS" "$FAIL"
[[ "$FAIL" == "0" ]]
