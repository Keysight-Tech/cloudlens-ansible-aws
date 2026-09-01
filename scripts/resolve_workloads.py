#!/usr/bin/env python3
"""
Resolve the customer's workload selection into the concrete plan both tapping
paths execute: which instances get a SENSOR, and which interfaces get a
MIRROR session, per tapped VPC.

Why this exists
---------------
Sensors discovered hosts from customer_input.yaml (tags ANDed, explicit ids,
VPC confinement) while the mirror path sent KVO one tag. Two answers to one
question. This script computes the single answer, from the same
workload_selection module the inventory renderer uses, and writes it to a
manifest that deploy-stack.sh and kvo_aws_mirror.py both consume.

It also answers, per instance, the question AWS answers silently at session
time: CAN this be mirrored at all? AWS VPC Traffic Mirroring requires Nitro
instances, and a xen-hypervisor source is skipped with no error anywhere. The
split comes from describe-instance-types (describe-instances reports the
misleading legacy "xen" for everything). Non-mirrorable hosts are the sensor
path's job, and the manifest says so by name.

Selector semantics (what KVO is told):
  - exactly one tag, no exclusions, one VPC -> the tag form, proven live.
  - anything else (ANDed tags, explicit ids, exclusions, non-Nitro pruning)
    -> ONE instance-id selector over the literal resolved ids, anchored
    alternation. The AND is resolved HERE, against AWS, because how KVO
    combines multiple selector rows is not documented; a literal id list is
    correct under either reading.

Usage:
  python3 scripts/resolve_workloads.py --region us-east-1 \
      [--input customer_input.yaml] [--out inventory/generated.workloads.json] \
      [--source-vpc-id vpc-...]... [--exclude-instance-id i-...]... \
      [--fallback-tag cloudlens=yes]

Exit codes: 0 ok (even when zero hosts match: the manifest says so),
2 bad input, 4 AWS unreachable.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from workload_selection import (as_list, build_selection, kvo_selector,
                                load_aws_block, to_cli_filters)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(msg):
    sys.stderr.write("[resolve-workloads] %s\n" % msg)


def aws_json(args_list, region, timeout=120):
    cmd = ["aws", "ec2"] + args_list + ["--region", region, "--output", "json"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        log("aws CLI failed: %s" % e)
        return None
    if out.returncode != 0:
        log("aws %s failed: %s" % (args_list[0], (out.stderr or "").strip()[:200]))
        return None
    try:
        return json.loads(out.stdout or "null")
    except ValueError:
        return None


def nitro_split(types, region):
    """instance type -> hypervisor, from describe-instance-types.

    Chunked at 100, and a type the API refuses (the whole call errors on one
    unknown type) degrades to per-type lookups rather than an empty answer.
    Unknown stays unknown: the caller treats it as its own bucket, never as
    'mirrorable'.
    """
    hyp = {}
    todo = sorted(set(types))
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        d = aws_json(["describe-instance-types", "--instance-types"] + chunk, region)
        if d is None and len(chunk) > 1:
            for t in chunk:
                d1 = aws_json(["describe-instance-types", "--instance-types", t], region)
                for row in (d1 or {}).get("InstanceTypes", []):
                    hyp[row["InstanceType"]] = row.get("Hypervisor", "")
            continue
        for row in (d or {}).get("InstanceTypes", []):
            hyp[row["InstanceType"]] = row.get("Hypervisor", "")
    return hyp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--input", default=os.path.join(REPO_ROOT, "customer_input.yaml"))
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "inventory",
                                                  "generated.workloads.json"))
    ap.add_argument("--source-vpc-id", action="append", default=[],
                    help="VPC to tap with mirroring (repeatable). Overrides "
                         "aws.source_vpc_ids from the input file.")
    ap.add_argument("--exclude-instance-id", action="append", default=[],
                    help="leave this instance alone on both paths (repeatable; "
                         "adds to aws.tapping.exclude_instance_ids)")
    ap.add_argument("--fallback-tag", default="cloudlens=yes",
                    help="selection used when the input file has no aws filters "
                         "at all (the deploy convention), key=value")
    ap.add_argument("--stamp", default="", help="timestamp recorded in the manifest")
    args = ap.parse_args()

    aws_block = load_aws_block(args.input)
    # The same base the sensor path derives from: inventory/aws_ec2.yaml. This
    # keeps 'no customer filters' meaning THE SAME THING on both paths (the
    # checked-in tag plus instance-state-name=running), instead of the mirror
    # falling back to a hardcoded tag that can drift from the file.
    base_filters = {"instance-state-name": "running"}
    try:
        import yaml as _yaml
        with open(os.path.join(REPO_ROOT, "inventory", "aws_ec2.yaml")) as _fh:
            _base = _yaml.safe_load(_fh) or {}
        if isinstance(_base.get("filters"), dict):
            base_filters = dict(_base["filters"])
            base_filters.setdefault("instance-state-name", "running")
    except Exception:
        pass
    selection = build_selection(aws_block, base_filters)
    if selection["mode"] == "static":
        log("aws.inventory_file is set: the workloads are not EC2-discoverable "
            "from here, so no mirror selection can be resolved. Sensors follow "
            "the static inventory; mirroring needs tag_filters or instance_ids.")
        return 2

    if selection["mode"] == "default":
        # No customer filters: the base inventory's own tag filters apply, the
        # same set sensor discovery uses. Only when the base has none either
        # does the deploy-convention fallback tag kick in.
        if not any(k.startswith("tag:") for k in selection["filters"]):
            key, _, val = args.fallback_tag.partition("=")
            if not key:
                log("--fallback-tag must be key=value")
                return 2
            selection["filters"]["tag:%s" % key] = val or "yes"
            selection["described"] = "tag:%s=%s (fallback)" % (key, val or "yes")
        selection["mode"] = "tags"
        selection["filters"].setdefault("instance-state-name", "running")

    exclude = sorted(set(selection["exclude_ids"]) | set(args.exclude_instance_id))
    source_vpcs = args.source_vpc_id or selection["source_vpc_ids"]

    # ONE describe-instances over the union scope: the file's confinement
    # (which already follows source_vpc_ids) plus any --source-vpc-id
    # overrides, then each view filters its own subset.
    query_filters = dict(selection["filters"])
    conf = query_filters.pop("vpc-id", None)
    confinement = conf if isinstance(conf, list) else ([conf] if conf else [])
    scope_vpcs = sorted(set(confinement + source_vpcs))
    # Explicit instance_ids mean 'exactly these hosts', wherever they live:
    # the sensor path deliberately skips VPC confinement for them, so the
    # query must too, or the two paths diverge. Ids outside the tapped VPCs
    # are then REPORTED below instead of silently vanishing.
    extra = None
    if scope_vpcs and selection["mode"] != "instance-ids":
        extra = {"vpc-id": scope_vpcs}
    # JSON filters, not shorthand: shorthand parses a comma INSIDE a tag value
    # as two ORed values, while the aws_ec2 plugin (boto3) treats it as one
    # literal string. JSON keeps the two paths matching the same hosts.
    merged = dict(query_filters)
    if extra:
        merged.update(extra)
    filter_json = json.dumps([
        {"Name": k, "Values": list(v) if isinstance(v, (list, tuple)) else [str(v)]}
        for k, v in merged.items()])
    d = aws_json(["describe-instances", "--filters", filter_json]
                 + ["--query", "Reservations[].Instances[].{Id:InstanceId,"
                    "Type:InstanceType,Az:Placement.AvailabilityZone,Vpc:VpcId,"
                    "Platform:Platform,"
                    "Name:Tags[?Key=='Name']|[0].Value,"
                    "Enis:length(NetworkInterfaces)}"], args.region)
    if d is None:
        log("could not enumerate instances; is the AWS CLI authenticated?")
        return 4
    matched = [i for i in d if i["Id"] not in exclude]
    excluded_present = [i["Id"] for i in d if i["Id"] in exclude]

    hyp = nitro_split([i["Type"] for i in matched], args.region) if matched else {}
    for i in matched:
        i["Hypervisor"] = hyp.get(i["Type"], "unknown")
        i["Mirrorable"] = i["Hypervisor"] == "nitro"

    # Explicit ids: sensors install on exactly those hosts wherever they are,
    # so the sensor view is unscoped in that mode, same as the inventory.
    if selection["mode"] == "instance-ids":
        sensor_view = list(matched)
    else:
        sensor_scope = source_vpcs or confinement
        sensor_view = [i for i in matched if not sensor_scope or i["Vpc"] in sensor_scope]
    mirror_vpcs = source_vpcs or sorted({i["Vpc"] for i in matched})
    single_vpc = len(mirror_vpcs) <= 1

    per_vpc = {}
    for vpc in mirror_vpcs:
        rows = [i for i in matched if i["Vpc"] == vpc]
        mirrorable = [i for i in rows if i["Mirrorable"]]
        need_sensor = [i for i in rows if not i["Mirrorable"]]
        ids = sorted(i["Id"] for i in mirrorable)
        per_vpc[vpc] = {
            "instance_ids": ids,
            "eni_count": sum(i["Enis"] for i in mirrorable),
            "azs": sorted({i["Az"] for i in mirrorable}),
            "not_mirrorable": [{"id": i["Id"], "type": i["Type"],
                                "hypervisor": i["Hypervisor"]} for i in need_sensor],
            "selector": kvo_selector(selection, ids, single_vpc),
        }

    manifest = {
        "schema": 1,
        "generated": args.stamp,
        "region": args.region,
        "described": selection["described"],
        "mode": selection["mode"],
        "confinement_vpcs": confinement,
        "source_vpc_ids": mirror_vpcs,
        "excluded": exclude,
        "instances": [{"id": i["Id"], "name": i["Name"], "type": i["Type"],
                       "az": i["Az"], "vpc": i["Vpc"], "enis": i["Enis"],
                       "platform": i["Platform"] or "linux",
                       "hypervisor": i["Hypervisor"],
                       "mirrorable": i["Mirrorable"]} for i in matched],
        "sensor_instance_ids": sorted(i["Id"] for i in sensor_view),
        "mirror": {"per_vpc": per_vpc},
        "counts": {
            "matched": len(matched),
            "sensor": len(sensor_view),
            "mirrorable": sum(1 for i in matched if i["Mirrorable"]),
            "mirrorable_interfaces": sum(i["Enis"] for i in matched if i["Mirrorable"]),
        },
    }

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with open(args.out, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")

    # The integration report: one place that says, before anything is built,
    # what BOTH paths will act on and what the operator still owes.
    c = manifest["counts"]
    log("selection: %s" % selection["described"])
    log("matched %d instance(s): %d sensor-eligible, %d mirrorable "
        "(Nitro, %d interface(s) = %d licence credit(s))"
        % (c["matched"], c["sensor"], c["mirrorable"], c["mirrorable_interfaces"],
           c["mirrorable_interfaces"]))
    if excluded_present:
        log("excluded as asked: %s" % ", ".join(excluded_present))
    outside = [i for i in matched
               if source_vpcs and i["Vpc"] not in source_vpcs and i["Mirrorable"]]
    for i in outside:
        log("OUTSIDE the tapped VPC(s): %s (%s) lives in %s. Sensors still cover "
            "it; to mirror it, add --source-vpc-id %s (with collector placement "
            "in that VPC)." % (i["Id"], i["Name"] or "?", i["Vpc"], i["Vpc"]))
    for vpc, blk in per_vpc.items():
        log("  %s: %d instance(s), %d interface(s), AZ(s) %s"
            % (vpc, len(blk["instance_ids"]), blk["eni_count"],
               ", ".join(blk["azs"]) or "-"))
        for nm in blk["not_mirrorable"]:
            log("    NOT mirrorable (hypervisor %s): %s (%s) - needs a sensor"
                % (nm["hypervisor"], nm["id"], nm["type"]))
        if blk["selector"] and blk["selector"][0].get("field") == "instance-id":
            log("    selector is a point-in-time instance list (ANDed tags, "
                "exclusions or explicit ids cannot be a live tag rule). "
                "Instances launched later are NOT tapped until the next run.")
        if len(blk["selector"]) > 1:
            log("    selector chunked into %d entries (>4KB of ids); verify the "
                "session count matches %d after commit"
                % (len(blk["selector"]), blk["eni_count"]))

    # Shell-sourceable summary for deploy-stack.sh.
    print("CL_WL_PATH=%s" % args.out)
    print("CL_WL_MATCHED=%d" % c["matched"])
    print("CL_WL_MIRRORABLE=%d" % c["mirrorable"])
    print("CL_WL_INTERFACES=%d" % c["mirrorable_interfaces"])
    print("CL_WL_VPCS=%s" % " ".join(mirror_vpcs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
