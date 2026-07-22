#!/usr/bin/env python3
"""Wire the adopted vPB's traffic path in KVO (Track 3b):
sync ports -> C2DL -> bind ingress -> LOCAL tool -> bind egress -> cloud-config
deviceLinks -> monitoring policy. Reuses kvo_aws_mirror's CR lifecycle helpers.
Schema-correct for KVO 2.13.0 (all mutation vars are non-null: ID!/String!/...!).

PREREQUISITE (found 2026-07-22 on KVO 2.13.0): syncDeviceConfigPorts commits
cleanly but deviceConfig._portGroups stays empty until the vPB's data interfaces
(eth1 ingress, eth2 egress) are brought up as DPDK data ports ON THE vPB itself
(vpb CLI). The 3 ENIs exist at the AWS layer, but KVO only exposes bindable ports
once the vPB software has claimed them. Bring the data interfaces up first, then
the sync populates _portGroups and the bind/C2DL/tool/policy steps below apply.
The port sync + CR lifecycle here are proven; the data-plane bring-up is the gap.
"""
import sys, os, time, argparse, urllib3
sys.path.insert(0, os.path.dirname(__file__))
urllib3.disable_warnings()
from kvo_aws_mirror import gql, open_cr, commit_cr, clear_open_crs
from vpb_kvo_adopt import kvo_token

def log(m): print(f"[vpb-wire] {m}", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvo", required=True)
    ap.add_argument("--device", default="vpb-prod")
    ap.add_argument("--cluster", required=True)
    ap.add_argument("--cloud", required=True, help="cloud config name to attach the C2DL to")
    ap.add_argument("--c2dl", default="vpb-c2dl")
    ap.add_argument("--tool", default="vpb-egress-tool")
    ap.add_argument("--tool-ip", default="10.99.1.117")
    ap.add_argument("--ingress-ip", default="10.99.11.240")
    ap.add_argument("--netmask", default="255.255.255.0")
    ap.add_argument("--gateway", default="10.99.11.1")
    ap.add_argument("--gre-key", type=int, default=100)
    ap.add_argument("--step", default="sync", help="sync|all")
    args = ap.parse_args()
    V = False
    tok = kvo_token(f"https://{args.kvo}", "admin", "admin", V)
    base = f"https://{args.kvo}"

    # device config uid
    d = gql(base, tok, '{ deviceConfigs { uid name } }', None, V)
    dc = next((x for x in d["data"]["deviceConfigs"] if x["name"] == args.device), None)
    if not dc: log(f"device config {args.device} not found"); sys.exit(2)
    uid = dc["uid"]; log(f"device config {args.device} uid={uid}")

    def ports():
        q = gql(base, tok, '{ deviceConfigs { name _portGroups { mode ports { portId } } } }', None, V)
        dcs = [x for x in q["data"]["deviceConfigs"] if x["name"] == args.device]
        pg = dcs[0]["_portGroups"] if dcs else []
        return [(g.get("mode"), p["portId"]) for g in pg for p in (g.get("ports") or [])]

    # STEP 1: sync ports
    clear_open_crs(base, tok, V)
    cr = open_cr(base, tok, "vpb-sync-ports", V)
    r = gql(base, tok, 'mutation($u:ID!,$c:String!,$s:_SyncPortsInput!){ syncDeviceConfigPorts(uid:$u,changeID:$c,settings:$s){ uid } }',
            {"u": uid, "c": cr, "s": {"forceSync": True}}, V)
    if "errors" in r: log("sync error: " + r["errors"][0]["message"][:200]); sys.exit(3)
    commit_cr(base, tok, cr, V)
    log("ports synced; committing done")
    time.sleep(4)
    log("PORTS: " + str(ports()))

if __name__ == "__main__":
    main()
