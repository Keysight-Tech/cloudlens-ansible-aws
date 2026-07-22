# Live Deployment Console Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a visitor on the static GitHub Pages docs site run a real CloudLens deployment on their own AWS account and watch it live, by bridging the page to a console running on their own machine.

**Architecture:** The page probes `http://127.0.0.1:8760`. If a console answers, the visitor pastes a one-time pairing code and the panel switches from replaying captured events to streaming real SSE from their machine. Credentials never enter the browser: the console inherits the visitor's shell AWS identity, as it does today. If no console answers, the page silently stays on the replay it already ships.

**Tech Stack:** Python 3 stdlib `ThreadingHTTPServer` (no framework), vanilla JS in `web/app.js`, pytest, Playwright for browser smoke.

**Design doc:** `docs/plans/2026-07-22-live-shell-design.md`

---

## Conventions for every task

- **The suite has no external dependencies and no pytest.** `tests/test_console.py` ends in a
  stdlib runner that executes every `test_*` function. Run the whole suite, it takes under a second:
  `cd console && python3 tests/test_console.py`
- Expected baseline before any change: `6 tests passed`
- There is no way to run a single test by name. Run them all and read the output; a new failing
  test shows as a traceback naming that function.
- The console under test is imported as `from cloudlens_console import server`
- Commit after each task. Small commits, present tense, no AI attribution.

---

## Task 1: Pairing code generated per process

**Files:**
- Modify: `console/cloudlens_console/server.py` (module level, near `JOBS = {}` on line 25)
- Test: `console/tests/test_console.py`

**Step 1: Write the failing test**

```python
def test_pairing_code_is_random_and_short():
    from cloudlens_console import server
    a = server.new_pair_code()
    b = server.new_pair_code()
    assert a != b, "pair codes must differ per call"
    assert len(a) == 6 and a.isalnum() and a.isupper()
```

**Step 2: Run it to make sure it fails**

Run: `cd console && python3 tests/test_console.py`
Expected: FAIL with `AttributeError: module 'cloudlens_console.server' has no attribute 'new_pair_code'`

**Step 3: Implement the minimal code**

Add to `server.py` after line 26 (`FIXTURES = ...`):

```python
import secrets

# Unambiguous alphabet: no O/0, no I/1. The visitor types this by hand.
_PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIR_CODE = None          # set by serve(); one per process


def new_pair_code(n=6):
    return "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(n))
```

**Step 4: Run the test and make sure it passes**

Run: `cd console && python3 tests/test_console.py`
Expected: PASS

**Step 5: Commit**

```bash
git add console/cloudlens_console/server.py console/tests/test_console.py
git commit -m "console: generate a per-process pairing code"
```

---

## Task 2: /health returns only {ok, version}

This endpoint is deliberately CORS-open so the page can probe without a code. It must therefore leak nothing: no account, no region, no flow list.

**Files:**
- Modify: `console/cloudlens_console/server.py:59-72` (`do_GET`)
- Test: `console/tests/test_console.py`

**Step 1: Write the failing test**

```python
def _handler_response(path, method="GET", headers=None, body=None):
    """Drive Handler.do_* without a socket, capturing what it writes."""
    import io
    from cloudlens_console import server

    class Cap(server.Handler):
        def __init__(self, path, method, headers, body):
            self.path = path
            self.command = method
            self.headers = headers or {}
            self.rfile = io.BytesIO(body or b"")
            self.wfile = io.BytesIO()
            self.status = None
            self.sent = {}
        def send_response(self, code, *a): self.status = code
        def send_header(self, k, v): self.sent[k] = v
        def end_headers(self): pass
        def log_message(self, *a): pass

    h = Cap(path, method, headers, body)
    getattr(h, "do_" + method)()
    raw = h.wfile.getvalue()
    try: payload = json.loads(raw.decode() or "{}")
    except Exception: payload = raw
    return h.status, h.sent, payload


def test_health_leaks_nothing():
    status, _, payload = _handler_response("/health")
    assert status == 200
    assert set(payload.keys()) == {"ok", "version"}
    assert payload["ok"] is True
```

**Step 2: Run it to make sure it fails**

Run: `cd console && python3 tests/test_console.py`
Expected: FAIL, status is 404 because `/health` is not routed yet.

**Step 3: Implement the minimal code**

In `server.py`, add a `VERSION` constant next to `PAIR_CODE`:

```python
VERSION = "1.0"
```

In `do_GET`, insert immediately after `path = self.path.split("?")[0]` (line 60):

```python
        if path == "/health":
            # CORS-open so the page can probe without pairing. Leak nothing.
            return self._send(200, {"ok": True, "version": VERSION})
```

**Step 4: Run the test and make sure it passes**

Run: `cd console && python3 tests/test_console.py`
Expected: PASS

**Step 5: Commit**

```bash
git add console/cloudlens_console/server.py console/tests/test_console.py
git commit -m "console: add /health probe endpoint that leaks nothing"
```

---

## Task 3: CORS with origin pinning and Private Network Access

Chrome requires a public page hitting a private address to preflight with `Access-Control-Request-Private-Network`, and the server must answer `Access-Control-Allow-Private-Network: true`. Miss it and the bridge silently fails in Chrome while working in Safari. This is the single most likely field failure.

**Files:**
- Modify: `console/cloudlens_console/server.py` (`_send` helper at :36, plus a new `do_OPTIONS`)
- Test: `console/tests/test_console.py`

**Step 1: Write the failing tests**

```python
PAGES_ORIGIN = "https://keysight-tech.github.io"

def test_cors_allows_the_pages_origin():
    _, sent, _ = _handler_response("/health", headers={"Origin": PAGES_ORIGIN})
    assert sent.get("Access-Control-Allow-Origin") == PAGES_ORIGIN

def test_cors_rejects_a_foreign_origin():
    _, sent, _ = _handler_response("/health", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in sent

def test_preflight_allows_private_network():
    status, sent, _ = _handler_response(
        "/run", method="OPTIONS",
        headers={"Origin": PAGES_ORIGIN, "Access-Control-Request-Private-Network": "true"})
    assert status == 204
    assert sent.get("Access-Control-Allow-Private-Network") == "true"
    assert "X-CloudLens-Pair" in sent.get("Access-Control-Allow-Headers", "")
```

**Step 2: Run them to make sure they fail**

Run: `cd console && python3 tests/test_console.py`
Expected: FAIL, no CORS headers emitted and `do_OPTIONS` does not exist.

**Step 3: Implement the minimal code**

Add near the top of `server.py`:

```python
ALLOWED_ORIGINS = {
    "https://keysight-tech.github.io",
    "http://127.0.0.1:8760",
    "http://localhost:8760",
}


def _allowed_origin(origin):
    return origin if origin in ALLOWED_ORIGINS else None
```

In `_send`, after `self.send_header("Cache-Control", "no-store")` (line 43):

```python
        origin = _allowed_origin(self.headers.get("Origin"))
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
```

Add a new method to `Handler`, after `do_POST`:

```python
    # ---- CORS preflight ----
    def do_OPTIONS(self):
        origin = _allowed_origin(self.headers.get("Origin"))
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CloudLens-Pair")
            self.send_header("Access-Control-Max-Age", "600")
            # Chrome Private Network Access: public page -> 127.0.0.1 needs this
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()
```

**Step 4: Run the tests and make sure they pass**

Run: `cd console && python3 tests/test_console.py`
Expected: the new tests appear in the ok list and the total rises

**Step 5: Commit**

```bash
git add console/cloudlens_console/server.py console/tests/test_console.py
git commit -m "console: pin CORS to the docs origin and answer Private Network Access preflight"
```

---

## Task 4: Enforce pairing on the acting routes

`/health` stays open. `/flows`, `/run` and `/stop/` require the code. Same-origin requests from the console's own UI carry no `Origin` header and are exempt, so the SE running locally sees no pairing prompt.

**Files:**
- Modify: `console/cloudlens_console/server.py` (`do_GET` :63, `do_POST` :77 and :91)
- Test: `console/tests/test_console.py`

**Step 1: Write the failing tests**

```python
def test_run_without_pair_code_is_rejected():
    from cloudlens_console import server
    server.PAIR_CODE = "ABC234"
    status, _, payload = _handler_response(
        "/run", method="POST",
        headers={"Origin": PAGES_ORIGIN, "Content-Length": "0"})
    assert status == 401
    assert payload["error"] == "pairing required"

def test_run_with_pair_code_is_accepted():
    from cloudlens_console import server
    server.PAIR_CODE = "ABC234"
    body = json.dumps({"flow": "stack", "inputs": {}, "replay": True}).encode()
    status, _, payload = _handler_response(
        "/run", method="POST",
        headers={"Origin": PAGES_ORIGIN, "Content-Length": str(len(body)),
                 "X-CloudLens-Pair": "ABC234"},
        body=body)
    assert status == 200 and "job_id" in payload

def test_same_origin_needs_no_pair_code():
    from cloudlens_console import server
    server.PAIR_CODE = "ABC234"
    status, _, _ = _handler_response("/flows")   # no Origin header at all
    assert status == 200
```

**Step 2: Run them to make sure they fail**

Run: `cd console && python3 tests/test_console.py`
Expected: the rejection test FAILs, `/run` currently answers 200 or 400 without checking.

**Step 3: Implement the minimal code**

Add a guard method to `Handler`:

```python
    def _paired(self):
        """True when the caller may act. Same-origin (no Origin header) is
        exempt: that is the console's own UI. Cross-origin must present the
        code this process printed at startup."""
        import hmac
        if not self.headers.get("Origin"):
            return True
        supplied = self.headers.get("X-CloudLens-Pair") or ""
        return bool(PAIR_CODE) and hmac.compare_digest(supplied, PAIR_CODE)
```

Guard the three routes. In `do_GET`, before the `/flows` branch:

```python
        if path == "/flows" and not self._paired():
            return self._send(401, {"error": "pairing required"})
```

In `do_POST`, as the first statement after `path = ...`:

```python
        if not self._paired():
            return self._send(401, {"error": "pairing required"})
```

**Step 4: Run the tests and make sure they pass**

Run: `cd console && python3 tests/test_console.py`
Expected: the new tests appear in the ok list and the total rises

**Step 5: Run the whole suite for regressions**

Run: `cd console && python3 tests/test_console.py`
Expected: every test ok, total is 6 plus the ones added so far

**Step 6: Commit**

```bash
git add console/cloudlens_console/server.py console/tests/test_console.py
git commit -m "console: require the pairing code on acting routes, exempt same-origin"
```

---

## Task 5: job_id becomes an unguessable capability

`EventSource` cannot send custom headers, so `/events/<job_id>` cannot check the pair code. Instead the job id itself is the authorization: it is only ever handed to a paired caller, so it must be unguessable. Today it is `uuid4().hex[:12]`, which is truncated for cosmetics.

**Files:**
- Modify: `console/cloudlens_console/server.py:82`
- Test: `console/tests/test_console.py`

**Step 1: Write the failing test**

```python
def test_job_ids_are_full_entropy_and_unique():
    from cloudlens_console import server
    ids = {server.new_job_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(i) == 32 for i in ids)
```

**Step 2: Run it to make sure it fails**

Run: `cd console && python3 tests/test_console.py`
Expected: FAIL, `new_job_id` does not exist.

**Step 3: Implement the minimal code**

Add next to `new_pair_code`:

```python
def new_job_id():
    """The job id IS the capability for /events/<id>: EventSource cannot send
    headers, so this must be unguessable rather than merely unique."""
    return secrets.token_hex(16)
```

Replace line 82 `job_id = uuid.uuid4().hex[:12]` with:

```python
            job_id = new_job_id()
```

**Step 4: Run the test and make sure it passes**

Run: `cd console && python3 tests/test_console.py`
Expected: PASS

**Step 5: Commit**

```bash
git add console/cloudlens_console/server.py console/tests/test_console.py
git commit -m "console: make job_id a full-entropy capability for the SSE stream"
```

---

## Task 6: Print the pairing code at startup

**Files:**
- Modify: `console/cloudlens_console/server.py` (`serve` at :163)
- Modify: `console/cloudlens_console/__main__.py:27-31`

**Step 1: Set the code in serve()**

Replace `serve` with:

```python
def serve(host="127.0.0.1", port=8760):
    global PAIR_CODE
    PAIR_CODE = new_pair_code()
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd
```

**Step 2: Print it in the banner**

In `__main__.py`, replace lines 29-31 with:

```python
    print("\n  CloudLens live console  ->  {}".format(url))
    print("  Loopback only. Runs the real deploy scripts against your AWS identity.")
    print("\n  Pairing code for the docs site:  {}".format(server.PAIR_CODE))
    print("  Enter it once at https://keysight-tech.github.io/cloudlens-ansible-aws/#watch-live")
    print("  New code each run. Ctrl-C to stop.\n")
```

**Step 3: Verify by eye**

Run: `cd ~/cloudlens-ansible-aws/console && python3 -m cloudlens_console --no-open`
Expected: the banner shows a six-character code. Ctrl-C.

**Step 4: Confirm /health answers**

Run in a second shell: `curl -s http://127.0.0.1:8760/health`
Expected: `{"ok": true, "version": "1.0"}`

**Step 5: Commit**

```bash
git add console/cloudlens_console/server.py console/cloudlens_console/__main__.py
git commit -m "console: print the pairing code in the startup banner"
```

---

## Task 7: Client string table

Everything the visitor reads goes here, so translation later is a data change rather than a refactor. No translation now.

**Files:**
- Create: `console/cloudlens_console/web/strings.js`

**Step 1: Create the file**

```javascript
// Every user-facing string. Translation later replaces this object, nothing else.
window.CLC_STRINGS = {
  en: {
    liveBadge:        "LIVE",
    replayBadge:      "REPLAY",
    goLiveTitle:      "Run this for real",
    goLiveBody:       "Start the console on your machine, then paste its pairing code.",
    goLiveCommand:    "python3 -m cloudlens_console",
    pairPrompt:       "Pairing code",
    pairHelp:         "Six characters, shown in the console banner.",
    pairBad:          "That code did not match. Check the console banner.",
    pairPlaceholder:  "ABC234",
    connected:        "Connected to your console",
    lostConsole:      "Lost the local console. The transcript below is kept.",
    blockedPNA:       "Your browser blocked the page from reaching 127.0.0.1. " +
                      "In Chrome, enable chrome://flags/#private-network-access-respect-preflight-results, " +
                      "or open the console UI directly at http://localhost:8760.",
    waitingOnAws:     "waiting on AWS"
  }
};
window.CLC_T = function (key) {
  var lang = (window.CLC_LANG || "en");
  var tbl = window.CLC_STRINGS[lang] || window.CLC_STRINGS.en;
  return tbl[key] || window.CLC_STRINGS.en[key] || key;
};
```

**Step 2: Commit**

```bash
git add console/cloudlens_console/web/strings.js
git commit -m "console: put every user-facing string in one table for later translation"
```

---

## Task 8: Bridge state machine as a pure function

The transitions are where the logic bugs will be. Keep them out of the DOM so they can be tested in Node.

**Files:**
- Create: `console/cloudlens_console/web/bridge.js`
- Create: `console/tests/test_bridge.mjs`

**Step 1: Write the failing test**

```javascript
// console/tests/test_bridge.mjs   run: node tests/test_bridge.mjs
import assert from "node:assert";
import { nextState } from "../cloudlens_console/web/bridge.js";

// no console on the machine -> replay, and that is not an error
assert.equal(nextState({ probe: "none" }).mode, "replay");
assert.equal(nextState({ probe: "none" }).error, null);

// console found, not yet paired
assert.equal(nextState({ probe: "ok" }).mode, "pairing");

// paired
assert.equal(nextState({ probe: "ok", paired: true }).mode, "live");

// browser blocked the private-network request: a specific, actionable message
const blocked = nextState({ probe: "blocked" });
assert.equal(blocked.mode, "replay");
assert.equal(blocked.error, "blockedPNA");

// bad code keeps us in pairing with a message
const bad = nextState({ probe: "ok", pairRejected: true });
assert.equal(bad.mode, "pairing");
assert.equal(bad.error, "pairBad");

// console died mid-stream: degraded, transcript preserved
const dead = nextState({ probe: "ok", paired: true, sse: "error" });
assert.equal(dead.mode, "degraded");
assert.equal(dead.keepTranscript, true);

console.log("bridge state machine: all assertions passed");
```

**Step 2: Run it to make sure it fails**

Run: `cd ~/cloudlens-ansible-aws/console && node tests/test_bridge.mjs`
Expected: FAIL, cannot resolve `bridge.js`

**Step 3: Implement the minimal code**

```javascript
// console/cloudlens_console/web/bridge.js
// Pure transitions. No DOM, no fetch: testable in node.
export function nextState(s) {
  if (s.probe === "blocked") {
    return { mode: "replay", error: "blockedPNA", keepTranscript: false };
  }
  if (s.probe !== "ok") {
    // Absence of a console is never an error.
    return { mode: "replay", error: null, keepTranscript: false };
  }
  if (!s.paired) {
    return { mode: "pairing", error: s.pairRejected ? "pairBad" : null, keepTranscript: false };
  }
  if (s.sse === "error") {
    return { mode: "degraded", error: "lostConsole", keepTranscript: true };
  }
  return { mode: "live", error: null, keepTranscript: false };
}
```

**Step 4: Run the test and make sure it passes**

Run: `node tests/test_bridge.mjs`
Expected: `bridge state machine: all assertions passed`

**Step 5: Commit**

```bash
git add console/cloudlens_console/web/bridge.js console/tests/test_bridge.mjs
git commit -m "console: bridge state machine as a pure, node-testable function"
```

---

## Task 9: Wire the bridge into the UI

**Files:**
- Modify: `console/cloudlens_console/web/app.js` (the `fetch("/flows")`, `fetch("/run")` and `EventSource("/events/...")` call sites)
- Modify: `console/cloudlens_console/web/index.html` (load `strings.js` and `bridge.js`)

**Step 1: Add an API base and pair header**

At the top of `app.js`:

```javascript
// Same-origin when served by the console; the local console when served by the docs site.
var CLC_BASE = (location.port === "8760" || location.hostname === "localhost")
  ? "" : "http://127.0.0.1:8760";
var CLC_PAIR = sessionStorage.getItem("clc_pair") || "";

function clcHeaders(extra) {
  var h = extra || {};
  if (CLC_PAIR) h["X-CloudLens-Pair"] = CLC_PAIR;
  return h;
}
```

**Step 2: Probe on load**

```javascript
function clcProbe() {
  var ctl = new AbortController();
  var t = setTimeout(function () { ctl.abort(); }, 1500);
  return fetch(CLC_BASE + "/health", { signal: ctl.signal })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) { clearTimeout(t); return (j && j.ok) ? "ok" : "none"; })
    .catch(function (e) {
      clearTimeout(t);
      // A PNA block and a plain absence both surface as TypeError. Treat an
      // immediate failure as blocked only when the page is https, since a
      // missing console on http would simply refuse the connection.
      return (location.protocol === "https:") ? "blocked" : "none";
    });
}
```

**Step 3: Route run and stream through the base**

Replace the three call sites:

```javascript
fetch(CLC_BASE + "/run", { method: "POST",
  headers: clcHeaders({ "Content-Type": "application/json" }),
  body: JSON.stringify(payload) })
```

```javascript
new EventSource(CLC_BASE + "/events/" + d.job_id)   // no header needed: job_id is the capability
```

```javascript
fetch(CLC_BASE + "/stop/" + window._job, { method: "POST", headers: clcHeaders() })
```

**Step 4: Load the new scripts**

In `index.html`, before `app.js`:

```html
<script src="strings.js"></script>
<script type="module" src="bridge.js"></script>
```

**Step 5: Verify locally**

Run: `cd ~/cloudlens-ansible-aws/console && python3 -m cloudlens_console`
Expected: the console still runs a flow exactly as before. Same-origin means no pairing prompt and no regression.

**Step 6: Commit**

```bash
git add console/cloudlens_console/web/
git commit -m "console UI: route calls through a configurable base and probe for a local console"
```

---

## Task 10: Bake the bridge into the built docs console

**Files:**
- Modify: `console/build_site.py` (the `CLIENT_APP` string)

**Step 1: Include the new files in the build**

`build_site.py` inlines the UI into `docs/console.html`. Inline `strings.js` and `bridge.js` the same way the existing client is inlined, then have the generated page call `clcProbe()` on load and choose between the replay engine already in `CLIENT_APP` and the live `EventSource` path.

The replay engine stays exactly as it is. It is the fallback and the default.

**Step 2: Rebuild and verify**

Run: `cd ~/cloudlens-ansible-aws/console && python3 build_site.py`
Expected: `docs/console.html` regenerates.

Run with no console listening: open `docs/console.html`.
Expected: replay works exactly as today, no error visible, and a "Run this for real" strip appears.

**Step 3: Commit**

```bash
git add console/build_site.py docs/console.html
git commit -m "docs console: bake in the bridge client, keep replay as the fallback"
```

---

## Task 11: Browser smoke tests

**Files:**
- Create: `console/tests/test_bridge_browser.py` (Playwright)

**Step 1: Write the tests**

Three cases, matching the failure table in the design:

1. console down: the page renders replay and shows no error
2. console up: pairing prompt appears, a wrong code shows `pairBad`, the right code reaches `live`
3. console killed mid-stream: the mode becomes `degraded` and the transcript is still in the DOM

**Step 2: Run them**

Run: `python3 -m pytest tests/test_bridge_browser.py -v`
Expected: the new tests appear in the ok list and the total rises

**Step 3: Commit**

```bash
git add console/tests/test_bridge_browser.py
git commit -m "console: browser smoke tests for the bridge state machine"
```

---

## Task 12: Cross-browser and one real end-to-end

Not automated. Do it once, record the result in the design doc.

**Step 1: Cross-browser matrix**

Serve the built page over https (GitHub Pages or a local https server; PNA does not trigger on http) and confirm the bridge connects in:

- Chrome, Edge: the Private Network Access path. Most likely to fail
- Safari
- Firefox

For any browser that blocks it, confirm the page shows `blockedPNA` with the fix, not a generic error.

**Step 2: One genuine deployment**

From the public page, pair with a local console and run the `stack` flow against a real AWS account. Confirm the events are real: the CloudFormation stack exists in the console afterwards, and the elapsed time matches a real deploy rather than the fixture's timing.

**Step 3: Record the outcome**

Append a "Verified" section to `docs/plans/2026-07-22-live-shell-design.md` with the browser results and the stack id used.

```bash
git add docs/plans/2026-07-22-live-shell-design.md
git commit -m "Record cross-browser and live verification results for the bridge"
```

---

## Out of scope

- Translation. Task 7 only puts the strings in one place.
- The outbound relay for locked-down networks. Revisit only if Task 12 shows the bridge is blocked in customer browsers.
- Arbitrary command execution. The page sends a flow id and never a command string.
