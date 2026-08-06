#!/usr/bin/env python3
"""
Render the AWS EC2 dynamic inventory that discovery will actually use, from
what the operator wrote in customer_input.yaml.

Why this exists
---------------
deploy-stack.sh accepts --discovery-tag-key/--discovery-tag-value and writes
them into customer_input.yaml as aws.tag_filters, but the thing that actually
discovered hosts was inventory/aws_ec2.yaml, which hard-coded
"tag:cloudlens: yes" and had regions commented out. The operator's tag and
region were silently ignored: a customer tagged Environment=prod got zero
hosts, no error, and two flags that appeared to do nothing.

Rather than ask the operator to hand-edit a YAML file that a script also
writes (exactly how the mismatch happened), this script DERIVES the runtime
inventory from the checked-in inventory/aws_ec2.yaml:

  inventory/aws_ec2.yaml            checked in, hand-maintained, the default
        + customer_input.yaml       what the operator asked for
        = inventory/generated.aws_ec2.yaml   what discovery runs against

Only "filters" and "regions" are ever rewritten. hostnames, keyed_groups,
groups and compose are copied through untouched, so the group naming
(os_ubuntu, os_rhel, os_windows, *_prod_vms) and the group_vars wiring keep
working and never drift from the documented file. The generated file is
written INTO inventory/ on purpose: Ansible resolves group_vars/ relative to
the inventory source, so it must sit beside inventory/group_vars.

What customer_input.yaml can set (all under "aws:"):

  regions:        list of regions to scan. Empty/absent = all enabled regions.
  tag_filters:    mapping of tag name -> value. ALL of them are applied (AND).
  instance_ids:   explicit list of instance ids. Wins over tag_filters.
  inventory_file: path to an existing Ansible inventory. Skips AWS discovery
                  entirely, for static hosts, other accounts, or non-EC2 boxes.

Usage:
  python3 scripts/render_inventory.py [--input customer_input.yaml]
                                      [--base inventory/aws_ec2.yaml]
                                      [--out inventory/generated.aws_ec2.yaml]

Writes the inventory and prints a shell-sourceable summary on stdout:

  CL_INV_PATH='inventory/generated.aws_ec2.yaml'
  CL_INV_MODE='tags'
  CL_INV_REGIONS='us-east-1'
  CL_INV_FILTERS='tag:cloudlens=yes tag:env=prod'
  CL_INV_DEFAULT='false'

Nothing secret is read or printed: only the aws: block is touched, never
cloudlens.project_key.
"""

import argparse
import os
import shlex
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - handled by the caller's preflight
    sys.stderr.write(
        "render_inventory.py needs PyYAML (it ships with Ansible).\n"
        "Install it with: python3 -m pip install --user pyyaml\n"
    )
    sys.exit(2)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HEADER = """# =====================================================================
# GENERATED FILE - DO NOT EDIT
# =====================================================================
# Written by scripts/render_inventory.py from:
#   inventory/aws_ec2.yaml   (the checked-in defaults and grouping rules)
#   customer_input.yaml      (aws.regions / aws.tag_filters / aws.instance_ids)
#
# Edit one of those two and re-run quickstart.sh. Anything you write here is
# overwritten on the next run.
#
# It lives in inventory/ so that inventory/group_vars still applies: Ansible
# resolves group_vars relative to the inventory source.
# =====================================================================
"""


def tag_value(value):
    """Render a customer_input tag value as the string EC2 will match.

    YAML turns an unquoted "yes" into the boolean True, and an unquoted 1 into
    an int. EC2 tag values are strings, so "tag:cloudlens: true" would match
    nothing. Normalise back to what a human meant.
    """
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value is None:
        return ""
    return str(value)


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v) != ""]
    return [str(value)]


def emit(pairs):
    for key, value in pairs:
        sys.stdout.write("%s=%s\n" % (key, shlex.quote(str(value))))


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--input", default=os.path.join(REPO_ROOT, "customer_input.yaml"))
    ap.add_argument("--base", default=os.path.join(REPO_ROOT, "inventory", "aws_ec2.yaml"))
    # The name is not cosmetic: amazon.aws.aws_ec2 refuses any inventory file
    # whose name does not end in aws_ec2.yml/aws_ec2.yaml, and it refuses it
    # with a WARNING, not an error, so the run continues against an empty
    # inventory. "aws_ec2.generated.yaml" silently parsed as nothing.
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "inventory", "generated.aws_ec2.yaml"))
    args = ap.parse_args()

    with open(args.base) as fh:
        base = yaml.safe_load(fh)
    if not isinstance(base, dict):
        sys.stderr.write("Base inventory %s did not parse as a mapping.\n" % args.base)
        return 2

    customer = {}
    if os.path.isfile(args.input):
        with open(args.input) as fh:
            loaded = yaml.safe_load(fh)
        if isinstance(loaded, dict):
            customer = loaded
    aws = customer.get("aws") or {}
    if not isinstance(aws, dict):
        aws = {}

    # --- Escape hatch: an inventory the operator already has -------------
    # Hosts that are not discoverable from EC2 at all: a static inventory, a
    # different account, or machines that are not EC2 instances.
    static = aws.get("inventory_file")
    if static:
        static = os.path.expanduser(str(static))
        if not os.path.isabs(static):
            static = os.path.join(REPO_ROOT, static)
        if not os.path.exists(static):
            sys.stderr.write(
                "aws.inventory_file points at %s, which does not exist.\n" % static
            )
            return 3
        emit([
            ("CL_INV_PATH", static),
            ("CL_INV_MODE", "static"),
            ("CL_INV_REGIONS", ""),
            ("CL_INV_FILTERS", ""),
            ("CL_INV_DEFAULT", "false"),
        ])
        return 0

    base_filters = dict(base.get("filters") or {})
    filters = {"instance-state-name": base_filters.get("instance-state-name", "running")}

    instance_ids = as_list(aws.get("instance_ids"))
    tag_filters = aws.get("tag_filters")
    if not isinstance(tag_filters, dict):
        tag_filters = {}

    if instance_ids:
        # Explicit ids win over tag discovery: "exactly these hosts".
        mode = "instance-ids"
        filters["instance-id"] = instance_ids
        described = " ".join(instance_ids)
    elif tag_filters:
        mode = "tags"
        described_parts = []
        for key, value in tag_filters.items():
            rendered = tag_value(value)
            filters["tag:%s" % key] = rendered
            described_parts.append("tag:%s=%s" % (key, rendered))
        described = " ".join(described_parts)
    else:
        # No customer_input.yaml, or one with no aws filters: behave exactly
        # like the checked-in default.
        mode = "tags"
        filters = dict(base_filters)
        described = " ".join(
            "%s=%s" % (k, tag_value(v)) for k, v in base_filters.items() if k.startswith("tag:")
        )

    # Confine discovery to one VPC when the deploy knows which one it built.
    #
    # Tag discovery is region-wide, and every stack tags its workloads the same
    # way by default (cloudlens=yes). With two stacks in one region that means a
    # sensor run for the second stack also finds the FIRST stack's workloads,
    # installs onto them, and re-registers them against the second stack's
    # manager. That happened live, and the identical default Name tags then
    # collapsed the duplicate hosts onto each other in this very inventory.
    #
    # aws.vpc_id is written by deploy-stack.sh. Absent (hand-written config, or
    # workloads deliberately spread across VPCs) nothing changes.
    vpc_id = aws.get("vpc_id")
    if vpc_id and mode != "instance-ids":
        filters["vpc-id"] = vpc_id
        described += " vpc-id=%s" % vpc_id

    base["filters"] = filters

    regions = as_list(aws.get("regions"))
    if regions:
        base["regions"] = regions
    else:
        base.pop("regions", None)

    # Same filters and same region scope as the checked-in file: discovery
    # will behave exactly as it did before this script existed.
    is_default = filters == base_filters and not regions

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with open(args.out, "w") as fh:
        fh.write(HEADER)
        yaml.safe_dump(base, fh, default_flow_style=False, sort_keys=False, width=10000)

    emit([
        ("CL_INV_PATH", args.out),
        ("CL_INV_MODE", mode),
        ("CL_INV_REGIONS", " ".join(regions)),
        ("CL_INV_FILTERS", described),
        ("CL_INV_DEFAULT", "true" if is_default else "false"),
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
