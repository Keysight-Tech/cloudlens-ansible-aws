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

With no --codes and an interactive terminal the script prompts for each activation
code, looks it up, and asks how many of each entitlement to activate. There is no
built-in code list and no fallback: with no --codes and no TTY the script exits 2.
Exit: 0 all activated / already installed, 2 no codes supplied non-interactively,
5 nothing could be activated, 6 auth/backend, 130 cancelled at the prompt.
"""
from __future__ import annotations
import argparse, json, sys, time, urllib.request, urllib.error, ssl


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


def accept_eula(kvo, verify):
    """Accept any pending KVO EULA.

    A freshly booted KVO 302-redirects EVERY request to /eula/static/ until its
    EULA is accepted, including the Keycloak token endpoint. So auth here fails
    with a JSON parse error on an HTML redirect body, which reads as a broken
    KVO rather than an unsigned agreement. Licensing runs before adoption in the
    deploy chain, and only the adopt script accepted the EULA, so on a fresh KVO
    licensing could never succeed no matter how valid the activation code was.

    This is a legal acceptance, so it is gated behind --accept-eula.
    """
    base = "https://%s" % kvo
    ctx = _ctx(verify)
    try:
        req = urllib.request.Request(base + "/eula/v1/eula")
        with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
            items = json.loads(r.read().decode("utf8", "ignore"))
    except Exception as e:
        print("[license] could not list EULAs: %s" % e); return False
    pending = [e for e in items if not e.get("accepted")]
    if not pending:
        return True
    for e in pending:
        try:
            req = urllib.request.Request(
                base + "/eula/v1/eula/%s" % e["id"],
                data=json.dumps({"accepted": True}).encode("utf8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
                print("[license] accepted KVO EULA %s (HTTP %s)" % (e["id"], r.status))
        except Exception as ex:
            print("[license] could not accept EULA %s: %s" % (e["id"], ex)); return False
    return True


def token(kvo, user, pw, verify):
    url = f"https://{kvo}/auth/realms/keysight/protocol/openid-connect/token"
    data = f"grant_type=password&client_id=vision-orchestrator&username={user}&password={pw}".encode()
    r = urllib.request.Request(url, data=data, method="POST")
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(r, context=_ctx(verify), timeout=30) as resp:
        return json.loads(resp.read().decode())["access_token"]


def poll_op(kvo, tok, first, verify, want_result=True, timeout=120, label=None):
    """Drive an async licensing op. `first` is the POST response ({"url": ...}).

    `label` prints a live heartbeat while the op runs. These calls go out to the
    KSM backend and can sit silent for tens of seconds; with no output the
    operator cannot tell activation from a hang, and reads a working deploy as
    stuck. Say what is being waited on, and keep ticking.
    """
    url = first.get("url") if isinstance(first, dict) else None
    if not url:
        return first  # already synchronous
    if url.startswith("/"):
        url = f"https://{kvo}{url}"
    deadline = time.time() + timeout
    state = "IN_PROGRESS"
    if label:
        print(f"    {label}", end="", flush=True)
    ticks = 0
    while time.time() < deadline:
        _, body = _req("GET", url, tok, verify=verify)
        state = (body or {}).get("state", "") if isinstance(body, dict) else ""
        if state and state != "IN_PROGRESS":
            break
        if label:
            ticks += 1
            # a dot every 2s, and the elapsed seconds every 10, so a long wait
            # still visibly advances instead of looking frozen
            print("." if ticks % 5 else f" {ticks*2}s", end="", flush=True)
        time.sleep(2)
    if label:
        print(f" {state or 'timed out'}", flush=True)
    if want_result:
        _, res = _req("GET", url.rstrip("/") + "/result", tok, verify=verify)
        return {"state": state, "result": res}
    return {"state": state}


def entitlements(result):
    """Normalise a retrieve-activation-code-info result to [(product, avail, total)].

    Observed KVO builds return a single object with one `product`, but the result is
    funnelled through a list so a code that unlocks several entitlements is handled
    without further changes.
    """
    if isinstance(result, list):
        cand = result
    elif isinstance(result, dict):
        cand = None
        for key in ("products", "entitlements", "licenses", "items"):
            v = result.get(key)
            if isinstance(v, list) and v:
                cand = v
                break
        if cand is None:
            cand = [result] if result.get("product") else []
    else:
        cand = []
    out = []
    for it in cand:
        if not isinstance(it, dict) or not it.get("product"):
            continue
        out.append((it.get("product"),
                    it.get("availableQuantity", it.get("totalQuantity", 0)),
                    it.get("totalQuantity")))
    return out


def lookup_code(kvo, base, tok, code, verify):
    """POST retrieve-activation-code-info for one code; returns (entitlements, info)."""
    st, resp = _req("POST", f"{base}/api/v2/licensing/operations/retrieve-activation-code-info",
                    tok, {"activationCode": code}, verify)
    info = poll_op(kvo, tok, resp, verify)
    result = info.get("result") if isinstance(info, dict) else None
    return entitlements(result), info


def ask_quantity(product, avail):
    """Ask how many of one entitlement to activate; blank means all of availableQuantity."""
    print(f"    -> {product:<24} available: {avail}")
    while True:
        raw = input(f"       How many to activate? [{avail}]: ").strip()
        if not raw:
            return avail
        try:
            q = int(raw)
        except ValueError:
            print(f"       not a number: {raw!r}. Enter 1 to {avail}, or blank for {avail}.")
            continue
        if q < 1:
            print(f"       quantity must be 1 or more. Enter 1 to {avail}, or blank for {avail}.")
            continue
        if q > avail:
            print(f"       only {avail} available. Enter 1 to {avail}, or blank for {avail}.")
            continue
        return q


# The three entitlements a full deployment draws on. KVO is the licence
# authority: the vPB and the sensors pull from its pool, so all three arrive as
# separate activation codes but land in one place. Keywords match the product
# names the KSM backend returns. Used both live (to tell the operator what is
# still missing while they still have codes to hand) and in the final summary.
COVERAGE_CHECKS = [
    ("KVO device licence", ("kvo-device", "visionorchestrator", "kvo device")),
    ("vPB feature licence", ("vpb", "advperm")),
    ("CloudLens sensor credits", ("cloudlens", "credit", "cl-credit")),
]


def coverage_missing(products_text):
    """Return the list of entitlement names not present in products_text (lowercased)."""
    p = (products_text or "").lower()
    return [name for name, keys in COVERAGE_CHECKS if not any(k in p for k in keys)]


def prompt_plan(kvo, base, tok, verify):
    """Interactive prompt: codes, then a quantity per entitlement the code unlocks.

    Raises EOFError / KeyboardInterrupt to the caller, which exits cleanly.
    """
    plan = []
    print("KVO licensing")
    print("  Enter every code you were issued, one at a time. A full KVO")
    print("  deployment usually needs several distinct entitlements, for")
    print("  example sensor credits, KVO device licences, and vPB feature")
    print("  licences. Each arrives as its own activation code.")
    while True:
        if not plan:
            code = input("  Activation code: ").strip()
        else:
            # "blank to finish" after a successful activation reads as a dead
            # end, not an invitation. An operator with a second code pressed
            # Enter and moved on with only one entitlement licensed, then had
            # to go back and add the other by hand. Ask the real question.
            # plan entries are {"code":..., "picks":[(product, qty), ...]}: there
            # is no "product" key at the top level, and reading one gave an empty
            # summary that said "nothing yet" right after a successful activation.
            have = ", ".join(sorted({prod for e in plan for prod, _q in e.get("picks", [])}))
            print(f"  Licensed so far: {have or 'nothing yet'}")
            # Guide, do not label the prompt: a code only reveals its entitlement
            # after the KSM lookup, so we cannot ask for "the CloudLens code"
            # specifically. Instead, say which of the three a full deployment
            # still needs, so the operator enters those before finishing.
            still = coverage_missing(have)
            if still:
                print(f"  Still needed for a full deployment: {', '.join(still)}")
                print("  Enter the activation code for one of those next.")
            else:
                print("  All three entitlements are covered. You can finish here.")
            more = input("  Add another activation code? [y/N]: ").strip().lower()
            if more not in ("y", "yes"):
                return plan
            code = input("  Activation code: ").strip()
        if not code:
            return plan
        print("    checking this code against the KSM licensing backend, one moment...", flush=True)
        ents, info = lookup_code(kvo, base, tok, code, verify)
        if not ents:
            state = info.get("state") if isinstance(info, dict) else info
            print(f"    lookup failed for that code ({state}); check it and try again")
            continue
        picks = []
        for product, avail, _total in ents:
            if not avail:
                print(f"    -> {product:<24} available: 0  (nothing left to activate)")
                continue
            picks.append((product, ask_quantity(product, avail)))
        if picks:
            plan.append({"code": code, "picks": picks})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvo", required=True)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin")
    ap.add_argument("--codes", nargs="*", default=None,
                    help="activation codes, each optionally CODE,QTY; omit to be prompted (TTY only)")
    ap.add_argument("--accept-eula", action="store_true",
                    help="Accept the KVO EULA if pending. A fresh KVO blocks ALL auth, "
                         "including the token endpoint, until this is done. Legal acceptance.")
    ap.add_argument("--insecure", action="store_true")
    a = ap.parse_args()
    verify = not a.insecure
    base = f"https://{a.kvo}"

    plan = []
    interactive = False
    if a.codes:
        for c in a.codes:
            if "," in c:
                code, q = c.split(",", 1)
                plan.append({"code": code.strip(), "qty": int(q.strip())})
            else:
                plan.append({"code": c.strip(), "qty": None})
    elif sys.stdin.isatty():
        interactive = True
    else:
        print("[license] no activation codes supplied and stdin is not a TTY: "
              "pass --codes CODE[,QTY] ...", file=sys.stderr)
        return 2

    # The EULA gate comes BEFORE auth on purpose: a fresh KVO 302-redirects the
    # token endpoint itself, so without this the next line fails with a JSON
    # parse error on an HTML body and reads as a broken KVO.
    if a.accept_eula:
        accept_eula(a.kvo, verify)
    try:
        tok = token(a.kvo, a.user, a.password, verify)
    except Exception as e:
        print(f"[license] auth failed: {e}", file=sys.stderr)
        if not a.accept_eula:
            print("[license] a freshly booted KVO redirects every request, including this "
                  "one, until its EULA is accepted. Re-run with --accept-eula.", file=sys.stderr)
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

    if interactive:
        try:
            plan = prompt_plan(a.kvo, base, tok, verify)
        except (EOFError, KeyboardInterrupt):
            print("\n[license] cancelled at the prompt; nothing activated", file=sys.stderr)
            return 130
        if not plan:
            print("[license] no activation codes entered; nothing to do", file=sys.stderr)
            return 5
        # Say what happens next and roughly how long. Answering "n" used to drop
        # straight into a silent backend round-trip, so a deploy that was working
        # normally read as hung right at the point the operator stopped typing.
        n_codes = len(plan)
        print()
        print(f"  Done collecting codes. Activating {n_codes} code"
              f"{'' if n_codes == 1 else 's'} on KVO now.")
        print("  Each one is sent to the KSM licensing backend and can take up to")
        print("  a minute. Progress is printed per code; nothing is stuck.")
        print("  After this the deploy continues to adoption (Phase 12).")
        print()

    activated = 0
    for item in plan:
        code = item["code"]
        picks = item.get("picks")
        if picks is None:
            qty = item["qty"]
            ents, info = lookup_code(a.kvo, base, tok, code, verify)
            if not ents:
                print(f"[license] code {code[:14]}...: NOT VALID / no info ({info.get('state') if isinstance(info,dict) else info}) -> {info.get('result') if isinstance(info,dict) else None}")
                continue
            picks = []
            for product, avail, total in ents:
                use = qty if qty is not None else avail
                print(f"[license] {code[:14]}... = {product}  avail={avail} total={total} -> activating {use}")
                if not use:
                    print(f"[license]   availableQuantity is 0 (likely stranded on the old host); skipping")
                    continue
                picks.append((product, use))
            if not picks:
                continue
        else:
            for product, use in picks:
                print(f"[license] {code[:14]}... = {product} -> activating {use}")
        if len(picks) == 1:
            body = [{"activationCode": code, "quantity": picks[0][1]}]
        else:
            body = [{"activationCode": code, "product": p, "quantity": q} for p, q in picks]
        st, resp = _req("POST", f"{base}/api/v2/licensing/operations/activate",
                        tok, body, verify)
        act = poll_op(a.kvo, tok, resp, verify,
                      label=f"activating {code[:14]}... against the KSM backend ")
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

    # Coverage, not just a count. KVO is the licence authority for the whole
    # fabric: the vPB and the sensors draw from ITS pool, they are not licensed
    # separately. So a KVO with one entitlement looks "licensed" while the
    # phases that need the others fail later for reasons that read as unrelated.
    # Say which parts of the deployment are covered, while the operator still
    # has their codes to hand.
    products = " ".join(str(L.get("product", "")) for L in (final or [])).lower()
    checks = [
        ("KVO device licences", "adopting the vController and the vPB",
         any(k in products for k in ("kvo-device", "visionorchestrator", "kvo device"))),
        ("vPB feature licences", "vPB advanced features: dedup, data masking, tunnelling",
         any(k in products for k in ("vpb", "advperm"))),
        ("CloudLens sensor credits", "sensors registering and being counted",
         any(k in products for k in ("cloudlens", "credit", "cl-credit"))),
    ]
    print("[license] coverage:")
    missing = []
    for name, purpose, have in checks:
        print(f"    {'yes' if have else 'NO '}  {name:<26} {purpose}")
        if not have:
            missing.append(name)
    if missing:
        print("[license] not covered: " + ", ".join(missing))
        print("[license] those come as separate activation codes. Re-run this script")
        print("[license] with them at any time; activations are additive.")
    return 0 if n > 0 else 5


if __name__ == "__main__":
    sys.exit(main())
