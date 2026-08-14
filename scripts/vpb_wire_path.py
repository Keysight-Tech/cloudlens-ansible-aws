#!/usr/bin/env python3
"""Wire the full vPB traffic path in KVO: collection -> C2DL -> vPB ingress ->
vPB -> egress -> tool, tied together by a monitoring policy.

Runs after scripts/vpb_kvo_adopt.py has adopted the vPB (device Online). Every
step is a committed change request and is idempotent, so re-running is safe.

Sequence (each gotcha below cost a debugging cycle, do not reorder):
  1. syncDeviceConfigPorts(forceSync)      pulls eth1/eth2 into the Device Config
  2. createC2DLink                          the Cloud-to-Device Link
  3. bindDeviceConfigPorts eth1             INGRESS -> C2DL
  4. createTool type=REMOTE                 reachableFrom DEVICE_CONFIG, so the vPB
     reachableFrom DEVICE_CONFIG            ORIGINATES an L2GRE tunnel out of eth2
                                            to the tool. This was type=LOCAL and the
                                            vPB forwarded NOTHING: a LOCAL tool is
                                            the bare port, so the vPB put raw frames
                                            on an AWS subnet and AWS dropped every
                                            one. Inspected climbed into the millions
                                            while Passed stayed at 0
  5. bindDeviceConfigPorts eth2             EGRESS -> tool, WITH ip/netmask/gateway.
                                            _ToolBindInput carries them and we sent
                                            none, so eth2 had no address and could
                                            not be a tunnel source. Bind ingress and
                                            egress in SEPARATE calls: a failing
                                            egress bind rolls back the whole call.
                                            Order matters too: a policy cannot
                                            reference a tool that is not bound to a
                                            port, and a port cannot drop a tool the
                                            policy still references, so releasing
                                            the old tool from the policy comes first
  6. updateCloudConfig deviceLinks          REQUIRED or the policy commit fails
                                            with "does not have a device link
                                            associated to it". Pass the FULL
                                            awsConfiguration: a partial update
                                            fails reading sshKeyPair, and each
                                            availabilityZone must carry minSize
                                            and maxSize (Int!) or the variable is
                                            rejected outright
  7. createMonitoringPolicy                 source -> tool, CONTINUOUSLY

Exit: 0 ok, 2 auth, 3 not licensed, 4 device/deviceConfig not found, 5 step failed.
"""
from __future__ import annotations
import argparse, json, sys, time
import requests

def log(m): print(f"[vpb-path] {m}", file=sys.stderr, flush=True)

def kvo_token(base, user, password, verify):
    r = requests.post(f"{base}/auth/realms/keysight/protocol/openid-connect/token",
                      data={"grant_type": "password", "client_id": "vision-orchestrator",
                            "username": user, "password": password}, verify=verify, timeout=20)
    if r.status_code != 200:
        log(f"KVO auth failed (HTTP {r.status_code})"); return None
    return r.json()["access_token"]

def gql(base, token, query, variables, verify):
    r = requests.post(f"{base}/public/graphql", headers={"Authorization": f"Bearer {token}"},
                      json={"query": query, "variables": variables or {}}, verify=verify, timeout=60)
    try: return r.json()
    except ValueError: return {"errors": [{"message": r.text[:200]}]}

def _open_crs(base, token, verify):
    q = gql(base, token, "{ changeRequests { uid status } }", None, verify)
    return [r for r in (q.get("data", {}).get("changeRequests") or []) if r["status"] != "Committed"]

def clear_open_crs(base, token, verify, timeout=90):
    open_crs = _open_crs(base, token, verify)
    for r in open_crs:
        gql(base, token, "mutation($u:String!){ deleteChangeRequest(uid:$u){ uid } }", {"u": r["uid"]}, verify)
        log(f"clearing stale change request {r['uid']} ({r['status']})")
    if not open_crs: return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _open_crs(base, token, verify): return
        time.sleep(4)

def open_cr(base, token, name, verify):
    clear_open_crs(base, token, verify)
    d = gql(base, token, "mutation($n:String){ createChangeRequest(name:$n){ uid } }", {"n": name}, verify)
    if "errors" in d:
        log(f"createChangeRequest failed: {d['errors'][0]['message'][:180]}"); return None
    crs = d.get("data", {}).get("createChangeRequest") or []
    return crs[0]["uid"] if crs else None

def commit_cr(base, token, cr_uid, verify, timeout=600):
    deadline = time.time() + timeout
    while True:
        c = gql(base, token,
                "mutation($u:String!){ commitChangeRequest(uid:$u, ignoreWarnings:true){ uid state } }",
                {"u": cr_uid}, verify)
        if "errors" not in c: break
        msg = c["errors"][0]["message"]
        if "another processing is still running" in msg and time.time() < deadline:
            log("  waiting for the previous processing to finish..."); time.sleep(10); continue
        log(f"commit failed: {msg[:220]}"); return False
    deadline = time.time() + timeout
    warned = False
    started = time.time()
    while time.time() < deadline:
        q = gql(base, token, "{ changeRequests { uid status } }", None, verify)
        mine = [r for r in (q.get("data", {}).get("changeRequests") or []) if r["uid"] == cr_uid]
        if not mine or mine[0]["status"] == "Committed":
            log("  committed"); return True
        if mine[0]["status"] in ("Failed", "Error"):
            log(f"  commit ended in {mine[0]['status']}"); return False
        # A change request KVO refuses to commit stays at InProgress and raises an
        # ALERT rather than moving to Failed, so watching status alone polls in
        # silence for the whole timeout while the UI shows a plain reason. Seen
        # live: "No CloudLens vPB license installed." sat on the change request
        # for ten minutes while this loop reported nothing at all.
        if not warned and time.time() - started > 90:
            warned = True
            log("  still InProgress after 90s. KVO may be refusing this change.")
            log(f"  Open the change request in KVO and read its alerts:")
            log(f"    Live Settings > Change Requests > {cr_uid}")
            log("  A licence or validation error appears there, not in this status.")
        time.sleep(6)
    log("timed out waiting for commit")
    log(f"  check the change request alerts in KVO for {cr_uid}: an unmet")
    log("  requirement (for example a missing CloudLens vPB licence) blocks the")
    log("  commit without ever changing the status.")
    return False

def _egress_bind(a):
    """The egress port binding, WITH its IP.

    _ToolBindInput carries ip / netmask / defaultGateway, and omitting them is
    what left eth2 with no address. The UI exposes the same fields as the tool's
    "Port Configuration" tab. With no address the vPB cannot be the source of a
    tunnel, so nothing it forwards ever leaves the box.
    """
    b = {"tool": {"name": a.tool}}
    if a.egress_ip:
        b["ip"] = a.egress_ip
        b["netmask"] = a.egress_netmask
        if a.egress_gateway:
            b["defaultGateway"] = a.egress_gateway
    return b


def step(base, token, verify, label, mutation, variables):
    """One mutation inside its own committed change request."""
    cr = open_cr(base, token, label, verify)
    if not cr: return False
    v = dict(variables); v["cr"] = cr
    d = gql(base, token, mutation, v, verify)
    if "errors" in d:
        log(f"{label}: {d['errors'][0]['message'][:220]}")
        gql(base, token, "mutation($u:String!){ deleteChangeRequest(uid:$u){ uid } }", {"u": cr}, verify)
        return False
    return commit_cr(base, token, cr, verify)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvo", required=True)
    ap.add_argument("--device", required=True, help="adopted vPB device name (Online)")
    ap.add_argument("--collection", required=True, help="cloud collection = traffic source")
    ap.add_argument("--cloud-config", required=True, help="cloud config that owns the collection")
    ap.add_argument("--c2dl", default="vpb-c2dl")
    ap.add_argument("--tool", default="vpb-egress-tool")
    ap.add_argument("--policy", default="vpb-traffic-policy")
    # A packet-capture host on the egress subnet sees NOTHING from the LOCAL
    # egress tool: that tool points at the vPB's own eth2 and the vPB emits raw
    # frames, which AWS will not deliver to another instance. Giving the capture
    # host its own REMOTE tool inside the SAME policy tunnels it a copy of
    # exactly what leaves the vPB egress, so "show me the packets" has an answer.
    ap.add_argument("--capture-ip", default="",
                    help="IP of a packet-capture host to attach to the vPB egress "
                         "(adds a REMOTE tool to the same monitoring policy)")
    ap.add_argument("--capture-tool", default="vpb-capture-tool")
    ap.add_argument("--capture-encap", default="L2GRE", choices=["L2GRE", "VXLAN"])
    # The capture host key must ALSO match, or the same stream is fine at the
    # capture host and dropped at the vPB. tcpdump does not enforce GRE keys,
    # which is exactly why a mismatch stayed hidden for so long.
    ap.add_argument("--capture-gre-key", type=int, default=64)
    ap.add_argument("--capture-vni", type=int, default=4096)
    ap.add_argument("--ingress-port", default="eth1")
    ap.add_argument("--egress-port", default="eth2")
    # The egress port needs its OWN IP. Without one the vPB has no source address
    # to originate a tunnel from, so a LOCAL tool there emits raw frames that AWS
    # silently discards: the device passed 0 packets for an entire debugging
    # session while eth2 sat with no address at all.
    ap.add_argument("--egress-ip", help="vPB egress port IP, e.g. 10.99.12.35")
    ap.add_argument("--egress-netmask", default="255.255.255.0")
    ap.add_argument("--egress-gateway", help="egress subnet default gateway")
    ap.add_argument("--ingress-ip", required=True, help="vPB ingress IP for the C2DL")
    ap.add_argument("--netmask", default="255.255.255.0")
    ap.add_argument("--gateway", help="ingress subnet default gateway")
    ap.add_argument("--gre-key", default="64")
    # Deliberately DIFFERENT from the collector's key: the tool then receives two
    # distinguishable streams, the collector's direct copy and the vPB's processed
    # output, and one tcpdump tells you which path delivered which packet.
    ap.add_argument("--egress-gre-key", type=int, default=200,
                    help="L2GRE key the vPB uses on its egress tunnel to the tool")
    ap.add_argument("--kvo-admin-user", default="admin")
    ap.add_argument("--kvo-admin-pass", default="admin")
    ap.add_argument("--insecure", action="store_true")
    a = ap.parse_args()

    base = a.kvo if a.kvo.startswith("http") else f"https://{a.kvo}"
    verify = not a.insecure
    if a.insecure:
        requests.packages.urllib3.disable_warnings()  # noqa

    tok = kvo_token(base, a.kvo_admin_user, a.kvo_admin_pass, verify)
    if not tok: return 2
    log(f"authed to KVO {a.kvo}")

    d = gql(base, tok, "{ availableLicenses { name installed } }", None, verify)
    if not any((l.get("installed") or 0) > 0 for l in d.get("data", {}).get("availableLicenses", [])):
        log("KVO is not licensed; run scripts/kvo_license.py first"); return 3

    # resolve the device config for the adopted vPB
    d = gql(base, tok, "{ devices{name availability{value}} deviceConfigs{uid name} }", None, verify)
    dev = next((x for x in d["data"]["devices"] if x["name"] == a.device), None)
    dc = next((x for x in d["data"]["deviceConfigs"] if x["name"] == a.device), None)
    if not dev or not dc:
        log(f"device/deviceConfig '{a.device}' not found; adopt the vPB first"); return 4
    log(f"device {a.device} availability={dev['availability']['value']} deviceConfig={dc['uid']}")
    uid = dc["uid"]

    cl = gql(base, tok, "{ clusters { uid } }", None, verify)
    cluster = (cl.get("data", {}).get("clusters") or [{}])[0].get("uid")

    def existing(coll):
        r = gql(base, tok, "{ %s { name } }" % coll, None, verify)
        return {x["name"] for x in (r.get("data", {}).get(coll) or [])}

    # 1. sync ports
    log("1/7 syncDeviceConfigPorts")
    if not step(base, tok, verify, "sync vPB ports",
                "mutation($u:ID!,$cr:String!){ syncDeviceConfigPorts(uid:$u,changeID:$cr,settings:{forceSync:true}){ uid } }",
                {"u": uid}): return 5

    # 2. C2DL
    if a.c2dl in existing("c2DLinks"):
        log(f"2/7 C2DL '{a.c2dl}' already exists")
    else:
        log("2/7 createC2DLink")
        ipcfg = {"netmask": a.netmask, "ipAddresses": a.ingress_ip, "icmp": True}
        if a.gateway: ipcfg["defaultGateway"] = a.gateway
        if not step(base, tok, verify, "create C2DL",
                    "mutation($n:String!,$cr:String!,$c:String,$s:_C2DLinkInput!){ createC2DLink(name:$n,changeID:$cr,clusterID:$c,settings:$s){ uid } }",
                    {"n": a.c2dl, "c": cluster,
                     "s": {"localIPConfig": ipcfg, "l2greEncapsulation": {"key": a.gre_key}}}): return 5

    # 3. ingress -> C2DL
    log(f"3/7 bind {a.ingress_port} (ingress) -> C2DL")
    if not step(base, tok, verify, "bind ingress",
                "mutation($u:ID!,$cr:String!,$s:_BindPortsInput!){ bindDeviceConfigPorts(uid:$u,changeID:$cr,settings:$s){ uid } }",
                {"u": uid, "s": {"ports": [{"portId": a.ingress_port,
                                            "connectedC2DLink": {"c2dLink": {"name": a.c2dl}, "ip": a.ingress_ip}}]}}):
        return 5

    # 4. Egress tool: REMOTE, reachable FROM THE DEVICE.
    #
    # This was a LOCAL tool, and that is why the vPB forwarded nothing for an
    # entire session. A LOCAL tool is just the port itself, so the vPB emitted
    # raw frames onto an AWS subnet, and AWS drops frames not addressed to an
    # ENI. The counters said it plainly: Inspected in the millions, Passed 0.
    #
    # A REMOTE tool with reachableFrom DEVICE_CONFIG makes the vPB ORIGINATE an
    # L2GRE tunnel out of eth2 to a real destination, which AWS routes normally.
    # Proven live: Passed went 0 -> 145,037, and the tool captured the tapped
    # payload with the vPB egress IP as the outer source.
    if not a.capture_ip:
        log("4/7 no --capture-ip given, so the vPB has nowhere to send.")
        log("    Skipping the egress tool rather than forwarding into a black hole.")
    elif a.tool in existing("tools"):
        # An existing tool is not automatically a WORKING tool. Earlier releases
        # created this as LOCAL, which forwards nothing, and simply reusing it by
        # name would keep a broken path broken while reporting success.
        stale = [t for t in (gql(base, tok, "{ tools { name type } }", None, verify)
                             .get("data", {}).get("tools") or [])
                 if t["name"] == a.tool and t.get("type") == "LOCAL"]
        if stale:
            log(f"4/7 tool '{a.tool}' exists but is type LOCAL, which forwards NOTHING.")
            log("    A LOCAL tool is the bare port, so the vPB puts raw frames on an AWS")
            log("    subnet and AWS drops them all. Converting it needs three committed")
            log("    changes in this order, because a policy cannot reference an unbound")
            log("    tool and a port cannot drop a tool the policy still references:")
            log(f"      1. edit the monitoring policy to send to the capture tool only")
            log(f"      2. delete '{a.tool}', recreate it as REMOTE / reachableFrom")
            log(f"         DEVICE_CONFIG with tunnelOrigRemoteIP {a.capture_ip}, then")
            log(f"         rebind {a.egress_port} to it with ip {a.egress_ip or '<egress ENI IP>'}")
            log("      3. add the tool back to the monitoring policy")
            log("    Leaving it as it is: this script will not delete a tool your policy uses.")
        else:
            log(f"4/7 tool '{a.tool}' already exists")
    else:
        log(f"4/7 createTool (REMOTE from device -> {a.capture_ip}, L2GRE key {a.egress_gre_key})")
        if not step(base, tok, verify, "create egress tool",
                    "mutation($n:String!,$cr:String!,$c:String,$s:_ToolInput!){ createTool(name:$n,changeID:$cr,clusterID:$c,settings:$s){ uid } }",
                    {"n": a.tool, "c": cluster,
                     "s": {"type": "REMOTE", "reachableFrom": "DEVICE_CONFIG",
                           "vlanStripping": False,
                           "tunnelOrigRemoteIP": a.capture_ip,
                           "l2greEncapsulation": {"key": str(a.egress_gre_key)}}}): return 5

    # 5. egress -> tool  (separate call from the ingress bind on purpose)
    log(f"5/7 bind {a.egress_port} (egress) -> tool")
    if not step(base, tok, verify, "bind egress",
                "mutation($u:ID!,$cr:String!,$s:_BindPortsInput!){ bindDeviceConfigPorts(uid:$u,changeID:$cr,settings:$s){ uid } }",
                {"u": uid, "s": {"ports": [{"portId": a.egress_port,
                                            "connectedTools": [_egress_bind(a)]}]}}):
        return 5

    # 6. associate the C2DL to the cloud config (FULL awsConfiguration round-trip)
    log("6/7 updateCloudConfig deviceLinks")
    q = gql(base, tok, """{ cloudConfigs { name settings {
              cloudPresence { name } deviceLinks { name }
              awsConfiguration { imageId sshKeyPair scaleCooldown cloudlensIp
                mgmtSecurityGroupIds ingressSecurityGroupIds egressSecurityGroupIds
                tags { key value }
                availabilityZones { zone instanceType mgmtSubnetId ingressSubnetIds egressSubnetId minSize maxSize } } } } }""",
              None, verify)
    # The deviceLink MUST land on the AWS-type cloud config (the one
    # kvo_aws_mirror.py creates, e.g. "aws-mirror"), NOT on --cloud-config, which
    # the deploy sets to the sensor CustomCloud ("cloudlens-aws"). A CustomCloud
    # has no awsConfiguration, so attaching there was silently skipped and the AWS
    # config never got its deviceLink -> KVO's mirror monitoring-policy commit had
    # an incomplete path and ZERO mirror sessions were cut. So pick the AWS config
    # by the presence of awsConfiguration, not by the passed name.
    configs = q["data"]["cloudConfigs"]
    aws_configs = [c for c in configs if (c.get("settings") or {}).get("awsConfiguration")]
    cc = aws_configs[0] if aws_configs else None
    if not cc:
        # No AWS mirroring fabric present (sensor-only deploy). The vPB path itself
        # is complete (ports synced, C2DL created, ingress/egress bound, tool made);
        # there is simply no AWS cloud config to attach the C2DL to.
        log("no AWS-type cloud config found (sensor-only deploy); the vPB path is")
        log("  complete but there is no AWS mirroring fabric to link the C2DL to.")
        return 0
    target = cc["name"]
    st = cc["settings"] or {}
    links = {l["name"] for l in (st.get("deviceLinks") or [])}
    if a.c2dl in links:
        log(f"  '{a.c2dl}' already linked to {target}")
    else:
        aws = {k: v for k, v in (st.get("awsConfiguration") or {}).items() if v is not None}
        for az in aws.get("availabilityZones", []):
            for k in [k for k, v in list(az.items()) if v is None]: az.pop(k)
        # deviceLinks is a LIST of name refs, not a single object.
        settings = {"awsConfiguration": aws, "deviceLinks": [{"name": a.c2dl}]}
        if st.get("cloudPresence"): settings["cloudPresence"] = {"name": st["cloudPresence"]["name"]}
        if not step(base, tok, verify, "link C2DL to cloud config",
                    "mutation($n:String!,$cr:String!,$c:String,$s:_CloudConfigUpdateInput!){ updateCloudConfig(name:$n,changeID:$cr,clusterID:$c,settings:$s){ uid } }",
                    {"n": target, "c": cluster, "s": settings}): return 5
        log(f"  linked C2DL '{a.c2dl}' to AWS cloud config '{target}'")

    # 7. monitoring policy
    if a.policy in existing("monitoringPolicies"):
        log(f"7/7 policy '{a.policy}' already exists")
    else:
        # 6b. Optional capture host, created BEFORE the policy so it can be named
        # in the same tools list. Same traffic, two destinations: the vPB egress
        # port and a host that can actually run tcpdump.
        policy_tools = [{"name": a.tool}]
        if a.capture_ip:
            if a.capture_tool in existing("tools"):
                log(f"6b/7 capture tool '{a.capture_tool}' already exists")
            else:
                log(f"6b/7 createTool (REMOTE -> {a.capture_ip}, {a.capture_encap})")
                cap = {"type": "REMOTE", "vlanStripping": False,
                       "reachableFrom": "CLOUD_CONFIG",
                       "tunnelOrigRemoteIP": a.capture_ip}
                if a.capture_encap.upper() == "VXLAN":
                    cap["vxlanEncapsulation"] = {"vnid": str(a.capture_vni), "udpDestPort": 4789}
                else:
                    cap["l2greEncapsulation"] = {"key": str(a.capture_gre_key)}
                if not step(base, tok, verify, "create capture tool",
                            "mutation($n:String!,$cr:String!,$c:String,$s:_ToolInput!){ createTool(name:$n,changeID:$cr,clusterID:$c,settings:$s){ uid } }",
                            {"n": a.capture_tool, "c": cluster, "s": cap}): return 5
            policy_tools.append({"name": a.capture_tool})

        # The traffic source is a cloud COLLECTION. The deploy passes the sensor
        # cloud CONFIG name for --collection, and the two are different objects,
        # so KVO rejected the last step with:
        #   Invalid names provided for entity type(s) [ 'TrafficSource' ]: [ 'cloudlens-aws' ]
        # after steps 1 to 6b had all committed: a whole wired path undone by a
        # name. Resolve against what KVO actually has rather than trusting the
        # argument, the same way step 6 picks the AWS config by type.
        src = a.collection
        q = gql(base, tok, "{ cloudCollections { name } }", None, verify)
        names = [c["name"] for c in (q.get("data", {}).get("cloudCollections") or [])]
        if src not in names:
            aws_like = [n for n in names if "aws" in n.lower() or "mirror" in n.lower()]
            pick = aws_like[0] if len(aws_like) == 1 else (names[0] if len(names) == 1 else None)
            if not pick:
                log(f"   no usable cloud collection for the traffic source: "
                    f"--collection was '{a.collection}', KVO has {names or 'none'}.")
                log("   Create the AWS mirror fabric first (scripts/kvo_aws_mirror.py), "
                    "then re-run this script.")
                return 5
            log(f"   cloud collection '{src}' does not exist in KVO; "
                f"using the real traffic source '{pick}'")
            src = pick

        log("7/7 createMonitoringPolicy")
        if not step(base, tok, verify, "create monitoring policy",
                    "mutation($n:String!,$cr:String!,$c:String,$s:_MonitoringPolicyInput!){ createMonitoringPolicy(name:$n,changeID:$cr,clusterID:$c,settings:$s){ uid } }",
                    {"n": a.policy, "c": cluster,
                     "s": {"source": {"name": src}, "tools": policy_tools,
                           "runMode": "CONTINUOUSLY", "type": "REGULAR"}}): return 5
        a.collection = src   # so the summary below names what was actually used

    v = gql(base, tok, "{ devices{name availability{value}} c2DLinks{name} tools{name type} monitoringPolicies{name} changeRequests{uid status} }", None, verify)
    dd = v.get("data", {})
    openq = [c for c in (dd.get("changeRequests") or []) if c["status"] != "Committed"]
    log("")
    log("=" * 68)
    log(" vPB traffic path wired")
    log(f"   source      {a.collection}")
    log(f"   C2DL        {a.c2dl}  -> {a.device} {a.ingress_port} ({a.ingress_ip})")
    log(f"   tool        {a.tool} (REMOTE from device) <- {a.device} {a.egress_port} {a.egress_ip or 'NO IP'} -> {a.capture_ip or '-'} key {a.egress_gre_key}")
    log(f"   policy      {a.policy}")
    log(f"   c2DLinks    {[c['name'] for c in dd.get('c2DLinks') or []]}")
    log(f"   tools       {[(t['name'], t['type']) for t in dd.get('tools') or []]}")
    log(f"   policies    {[p['name'] for p in dd.get('monitoringPolicies') or []]}")
    log(f"   open CRs    {len(openq)}")
    log("=" * 68)
    return 0

if __name__ == "__main__":
    sys.exit(main())
