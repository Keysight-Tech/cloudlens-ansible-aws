#!/usr/bin/env python3
"""
Automate project creation and API-key retrieval on Keysight CloudLens vController.

Used by demo/setup-aws-visibility-demo.sh so the live demo build is fully hands-off:
no operator click required between deploying vController and running the
sensor Ansible playbook.

Behaviour:
  1. Wait until the REST API (not just nginx) is serving.
  2. Log in as admin. Try the known password first, then the marketplace default.
  3. If logged in with the default, complete the forced first-login password
     change to a known value. This is essential: the appliance forces that
     change on the first UI login, which invalidates every session, so leaving
     the default in place would lock out whoever is handed the login.
  4. Create (or look up) a project by name. Its api_key IS the project key, so
     no separate user and no separate key-minting call are needed.
  5. Print the project key on stdout (nothing else), and print the full working
     UI login (url, user, password, project, key) on stderr for the operator.

Stderr carries progress and the login banner so callers can capture stdout
cleanly:
    KEY=$(python3 vcontroller_project_key.py --host 1.2.3.4 --project demo)

Exit codes:
   0   key printed
   1   vController not reachable in time
   2   login / password change failed
   3   project / key creation failed

TLS verification:
  - Default: verify against the system CA store.
  - Recommended: pass --ca-bundle /path/to/ca.pem to verify against an
    explicit bundle. For self-signed vController, fetch the cert with
    `openssl s_client -connect IP:443 -showcerts < /dev/null` after
    deploy and trust it locally.
  - Last resort: --insecure disables verification entirely. Only use when
    you provisioned the vController yourself moments ago and are reaching
    it by IP over the AWS backbone, never on a path you cannot attest.

API notes:
  - Endpoints live under /cloudlens/api/v1 in four families: admin, identity,
    mgmt, agent. Confirmed live against vController 6.14.1 by fetching each
    family's spec from <api>/cloudlens/api/v1/<family>/generate-spec. An earlier
    version targeted /api/v3, which does not exist on this product and returned
    405 for every call via the SPA fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import secrets
import time
from typing import Optional, Union

import requests

# `verify` is either True (use system CA bundle - default), a string path to a
# CA bundle (TOFU pinning), or False (explicit --insecure opt-in).
TLSVerify = Union[bool, str]

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "Cl0udLens@dm!n"

# vController / CloudLens Manager REST API endpoints (v6.x lineage)
# Endpoint layout comes from the CloudLens vController User Guide v6.14.1,
# "REST API" (p.180-182). The API is split into four families under a single
# /cloudlens/api/v1 root:
#
#   admin     <api>/cloudlens/api/v1/admin/...      identity sources, users
#   identity  <api>/cloudlens/api/v1/identity/...   registration and login
#   mgmt      <api>/cloudlens/api/v1/mgmt/...       application core (projects)
#   agent     <api>/cloudlens/api/v1/agent/...      called by sensors
#
# Authentication is a JWT returned at login and sent as "Authorization: jwt
# <token>" on every subsequent request. The guide's own worked example creates a
# project by logging in through the identity API, taking the token AND the
# account id from the response, then posting to the management API.
#
# An earlier version of this script targeted /api/v3/... That prefix does not
# exist on this product: every request returned 405 because the SPA fallback
# answers any unmatched path, which also made the old readiness probe pass
# while nothing was actually up.
# Endpoints and payloads below were confirmed live against vController 6.14.1
# (build CLMS 6.12.1-32) by fetching each family's OpenAPI spec from
# <api>/cloudlens/api/v1/<family>/generate-spec and exercising the calls.
API_ROOT      = "/cloudlens/api/v1"
EP_LOGIN      = API_ROOT + "/identity/login"
EP_PREFS      = API_ROOT + "/mgmt/accounts/{account_id}/preferences"
# Two password endpoints exist. admin/password/change takes a challenge/answer
# pair and rejects a plain new_password with 409 "Token validation failed", even
# on a fresh token. The mgmt one below takes old_password + new_password and is
# the correct call for completing the forced first-login change. Both verified
# live: the mgmt call returns 200 and the new password logs in immediately.
EP_PWCHANGE   = API_ROOT + "/mgmt/accounts/{account_id}/change_password"
EP_PROJECTS   = API_ROOT + "/mgmt/accounts/{account_id}/projects"

# Sent on every authenticated call. Note the scheme is literally "jwt", not
# "Bearer"; the guide is explicit about this and Bearer is rejected.
AUTH_SCHEME   = "jwt"


def log(msg: str) -> None:
    print(f"[vc-project-key] {msg}", file=sys.stderr, flush=True)


def wait_for_api(base_url: str, verify: TLSVerify, max_seconds: int = 1200) -> None:
    """Poll until the vController REST API is actually serving, not just nginx.

    Do NOT probe GET / for a 200 containing "cloudlens". nginx starts serving
    the single-page app roughly ten minutes before the API backend comes up,
    and the SPA fallback returns 200 with that HTML for EVERY unmatched path,
    including bogus ones. A probe like that reports ready far too early and the
    first real call then fails with a confusing HTTP 405.

    Probe the login endpoint itself instead. While only the SPA is up, POST is
    unroutable and nginx answers 405. Once the API is live the route exists and
    answers 400/401/422 for bad credentials, which is what we are waiting for.
    """
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        try:
            r = requests.post(f"{base_url}{EP_LOGIN}",
                              json={"username": "__readiness_probe__", "password": "__probe__"},
                              verify=verify, timeout=8)
            ctype = r.headers.get("Content-Type", "").lower()
            # 405 = SPA fallback only. Anything else from this route, or any
            # JSON body, means the API is answering.
            # 405 = SPA fallback only, backend not routed yet.
            # 503 = nginx is up and proxying but the API process is still
            #       starting; this is the normal state for ~10 minutes and must
            #       NOT be mistaken for ready.
            if r.status_code not in (405, 502, 503, 504) and (
                    r.status_code in (200, 400, 401, 403, 422) or "json" in ctype):
                log(f"vController API is serving (login probe HTTP {r.status_code})")
                return
        except requests.RequestException:
            pass
        remaining = int(deadline - time.time())
        log(f"  waiting for vController API on {base_url} ({remaining}s remaining)")
        time.sleep(15)
    log("vController API did not start in time. The appliance needs about 15 "
        "minutes after CREATE_COMPLETE, and a private subnet still needs "
        "outbound internet for first-boot activation.")
    raise SystemExit(1)


def _dig(obj, *names):
    """Find the first matching key anywhere in a nested dict/list response.

    The guide states a JWT and an account id come back from login but does not
    print the payload, and the shape has moved between releases. Rather than
    hardcode one path, search for any of the plausible key names.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names and isinstance(v, (str, int)):
                return str(v)
        for v in obj.values():
            found = _dig(v, *names)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _dig(v, *names)
            if found:
                return found
    return None


def login(session: requests.Session, base_url: str, username: str, password: str,
          verify: TLSVerify):
    """Authenticate and arm the session with the JWT.

    Returns (ok, account_id). The account id is needed as a parameter when
    creating a project through the management API.
    """
    # The identity API expects capitalised Email/Password, not username/password.
    # Wrong field names return 200-less validation errors naming the fields.
    r = session.post(f"{base_url}{EP_LOGIN}",
                     json={"Email": username, "Password": password},
                     verify=verify, timeout=15)
    if r.status_code != 200:
        log(f"login failed (HTTP {r.status_code}): {r.text[:200]}")
        return False, None

    try:
        body = r.json()
    except ValueError:
        body = {}

    # Verified response shape: ActiveSessionCredentials.JwtToken and
    # Accounts.OwningAccount.id. _dig still walks the tree in case a future
    # build renames them, but these are the confirmed keys.
    token = (_dig(body, "jwttoken", "token", "jwt", "access_token")
             or None)
    account = _dig(body, "id") if isinstance(body.get("Accounts"), dict) else None
    if not account:
        account = _dig(body, "account_id", "accountid", "account")

    if not token:
        keys = list(body.keys()) if isinstance(body, dict) else type(body).__name__
        log(f"login returned 200 but no token found. Response keys: {keys}")
        return False, None

    session.headers["Authorization"] = f"{AUTH_SCHEME} {token}"
    # change_password True means the appliance will force a change on the next
    # UI login. The caller rotates it to a known value so no human is ambushed.
    must_change = _dig(body, "change_password")
    log(f"login succeeded as {username}"
        + (f" (account {account})" if account else " (no account id in response)")
        + (" [password change pending]" if str(must_change).lower() == "true" else ""))
    return True, account


def rotate_password(session: requests.Session, base_url: str, account_id: str,
                    old_password: str, new_password: str, verify: TLSVerify) -> bool:
    """Complete the forced first-login password change to a KNOWN value.

    This is the fix for the ambush: the appliance flags admin to change the
    password on first UI login. If automation logs in with the default, creates
    a project, and leaves, the first human to open the UI is forced to set a new
    password, which silently invalidates every token and locks out anyone who
    only had the default. Rotating here to a value the caller reports means the
    login the operator is handed actually works.

    Verified endpoint: PUT /mgmt/accounts/{account_id}/change_password, body
    {"old_password": ..., "new_password": ...} -> 200. (The admin/password/change
    endpoint is a different, challenge/answer flow and rejects a plain payload
    with 409.) Returns True on success.
    """
    try:
        r = session.put(f"{base_url}{EP_PWCHANGE.format(account_id=account_id)}",
                        json={"old_password": old_password, "new_password": new_password},
                        verify=verify, timeout=15)
        if r.status_code in (200, 204):
            log("admin password set to the known value")
            return True
        log(f"password change returned {r.status_code} (continuing): {r.text[:160]}")
    except requests.RequestException as exc:
        log(f"password change non-fatal error: {exc}")
    return False


def find_or_create_project(session: requests.Session, base_url: str, account_id: str,
                           project_name: str, verify: TLSVerify):
    """Create the project (or reuse it) and return (project_id, project_key).

    The create-project response carries the api_key directly, so there is no
    separate key-minting call. This is the whole reason the API path needs no
    manually created user: the project sits under the login account's
    OwningAccount, and its api_key IS the project key shown in the UI.

    Verified: POST /mgmt/accounts/{account_id}/projects, body {"project_name":
    ..., "comment": ...} -> {"id": ..., "api_key": ...}.
    """
    url = f"{base_url}{EP_PROJECTS.format(account_id=account_id)}"

    # Reuse an existing project of the same name so re-runs are idempotent.
    r = session.get(url, verify=verify, timeout=15)
    if r.status_code == 200:
        try:
            existing = r.json()
            rows = existing.get("data", existing) if isinstance(existing, dict) else existing
            for proj in (rows or []):
                if proj.get("name") == project_name or proj.get("project_name") == project_name:
                    pid = str(proj.get("id") or proj.get("project_id"))
                    key = proj.get("api_key") or proj.get("project_key")
                    log(f"project exists: {project_name} ({pid})")
                    return pid, key
        except ValueError:
            pass

    create = session.post(url,
                          json={"project_name": project_name,
                                "comment": "Auto-created by CloudLens Autopilot"},
                          verify=verify, timeout=15)
    if create.status_code in (200, 201):
        body = create.json()
        pid = str(body.get("id") or body.get("project_id"))
        key = body.get("api_key") or body.get("project_key")
        log(f"project created: {project_name} ({pid})")
        return pid, key
    log(f"project creation failed (HTTP {create.status_code}): {create.text[:200]}")
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="vController IP or FQDN")
    ap.add_argument("--project", default="cloudlens-demo", help="project name to create / find")
    ap.add_argument("--new-password",
                    default=os.environ.get("VCONTROLLER_NEW_PASS", ""),
                    help="set admin to this known password (also via VCONTROLLER_NEW_PASS env). "
                         "If omitted a strong one is generated and reported.")
    ap.add_argument("--creds-file",
                    default=os.environ.get("VCONTROLLER_CREDS_FILE", ""),
                    help="also write the working UI login (url, user, password, project, key) "
                         "to this file as JSON, mode 600")
    ap.add_argument("--wait", type=int, default=1200, help="max seconds to wait for boot")
    tls_grp = ap.add_mutually_exclusive_group()
    tls_grp.add_argument("--ca-bundle", default=os.environ.get("REQUESTS_CA_BUNDLE", ""),
                         help="path to a CA bundle that signs the vController cert (recommended)")
    tls_grp.add_argument("--insecure", action="store_true",
                         help="DISABLE TLS verification. Only safe for a freshly-deployed VM "
                              "you provisioned yourself, reached over the AWS backbone, by IP. "
                              "Never use against an FQDN you cannot fully attest.")
    args = ap.parse_args()

    if args.insecure:
        # Localised, opt-in disable. Silence only the warning that follows from
        # our own choice; do not blanket-suppress urllib3 for the whole process.
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log("WARNING: TLS verification disabled (--insecure). MITM is theoretically possible.")
        verify: TLSVerify = False
    elif args.ca_bundle:
        verify = args.ca_bundle
        log(f"TLS verification ON using CA bundle {args.ca_bundle}")
    else:
        verify = True
        log("TLS verification ON using system CA store")

    # A known admin password is not optional: the appliance forces a change on
    # first UI login, so leaving the default in place guarantees the operator's
    # handed-over login stops working the moment someone opens the console.
    # Generate a strong one if the caller did not supply it.
    new_password = args.new_password or ("Cl" + secrets.token_urlsafe(12) + "@1")

    base = f"https://{args.host}"
    wait_for_api(base, verify, max_seconds=args.wait)

    session = requests.Session()
    admin_password = None

    # Try the known password first (idempotent re-run), then the default.
    ok, account_id = login(session, base, DEFAULT_ADMIN_USER, new_password, verify)
    if ok:
        admin_password = new_password
        log("admin already set to the known password")
    else:
        session = requests.Session()
        ok, account_id = login(session, base, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASS, verify)
        if not ok:
            log("could not log in with the known password or the marketplace "
                "default. If a human already changed it in the UI, pass that "
                "value with --new-password.")
            return 2
        # Logged in with the default: complete the forced change now.
        if rotate_password(session, base, account_id, DEFAULT_ADMIN_PASS, new_password, verify):
            session = requests.Session()
            ok, account_id = login(session, base, DEFAULT_ADMIN_USER, new_password, verify)
            admin_password = new_password if ok else DEFAULT_ADMIN_PASS
        else:
            admin_password = DEFAULT_ADMIN_PASS
        if not ok:
            return 2

    if not account_id:
        log("no account id resolved from login; cannot address the projects API")
        return 2

    proj_id, key = find_or_create_project(session, base, account_id, args.project, verify)
    if not key:
        return 3

    # The machine-readable line the sensor step consumes: the project key alone
    # on stdout. Everything else goes to stderr so piping stays clean.
    print(key)

    ui_url = f"{base}/cloudlens/login"
    log("")
    log("=====================================================================")
    log(" CloudLens vController is ready. Log in to see the project and the")
    log(" VMs whose sensors have registered:")
    log(f"   URL       {ui_url}")
    log(f"   Username  {DEFAULT_ADMIN_USER}")
    log(f"   Password  {admin_password}")
    log(f"   Project   {args.project}")
    log(f"   Key       {key}")
    log("=====================================================================")

    if args.creds_file:
        try:
            fd = os.open(args.creds_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"url": ui_url, "username": DEFAULT_ADMIN_USER,
                           "password": admin_password, "project": args.project,
                           "project_key": key}, fh, indent=2)
            log(f"login written to {args.creds_file} (mode 600)")
        except OSError as exc:
            log(f"could not write creds file: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
