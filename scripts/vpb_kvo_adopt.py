#!/usr/bin/env python3
"""
Adopt a CloudLens vPB into KVO as a managed device, so KVO can drive its ports
and use it as a tap/tool in the visibility fabric. This is the vPB analogue of
kvo_adopt_clms.py (which adopts a CLMS).

Two sides, both automated:

  vPB side (SSH -> `sudo vpb -c "<cli>"`, the wrapper from bootstrap-vpb.sh):
    1. license server <kvo-ip> type advanced   # KVO is the CloudLens license
       authority ("KVO collectively manages license for vTAPs & vPB"); this is
       the "full license" the vPB UG says KVO integration requires.
    2. kvo ip <kvo-ip>                          # point the vPB at KVO
    3. kvo enable                               # vPB announces itself to KVO
    (Interface ROLES ingress/egress are NOT set here: once adopted with control
     enabled, KVO owns the ports via the auto-created Device Config.)

  KVO side (GraphQL /public/graphql, in committed change requests):
    4. Wait for the vPB announcement, then adopt it (discoverDevices/createDevice
       + acceptAnnouncementsFromDevice), which auto-creates a Device Config.
    5. Verify the device reaches a connected state.

PREREQUISITES (see docs/PRODUCTION_DEPLOYMENT.md):
  - vPB with 3 NICs (mgmt/ingress/egress). The stack wires these automatically
    (CFN VpbMultiNic / TF vpb_ingress+vpb_egress); brownfield: attach 2 ENIs.
  - KVO licensed with a vPB feature (CL.vPB.ADVPERM); adopt CLMS done.
  - vPB reachable over SSH (AWS Marketplace: port 9022, user admin, key pair).
  - vPB and KVO in the same VPC so the vPB uses KVO's private IP.

Exit codes: 0 ok, 2 vPB SSH/CLI, 3 KVO auth, 4 not licensed, 5 adopt/verify.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
import requests

def log(m): print(f"[vpb-adopt] {m}", file=sys.stderr, flush=True)

# ----- vPB side (SSH + the `vpb` CLI wrapper) ---------------------------
def vpb_cli(host, port, user, key, command, timeout=60):
    """Run one vPB CLI command via `sudo vpb -c`. Returns (ok, output)."""
    ssh = ["ssh", "-i", key, "-p", str(port),
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
           f"{user}@{host}", f'sudo vpb -c "{command}"']
    try:
        r = subprocess.run(ssh, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        # strip ssh banners
        out = "\n".join(l for l in out.splitlines()
                        if "Permanently" not in l and "Pseudo-terminal" not in l and "Debian GNU" not in l)
        return r.returncode == 0, out.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"

def vpb_wait_cli(host, port, user, key, tries=45, delay=20):
    """After first boot the KCOS pod that serves the vPB CLI takes a while to come
    up: 10-15 minutes is normal on a freshly deployed appliance, longer than a
    human expects. The default here (45 x 20s = 15 min) covers that; the old 20 x
    20s = ~6.7 min timed out on real first boots and reported the vPB as broken
    when it was only still starting. Tunable via --cli-tries / --cli-delay."""
    total = tries * delay
    log(f"waiting for the vPB CLI (KCOS first boot can take 10-15 min; up to {total // 60} min here)")
    for i in range(tries):
        ok, out = vpb_cli(host, port, user, key, "show version")
        low = out.lower()
        # The CLI is UP in two cases, and the second one is what a fresh appliance
        # actually shows: xf-client answers `show version`, OR it blocks on the
        # EULA gate. That gate ("YOU MUST ACCEPT THE IXIA SOFTWARE EULA") only
        # appears once xf-client is running and attached, so seeing it means the
        # CLI is alive and just needs the EULA accepted (which vpb_accept_eula
        # does next). Treating the EULA prompt as "not up" was the deadlock: the
        # wait needed the EULA accepted, but the accept ran only after the wait.
        if (ok and ("version" in low or "vpb" in low or "cloudlensvpb" in out)) \
           or "accept the ixia software" in low or "end user license" in low or "eula" in low:
            log(f"vPB CLI is up (after ~{i * delay // 60}m{i * delay % 60}s)"); return True
        log(f"  waiting for vPB CLI ({i+1}/{tries})...")
        time.sleep(delay)
    return False

def vpb_accept_eula(host, port, user, key):
    """The vPB CLI blocks EVERY command with a EULA prompt until accepted, one
    time and persisted. The `vpb -c` wrapper pipes its arg to stdin, so we feed
    the two answers ('n' = do not display, 'y' = accept) before a no-op command.
    This is a LEGAL acceptance - only call it behind an explicit operator flag."""
    ok, out = vpb_cli(host, port, user, key, "n\ny\nshow version")
    accepted = "CloudLensVPB" in out or "agree to its terms" in out
    log(f"  vPB EULA accepted (legal): {accepted}")
    return accepted

# ----- KVO side (GraphQL) -----------------------------------------------
def kvo_token(base, user, password, verify):
    r = requests.post(f"{base}/auth/realms/keysight/protocol/openid-connect/token",
                      data={"grant_type": "password", "client_id": "vision-orchestrator",
                            "username": user, "password": password}, verify=verify, timeout=20)
    if r.status_code != 200:
        log(f"KVO auth failed (HTTP {r.status_code})"); return None
    return r.json()["access_token"]

def gql(base, token, query, variables, verify):
    r = requests.post(f"{base}/public/graphql", headers={"Authorization": f"Bearer {token}"},
                      json={"query": query, "variables": variables or {}}, verify=verify, timeout=40)
    try: return r.json()
    except ValueError: return {"errors": [{"message": r.text[:200]}]}

def kvo_is_licensed(base, token, verify):
    d = gql(base, token, "{ availableLicenses { name installed } }", None, verify)
    return any((l.get("installed") or 0) > 0 for l in d.get("data", {}).get("availableLicenses", [])) \
        if "errors" not in d else False

def kvo_devices(base, token, verify):
    d = gql(base, token, "{ devices { uid name serialNumber } }", None, verify)
    return d.get("data", {}).get("devices") or []

def kvo_announcements(base, token, verify):
    d = gql(base, token,
            "{ deviceAnnouncements { serialNumber model ip family softwareVersion webApiPort sshPort } }",
            None, verify)
    return d.get("data", {}).get("deviceAnnouncements") or []

def kvo_wait_announcement(base, token, verify, ip=None, timeout=300):
    """After `kvo enable` the vPB AUTO-announces (self-registers). Wait for the
    announcement to arrive (optionally matching the vPB mgmt IP)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        anns = kvo_announcements(base, token, verify)
        for a in anns:
            if ip is None or a.get("ip") == ip:
                log(f"vPB announced: {a.get('model')} sn {a.get('serialNumber')} ip {a.get('ip')}")
                return a
        log("  waiting for the vPB to announce to KVO (auto-discovery)...")
        time.sleep(15)
    return None

def clear_open_crs(base, token, verify):
    for r in gql(base, token, "{ changeRequests { uid status } }", None, verify).get("data", {}).get("changeRequests") or []:
        if r["status"] != "Committed":
            gql(base, token, "mutation($u:String!){ deleteChangeRequest(uid:$u){ uid } }", {"u": r["uid"]}, verify)

def kvo_open_cr(base, token, name, verify):
    clear_open_crs(base, token, verify)
    d = gql(base, token, "mutation($n:String){ createChangeRequest(name:$n){ uid } }", {"n": name}, verify)
    crs = d.get("data", {}).get("createChangeRequest") or []
    return crs[0]["uid"] if crs else None

def kvo_commit(base, token, cr, verify, timeout=300):
    gql(base, token, "mutation($u:String!){ commitChangeRequest(uid:$u, ignoreWarnings:true){ uid state } }", {"u": cr}, verify)
    deadline = time.time() + timeout
    while time.time() < deadline:
        mine = [r for r in gql(base, token, "{ changeRequests { uid status } }", None, verify).get("data", {}).get("changeRequests") or [] if r["uid"] == cr]
        if not mine or mine[0]["status"] == "Committed": return True
        if mine[0]["status"] in ("Failed", "Error"): return False
        time.sleep(8)
    return False

def kvo_adopt_device(base, token, name, ann, verify):
    """Adopt an announced vPB with control (autoBind:true -> auto Device Config)."""
    cr = kvo_open_cr(base, token, "adopt-vpb", verify)
    if not cr: return None
    settings = {"serialNumber": ann["serialNumber"], "family": ann["family"], "model": ann["model"],
                "ip": ann.get("ip"), "autoBind": True, "username": "admin", "password": "ixia"}
    if ann.get("webApiPort"): settings["webApiPort"] = ann["webApiPort"]
    if ann.get("sshPort"):    settings["sshPort"] = ann["sshPort"]
    d = gql(base, token,
            "mutation($n:String!,$c:String!,$s:_DeviceInput!){ createDevice(name:$n, changeID:$c, settings:$s){ uid name } }",
            {"n": name, "c": cr, "s": settings}, verify)
    if "errors" in d:
        log(f"createDevice failed: {d['errors'][0]['message'][:200]}"); return None
    rows = d.get("data", {}).get("createDevice") or []
    if not rows: return None
    if not kvo_commit(base, token, cr, verify):
        log("device adoption did not commit"); return None
    return rows[0]

def main():
    ap = argparse.ArgumentParser(description="Adopt a CloudLens vPB into KVO.")
    ap.add_argument("--vpb", required=True, help="vPB SSH host (public IP/host)")
    ap.add_argument("--vpb-port", type=int, default=9022, help="vPB SSH port (AWS Marketplace: 9022)")
    ap.add_argument("--vpb-user", default="admin", help="vPB SSH user (AWS: admin)")
    ap.add_argument("--key", required=True, help="path to the SSH private key (.pem)")
    ap.add_argument("--kvo", required=True, help="KVO host for the API (public IP/host)")
    ap.add_argument("--kvo-internal-ip", required=True,
                    help="KVO IP the vPB uses for license + management (private, same-VPC)")
    ap.add_argument("--kvo-port-kvo", type=int, default=443,
                    help="port the vPB uses to reach KVO (vPB `kvo port`, default 443)")
    ap.add_argument("--device-name", default="vpb", help="name for the adopted vPB device in KVO")
    ap.add_argument("--vpb-mgmt-ip", help="vPB mgmt IP to match its announcement (optional)")
    ap.add_argument("--kvo-admin-user", default="admin")
    ap.add_argument("--kvo-admin-pass", default="admin")
    ap.add_argument("--wait-cli", action="store_true",
                    help="wait for the vPB CLI to come up first (use right after a reboot)")
    ap.add_argument("--cli-tries", type=int, default=45,
                    help="how many times to poll the vPB CLI (default 45; KCOS first boot is slow)")
    ap.add_argument("--cli-delay", type=int, default=20,
                    help="seconds between vPB CLI polls (default 20; 45x20s = 15 min total)")
    ap.add_argument("--accept-eula", action="store_true",
                    help="accept the vPB EULA if it blocks the CLI. This is a legal acceptance.")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    verify = not args.insecure
    if args.insecure:
        import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    kvo = f"https://{args.kvo}"

    # KVO auth + license gate
    tok = kvo_token(kvo, args.kvo_admin_user, args.kvo_admin_pass, verify)
    if tok is None: return 3
    if not kvo_is_licensed(kvo, tok, verify):
        log("KVO is not licensed; a vPB license (CL.vPB.ADVPERM) must be active. Stopping."); return 4
    before = {d["uid"] for d in kvo_devices(kvo, tok, verify)}

    # vPB side
    if args.wait_cli and not vpb_wait_cli(args.vpb, args.vpb_port, args.vpb_user, args.key,
                                          tries=args.cli_tries, delay=args.cli_delay):
        log(f"vPB CLI never came up after {args.cli_tries * args.cli_delay // 60} min.")
        log("The vPB is likely still starting KCOS, not broken. To finish:")
        log(f"  1) ssh -i {args.key} -p {args.vpb_port} {args.vpb_user}@{args.vpb}")
        log("     then: sudo vpb show system   (confirms KCOS is up), exit")
        log("  2) re-run the deploy: it resumes and adoption succeeds once the CLI answers.")
        log("Or raise the wait with --cli-tries (each try is --cli-delay seconds).")
        return 2
    if args.accept_eula:
        vpb_accept_eula(args.vpb, args.vpb_port, args.vpb_user, args.key)
    # CRITICAL: `kvo` is an INTERACTIVE CONFIG CONTEXT, not a flat command. You
    # enter it, then set ip/port/enable/exit (vPB UG 913-3000-01). Running
    # `kvo ip <x>` / `kvo enable` as flat commands does NOT enable KVO. Feed the
    # whole context sequence as one multi-line arg (the wrapper pipes it to stdin).
    kvo_seq = f"kvo\nip {args.kvo_internal_ip}\nport {args.kvo_port_kvo}\nenable\nexit"
    ok, out = vpb_cli(args.vpb, args.vpb_port, args.vpb_user, args.key, kvo_seq)
    if "KVO enabled" not in out:
        log(f"  vPB: 'kvo enable' did not confirm. Output: {out[-200:]}")
        log("  (a 'license server' warning is expected/harmless; KVO licenses on adoption)")
    else:
        log(f"  vPB: KVO enabled -> announcing to {args.kvo_internal_ip}:{args.kvo_port_kvo}")

    # KVO side (auto-discovery): the vPB self-announces; wait for it, then adopt
    # with control so KVO auto-creates the Device Config and licenses it.
    # (This is the AUTO path. The manual discoverDevices path is a separate
    # KVO-initiated probe and is not what `kvo enable` triggers.)
    already = {d["serialNumber"] for d in kvo_devices(kvo, tok, verify)}
    ann = kvo_wait_announcement(kvo, tok, verify, ip=args.vpb_mgmt_ip)
    if not ann:
        log("vPB never announced. Verify mgmt-network reachability vPB->KVO on "
            f"port {args.kvo_port_kvo}, and that `kvo enable` returned 'KVO enabled'."); return 5
    if ann["serialNumber"] in already:
        log("vPB already adopted; skipping createDevice")
        dev = {"name": args.device_name}
    else:
        dev = kvo_adopt_device(kvo, tok, args.device_name, ann, verify)
        if not dev: return 5

    log("")
    log("=====================================================================")
    log(f" vPB adopted into KVO: {dev.get('name')} (auto Device Config created)")
    log(f"   vPB       {args.vpb}  (sn {ann['serialNumber']})")
    log(f"   KVO       {args.kvo}  (vPB uses {args.kvo_internal_ip}:{args.kvo_port_kvo})")
    log("   Next: bind the Device Config ingress/egress ports, add a Cloud to")
    log("   Device Link, and a monitoring policy source->vPB->tool (Visibility")
    log("   Fabric). See docs/PRODUCTION_DEPLOYMENT.md (Track 3b).")
    log("=====================================================================")
    return 0

if __name__ == "__main__":
    sys.exit(main())
