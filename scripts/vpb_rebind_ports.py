#!/usr/bin/env python3
"""Move an already-wired vPB traffic path onto different port names.

Needed when a stack was wired assuming vPB eth1 == AWS device index 1 and the
device actually enumerated its NICs the other way round. A fresh deploy does not
need this: scripts/vpb_port_map.py gets the ports right the first time and the
ports start unbound. This is the REMEDIATION for a stack already bound wrongly.

You cannot simply re-bind. THREE KVO rules box you in:

  * a port will not accept an IP another port already holds
        "bind ingress: Binding IP 10.99.11.67 is already used."
  * a port will not drop a tool the monitoring policy still references
        the change request sits InProgress forever and never fails
  * the C2DL must never be left with no port bound at all
        "Validate Cloud Config: missing bindings in C2DLink for cloud config"

That last one kills the obvious approach. Releasing both ports and rebinding
them in separate change requests leaves the C2DL unbound in between, and KVO
refuses to commit that intermediate state.

So the swap has to be ATOMIC: one change request that moves BOTH ports at once.
Validation runs on the FINAL state, where the C2DL is bound to the new ingress
port and no IP is held twice, so all three rules are satisfied together.

  1. monitoring policy -> capture tool only   (frees the egress tool to move)
  2. ONE change request binding both ports to their correct roles
  3. monitoring policy -> both tools again

Exit: 0 ok, 2 auth, 4 device not found, 5 a step failed.
"""
from __future__ import annotations
import argparse, sys, importlib.util, os

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("wire", os.path.join(_here, "vpb_wire_path.py"))
_w = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_w)


def log(m): print(f"[vpb-rebind] {m}", file=sys.stderr, flush=True)


def set_policy_tools(base, tok, verify, policy, tools, cluster):
    return _w.step(base, tok, verify, f"policy -> {', '.join(tools)}",
        # UPDATE takes _MonitoringPolicyUpdateInput, not the create type. KVO
        # rejects the wrong one outright rather than coercing it.
        "mutation($n:String!,$cr:String!,$c:String,$s:_MonitoringPolicyUpdateInput!){"
        " updateMonitoringPolicy(name:$n,changeID:$cr,clusterID:$c,settings:$s){ uid } }",
        {"n": policy, "c": cluster, "s": {"tools": [{"name": t} for t in tools]}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvo", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--policy", default="vpb-traffic-policy")
    ap.add_argument("--capture-tool", default="vpb-capture-tool")
    ap.add_argument("--egress-tool", default="vpb-egress-tool")
    ap.add_argument("--c2dl", default="vpb-c2dl")
    ap.add_argument("--ingress-port", required=True, help="port that IS the ingress NIC")
    ap.add_argument("--egress-port", required=True, help="port that IS the egress NIC")
    ap.add_argument("--ingress-ip", required=True)
    ap.add_argument("--egress-ip", required=True)
    ap.add_argument("--egress-netmask", default="255.255.255.0")
    ap.add_argument("--egress-gateway")
    ap.add_argument("--kvo-admin-user", default="admin")
    ap.add_argument("--kvo-admin-pass", default="admin")
    ap.add_argument("--insecure", action="store_true")
    a = ap.parse_args()

    verify = not a.insecure
    if a.insecure:
        import urllib3; urllib3.disable_warnings()
    base = a.kvo if a.kvo.startswith("http") else f"https://{a.kvo}"

    tok = _w.kvo_token(base, a.kvo_admin_user, a.kvo_admin_pass, verify)
    if not tok: return 2
    log(f"authed to KVO {a.kvo}")

    _w.clear_open_crs(base, tok, verify)

    d = _w.gql(base, tok, "{ deviceConfigs { uid name } clusters { uid name } }", None, verify)
    cfgs = (d.get("data", {}) or {}).get("deviceConfigs") or []
    uid = next((c["uid"] for c in cfgs if c["name"] == a.device), None)
    if not uid:
        log(f"no deviceConfig named {a.device}"); return 4
    clusters = (d.get("data", {}) or {}).get("clusters") or []
    cluster = next((c["uid"] for c in clusters if c.get("name") == "PredefinedCluster"),
                   clusters[0]["uid"] if clusters else None)

    bind_q = ("mutation($u:ID!,$cr:String!,$s:_BindPortsInput!){"
              " bindDeviceConfigPorts(uid:$u,changeID:$cr,settings:$s){ uid } }")

    log(f"1/3 policy -> {a.capture_tool} only, so the port can let the egress tool go")
    if not set_policy_tools(base, tok, verify, a.policy, [a.capture_tool], cluster):
        log("could not release the tool from the policy; stopping before touching ports")
        return 5

    # ONE change request, both ports. Doing this as separate releases and binds
    # cannot work: the intermediate state has the C2DL bound to nothing, and KVO
    # rejects it with "missing bindings in C2DLink for cloud config".
    log(f"2/3 atomic swap: {a.ingress_port} -> C2DL {a.ingress_ip}, "
        f"{a.egress_port} -> {a.egress_tool} {a.egress_ip}")
    ebind = {"tool": {"name": a.egress_tool}, "ip": a.egress_ip, "netmask": a.egress_netmask}
    if a.egress_gateway: ebind["defaultGateway"] = a.egress_gateway
    if not _w.step(base, tok, verify, "swap ports", bind_q,
                   {"u": uid, "s": {"ports": [
                       {"portId": a.ingress_port,
                        "connectedC2DLink": {"c2dLink": {"name": a.c2dl}, "ip": a.ingress_ip}},
                       {"portId": a.egress_port, "connectedTools": [ebind]}]}}):
        log("the atomic swap was refused. Read the change request's ALERTS in KVO:")
        log("  Live Settings > Change Requests > (newest) > Change request alerts")
        log("  The alert names the exact validation that failed; the status does not.")
        return 5

    log("3/3 policy -> both tools again")
    if not set_policy_tools(base, tok, verify, a.policy,
                            [a.capture_tool, a.egress_tool], cluster):
        return 5

    log("done. Confirm on the DEVICE, not in KVO:")
    log(f"  sudo vpb -c 'show traffic-rule-status'        Ingress Interface must be {a.ingress_port}")
    log("  sudo vpb -c 'show traffic-rule-packet-counters'  Passed must climb")
    return 0


if __name__ == "__main__":
    sys.exit(main())
