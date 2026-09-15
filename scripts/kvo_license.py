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

Non-interactive modes, used by deploy/teardown-stack.sh (see RELEASE MODES below):
  python3 kvo_license.py --kvo <ip> --list [--json]
  python3 kvo_license.py --kvo <ip> --release-all
  python3 kvo_license.py --kvo <ip> --release CODE[,QTY]
Exit: 0 listed / the KVO reports no licence left, 3 an operation failed or its
outcome is unknown or something remains (the output says which), 6 auth or
unreachable. These modes never print a whole activation code.
"""
from __future__ import annotations
import argparse, json, os, sys, time, urllib.request, urllib.error, urllib.parse, ssl


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


def poll_op(kvo, tok, first, verify, want_result=True, timeout=120, label=None, http_timeout=30):
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
        _, body = _req("GET", url, tok, verify=verify, timeout=http_timeout)
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
        _, res = _req("GET", url.rstrip("/") + "/result", tok, verify=verify, timeout=http_timeout)
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


def read_codes_paste():
    """Read a paste of activation codes: any mix of lines, spaces and commas,
    finished with an empty line. Pasting the whole KSM email at once is the
    point; one-at-a-time was the complaint that produced this."""
    print("  Paste ALL your activation codes at once (one per line, or separated")
    print("  by spaces/commas), then press Enter on an empty line to check them")
    print("  together. A single code works the same way.")
    raw = []
    while True:
        try:
            line = input("  > ").strip()
        except EOFError:
            break
        if not line:
            break
        raw.append(line)
    codes, seen = [], set()
    for tok_ in " ".join(raw).replace(",", " ").split():
        c = tok_.strip()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    return codes


def prompt_plan(kvo, base, tok, verify):
    """Interactive prompt: ALL codes in one paste, ONE batch check against KSM
    showing what every code holds, then a quantity question per entitlement.

    Raises EOFError / KeyboardInterrupt to the caller, which exits cleanly.
    """
    plan = []
    print("KVO licensing")
    print("  A full KVO deployment usually needs several distinct entitlements,")
    print("  for example sensor credits, KVO device licences, and vPB feature")
    print("  licences. Each arrives as its own activation code.")
    while True:
        codes = read_codes_paste()
        if not codes and not plan:
            return plan
        if codes:
            # 1. ONE sweep: every code checked before any question is asked,
            # so the operator sees the whole picture first.
            print(f"    checking {len(codes)} code(s) against the KSM licensing backend, one moment...",
                  flush=True)
            checked = []
            for code in codes:
                ents, info = lookup_code(kvo, base, tok, code, verify)
                checked.append((code, ents, info))
            print()
            print("  What your codes hold:")
            for code, ents, info in checked:
                short = code if len(code) <= 24 else code[:21] + "..."
                if not ents:
                    state = info.get("state") if isinstance(info, dict) else info
                    print(f"    {short:<24}  LOOKUP FAILED ({state})")
                    continue
                for product, avail, total in ents:
                    print(f"    {short:<24}  {product:<24} available: {avail} of {total}")
            print()
            # 2. Quantities, per entitlement, in the same order.
            for code, ents, _info in checked:
                picks = []
                for product, avail, _total in (ents or []):
                    if not avail:
                        print(f"    -> {product:<24} available: 0  (nothing left to activate)")
                        continue
                    picks.append((product, ask_quantity(product, avail)))
                if picks:
                    plan.append({"code": code, "picks": picks})
        # 3. Coverage: say what a full deployment still lacks, then offer
        # another paste rather than silently finishing half-licensed.
        have = ", ".join(sorted({prod for e in plan for prod, _q in e.get("picks", [])}))
        print(f"  Licensed so far: {have or 'nothing yet'}")
        still = coverage_missing(have)
        if still:
            print(f"  Still needed for a full deployment: {', '.join(still)}")
        else:
            print("  All three entitlements are covered. You can finish here.")
        more = input("  Add more activation codes? [y/N]: ").strip().lower()
        if more not in ("y", "yes"):
            return plan


# =====================================================================
# RELEASE MODES: --list, --release-all, --release CODE[,QTY]
#
# Added for deploy/teardown-stack.sh. Activation codes are bound to the KVO
# host they were activated on; while that host is alive the counts can be
# returned with operations/deactivate (proven: 20 counts recovered that way),
# and once it is deleted they are stranded for good. Teardown is the last
# moment the KVO is alive, so it needs a way to release everything the KVO
# holds and to know, not guess, whether that worked.
#
# The rules below are the console's (cloudlens_console/api.py) and are
# deliberately strict in the safe direction:
#   * only SUCCESS counts as a finished, successful operation;
#   * a poll that ran out of time is UNKNOWN, never a success;
#   * a licence list that could not be read is UNKNOWN, never "clear";
#   * exit 0 only when every operation succeeded AND the KVO then reports
#     no licence left. Anything else is exit 3 and the output says which.
# A whole activation code is never printed: it is a credential, and the
# teardown's output lands in terminals and logs. The last 4 characters are
# enough to match a row against the KVO UI.
# =====================================================================

# The states that mean an operation FINISHED and succeeded. An allow-list,
# and a short one: the deactivates that proved the release path answered
# SUCCESS, and a word wrongly counted as success is a licence count nobody
# released under a banner saying nothing will be stranded.
_OP_DONE = ("SUCCESS",)

# The envelope keys a licence list could plausibly arrive under. Enough to
# say the KVO STILL HOLDS something, never enough to say it holds nothing.
_LIST_KEYS = ("licenses", "licences", "items", "rows", "data")

EXIT_UNKNOWN = 3   # an op failed or its outcome is unknown, or something remains
EXIT_AUTH = 6      # wrong password, EULA pending, or the KVO did not answer


def _op_failed(state):
    """Whether the KVO REFUSED the operation: FAIL and ERROR as substrings,
    so FAILED, FAILURE and INTERNAL_ERROR are one answer."""
    s = str(state or "").upper()
    return "FAIL" in s or "ERROR" in s


def _op_ok(state):
    """Whether the operation finished AND succeeded. Allow-list only: a
    deny-list let IN_PROGRESS (a poll that ran out of time) count as done."""
    return str(state or "").upper() in _OP_DONE


def _op_running(state):
    """Whether the outcome is NOT KNOWN: IN_PROGRESS at the deadline, no
    state at all (the poll never read a JSON object), or a word that is
    neither a known success nor a failure. None of these is a refusal, and
    none of them is a success."""
    return not _op_ok(state) and not _op_failed(state)


def mask(code):
    """The last 4 characters of an activation code, the rest hidden."""
    s = str(code or "")
    if not s:
        return "(no code)"
    return "****-" + s[-4:]


def _scrub(obj, codes):
    """Replace every full activation code inside a KVO response with its
    mask, recursively, so a failure detail can be printed without leaking
    the credential it is about."""
    if isinstance(obj, dict):
        return {k: _scrub(v, codes) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, codes) for v in obj]
    if isinstance(obj, str):
        for c in codes:
            if c and c in obj:
                obj = obj.replace(c, mask(c))
        return obj
    return obj


def _field(row, *keys):
    """First present, non-empty value among several plausible key names.
    The proven rows carry activationCode, product and quantity; part id and
    expiry are read tolerantly and shown as '-' when the build omits them."""
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _row_code(row):
    return _field(row, "activationCode", "code") if isinstance(row, dict) else None


def _row_qty(row):
    try:
        q = int(row.get("quantity", 0)) if isinstance(row, dict) else 0
    except (TypeError, ValueError):
        q = 0
    return max(q, 0)


def row_view(row):
    """One licence row with its code masked: what --list prints and what
    --json returns. Nothing else from the row is passed through."""
    return {
        "part": _field(row, "partNumber", "partId", "part", "partNo") or "-",
        "product": _field(row, "product", "productName") or "-",
        "quantity": _row_qty(row),
        "code_last4": mask(_row_code(row)),
        "expiry": _field(row, "expirationDate", "expiryDate", "expiration", "expires", "endDate") or "-",
    }


def _shape(body):
    if body is None:
        return "no body"
    if isinstance(body, list):
        return "a list"
    if isinstance(body, dict):
        return "an object"
    return "text (an HTML page, or a plain-text error)"


def kvo_base(kvo):
    """The API base for the release modes. A bare address is https, as the
    activation flow assumes; an explicit scheme is honoured so a KVO reached
    through a plain-http hop, or a test double, works without special-casing."""
    return kvo if "://" in kvo else "https://%s" % kvo


def auth(kvo, user, pw, verify, timeout):
    """(token, None) or (None, why). The reasons are the ones the teardown
    has to tell apart: unreachable, refused credentials, and a pending EULA,
    which redirects the token endpoint itself to an HTML page."""
    url = "%s/auth/realms/keysight/protocol/openid-connect/token" % kvo_base(kvo)
    data = ("grant_type=password&client_id=vision-orchestrator&username=%s&password=%s"
            % (urllib.parse.quote(user, safe=""), urllib.parse.quote(pw, safe=""))).encode()
    r = urllib.request.Request(url, data=data, method="POST")
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(r, context=_ctx(verify), timeout=timeout) as resp:
            body = resp.read().decode("utf8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return None, "the KVO at %s refused the credentials for user %r (HTTP %s)" % (kvo, user, e.code)
        return None, "the KVO at %s answered HTTP %s at the login endpoint" % (kvo, e.code)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return None, "the KVO at %s did not answer within %ss" % (kvo, timeout)
        return None, "could not reach the KVO at %s (%s)" % (kvo, reason)
    except (TimeoutError, OSError) as e:
        return None, "could not reach the KVO at %s (%s)" % (kvo, e)
    try:
        tok = json.loads(body).get("access_token")
    except ValueError:
        return None, ("the KVO at %s answered the login with something other than a token: "
                      "a KVO whose EULA is not yet accepted redirects every request, including "
                      "login, to its EULA page" % kvo)
    if not tok:
        return None, "the KVO at %s answered the login without a token" % kvo
    return tok, None


def read_list(base, tok, verify, timeout):
    """(rows, why): the licences the KVO says it holds, and why they could
    not be read. `why` is None only when the KVO answered 200 with the JSON
    ARRAY this endpoint returns.

    _req does not raise on an HTTP status error: the body of a 500 is a
    dict, of a pending EULA an HTML page, of no answer None. Reading every
    one of those as "no licences" is the same value a KVO holding nothing
    returns, and the teardown gate would stand on it. A wrapped array is
    mined for rows, because rows that ARE there prove the KVO holds
    licences; its emptiness proves nothing, so it still carries a why."""
    try:
        code, body = _req("GET", base + "/api/v2/licensing/licenses", tok, verify=verify, timeout=timeout)
    except Exception as exc:
        return [], "the KVO did not answer GET licenses (%s)" % type(exc).__name__
    if isinstance(body, list) and code == 200:
        return body, None
    rows = []
    if isinstance(body, dict):
        for key in _LIST_KEYS:
            if isinstance(body.get(key), list):
                rows = body[key]
                break
    return rows, ("GET licenses answered HTTP %s with %s, not the list of licences this KVO's API returns"
                  % (code, _shape(body)))


def holdings(base, tok, verify, timeout):
    """{licences, count, clear, unreadable?}. `clear` is tri-state and is
    the only thing a teardown may stand on: True on a genuine empty list,
    False on any answer that named a licence, None when the list could not
    be read (with the reason beside it)."""
    rows, why = read_list(base, tok, verify, timeout)
    out = {"licences": rows, "count": len(rows),
           "clear": (False if rows else None) if why else not rows}
    if why:
        out["unreadable"] = why
    return out


def _deadline_left(deadline):
    return max(1, int(deadline - time.time()))


def release_rows(kvo, base, tok, targets, verify, deadline, http_timeout):
    """operations/deactivate per (code, qty), each polled to a terminal state
    within what is left of the overall budget. Returns a list of
    {code_last4, quantity, state, ok, running, detail?}."""
    results = []
    codes = [c for c, _q in targets]
    for code, qty in targets:
        if time.time() >= deadline:
            print("[license]   %s x%s: not attempted, the overall time budget is spent" % (mask(code), qty))
            results.append({"code_last4": mask(code), "quantity": qty, "state": "",
                            "ok": False, "running": True})
            continue
        try:
            _, resp = _req("POST", "%s/api/v2/licensing/operations/deactivate" % base, tok,
                           [{"activationCode": code, "quantity": qty}], verify, timeout=http_timeout)
        except Exception as exc:
            print("[license]   %s x%s: the deactivate request was not answered (%s)"
                  % (mask(code), qty, type(exc).__name__))
            results.append({"code_last4": mask(code), "quantity": qty, "state": "",
                            "ok": False, "running": True})
            continue
        # poll_op prefixes a relative op url with https://<kvo>; resolve it
        # against the base here so an explicit scheme in --kvo is kept.
        if isinstance(resp, dict) and str(resp.get("url", "")).startswith("/"):
            resp = dict(resp, url=base + resp["url"])
        try:
            op = poll_op(kvo, tok, resp, verify, timeout=_deadline_left(deadline),
                         label="releasing %s x%s " % (mask(code), qty), http_timeout=http_timeout)
        except Exception as exc:
            # the KVO stopped answering mid-poll: the op was accepted, its
            # outcome is unknown, and unknown is not success
            print()
            op = {"state": "", "result": None, "error": type(exc).__name__}
        state = op.get("state") if isinstance(op, dict) else op
        result = op.get("result") if isinstance(op, dict) else None
        row = {"code_last4": mask(code), "quantity": qty, "state": str(state or ""),
               "ok": _op_ok(state), "running": _op_running(state)}
        if row["ok"]:
            print("[license]   %s x%s: released" % (mask(code), qty))
        elif row["running"]:
            print("[license]   %s x%s: outcome UNKNOWN (state %r at the deadline): not counted as released"
                  % (mask(code), qty, str(state or "")))
        else:
            detail = json.dumps(_scrub(result, codes), sort_keys=True)[:300] if result is not None else ""
            row["detail"] = detail
            print("[license]   %s x%s: FAILED (%s) %s" % (mask(code), qty, state, detail))
        results.append(row)
    return results


def _pick_targets(rows, wanted):
    """(targets, error). With `wanted` None every row with a quantity is a
    target. With `wanted` (code, qty): the row whose code matches, in full
    or by its last 4 characters; qty None means the row's full quantity."""
    if wanted is None:
        return [(_row_code(r), _row_qty(r)) for r in rows if _row_code(r) and _row_qty(r) > 0], None
    code, qty = wanted
    hits = [r for r in rows if _row_code(r) and (str(_row_code(r)) == code or str(_row_code(r)).endswith(code))]
    if not hits:
        return [], "no installed licence matches %s" % mask(code)
    if len(hits) > 1:
        return [], "%d installed licences end in %s; pass the full code" % (len(hits), mask(code))
    row = hits[0]
    use = qty if qty is not None else _row_qty(row)
    if use < 1:
        return [], "%s has quantity %s: nothing to release" % (mask(_row_code(row)), _row_qty(row))
    return [(_row_code(row), use)], None


def _print_rows(rows, kvo):
    views = [row_view(r) for r in rows if isinstance(r, dict)]
    print("[license] %d licence(s) installed on KVO %s" % (len(views), kvo))
    if views:
        print("    %-16s %-24s %8s  %-10s %s" % ("part", "product", "quantity", "code", "expiry"))
        for v in views:
            print("    %-16s %-24s %8s  %-10s %s"
                  % (str(v["part"])[:16], str(v["product"])[:24], v["quantity"], v["code_last4"], v["expiry"]))
    return views


def run_mode(a):
    """--list / --release-all / --release. Exit codes are the contract the
    teardown reads: 0, EXIT_UNKNOWN (3), EXIT_AUTH (6)."""
    verify = not a.insecure
    base = kvo_base(a.kvo)
    deadline = time.time() + a.timeout
    tok, why = auth(a.kvo, a.user, a.password, verify, a.http_timeout)
    if not tok:
        if a.json:
            print(json.dumps({"kvo": a.kvo, "error": why, "exit": EXIT_AUTH}))
        print("[license] %s" % why, file=sys.stderr)
        return EXIT_AUTH

    if a.list:
        h = holdings(base, tok, verify, a.http_timeout)
        if a.json:
            out = {"kvo": a.kvo, "count": h["count"], "clear": h["clear"],
                   "licences": [row_view(r) for r in h["licences"] if isinstance(r, dict)]}
            if h.get("unreadable"):
                out["unreadable"] = h["unreadable"]
            print(json.dumps(out, sort_keys=True))
        else:
            _print_rows(h["licences"], a.kvo)
        if h.get("unreadable"):
            print("[license] the licence list could not be read: %s" % h["unreadable"], file=sys.stderr)
            return EXIT_UNKNOWN
        return 0

    # --release-all / --release CODE[,QTY]
    wanted = None
    if a.release:
        code, _, q = a.release.partition(",")
        code = code.strip()
        if not code:
            print("[license] --release needs CODE[,QTY]", file=sys.stderr)
            return 2
        try:
            qty = int(q.strip()) if q.strip() else None
        except ValueError:
            print("[license] --release quantity must be a number", file=sys.stderr)
            return 2
        wanted = (code, qty)
    rows, why = read_list(base, tok, verify, a.http_timeout)
    if why:
        print("[license] cannot release what cannot be listed: %s" % why, file=sys.stderr)
        return EXIT_UNKNOWN
    views = _print_rows(rows, a.kvo)
    targets, err = _pick_targets(rows, wanted)
    if err:
        print("[license] %s" % err, file=sys.stderr)
        return EXIT_UNKNOWN
    if not targets:
        print("[license] nothing to release")
    else:
        total = sum(q for _c, q in targets)
        print("[license] releasing %d licence(s), %d count(s) in total, back to the entitlement..."
              % (len(targets), total))
    results = release_rows(a.kvo, base, tok, targets, verify, deadline, a.http_timeout)
    failed = [r for r in results if not r["ok"] and not r["running"]]
    unknown = [r for r in results if r["running"]]

    # Re-read, and stand only on what the KVO actually says now.
    after = holdings(base, tok, verify, a.http_timeout)
    remaining = [row_view(r) for r in after["licences"] if isinstance(r, dict)]
    problems = []
    if failed:
        problems.append("%d operation(s) FAILED: %s" % (len(failed), ", ".join(r["code_last4"] for r in failed)))
    if unknown:
        problems.append("%d operation(s) with an UNKNOWN outcome (not success): %s"
                        % (len(unknown), ", ".join(r["code_last4"] for r in unknown)))
    if after["clear"] is None:
        problems.append("the licence list could not be re-read after the release, so the KVO's state is "
                        "UNKNOWN, not clear: %s" % after.get("unreadable"))
    elif after["clear"] is False:
        problems.append("the KVO still holds %d licence(s): %s"
                        % (len(remaining), ", ".join("%s x%s" % (v["code_last4"], v["quantity"]) for v in remaining)))
    for p in problems:
        print("[license] %s" % p, file=sys.stderr)
    if problems:
        print("[license] NOT clear: the counts still on this KVO will be stranded if it is deleted", file=sys.stderr)
    elif wanted is None or not views:
        print("[license] released %d licence(s); the KVO reports no licence left. Nothing will be stranded."
              % sum(1 for r in results if r["ok"]))
    else:
        print("[license] released %d licence(s); the KVO reports no licence left." % sum(1 for r in results if r["ok"]))
    if a.json:
        # last line of stdout, so a caller can take the tail of the output
        print(json.dumps({"kvo": a.kvo, "results": results, "released": sum(1 for r in results if r["ok"]),
                          "failed": len(failed), "unknown": len(unknown), "clear": after["clear"],
                          "remaining": remaining, "unreadable": after.get("unreadable")}, sort_keys=True))
    return EXIT_UNKNOWN if problems else 0


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
    # Non-interactive modes (see RELEASE MODES above). One at a time.
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--list", action="store_true",
                       help="print the installed licences (codes masked to their last 4) and exit")
    modes.add_argument("--release-all", action="store_true",
                       help="deactivate every installed licence with its full quantity; exit 0 only "
                            "when the KVO then reports no licence left")
    modes.add_argument("--release", metavar="CODE[,QTY]",
                       help="deactivate one installed licence (full code, or its last 4 as --list shows it)")
    ap.add_argument("--json", action="store_true", help="machine output for --list / the release modes")
    ap.add_argument("--password-env", metavar="VAR",
                    help="read the password from this environment variable instead of --password, "
                         "so it never appears on a command line")
    ap.add_argument("--timeout", type=int, default=300,
                    help="overall bound in seconds for a release (default 300)")
    ap.add_argument("--http-timeout", type=int, default=15,
                    help="bound in seconds for each HTTP call in the release modes (default 15)")
    a = ap.parse_args()
    if a.password_env:
        pw = os.environ.get(a.password_env)
        if pw is None:
            print(f"[license] --password-env {a.password_env}: that variable is not set", file=sys.stderr)
            return 2
        a.password = pw
    if a.list or a.release_all or a.release:
        return run_mode(a)
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
