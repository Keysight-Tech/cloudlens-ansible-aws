#!/usr/bin/env python3
"""
Automate adopting a CloudLens Manager (vController) into Keysight Vision
Orchestrator (KVO), replacing the manual UI dance:

  Manual: log into CLMS, create a KVO user, log into KVO, Inventory ->
  CloudLens Manager -> Discover CloudLens Manager, enter name + CLMS IP + the
  KVO user credentials, then commit the Change Request KVO raises.

Every endpoint here was discovered live and is documented in
~/keysight-kb/digests/kvo-clms-adoption-api.md. Nothing is in a public manual.

Order matters and is enforced below:
  0. KVO must be LICENSED. Every write mutation (adopt, even createChangeRequest)
     returns "Not Authorised!" on an unlicensed KVO. This script checks and
     stops early with a clear message rather than failing opaquely later.
  1. Create the KVO user on CLMS (a non-admin CLM user, one per deployment).
  2. Accept the KVO EULA (gated behind --accept-eula; it is a legal acceptance).
  3. Get a Keycloak bearer token for KVO.
  4. Adopt: createCloudLensManager(name, {ip, user, password}).
  5. Commit the Change Request KVO stages for that adopt.
  6. Poll until the CLM status reaches CONNECTED.

Exit codes: 0 ok, 2 CLMS auth/user, 3 KVO auth/eula, 4 not licensed,
5 adopt/commit, 6 never reached CONNECTED.
"""
from __future__ import annotations
import argparse, json, sys, time, urllib.parse
import requests

def log(m): print(f"[kvo-adopt] {m}", file=sys.stderr, flush=True)

# ----- CLMS -------------------------------------------------------------
def clms_login(base, user, password, verify):
    r = requests.post(f"{base}/cloudlens/api/v1/identity/login",
                      json={"Email": user, "Password": password}, verify=verify, timeout=20)
    if r.status_code != 200:
        log(f"CLMS login failed (HTTP {r.status_code}): {r.text[:160]}"); return None
    return r.json()["ActiveSessionCredentials"]["JwtToken"]

def clms_create_kvo_user(base, token, email, password, verify):
    """POST /admin/kvo_users. The KVO user is a non-admin CLM user, one per
    deployment, so a 409/'exists' is success for our purposes."""
    r = requests.post(f"{base}/cloudlens/api/v1/admin/kvo_users",
                      headers={"Authorization": f"jwt {token}"},
                      json={"email": email, "password": password}, verify=verify, timeout=20)
    if r.status_code in (200, 201):
        log(f"KVO user created on CLMS: {email}"); return True
    if r.status_code in (409,) or "exist" in r.text.lower():
        log(f"KVO user already exists on CLMS: {email} (reusing)"); return True
    log(f"KVO user create failed (HTTP {r.status_code}): {r.text[:200]}"); return False

# ----- KVO --------------------------------------------------------------
def kvo_accept_eula(base, verify):
    r = requests.get(f"{base}/eula/v1/eula", verify=verify, timeout=20)
    if r.status_code != 200:
        log(f"could not list KVO EULAs (HTTP {r.status_code})"); return False
    pending = [e for e in r.json() if not e.get("accepted")]
    for e in pending:
        a = requests.post(f"{base}/eula/v1/eula/{e['id']}", json={"accepted": True},
                          verify=verify, timeout=20)
        log(f"accepted KVO EULA {e['id']} (HTTP {a.status_code})")
    return True

def kvo_token(base, user, password, verify):
    url = f"{base}/auth/realms/keysight/protocol/openid-connect/token"
    r = requests.post(url, data={"grant_type": "password",
                                 "client_id": "vision-orchestrator",
                                 "username": user, "password": password},
                      verify=verify, timeout=20)
    if r.status_code != 200:
        log(f"KVO auth failed (HTTP {r.status_code}): {r.text[:160]}"); return None
    return r.json()["access_token"]

def gql(base, token, query, verify):
    r = requests.post(f"{base}/public/graphql", headers={"Authorization": f"Bearer {token}"},
                      json={"query": query}, verify=verify, timeout=30)
    try: body = r.json()
    except ValueError: body = {"errors": [{"message": r.text[:200]}]}
    return body

def kvo_is_licensed(base, token, verify):
    d = gql(base, token, "{ availableLicenses { name installed } }", verify)
    if "errors" in d: return False
    lic = d.get("data", {}).get("availableLicenses", [])
    return any((l.get("installed") or 0) > 0 for l in lic)

def _q(s):  # escape a value for inline GraphQL
    return json.dumps(s)

def kvo_adopt(base, token, name, clms_ip, kvo_user, kvo_pass, verify):
    m = ("mutation { createCloudLensManager("
         f"name: {_q(name)}, settings: {{ ip: {_q(clms_ip)}, "
         f"user: {_q(kvo_user)}, password: {_q(kvo_pass)} }}) "
         "{ uid name ip user status } }")
    d = gql(base, token, m, verify)
    if "errors" in d:
        log(f"adopt failed: {d['errors'][0]['message'][:200]}"); return None
    return d["data"]["createCloudLensManager"]

def kvo_commit(base, token, verify):
    d = gql(base, token, 'mutation { createChangeRequest(name: "adopt-clms") { uid } }', verify)
    if "errors" in d:
        log(f"createChangeRequest failed: {d['errors'][0]['message'][:200]}"); return False
    uid = d["data"]["createChangeRequest"]["uid"]
    log(f"change request {uid} opened; committing")
    c = gql(base, token, f'mutation {{ commitChangeRequest(uid: {_q(uid)}, ignoreWarnings: true) }}', verify)
    if "errors" in c:
        log(f"commit failed: {c['errors'][0]['message'][:200]}"); return False
    log("change request committed"); return True

def kvo_wait_connected(base, token, name, verify, timeout=300):
    q = ("{ cloudLensManagersFeed { cloudLensManagers { name ip status } } }")
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = gql(base, token, q, verify)
        rows = (d.get("data", {}).get("cloudLensManagersFeed", {}) or {}).get("cloudLensManagers", [])
        for r in rows:
            if r.get("name") == name:
                st = r.get("status")
                log(f"  {name} status: {st}")
                if st == "CONNECTED": return True
                if st and st.upper() in ("FAILED", "ERROR", "DISCONNECTED"):
                    log(f"adoption ended in {st}"); return False
        time.sleep(10)
    log("timed out waiting for CONNECTED"); return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clms", required=True, help="CLMS/vController IP or host")
    ap.add_argument("--clms-admin-pass", required=True, help="CLMS admin password (known value)")
    ap.add_argument("--kvo", required=True, help="KVO IP or host")
    ap.add_argument("--name", default="cloudlens-manager", help="name for the CLM inside KVO")
    ap.add_argument("--kvo-user-email", default="kvo@cloudlens.local")
    ap.add_argument("--kvo-user-pass", default="KvoAdopt@2026")
    ap.add_argument("--kvo-admin-user", default="admin")
    ap.add_argument("--kvo-admin-pass", default="admin")
    ap.add_argument("--accept-eula", action="store_true",
                    help="accept the KVO EULA if pending. This is a legal acceptance.")
    ap.add_argument("--insecure", action="store_true", help="disable TLS verification")
    args = ap.parse_args()
    verify = not args.insecure
    if args.insecure:
        import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    clms = f"https://{args.clms}"
    kvo = f"https://{args.kvo}"

    # 1. CLMS: token + KVO user
    ctok = clms_login(clms, "admin", args.clms_admin_pass, verify)
    if not ctok: return 2
    if not clms_create_kvo_user(clms, ctok, args.kvo_user_email, args.kvo_user_pass, verify):
        return 2

    # 2. KVO EULA
    ktok_probe = kvo_token(kvo, args.kvo_admin_user, args.kvo_admin_pass, verify)
    if ktok_probe is None:
        if not args.accept_eula:
            log("KVO auth failed; if the EULA is unaccepted, re-run with --accept-eula")
        else:
            kvo_accept_eula(kvo, verify)
            ktok_probe = kvo_token(kvo, args.kvo_admin_user, args.kvo_admin_pass, verify)
    elif args.accept_eula:
        kvo_accept_eula(kvo, verify)
    if ktok_probe is None:
        return 3

    # 3. licence gate
    if not kvo_is_licensed(kvo, ktok_probe, verify):
        log("KVO reports no installed licence. Every write is blocked until it is "
            "licensed (Settings > Product Licensing, or the licensing API). Stopping.")
        return 4

    # 4-5. adopt + commit
    adopted = kvo_adopt(kvo, ktok_probe, args.name, args.clms,
                        args.kvo_user_email, args.kvo_user_pass, verify)
    if not adopted: return 5
    log(f"adopt staged: {adopted}")
    if not kvo_commit(kvo, ktok_probe, verify): return 5

    # 6. wait for CONNECTED
    if not kvo_wait_connected(kvo, ktok_probe, args.name, verify): return 6

    log("")
    log("=====================================================================")
    log(f" {args.name} adopted into KVO and CONNECTED.")
    log(f"   CLMS      https://{args.clms}")
    log(f"   KVO       https://{args.kvo}")
    log(f"   KVO user  {args.kvo_user_email}")
    log("=====================================================================")
    return 0

if __name__ == "__main__":
    sys.exit(main())
