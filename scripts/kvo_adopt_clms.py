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
5 adopt/commit, 6 never reached CONNECTED, 7 cloud config / project key.
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
        msg = d["errors"][0]["message"]
        if "already exists" in msg.lower():
            log(f"CLM {name} already adopted; will verify its status"); return "EXISTS"
        log(f"adopt failed: {msg[:200]}"); return None
    return d["data"]["createCloudLensManager"]

def kvo_commit(base, token, verify, timeout=240):
    """Commit the change request the adopt staged. This IS required: the adopt
    reports CONNECTED but leaves an "uncommitted changes" change request in the
    UI, and the config is not applied until it is committed.

    Shapes learned only by running it live:
      - createChangeRequest and commitChangeRequest both return a LIST.
      - commit is ASYNC: it returns a processing job (state
        ExecuteUpfrontChecks) and the change request sits InProgress until it
        flips to Committed, so it must be polled.
    """
    d = gql(base, token, 'mutation { createChangeRequest(name: "adopt-clms") { uid } }', verify)
    if "errors" in d:
        log(f"createChangeRequest failed: {d['errors'][0]['message'][:180]}"); return False
    crs = d.get("data", {}).get("createChangeRequest") or []
    if not crs:
        log("no change request was opened; nothing to commit"); return True
    uid = crs[0]["uid"]
    log(f"change request {uid} opened; committing (async)")

    c = gql(base, token,
            f'mutation {{ commitChangeRequest(uid: {_q(uid)}, ignoreWarnings: true) {{ uid state }} }}',
            verify)
    if "errors" in c:
        log(f"commit call failed: {c['errors'][0]['message'][:200]}"); return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        q = gql(base, token, "{ changeRequests { uid status } }", verify)
        mine = [r for r in (q.get("data", {}).get("changeRequests") or []) if r["uid"] == uid]
        if not mine:
            log("change request cleared (committed)"); return True
        st = mine[0]["status"]
        if st == "Committed":
            log("change request committed"); return True
        if st in ("Failed", "Error"):
            log(f"commit ended in {st}"); return False
        log(f"  commit in progress: {st}")
        time.sleep(6)
    log("timed out waiting for the commit to finish"); return False

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

def kvo_clm_uid(base, token, name, verify):
    d = gql(base, token, "{ cloudLensManagersFeed { cloudLensManagers { uid name } } }", verify)
    for r in (d.get("data", {}).get("cloudLensManagersFeed", {}) or {}).get("cloudLensManagers", []):
        if r.get("name") == name:
            return r["uid"]
    return None

def kvo_commit_cr(base, token, cr_uid, verify, timeout=240):
    """Commit a specific change request by uid and poll until Committed.
    Same async pattern as kvo_commit; factored out so the cloud-config flow
    reuses it."""
    c = gql(base, token,
            f'mutation {{ commitChangeRequest(uid: {_q(cr_uid)}, ignoreWarnings: true) {{ uid state }} }}',
            verify)
    if "errors" in c:
        log(f"commit call failed: {c['errors'][0]['message'][:200]}"); return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = gql(base, token, "{ changeRequests { uid status } }", verify)
        mine = [r for r in (q.get("data", {}).get("changeRequests") or []) if r["uid"] == cr_uid]
        if not mine or mine[0]["status"] == "Committed":
            log("change request committed"); return True
        if mine[0]["status"] in ("Failed", "Error"):
            log(f"commit ended in {mine[0]['status']}"); return False
        time.sleep(6)
    log("timed out waiting for the commit"); return False

def kvo_create_cloud_config(base, token, clm_uid, cc_name, verify):
    """Create a Custom Cloud (Cloud Config) bound to the adopted CLM and return
    the project key KVO generates for it.

    This is the KVO-managed way to get a project key: rather than creating a
    project directly on the CLMS, KVO's Cloud Config creates one through the
    adopted manager and returns it as clmsProjectApiKey. That key is what the
    sensors register with.

    createCustomCloud REQUIRES a changeID (unlike adopt), so a change request is
    opened first, the cloud config is created inside it, and the change request
    is committed. The key is present in the create response immediately.
    Returns (project_id, project_key) or (None, None).
    """
    # Check for an existing cloud config FIRST, before opening a change request.
    # Opening one and then bailing on "already exists" would leave an orphaned
    # InProgress change request behind.
    q = gql(base, token, "{ customClouds { name clmsProjectId clmsProjectApiKey } }", verify)
    for c in (q.get("data", {}).get("customClouds") or []):
        if c.get("name") == cc_name:
            log(f"cloud config {cc_name} already exists; reading its key")
            return c.get("clmsProjectId"), c.get("clmsProjectApiKey")

    cr = gql(base, token, 'mutation { createChangeRequest(name: "create-cloud-config") { uid } }', verify)
    if "errors" in cr:
        log(f"createChangeRequest failed: {cr['errors'][0]['message'][:180]}"); return None, None
    crs = cr.get("data", {}).get("createChangeRequest") or []
    if not crs:
        log("no change request opened for cloud config"); return None, None
    cr_uid = crs[0]["uid"]

    m = (f'mutation {{ createCustomCloud(name: {_q(cc_name)}, changeID: {_q(cr_uid)}, '
         f'settings: {{ cloudLensManagerId: {_q(clm_uid)}, description: "CloudLens Autopilot" }}) '
         '{ uid name clmsProjectId clmsProjectApiKey } }')
    r = gql(base, token, m, verify)
    if "errors" in r:
        log(f"createCustomCloud failed: {r['errors'][0]['message'][:200]}"); return None, None
    rows = r.get("data", {}).get("createCustomCloud") or []
    if not rows:
        log("createCustomCloud returned no rows"); return None, None
    cc = rows[0]
    pid, key = cc.get("clmsProjectId"), cc.get("clmsProjectApiKey")
    log(f"cloud config {cc_name} created; project {pid}")

    if not kvo_commit_cr(base, token, cr_uid, verify):
        log("cloud config created but its change request did not commit"); return pid, key
    return pid, key

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
    ap.add_argument("--cloud-config",
                    help="after adopting, create a KVO Cloud Config (Custom Cloud) bound to the "
                         "CLM with this name, and print the project key it generates on stdout. "
                         "That key is what sensors register with in a KVO-managed deployment.")
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
    if adopted == "EXISTS":
        # Idempotent re-run: nothing to stage, just confirm it is CONNECTED.
        pass
    else:
        log(f"adopt staged: {adopted}")
        if not kvo_commit(kvo, ktok_probe, verify): return 5

    # 6. wait for CONNECTED
    if not kvo_wait_connected(kvo, ktok_probe, args.name, verify): return 6

    # 7. optional: create a Cloud Config and emit its project key
    proj_id = proj_key = None
    if args.cloud_config:
        clm_uid = kvo_clm_uid(kvo, ktok_probe, args.name, verify)
        if not clm_uid:
            log("could not resolve the adopted CLM uid; skipping cloud config"); return 7
        proj_id, proj_key = kvo_create_cloud_config(kvo, ktok_probe, clm_uid, args.cloud_config, verify)
        if not proj_key:
            return 7
        # stdout carries ONLY the project key so the sensor step can capture it.
        print(proj_key)

    log("")
    log("=====================================================================")
    log(f" {args.name} adopted into KVO and CONNECTED.")
    log(f"   CLMS      https://{args.clms}")
    log(f"   KVO       https://{args.kvo}")
    log(f"   KVO user  {args.kvo_user_email}")
    if proj_key:
        log(f"   Cloud     {args.cloud_config}")
        log(f"   Project   {proj_id}")
        log(f"   Key       {proj_key}   <- install sensors with this")
    log("=====================================================================")
    return 0

if __name__ == "__main__":
    sys.exit(main())
