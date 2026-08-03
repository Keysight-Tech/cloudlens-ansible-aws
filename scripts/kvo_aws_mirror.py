#!/usr/bin/env python3
"""
Automate KVO AWS Zone Tapping (agentless AWS VPC Traffic Mirroring).

This is the agentless complement to sensor install. Instead of putting a sensor
inside each VM, KVO deploys collector Service VMs and drives AWS VPC Traffic
Mirroring so AWS copies traffic from every matching source ENI to the collectors.
When this finishes and commits, KVO creates real AWS resources in the account:
Traffic Mirror Target(s), Filter(s), and one Session per selected source ENI.
They appear under VPC > Traffic Mirroring in the console.

The KVO-side chain (all GraphQL at /public/graphql, each in a committed change
request, discovered by introspection; see
~/keysight-kb/digests/kvo-clms-adoption-api.md):

  1. createAwsPresence  {cloudLensManagerId, region, vpcId, [accessKeyId,
                         secretAccessKey], [clmsProjectId, clmsProjectApiKey]}
     -> the AWS cloud presence; KVO deploys collector SVMs here.
  2. createCloudConfig  {cloudConfigType: Aws, cloudPresence:{name},
                         awsConfiguration:{...}}
     -> plugs the presence into the Visibility Fabric (this is what actually
        provisions and starts the AWS-side work, same lesson as CustomCloud).
  3. createCloudCollection {cloudConfig:{name}, tapType: RAW,
                            resourceSelector:[{field:"tag", tag:"<k>", regex:"<v>"}]}
     -> selects which sources to mirror (here: the cloudlens=yes tag). KVO turns
        this into AWS Traffic Mirror Sessions.

PREREQUISITE: KVO must be able to call AWS. Either the KVO instance carries the
Zone Tapping instance profile (greenfield: EnableZoneTapping=yes, or brownfield:
scripts/kvo_enable_zonetap_iam.sh) and you pass NO keys, or you pass
--aws-access-key/--aws-secret-key for a user holding the same least-privilege
policy (deploy/iam/cloudlens-zonetap-policy.json). Without one of these,
createAwsPresence fails and no mirror sessions are created.

Exit codes: 0 ok, 3 KVO auth/eula, 4 not licensed, 5 presence/commit,
6 cloud config, 7 collection, 8 CLM not found/CONNECTED.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
import requests

def log(m): print(f"[kvo-mirror] {m}", file=sys.stderr, flush=True)
def _q(s): return json.dumps(s)

# The collector Service VM is Keysight's public Marketplace image
# "cloudlens-vpb-svm-*", owner 679593333241. Its AMI id differs per region, so
# resolve it at runtime instead of hardcoding: pick the newest matching image in
# the target region. Keeps the script region-agnostic for any SE/customer.
COLLECTOR_AMI_OWNER = "679593333241"
COLLECTOR_AMI_NAME = "cloudlens-vpb-svm-*"

def resolve_collector_ami(region):
    try:
        out = subprocess.run(
            ["aws", "ec2", "describe-images", "--region", region,
             "--owners", COLLECTOR_AMI_OWNER,
             "--filters", f"Name=name,Values={COLLECTOR_AMI_NAME}", "Name=state,Values=available",
             "--query", "sort_by(Images,&CreationDate)[-1].{Id:ImageId,Name:Name}", "--output", "json"],
            capture_output=True, text=True, timeout=40)
        if out.returncode != 0:
            log(f"could not resolve collector AMI: {out.stderr.strip()[:160]}"); return None
        img = json.loads(out.stdout or "null")
        if not img or not img.get("Id"):
            log(f"no cloudlens-vpb-svm AMI found in {region}"); return None
        log(f"resolved collector AMI in {region}: {img['Id']} ({img.get('Name')})")
        return img["Id"]
    except Exception as e:
        log(f"collector AMI resolution failed ({e}); pass --image-id explicitly"); return None

# ----- KVO auth (Keycloak) ----------------------------------------------
def kvo_token(base, user, password, verify):
    r = requests.post(f"{base}/auth/realms/keysight/protocol/openid-connect/token",
                      data={"grant_type": "password", "client_id": "vision-orchestrator",
                            "username": user, "password": password}, verify=verify, timeout=20)
    if r.status_code != 200:
        log(f"KVO auth failed (HTTP {r.status_code}): {r.text[:160]}"); return None
    return r.json()["access_token"]

def kvo_accept_eula(base, verify):
    r = requests.get(f"{base}/eula/v1/eula", verify=verify, timeout=20)
    if r.status_code != 200: return False
    for e in [e for e in r.json() if not e.get("accepted")]:
        requests.post(f"{base}/eula/v1/eula/{e['id']}", json={"accepted": True}, verify=verify, timeout=20)
        log(f"accepted KVO EULA {e['id']}")
    return True

def gql(base, token, query, variables, verify):
    r = requests.post(f"{base}/public/graphql", headers={"Authorization": f"Bearer {token}"},
                      json={"query": query, "variables": variables or {}}, verify=verify, timeout=40)
    try: return r.json()
    except ValueError: return {"errors": [{"message": r.text[:200]}]}

def kvo_is_licensed(base, token, verify):
    d = gql(base, token, "{ availableLicenses { name installed } }", None, verify)
    return any((l.get("installed") or 0) > 0 for l in d.get("data", {}).get("availableLicenses", [])) \
        if "errors" not in d else False

# ----- change request helpers (async commit, poll to Committed) ---------
def _open_crs(base, token, verify):
    q = gql(base, token, "{ changeRequests { uid status } }", None, verify)
    return [r for r in (q.get("data", {}).get("changeRequests") or []) if r["status"] != "Committed"]

def clear_open_crs(base, token, verify, timeout=90):
    """KVO allows one open change request per user. A failed create leaves an
    empty CR behind and blocks the next open; delete any non-Committed CR and
    wait for the (async) delete to finalize before returning."""
    open_crs = _open_crs(base, token, verify)
    for r in open_crs:
        gql(base, token, "mutation($u:String!){ deleteChangeRequest(uid:$u){ uid } }", {"u": r["uid"]}, verify)
        log(f"clearing stale open change request {r['uid']} ({r['status']})")
    if not open_crs:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _open_crs(base, token, verify):
            log("stale change requests cleared"); return
        time.sleep(4)
    log("warning: open change requests still present after cleanup")

def open_cr(base, token, name, verify):
    clear_open_crs(base, token, verify)
    d = gql(base, token, "mutation($n:String){ createChangeRequest(name:$n){ uid } }", {"n": name}, verify)
    if "errors" in d:
        log(f"createChangeRequest failed: {d['errors'][0]['message'][:180]}"); return None
    crs = d.get("data", {}).get("createChangeRequest") or []
    return crs[0]["uid"] if crs else None

def commit_cr(base, token, cr_uid, verify, timeout=600):
    # KVO serializes processing; a prior commit (e.g. collector deploy) may still
    # be running. Retry the commit call until KVO accepts it.
    deadline = time.time() + timeout
    while True:
        c = gql(base, token,
                "mutation($u:String!){ commitChangeRequest(uid:$u, ignoreWarnings:true){ uid state } }",
                {"u": cr_uid}, verify)
        if "errors" not in c:
            break
        msg = c["errors"][0]["message"]
        if "another processing is still running" in msg and time.time() < deadline:
            log("  waiting for the previous processing to finish..."); time.sleep(10); continue
        log(f"commit failed: {msg[:200]}"); return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = gql(base, token, "{ changeRequests { uid status } }", None, verify)
        mine = [r for r in (q.get("data", {}).get("changeRequests") or []) if r["uid"] == cr_uid]
        if not mine or mine[0]["status"] == "Committed":
            log("change request committed"); return True
        if mine[0]["status"] in ("Failed", "Error"):
            log(f"commit ended in {mine[0]['status']}"); return False
        time.sleep(6)
    log("timed out waiting for commit"); return False

def clm_info(base, token, name, verify):
    d = gql(base, token, "{ cloudLensManagersFeed { cloudLensManagers { uid name status } } }", None, verify)
    for r in (d.get("data", {}).get("cloudLensManagersFeed", {}) or {}).get("cloudLensManagers", []):
        if r.get("name") == name:
            return r
    return None

# ----- the three-object AWS mirroring chain -----------------------------
def create_aws_presence(base, token, cr, name, clm_uid, region, vpc_id, ak, sk, verify):
    settings = {"cloudLensManagerId": clm_uid, "region": region, "vpcId": vpc_id,
                "description": "CloudLens AWS Zone Tapping"}
    if ak and sk:
        settings["accessKeyId"] = ak; settings["secretAccessKey"] = sk
    q = ("mutation($n:String!,$c:String!,$s:_AwsPresenceInput!){ "
         "createAwsPresence(name:$n, changeID:$c, settings:$s){ uid name clmsProjectId clmsProjectApiKey } }")
    d = gql(base, token, q, {"n": name, "c": cr, "s": settings}, verify)
    if "errors" in d:
        log(f"createAwsPresence failed: {d['errors'][0]['message'][:220]}"); return None
    rows = d.get("data", {}).get("createAwsPresence") or []
    return rows[0] if rows else None

def create_aws_cloud_config(base, token, cr, name, cluster, aws_cfg, verify):
    settings = {"cloudConfigType": "Aws", "cloudPresence": {"name": name}, "awsConfiguration": aws_cfg}
    q = ("mutation($n:String!,$c:String!,$cl:String!,$s:_CloudConfigInput!){ "
         "createCloudConfig(name:$n, changeID:$c, clusterID:$cl, settings:$s){ uid name cloudConfigType } }")
    d = gql(base, token, q, {"n": name, "c": cr, "cl": cluster, "s": settings}, verify)
    if "errors" in d:
        log(f"createCloudConfig(Aws) failed: {d['errors'][0]['message'][:220]}"); return None
    rows = d.get("data", {}).get("createCloudConfig") or []
    return rows[0] if rows else None

def create_collection(base, token, cr, name, cluster, cfg_name, tag_key, tag_val, verify):
    settings = {"cloudConfig": {"name": cfg_name}, "tapType": "RAW",
                "resourceSelector": [{"field": "tag", "tag": tag_key, "regex": tag_val}]}
    q = ("mutation($n:String!,$c:String!,$cl:String!,$s:_CloudCollectionInput!){ "
         "createCloudCollection(name:$n, changeID:$c, clusterID:$cl, settings:$s){ uid name } }")
    d = gql(base, token, q, {"n": name, "c": cr, "cl": cluster, "s": settings}, verify)
    if "errors" in d:
        log(f"createCloudCollection failed: {d['errors'][0]['message'][:220]}"); return None
    rows = d.get("data", {}).get("createCloudCollection") or []
    return rows[0] if rows else None

def cluster_uid(base, token, verify):
    d = gql(base, token, "{ clusters { uid name } }", None, verify)
    rows = d.get("data", {}).get("clusters") or []
    for r in rows:
        if r.get("name") == "PredefinedCluster": return r["uid"]
    return rows[0]["uid"] if rows else None

# ----- destination tool + monitoring policy (complete the path) ---------
# The presence/config/collection define WHAT to tap; nothing moves traffic and
# KVO creates NO AWS mirror sessions until a destination Tool exists and a
# Monitoring Policy wires the collection (source) to that tool. The two
# commit-time gotchas the KVO doc (913-3373-01) names are baked in here so an SE
# cannot hit them: for a cloud remote tool, vlanStripping MUST be false
# ("Strip Path Solver VLAN" off) and reachableFrom MUST be CLOUD_CONFIG.
def create_remote_tool(base, token, cr, name, cluster, remote_ip, encap, gre_key, vni, verify):
    settings = {
        "type": "REMOTE",
        "vlanStripping": False,          # "Strip Path Solver VLAN" OFF (else commit fails for cloud)
        "reachableFrom": "CLOUD_CONFIG",  # "Traffic source is a cloud"
        "tunnelOrigRemoteIP": remote_ip,  # IP of the analyzer/NPB that receives the tapped traffic
    }
    if (encap or "L2GRE").upper() == "VXLAN":
        settings["vxlanEncapsulation"] = {"vnid": str(vni), "udpDestPort": 4789}
    else:
        settings["l2greEncapsulation"] = {"key": str(gre_key)}
    q = ("mutation($n:String!,$c:String!,$cl:String!,$s:_ToolInput!){ "
         "createTool(name:$n, changeID:$c, clusterID:$cl, settings:$s){ uid name } }")
    d = gql(base, token, q, {"n": name, "c": cr, "cl": cluster, "s": settings}, verify)
    if "errors" in d:
        log(f"createTool failed: {d['errors'][0]['message'][:220]}"); return None
    rows = d.get("data", {}).get("createTool") or []
    return rows[0] if rows else None

def create_monitoring_policy(base, token, cr, name, cluster, collection_name, tool_name, verify):
    settings = {
        "source": {"name": collection_name},
        "tools": {"name": tool_name},
        "runMode": "CONTINUOUSLY",
        "type": "REGULAR",
    }
    q = ("mutation($n:String!,$c:String!,$cl:String!,$s:_MonitoringPolicyInput!){ "
         "createMonitoringPolicy(name:$n, changeID:$c, clusterID:$cl, settings:$s){ uid name } }")
    d = gql(base, token, q, {"n": name, "c": cr, "cl": cluster, "s": settings}, verify)
    if "errors" in d:
        log(f"createMonitoringPolicy failed: {d['errors'][0]['message'][:220]}"); return None
    rows = d.get("data", {}).get("createMonitoringPolicy") or []
    return rows[0] if rows else None

def exists_named(base, token, collection, name, verify):
    d = gql(base, token, "{ %s { name } }" % collection, None, verify)
    return any(x.get("name") == name for x in (d.get("data", {}).get(collection) or []))

def main():
    ap = argparse.ArgumentParser(description="Automate KVO AWS Zone Tapping (VPC Traffic Mirroring).")
    ap.add_argument("--kvo", required=True, help="KVO IP or host")
    ap.add_argument("--clm-name", required=True, help="name of the adopted CLM inside KVO")
    ap.add_argument("--name", default="aws-mirror", help="name for the AWS presence / cloud config")
    ap.add_argument("--region", required=True, help="AWS region of the source VPC")
    ap.add_argument("--vpc-id", required=True, help="source VPC id to mirror from")
    ap.add_argument("--source-tag", default="cloudlens=yes",
                    help="tag selecting sources to mirror, key=value (default cloudlens=yes)")
    ap.add_argument("--image-id", help="collector Service VM AMI. Omit to auto-resolve the newest "
                    "cloudlens-vpb-svm Marketplace image in --region (recommended; region-agnostic).")
    ap.add_argument("--ssh-key", help="EC2 key pair name for the collector SVMs")
    ap.add_argument("--mgmt-sg", help="collector mgmt security group id")
    ap.add_argument("--ingress-sg", help="collector ingress security group id")
    ap.add_argument("--egress-sg", help="collector egress security group id")
    ap.add_argument("--cloudlens-ip", help="CLMS IP the collectors register to")
    ap.add_argument("--scale-cooldown", type=int, default=300)
    # Collector autoscaling group placement, per AZ. One --zone with its three
    # subnet roles (like a mini-vPB). Repeat the flag set for multi-AZ is not yet
    # supported here; extend availability_zones() for that.
    ap.add_argument("--zone", help="AZ for the collectors, e.g. us-east-1a")
    ap.add_argument("--collector-type", default="t3.xlarge", help="collector SVM instance type")
    ap.add_argument("--mgmt-subnet", help="collector mgmt subnet id (registers to CLMS)")
    ap.add_argument("--ingress-subnet", help="collector ingress subnet id (receives mirrored traffic)")
    ap.add_argument("--egress-subnet", help="collector egress subnet id (to tools)")
    ap.add_argument("--min-size", type=int, default=1, help="collector ASG min size")
    ap.add_argument("--max-size", type=int, default=1, help="collector ASG max size")
    # Destination tool: the analyzer/NPB IP that receives the tapped traffic.
    # Required to actually create mirror sessions (KVO needs a complete path).
    ap.add_argument("--tool-remote-ip", help="IP of the destination analyzer/vPB that receives "
                    "the tapped traffic (reachable from the collector egress). Required to create sessions.")
    ap.add_argument("--tool-encap", default="L2GRE", choices=["L2GRE", "VXLAN"],
                    help="encapsulation from collector to the tool (L2GRE or VXLAN)")
    ap.add_argument("--gre-key", type=int, default=64, help="L2GRE key (when --tool-encap L2GRE)")
    ap.add_argument("--vni", type=int, default=4096, help="VXLAN VNI (when --tool-encap VXLAN)")
    ap.add_argument("--aws-access-key", help="AWS access key for KVO (omit to use KVO's instance role)")
    ap.add_argument("--aws-secret-key", help="AWS secret key for KVO (omit to use KVO's instance role)")
    ap.add_argument("--kvo-admin-user", default="admin")
    ap.add_argument("--kvo-admin-pass", default="admin")
    ap.add_argument("--accept-eula", action="store_true", help="accept KVO EULA if pending (legal acceptance)")
    ap.add_argument("--insecure", action="store_true", help="disable TLS verification")
    args = ap.parse_args()
    verify = not args.insecure
    if args.insecure:
        import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    kvo = f"https://{args.kvo}"

    tok = kvo_token(kvo, args.kvo_admin_user, args.kvo_admin_pass, verify)
    if tok is None:
        if args.accept_eula:
            kvo_accept_eula(kvo, verify); tok = kvo_token(kvo, args.kvo_admin_user, args.kvo_admin_pass, verify)
        if tok is None:
            log("KVO auth failed; if the EULA is unaccepted, re-run with --accept-eula"); return 3
    elif args.accept_eula:
        kvo_accept_eula(kvo, verify)

    if not kvo_is_licensed(kvo, tok, verify):
        log("KVO reports no installed license; every write is blocked. Stopping."); return 4

    clm = clm_info(kvo, tok, args.clm_name, verify)
    if not clm:
        log(f"CLM '{args.clm_name}' not found in KVO"); return 8
    if clm.get("status") != "CONNECTED":
        log(f"CLM '{args.clm_name}' is {clm.get('status')}, not CONNECTED"); return 8
    clm_uid = clm["uid"]
    cluster = cluster_uid(kvo, tok, verify)

    if not (args.aws_access_key and args.aws_secret_key):
        log("no AWS keys supplied; relying on KVO's instance-role credentials "
            "(EnableZoneTapping / kvo_enable_zonetap_iam.sh)")

    # 1. AWS presence (idempotent on name)
    existing = gql(kvo, tok, "{ cloudPresences { name } }", None, verify)
    have_pres = any(p.get("name") == args.name
                    for p in (existing.get("data", {}).get("cloudPresences") or []))
    if have_pres:
        log(f"AWS presence '{args.name}' already exists; reusing")
    else:
        cr = open_cr(kvo, tok, "aws-presence", verify)
        if not cr: return 5
        pres = create_aws_presence(kvo, tok, cr, args.name, clm_uid, args.region, args.vpc_id,
                                   args.aws_access_key, args.aws_secret_key, verify)
        if not pres:
            return 5
        if not commit_cr(kvo, tok, cr, verify): return 5
        log(f"AWS presence '{args.name}' created (project {pres.get('clmsProjectId')})")

    # 2. AWS cloud config (provisions + starts the AWS-side work)
    image_id = args.image_id or resolve_collector_ami(args.region)
    if not image_id:
        log("no collector AMI (imageId is required); pass --image-id or ensure the "
            "cloudlens-vpb-svm Marketplace image is available in the region"); return 6
    aws_cfg = {"scaleCooldown": args.scale_cooldown, "imageId": image_id}
    if args.ssh_key:             aws_cfg["sshKeyPair"] = args.ssh_key
    # The KVO UG (AWS Cloud Configs) requires the management, ingress, and egress
    # security groups to be DISTINCT (each in its own subnet). Passing one SG for
    # all three leaves the vHub's interface roles ambiguous. Warn loudly.
    # These three are NON-NULL in the KVO schema (_AwsConfigurationInput:
    # mgmtSecurityGroupIds!, ingressSecurityGroupIds!, egressSecurityGroupIds!),
    # so omitting them does not create a config without security groups: the
    # whole mutation is rejected. It was treated as optional here, and a live
    # run failed with a raw GraphQL dump naming only "$s" and awsConfiguration,
    # which reads as a malformed request rather than three missing arguments.
    # Fail on the actual reason, before the round trip.
    _missing = [n for n, v in (("--mgmt-sg", args.mgmt_sg),
                               ("--ingress-sg", args.ingress_sg),
                               ("--egress-sg", args.egress_sg)) if not v]
    if _missing:
        log("missing required security groups: " + ", ".join(_missing))
        log("  KVO requires all three. They must be THREE DISTINCT security groups,")
        log("  one per interface role. The management SG needs inbound 443 and 22")
        log("  (KVO UG, AWS Cloud Configs). List candidates with:")
        log(f"    aws ec2 describe-security-groups --region {args.region} \\")
        log(f"      --filters Name=vpc-id,Values={args.vpc_id} --query 'SecurityGroups[].[GroupId,GroupName]' --output text")
        return 2
    if len({args.mgmt_sg, args.ingress_sg, args.egress_sg}) < 3:
        log("WARNING: mgmt/ingress/egress security groups must be THREE DISTINCT SGs "
            "(KVO UG requirement); using the same SG can break vHub bring-up.")
    if args.mgmt_sg:             aws_cfg["mgmtSecurityGroupIds"] = [args.mgmt_sg]
    if args.ingress_sg:          aws_cfg["ingressSecurityGroupIds"] = [args.ingress_sg]
    if args.egress_sg:           aws_cfg["egressSecurityGroupIds"] = [args.egress_sg]
    if args.cloudlens_ip:        aws_cfg["cloudlensIp"] = args.cloudlens_ip
    if args.zone and args.mgmt_subnet and args.ingress_subnet and args.egress_subnet:
        aws_cfg["availabilityZones"] = [{
            "zone": args.zone,
            "instanceType": args.collector_type,
            "mgmtSubnetId": args.mgmt_subnet,
            "ingressSubnetIds": [args.ingress_subnet],
            "egressSubnetId": args.egress_subnet,
            "minSize": args.min_size,
            "maxSize": args.max_size,
        }]
    aws_cfg["tags"] = []  # required list of {key,value}; empty = no extra tags
    have_cfg = any(c.get("name") == args.name for c in
                   (gql(kvo, tok, "{ cloudConfigs { name } }", None, verify).get("data", {}).get("cloudConfigs") or []))
    if have_cfg:
        log(f"AWS cloud config '{args.name}' already exists; reusing")
    else:
        cr = open_cr(kvo, tok, "aws-cloud-config", verify)
        if not cr: return 6
        cfg = create_aws_cloud_config(kvo, tok, cr, args.name, cluster, aws_cfg, verify)
        if not cfg:
            return 6
        if not commit_cr(kvo, tok, cr, verify): return 6
        log(f"AWS cloud config '{args.name}' live")

    # 3. Cloud collection (tag selector) -> KVO creates AWS mirror sessions
    if "=" in args.source_tag:
        tag_key, tag_val = args.source_tag.split("=", 1)
    else:
        tag_key, tag_val = args.source_tag, ".*"
    coll_name = f"{args.name}-collect"
    have_coll = any(c.get("name") == coll_name for c in
                    (gql(kvo, tok, "{ cloudCollections { name } }", None, verify).get("data", {}).get("cloudCollections") or []))
    if have_coll:
        log(f"cloud collection '{coll_name}' already exists; reusing")
    else:
        cr = open_cr(kvo, tok, "aws-collection", verify)
        if not cr: return 7
        coll = create_collection(kvo, tok, cr, coll_name, cluster, args.name, tag_key, tag_val, verify)
        if not coll:
            return 7
        if not commit_cr(kvo, tok, cr, verify): return 7

    # 4 + 5. Destination tool + monitoring policy COMPLETE the path. Without them
    # KVO has nowhere to send the traffic and creates NO mirror sessions. Skipped
    # only if no --tool-remote-ip was given (collection-only staging).
    tool_name = f"{args.name}-tool"
    policy_name = f"{args.name}-policy"
    if not args.tool_remote_ip:
        log("no --tool-remote-ip given; stopping after the collection. Provide a "
            "destination (analyzer/vPB IP) to create the tool + policy that make "
            "KVO create the mirror sessions.")
    else:
        # 4. remote tool
        if exists_named(kvo, tok, "tools", tool_name, verify):
            log(f"tool '{tool_name}' already exists; reusing")
        else:
            cr = open_cr(kvo, tok, "aws-tool", verify)
            if not cr: return 9
            tool = create_remote_tool(kvo, tok, cr, tool_name, cluster, args.tool_remote_ip,
                                      args.tool_encap, args.gre_key, args.vni, verify)
            if not tool:
                return 9
            if not commit_cr(kvo, tok, cr, verify): return 9
            log(f"remote tool '{tool_name}' -> {args.tool_remote_ip} ({args.tool_encap}) created")
        # 5. monitoring policy wiring collection -> tool
        if exists_named(kvo, tok, "monitoringPolicies", policy_name, verify):
            log(f"monitoring policy '{policy_name}' already exists; reusing")
        else:
            cr = open_cr(kvo, tok, "aws-policy", verify)
            if not cr: return 10
            pol = create_monitoring_policy(kvo, tok, cr, policy_name, cluster, coll_name, tool_name, verify)
            if not pol:
                return 10
            if not commit_cr(kvo, tok, cr, verify): return 10
            log(f"monitoring policy '{policy_name}' ({coll_name} -> {tool_name}) committed")

    log("")
    log("=====================================================================")
    log(f" AWS Zone Tapping configured for VPC {args.vpc_id} in {args.region}.")
    log(f"   Sources selected by tag: {tag_key}={tag_val}")
    if args.tool_remote_ip:
        log(f"   Destination tool: {args.tool_remote_ip} ({args.tool_encap})")
        log(f"   Policy: {coll_name} -> {tool_name}")
        log("   KVO will now create the AWS VPC Traffic Mirror Sessions. Verify:")
    else:
        log("   NOTE: no destination tool/policy yet -> no sessions will be created.")
    log(f"     aws ec2 describe-traffic-mirror-sessions --region {args.region}")
    log(f"     aws ec2 describe-traffic-mirror-targets  --region {args.region}")
    log("   and in KVO: Visibility Fabric > Cloud Configs / Tools / Monitoring Policies.")
    log("=====================================================================")
    return 0

if __name__ == "__main__":
    sys.exit(main())
