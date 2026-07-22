#!/usr/bin/env python3
"""Activate KVO (Vision Orchestrator) product licenses over the REST licensing API.

kvo_adopt_clms.py only CHECKS licensing and stops if the KVO is unlicensed; this
script is the missing piece that opens the gate. It drives the same sequence the
Settings > Product Licensing UI performs:

  1. auth (Keycloak bearer, same as the rest of the API)
  2. operations/test-backend-connectivity   (reach the Keysight backend)
  3. operations/retrieve-activation-code-info per code -> product + availableQuantity
  4. operations/activate  [{activationCode, quantity}]
  5. GET licenses to confirm installed > 0

Every licensing call is async: POST returns {"url": ...}; poll GET <url> until
state leaves IN_PROGRESS, and the detailed result is one level deeper at
GET <url>/result. Quantity to activate defaults to the availableQuantity the
backend reports for each code, so a code that was partly consumed elsewhere still
activates whatever is left rather than failing on "Invalid quantity".

Usage:
  python3 kvo_license.py --kvo <ip> [--codes CODE[,QTY] ...] [--insecure]

With no --codes it uses the three known lab codes (2 CloudLens + 1 VisionOrchestrator).
Exit: 0 all activated / already installed, 5 nothing could be activated, 6 auth/backend.
"""
from __future__ import annotations
import argparse, json, sys, time, urllib.request, urllib.error, ssl

# The three lab activation codes (two CloudLens + one VisionOrchestrator).
DEFAULT_CODES = [
    "889D-0CB0-BC3D-3179",
    "F5A5-CA75-1AED-C0AC",
    "23a9798736-347903a1-b1ad4788-cfa78bb7-b17c98d7-90232753",
]


def _ctx(verify):
    if verify:
        return None
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def _req(method, url, token=None, body=None, verify=False, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, context=_ctx(verify), timeout=timeout) as resp:
            raw = resp.read().decode()
            ct = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if raw and "json" in ct else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def token(kvo, user, pw, verify):
    url = f"https://{kvo}/auth/realms/keysight/protocol/openid-connect/token"
    data = f"grant_type=password&client_id=vision-orchestrator&username={user}&password={pw}".encode()
    r = urllib.request.Request(url, data=data, method="POST")
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(r, context=_ctx(verify), timeout=30) as resp:
        return json.loads(resp.read().decode())["access_token"]


def poll_op(kvo, tok, first, verify, want_result=True, timeout=120):
    """Drive an async licensing op. `first` is the POST response ({"url": ...})."""
    url = first.get("url") if isinstance(first, dict) else None
    if not url:
        return first  # already synchronous
    if url.startswith("/"):
        url = f"https://{kvo}{url}"
    deadline = time.time() + timeout
    state = "IN_PROGRESS"
    while time.time() < deadline:
        _, body = _req("GET", url, tok, verify=verify)
        state = (body or {}).get("state", "") if isinstance(body, dict) else ""
        if state and state != "IN_PROGRESS":
            break
        time.sleep(2)
    if want_result:
        _, res = _req("GET", url.rstrip("/") + "/result", tok, verify=verify)
        return {"state": state, "result": res}
    return {"state": state}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvo", required=True)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--codes", nargs="*", default=None,
                    help="activation codes, each optionally CODE,QTY; default = 3 lab codes")
    ap.add_argument("--insecure", action="store_true")
    a = ap.parse_args()
    verify = not a.insecure
    base = f"https://{a.kvo}"

    raw_codes = a.codes if a.codes else DEFAULT_CODES
    codes = []
    for c in raw_codes:
        if "," in c:
            code, q = c.split(",", 1)
            codes.append((code.strip(), int(q.strip())))
        else:
            codes.append((c.strip(), None))

    try:
        tok = token(a.kvo, a.user, a.password, verify)
    except Exception as e:
        print(f"[license] auth failed: {e}", file=sys.stderr)
        return 6
    print(f"[license] authed to KVO {a.kvo}")

    # already licensed?
    st, existing = _req("GET", f"{base}/api/v2/licensing/licenses", tok, verify=verify)
    if isinstance(existing, list) and existing:
        print(f"[license] already {len(existing)} license(s) installed:")
        for L in existing:
            print(f"    {L.get('activationCode')} {L.get('product')} qty={L.get('quantity')}")
        # continue anyway to top up any missing code, but note it

    # backend connectivity (async, best-effort)
    st, resp = _req("POST", f"{base}/api/v2/licensing/operations/test-backend-connectivity", tok, {}, verify)
    conn = poll_op(a.kvo, tok, resp, verify, want_result=False)
    print(f"[license] backend connectivity: {conn.get('state', resp)}")

    activated = 0
    for code, qty in codes:
        st, resp = _req("POST", f"{base}/api/v2/licensing/operations/retrieve-activation-code-info",
                        tok, {"activationCode": code}, verify)
        info = poll_op(a.kvo, tok, resp, verify)
        result = info.get("result") if isinstance(info, dict) else None
        if not isinstance(result, dict) or not result.get("product"):
            print(f"[license] code {code[:14]}...: NOT VALID / no info ({info.get('state') if isinstance(info,dict) else info}) -> {result}")
            continue
        avail = result.get("availableQuantity", result.get("totalQuantity", 0))
        product = result.get("product")
        use = qty if qty is not None else avail
        print(f"[license] {code[:14]}... = {product}  avail={avail} total={result.get('totalQuantity')} -> activating {use}")
        if not use:
            print(f"[license]   availableQuantity is 0 (likely stranded on the old host); skipping")
            continue
        st, resp = _req("POST", f"{base}/api/v2/licensing/operations/activate",
                        tok, [{"activationCode": code, "quantity": use}], verify)
        act = poll_op(a.kvo, tok, resp, verify)
        state = act.get("state") if isinstance(act, dict) else act
        print(f"[license]   activate -> {state}")
        if state and "FAIL" not in str(state).upper() and "ERROR" not in str(state).upper():
            activated += 1
        else:
            print(f"[license]   activate result: {act.get('result') if isinstance(act,dict) else act}")

    st, final = _req("GET", f"{base}/api/v2/licensing/licenses", tok, verify=verify)
    n = len(final) if isinstance(final, list) else 0
    print(f"[license] final: {n} license(s) installed")
    if isinstance(final, list):
        for L in final:
            print(f"    {L.get('activationCode')} {L.get('product')} qty={L.get('quantity')}")
    return 0 if n > 0 else 5


if __name__ == "__main__":
    sys.exit(main())
