#!/usr/bin/env python3
"""
Discover what there is to tap, across accounts and regions, and propose the
--source-vpc specs and collector placement for every VPC that has tagged
workloads. The operator gets a table and a ready-made profile instead of a
list of VPC and subnet ids to type.

Why this exists
---------------
deploy-stack.sh taps one VPC per --source-vpc spec, and each spec carries the
collector placement by hand: vpc:az:mgmt-subnet:ingress-subnet:egress-subnet.
That is exact, and it is also the step that stops a 100-VPC estate from being
automatable: someone has to know every VPC, every AZ and three subnets in it.
This script produces those specs from the tag, so a new VPC or a new account
is one re-run, not a support call.

What it does (read-only, describe and list calls only)
-----------------------------------------------------
For each account in scope (this one, or every active account in the AWS
Organization reachable through an assumable role) and each region:
  1. describe-instances filtered by the discovery tag, running only, grouped
     by VPC. Nitro or not comes from describe-instance-types (Hypervisor),
     because VPC Traffic Mirroring silently skips non-Nitro sources and
     describe-instances reports the legacy "xen" for everything.
  2. For each VPC with matches, pick the collector subnets:
       a. subnets tagged <role-tag>=mgmt / ingress / egress, one AZ, win;
       b. otherwise the AZ holding most tapped instances, and three distinct
          subnets in it, private ones first (MapPublicIpOnLaunch false).
     Fewer than three subnets in that AZ = placement INCOMPLETE, reported
     with what to create; the VPC is never guessed into a broken fabric.
  3. Write inventory/discovered.json, print a table, and write one deploy
     profile per account and region holding only allowlisted CLOUDLENS_* keys
     (CLOUDLENS_SOURCE_VPCS with the COMPLETE specs, the tag, the region).

Usage
-----
  python3 scripts/discover_workloads.py --regions us-east-1,us-east-2 \
      [--tag cloudlens=yes] [--accounts self|organization] \
      [--role OrganizationAccountAccessRole] [--subnet-role-tag cloudlens:role] \
      [--out inventory/discovered.json] [--profile-dir .] [--print-specs]

  --print-specs prints one complete spec per line on stdout and nothing else,
  for deploy-stack.sh to read; the table goes to stderr.

Exit codes: 0 at least one complete spec, 3 nothing tappable found,
2 bad input, 4 AWS unreachable for the calling account.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict

PROFILE_KEYS_ALLOWED = ("CLOUDLENS_REGION", "CLOUDLENS_SOURCE_VPCS",
                        "CLOUDLENS_DISCOVERY_TAG_KEY", "CLOUDLENS_DISCOVERY_TAG_VALUE")
ROLE_VALUES = ("mgmt", "ingress", "egress")


def log(msg):
    sys.stderr.write(msg + "\n")


def aws_json(args_list, region=None, env=None, timeout=120):
    """Run one aws CLI command and return its JSON, or None with the reason logged."""
    cmd = ["aws"] + args_list + ["--output", "json"]
    if region:
        cmd += ["--region", region]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        log("aws CLI failed: %s" % e)
        return None
    if out.returncode != 0:
        last = ((out.stderr or "").strip().splitlines() or [""])[-1][:200]
        log("aws %s failed: %s" % (" ".join(args_list[:2]), last))
        return None
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        log("aws %s returned something that is not JSON" % args_list[0])
        return None


def tag(tags, key):
    for t in tags or []:
        if t.get("Key") == key:
            return t.get("Value", "")
    return ""


# ------------------------------------------------------------------ accounts
def caller(env=None):
    ident = aws_json(["sts", "get-caller-identity"], env=env)
    return ident.get("Account") if ident else None


def organization_accounts(env=None):
    """Every ACTIVE account in the organization, or None when the API is refused."""
    accounts, token = [], None
    while True:
        args = ["organizations", "list-accounts"]
        if token:
            args += ["--next-token", token]
        page = aws_json(args, env=env)
        if page is None:
            return None
        accounts += [a for a in page.get("Accounts", []) if a.get("Status") == "ACTIVE"]
        token = page.get("NextToken")
        if not token:
            return accounts


def assume(account_id, role, env=None):
    """Temporary credentials for another account, as an env dict, or None."""
    arn = "arn:aws:iam::%s:role/%s" % (account_id, role)
    res = aws_json(["sts", "assume-role", "--role-arn", arn, "--role-session-name", "cloudlens-discovery",
                    "--duration-seconds", "900"], env=env)
    if not res:
        return None
    c = res.get("Credentials", {})
    e = dict(os.environ if env is None else env)
    e.update({"AWS_ACCESS_KEY_ID": c.get("AccessKeyId", ""), "AWS_SECRET_ACCESS_KEY": c.get("SecretAccessKey", ""),
              "AWS_SESSION_TOKEN": c.get("SessionToken", "")})
    e.pop("AWS_PROFILE", None)
    return e


# ------------------------------------------------------------------ discovery
def nitro_types(types, region, env):
    """instance type -> True when its hypervisor is nitro. Unknown types count as not."""
    result = {}
    types = sorted(t for t in types if t)
    for i in range(0, len(types), 100):
        chunk = types[i:i + 100]
        res = aws_json(["ec2", "describe-instance-types", "--instance-types"] + chunk, region, env)
        for it in (res or {}).get("InstanceTypes", []):
            result[it.get("InstanceType")] = (it.get("Hypervisor") == "nitro")
    return result


def tagged_instances(region, key, value, env):
    res = aws_json(["ec2", "describe-instances", "--filters",
                    "Name=tag:%s,Values=%s" % (key, value), "Name=instance-state-name,Values=running"], region, env)
    if res is None:
        return None
    out = []
    for r in res.get("Reservations", []):
        for inst in r.get("Instances", []):
            out.append({"id": inst.get("InstanceId"), "vpc": inst.get("VpcId"), "type": inst.get("InstanceType"),
                        "az": inst.get("Placement", {}).get("AvailabilityZone"), "name": tag(inst.get("Tags"), "Name"),
                        "platform": inst.get("PlatformDetails", "")})
    return out


def vpc_names(region, env):
    res = aws_json(["ec2", "describe-vpcs"], region, env) or {}
    return {v.get("VpcId"): (tag(v.get("Tags"), "Name"), v.get("CidrBlock", "")) for v in res.get("Vpcs", [])}


def subnets_of(vpc, region, env):
    res = aws_json(["ec2", "describe-subnets", "--filters", "Name=vpc-id,Values=%s" % vpc], region, env) or {}
    return [{"id": s.get("SubnetId"), "az": s.get("AvailabilityZone"), "public": bool(s.get("MapPublicIpOnLaunch")),
             "name": tag(s.get("Tags"), "Name"), "role": tag(s.get("Tags"), ROLE_TAG), "cidr": s.get("CidrBlock", "")}
            for s in res.get("Subnets", [])]


ROLE_TAG = "cloudlens:role"


def place_collector(subnets, az_counts):
    """Pick mgmt/ingress/egress subnets in one AZ. Returns (placement dict, reason)."""
    # a. explicit roles, all in one AZ
    by_role = {r: [s for s in subnets if s["role"] == r] for r in ROLE_VALUES}
    if all(by_role[r] for r in ROLE_VALUES):
        for az in sorted({s["az"] for s in by_role["mgmt"]}):
            pick = {r: next((s for s in by_role[r] if s["az"] == az), None) for r in ROLE_VALUES}
            if all(pick.values()) and len({p["id"] for p in pick.values()}) == 3:
                return ({"az": az, "mgmt": pick["mgmt"]["id"], "ingress": pick["ingress"]["id"],
                         "egress": pick["egress"]["id"], "how": "subnets tagged %s" % ROLE_TAG}, "")
        return (None, "subnets carry %s tags but not mgmt, ingress and egress in ONE availability zone" % ROLE_TAG)
    # b. the AZ with most tapped instances, three distinct subnets, private first
    if not az_counts:
        return (None, "no tapped instance reports an availability zone")
    # most tapped instances wins; a tie goes to the alphabetically first AZ
    az = sorted(az_counts, key=lambda a: (-az_counts[a], a))[0]
    cands = sorted((s for s in subnets if s["az"] == az), key=lambda s: (s["public"], s["id"]))
    if len(cands) < 3:
        return (None, "only %d subnet(s) in %s; the collector needs three distinct subnets in one AZ "
                      "(create two more, or tag three existing ones %s=mgmt/ingress/egress)" % (len(cands), az, ROLE_TAG))
    return ({"az": az, "mgmt": cands[0]["id"], "ingress": cands[1]["id"], "egress": cands[2]["id"],
             "how": "AZ with most tapped instances; private subnets first"}, "")


def discover_region(region, key, value, env):
    insts = tagged_instances(region, key, value, env)
    if insts is None:
        return None
    if not insts:
        return {"region": region, "vpcs": []}
    nitro = nitro_types({i["type"] for i in insts}, region, env)
    names = vpc_names(region, env)
    by_vpc = defaultdict(list)
    for i in insts:
        by_vpc[i["vpc"]].append(i)
    vpcs = []
    for vpc, members in sorted(by_vpc.items()):
        az_counts = Counter(m["az"] for m in members if m["az"])
        subs = subnets_of(vpc, region, env)
        placement, reason = place_collector(subs, az_counts)
        n_nitro = sum(1 for m in members if nitro.get(m["type"], False))
        entry = {"vpc_id": vpc, "name": names.get(vpc, ("", ""))[0], "cidr": names.get(vpc, ("", ""))[1],
                 "matches": len(members), "nitro": n_nitro, "non_nitro": len(members) - n_nitro,
                 "non_nitro_ids": [m["id"] for m in members if not nitro.get(m["type"], False)],
                 "azs": dict(az_counts), "placement": placement, "status": "complete" if placement else "incomplete",
                 "reason": reason}
        if placement:
            entry["spec"] = "%s:%s:%s:%s:%s" % (vpc, placement["az"], placement["mgmt"], placement["ingress"], placement["egress"])
        vpcs.append(entry)
    return {"region": region, "vpcs": vpcs}


# ------------------------------------------------------------------ output
def write_profiles(result, key, value, profile_dir):
    """One profile per account and region, complete specs only, allowlisted keys only."""
    written = []
    for acct in result["accounts"]:
        if not acct.get("reachable"):
            continue
        for reg in acct["regions"]:
            specs = [v["spec"] for v in reg["vpcs"] if v.get("spec")]
            if not specs:
                continue
            os.makedirs(profile_dir, exist_ok=True)
            path = os.path.join(profile_dir, "deploy-profile-discovered-%s-%s.env" % (acct["id"], reg["region"]))
            lines = ["# CloudLens discovery %s: account %s (%s), region %s" % (result["generated"], acct["id"], acct.get("name", ""), reg["region"]),
                     "# Replay with: deploy-stack.sh --profile %s ; every key below is on the profile allowlist." % os.path.basename(path),
                     "CLOUDLENS_REGION=%s" % reg["region"], "CLOUDLENS_DISCOVERY_TAG_KEY=%s" % key,
                     "CLOUDLENS_DISCOVERY_TAG_VALUE=%s" % value, "CLOUDLENS_SOURCE_VPCS=%s" % " ".join(specs)]
            for l in lines:
                k = l.split("=", 1)[0]
                assert l.startswith("#") or k in PROFILE_KEYS_ALLOWED, k
            with open(path, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(path, 0o600)
            written.append(path)
    return written


def print_table(result):
    log("")
    log("%-14s %-11s %-22s %-24s %5s %5s %5s  %s" % ("account", "region", "vpc", "name", "match", "nitro", "other", "collector placement"))
    for acct in result["accounts"]:
        if not acct.get("reachable"):
            log("%-14s %-11s %s" % (acct["id"], "-", "UNREACHABLE: %s" % acct.get("reason", "")))
            continue
        empty = True
        for reg in acct["regions"]:
            for v in reg["vpcs"]:
                empty = False
                where = ("%s  mgmt %s  in %s  out %s" % (v["placement"]["az"], v["placement"]["mgmt"], v["placement"]["ingress"], v["placement"]["egress"])
                         if v["placement"] else "INCOMPLETE: %s" % v["reason"])
                log("%-14s %-11s %-22s %-24s %5d %5d %5d  %s" % (acct["id"], reg["region"], v["vpc_id"], v["name"][:24], v["matches"], v["nitro"], v["non_nitro"], where))
        if empty:
            log("%-14s %-11s %s" % (acct["id"], ",".join(r["region"] for r in acct["regions"]), "no running instance carries the tag"))
    log("")


def main():
    global ROLE_TAG
    ap = argparse.ArgumentParser(description="Discover tagged workloads per account, region and VPC; propose --source-vpc specs.")
    ap.add_argument("--regions", required=True, help="comma-separated, e.g. us-east-1,us-east-2")
    ap.add_argument("--tag", default="cloudlens=yes", help="discovery tag key=value (default cloudlens=yes)")
    ap.add_argument("--accounts", choices=("self", "organization"), default="self")
    ap.add_argument("--role", default="OrganizationAccountAccessRole", help="role assumed in the other accounts")
    ap.add_argument("--subnet-role-tag", default=ROLE_TAG, help="subnet tag naming the collector role (mgmt/ingress/egress)")
    ap.add_argument("--out", default="inventory/discovered.json")
    ap.add_argument("--profile-dir", default=".")
    ap.add_argument("--print-specs", action="store_true", help="stdout = one complete spec per line, nothing else")
    a = ap.parse_args()
    if "=" not in a.tag:
        log("--tag must be key=value"); return 2
    key, value = a.tag.split("=", 1)
    if not re.match(r"^[\w.:/+@=-]+$", key) or not re.match(r"^[\w.:/+@ -]*$", value):
        log("--tag has characters AWS tags cannot carry"); return 2
    ROLE_TAG = a.subnet_role_tag
    regions = [r for r in a.regions.replace(" ", ",").split(",") if r]
    if not regions or any(not re.match(r"^[a-z]{2}(-gov)?-[a-z]+-\d$", r) for r in regions):
        log("--regions must be AWS region names"); return 2

    me = caller()
    if not me:
        log("AWS is unreachable for the calling identity (credentials expired, or no network)."); return 4
    targets = [{"id": me, "name": "this account", "env": None}]
    if a.accounts == "organization":
        listed = organization_accounts()
        if listed is None:
            log("organizations:ListAccounts refused or unavailable: scanning this account only. "
                "Run from the management account, or a delegated administrator, to scan the organization.")
        else:
            for acc in listed:
                if acc.get("Id") == me:
                    continue
                targets.append({"id": acc["Id"], "name": acc.get("Name", ""), "env": "assume"})

    result = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tag": a.tag, "regions": regions,
              "role_tag": ROLE_TAG, "accounts": []}
    for t in targets:
        env = None
        if t["env"] == "assume":
            env = assume(t["id"], a.role)
            if env is None:
                result["accounts"].append({"id": t["id"], "name": t["name"], "reachable": False,
                                           "reason": "cannot assume role %s (create it, or pass --role)" % a.role, "regions": []})
                continue
        acct = {"id": t["id"], "name": t["name"], "reachable": True, "regions": []}
        for region in regions:
            reg = discover_region(region, key, value, env)
            if reg is None:
                acct["regions"].append({"region": region, "vpcs": [], "error": "describe-instances refused or region unreachable"})
                log("%s %s: describe-instances refused or region unreachable" % (t["id"], region))
            else:
                acct["regions"].append(reg)
        result["accounts"].append(acct)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(result, f, indent=2)
    profiles = write_profiles(result, key, value, a.profile_dir)
    print_table(result)
    specs = [v["spec"] for acc in result["accounts"] for reg in acc.get("regions", []) for v in reg["vpcs"] if v.get("spec")]
    incomplete = [v for acc in result["accounts"] for reg in acc.get("regions", []) for v in reg["vpcs"] if not v.get("spec")]
    non_nitro = sum(v["non_nitro"] for acc in result["accounts"] for reg in acc.get("regions", []) for v in reg["vpcs"])
    log("%d VPC(s) ready to tap, %d need subnets, %d non-Nitro instance(s) go to the sensor path. Written: %s%s"
        % (len(specs), len(incomplete), non_nitro, a.out, (", " + ", ".join(profiles)) if profiles else ""))
    if a.print_specs:
        for s in specs:
            print(s)
    return 0 if specs else 3


if __name__ == "__main__":
    sys.exit(main())
