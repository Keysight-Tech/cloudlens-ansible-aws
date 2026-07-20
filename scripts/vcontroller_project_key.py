#!/usr/bin/env python3
"""
Automate project creation and API-key retrieval on Keysight CloudLens vController.

Used by demo/setup-aws-visibility-demo.sh so the live demo build is fully hands-off:
no operator click required between deploying vController and running the
sensor Ansible playbook.

Behaviour:
  1. Wait for the EULA endpoint to respond.
  2. Accept the EULA.
  3. Change the admin password from the marketplace default to a known value.
  4. Log in.
  5. Create (or look up) a project by name.
  6. Mint (or fetch) an API key for that project.
  7. Print the key on stdout (nothing else).

Stderr carries progress messages so callers can capture stdout cleanly:
    KEY=$(python3 vcontroller_project_key.py --host 1.2.3.4 --project netrefer-demo)

Exit codes:
   0   key printed
   1   vController not reachable in time
   2   login failed
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
  - The CloudLens vController REST API surface used here matches the public
    "/api/v3" endpoints documented for CloudLens Manager 6.x. If a future
    vController rev moves endpoints, swap the URL paths at the top of the
    file - the auth/session pattern is unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional, Union

import requests

# `verify` is either True (use system CA bundle - default), a string path to a
# CA bundle (TOFU pinning), or False (explicit --insecure opt-in).
TLSVerify = Union[bool, str]

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "Cl0udLens@dm!n"

# vController / CloudLens Manager REST API endpoints (v6.x lineage)
EP_EULA       = "/api/v3/users/me/agreements"
EP_LOGIN      = "/api/v3/users/login"
EP_PWCHANGE   = "/api/v3/users/password"
EP_PROJECTS   = "/api/v3/projects"
EP_PROJ_KEYS  = "/api/v3/projects/{project_id}/keys"


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
            if r.status_code != 405 and (r.status_code in (200, 400, 401, 403, 422)
                                         or "json" in ctype):
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


def login(session: requests.Session, base_url: str, username: str, password: str,
          verify: TLSVerify) -> bool:
    r = session.post(f"{base_url}{EP_LOGIN}",
                     json={"username": username, "password": password},
                     verify=verify, timeout=15)
    if r.status_code == 200:
        log(f"login succeeded as {username}")
        return True
    log(f"login failed (HTTP {r.status_code}): {r.text[:160]}")
    return False


def accept_eula(session: requests.Session, base_url: str, verify: TLSVerify) -> None:
    """Best-effort EULA acceptance. Older builds gate the first API call on this."""
    try:
        session.put(f"{base_url}{EP_EULA}",
                    json={"acceptedEula": True}, verify=verify, timeout=10)
        log("EULA acceptance posted")
    except requests.RequestException as exc:
        log(f"EULA acceptance not strictly required (or skipped): {exc}")


def rotate_password(session: requests.Session, base_url: str, new_password: str,
                    verify: TLSVerify) -> None:
    """vController forces a password change on first login. Set it to a known value."""
    try:
        r = session.put(f"{base_url}{EP_PWCHANGE}",
                        json={"currentPassword": DEFAULT_ADMIN_PASS,
                              "newPassword": new_password},
                        verify=verify, timeout=15)
        if r.status_code in (200, 204):
            log("admin password rotated")
        else:
            log(f"password rotate returned {r.status_code} (continuing): {r.text[:120]}")
    except requests.RequestException as exc:
        log(f"password rotate non-fatal error: {exc}")


def find_or_create_project(session: requests.Session, base_url: str,
                           project_name: str, verify: TLSVerify) -> Optional[str]:
    r = session.get(f"{base_url}{EP_PROJECTS}", verify=verify, timeout=15)
    if r.status_code == 200:
        for proj in r.json().get("data", r.json() if isinstance(r.json(), list) else []):
            if proj.get("name") == project_name:
                log(f"project exists: {project_name} ({proj.get('id', proj.get('_id'))})")
                return str(proj.get("id", proj.get("_id")))
    create = session.post(f"{base_url}{EP_PROJECTS}",
                          json={"name": project_name,
                                "description": "Auto-created by setup-aws-visibility-demo.sh"},
                          verify=verify, timeout=15)
    if create.status_code in (200, 201):
        body = create.json()
        proj_id = body.get("id") or body.get("_id") or body.get("data", {}).get("id")
        log(f"project created: {project_name} ({proj_id})")
        return str(proj_id)
    log(f"project creation failed (HTTP {create.status_code}): {create.text[:160]}")
    return None


def get_project_api_key(session: requests.Session, base_url: str,
                        project_id: str, verify: TLSVerify) -> Optional[str]:
    r = session.get(f"{base_url}{EP_PROJ_KEYS.format(project_id=project_id)}",
                    verify=verify, timeout=15)
    if r.status_code == 200:
        body = r.json()
        keys = body.get("data", body if isinstance(body, list) else [])
        if keys:
            key = keys[0].get("apiKey") or keys[0].get("key") or keys[0].get("value")
            if key:
                log("reused existing project API key")
                return key
    create = session.post(f"{base_url}{EP_PROJ_KEYS.format(project_id=project_id)}",
                         json={"name": "demo-sensors", "description": "Auto-created"},
                         verify=verify, timeout=15)
    if create.status_code in (200, 201):
        body = create.json()
        key = body.get("apiKey") or body.get("key") or body.get("value") \
              or body.get("data", {}).get("apiKey")
        if key:
            log("minted new project API key")
            return key
    log(f"key fetch / mint failed (HTTP {create.status_code}): {create.text[:160]}")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="vController IP or FQDN")
    ap.add_argument("--project", default="cloudlens-demo", help="project name to create / find")
    ap.add_argument("--new-password",
                    default=os.environ.get("VCONTROLLER_NEW_PASS", ""),
                    help="rotate admin to this password on first login (also via VCONTROLLER_NEW_PASS env)")
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

    base = f"https://{args.host}"
    wait_for_api(base, verify, max_seconds=args.wait)

    session = requests.Session()
    if not login(session, base, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASS, verify):
        if args.new_password and login(session, base, DEFAULT_ADMIN_USER, args.new_password, verify):
            log("admin password was already rotated; using supplied --new-password")
        else:
            return 2
    else:
        accept_eula(session, base, verify)
        if args.new_password:
            rotate_password(session, base, args.new_password, verify)
            session = requests.Session()
            if not login(session, base, DEFAULT_ADMIN_USER, args.new_password, verify):
                return 2

    proj_id = find_or_create_project(session, base, args.project, verify)
    if not proj_id:
        return 3
    key = get_project_api_key(session, base, proj_id, verify)
    if not key:
        return 3

    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
