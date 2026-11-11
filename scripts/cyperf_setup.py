#!/usr/bin/env python3
"""Configure a freshly deployed Keysight CyPerf controller and start traffic.

deploy-stack.sh --with-cyperf launches the controller and two agents (stack
<stack>-cyperf); this script does everything after boot, over the controller's
REST API, with the standard library only:

  1. wait for the controller (the web server answers ~10 minutes before the
     login service does, so the real gate is a token, not a 200)
  2. accept the CyPerf EULA (a fresh controller blocks the API until it is)
  3. authenticate (Keycloak password grant)
  4. activate the licence, quantity 1, unless the controller already holds one
  5. wait for the client and the server agent to register
  6. create a two-arm session: Client segment <- client agent, Server segment
     <- server agent, one HTTP application, a throughput objective
  7. start the test and confirm it reaches STARTED

Safe to re-run: a licensed controller is not licensed again, an existing
session of the same name is reused, a running test is left running.

Secrets never travel on the command line:
  CLOUDLENS_CYPERF_LICENSE_CODE   activation code (optional)
  CLOUDLENS_CYPERF_ADMIN_PASS     admin password (default: the factory default)

Other modes:
  --status            print licence, agents and test state, change nothing
  --stop              stop the running test of --session-name
  --release-license   deactivate every activation code installed on this
                      controller, so the quantity returns to the entitlement
                      (run BEFORE deleting the controller)

Exit codes: 0 done / traffic running, 1 error, 3 configured but NO licence
(test not started), 4 the controller refused the activation code.

API paths follow Keysight's own wrapper, github.com/Keysight/cyperf
utils/rest_api_wrapper/RESTasV3.py.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FACTORY_PASS = "CyPerf&Keysight#1"   # documented product default
CLIENT_ID = "clt-wap"
BASE_CONFIG = "appsec-two-arm-base"


def log(msg):
    print("[cyperf] %s" % msg, file=sys.stderr, flush=True)


class Api:
    def __init__(self, host, verify):
        self.base = "https://%s" % host
        self.ctx = None
        if not verify:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.token = None
        self.transient_wait = 600
        self.user = None
        self.password = None

    def raw(self, method, path, body=None, form=None, timeout=30, auth=True):
        """-> (status, parsed-or-text). Never raises on an HTTP status."""
        url = path if path.startswith("http") else self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                status, text = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:
            return 0, str(e)
        try:
            return status, json.loads(text) if text.strip() else None
        except ValueError:
            return status, text

    def call(self, method, path, body=None, ok=(200, 201, 202, 204), timeout=30):
        # A controller that has just started issuing tokens still answers 502,
        # 503 or 504 from the services behind its gateway for a few minutes
        # (seen live on Azure: licensing returned 503 seconds after the first
        # successful login). Those are "not yet", never "no": wait them out.
        deadline = time.time() + self.transient_wait
        said = False
        while True:
            status, out = self.raw(method, path, body=body, timeout=timeout)
            if status == 401 and self.login():
                status, out = self.raw(method, path, body=body, timeout=timeout)
            if status not in (0, 502, 503, 504) or time.time() >= deadline:
                break
            if not said:
                log("%s is not serving yet (HTTP %s); waiting for the controller's services" % (path.split("?")[0], status))
                said = True
            time.sleep(10)
        if status not in ok:
            raise RuntimeError("%s %s -> HTTP %s: %s" % (method, path, status, str(out)[:400]))
        return out

    def login(self):
        status, out = self.raw("POST", "/auth/realms/keysight/protocol/openid-connect/token",
                               form={"grant_type": "password", "client_id": CLIENT_ID,
                                     "username": self.user, "password": self.password},
                               auth=False)
        if status == 200 and isinstance(out, dict) and out.get("access_token"):
            self.token = out["access_token"]
            return True
        self.last_login = (status, str(out)[:200])
        return False


def accept_eula(api):
    """A fresh controller answers every API call with the EULA until this is done."""
    status, out = api.raw("GET", "/eula/v1/eula", auth=False)
    if status != 200:
        return False
    items = out if isinstance(out, list) else [out]
    pending = [e for e in items if isinstance(e, dict) and not e.get("accepted", False)]
    if not pending:
        return True
    for e in pending:
        name = e.get("name") or e.get("id") or "CyPerf"
        st, _ = api.raw("POST", "/eula/v1/eula/%s" % urllib.parse.quote(str(name)),
                        body={"accepted": True}, auth=False)
        log("accepted the %s EULA on your behalf (HTTP %s)" % (name, st))
    return True


def wait_ready(api, max_seconds):
    """Up = EULA endpoint answers AND a token is issued. '/' answering proves nothing."""
    deadline = time.time() + max_seconds
    said = 0
    eula_done = False
    while time.time() < deadline:
        if not eula_done:
            eula_done = accept_eula(api)
        if eula_done and api.login():
            log("controller is up and login works")
            return True
        if time.time() - said > 60:
            left = int(deadline - time.time())
            why = getattr(api, "last_login", ("-", "no answer yet"))
            log("waiting for the controller (%ss left; eula=%s, login HTTP %s)"
                % (left, "ok" if eula_done else "not yet", why[0]))
            said = time.time()
        time.sleep(15)
    return False


def poll_op(api, path, max_seconds=180):
    """Async operations: poll <path> until state leaves IN_PROGRESS. -> final dict."""
    deadline = time.time() + max_seconds
    last = {}
    while time.time() < deadline:
        last = api.call("GET", path) or {}
        state = str(last.get("state", "")).upper()
        if state in ("SUCCESS", "ERROR", "FAILED", "FAILURE"):
            return last
        time.sleep(2)
    last = dict(last)
    last.setdefault("state", "TIMEOUT")
    return last


def op_path(api, started, fallback):
    """The poll URL of a just-started operation, as a path on this controller."""
    if isinstance(started, dict):
        url = started.get("url")
        if url:
            if url.startswith("http"):
                return urllib.parse.urlparse(url).path
            return url if url.startswith("/") else "/" + url
        if started.get("id") is not None:
            return "%s/%s" % (fallback, started["id"])
    raise RuntimeError("operation did not return an id: %s" % str(started)[:200])


def license_features(api):
    """-> list of counted licence features on the controller ([] when unlicensed)."""
    base = "/api/v2/licensing/operations/retrieve-counted-feature-stats"
    started = api.call("POST", base, body={})
    path = op_path(api, started, base)
    done = poll_op(api, path, 90)
    if str(done.get("state", "")).upper() != "SUCCESS":
        return []
    res = api.call("GET", path + "/result")
    if isinstance(res, dict):
        res = res.get("result", res.get("features", res.get("data", [])))
    return res if isinstance(res, list) else []


def installed_codes(api):
    """Activation codes installed on the controller (for --release-license)."""
    status, out = api.raw("GET", "/api/v2/licensing/licenses")
    if status != 200:
        return []
    items = out if isinstance(out, list) else (out or {}).get("data", [])
    codes = []
    for it in items if isinstance(items, list) else []:
        code = it.get("activationCode")
        if code and code not in [c for c, _ in codes]:
            qty = it.get("installedCount") or it.get("quantity") or 1
            codes.append((code, qty))
    return codes


def is_licensed(api):
    feats = license_features(api)
    for f in feats:
        if not isinstance(f, dict):
            continue
        total = f.get("installedCount", f.get("total", f.get("count", 0))) or 0
        try:
            if int(total) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return bool(installed_codes(api))


def activate(api, code, qty):
    base = "/api/v2/licensing/operations/activate"
    started = api.call("POST", base, body=[{"activationCode": code, "quantity": qty}])
    done = poll_op(api, op_path(api, started, base), 180)
    if str(done.get("state", "")).upper() == "SUCCESS":
        return True, ""
    return False, str(done.get("message") or done.get("result") or done)[:400]


def deactivate(api, code, qty):
    base = "/api/v2/licensing/operations/deactivate"
    started = api.call("POST", base, body=[{"activationCode": code, "quantity": qty}])
    done = poll_op(api, op_path(api, started, base), 180)
    return str(done.get("state", "")).upper() == "SUCCESS", str(done.get("message") or "")[:300]


def agent_ips(agent):
    ips = set()
    for k in ("IP", "ip", "MgmtIP"):
        if agent.get(k):
            ips.add(str(agent[k]))
    for itf in agent.get("Interfaces") or []:
        for k in ("IP", "ip"):
            v = itf.get(k) if isinstance(itf, dict) else None
            if isinstance(v, list):
                ips.update(str(x) for x in v)
            elif v:
                ips.add(str(v))
    return ips


def wait_agents(api, want_ips, max_seconds):
    """-> {ip: agent} once every wanted agent has registered."""
    deadline = time.time() + max_seconds
    said = 0
    found = {}
    while time.time() < deadline:
        agents = api.call("GET", "/api/v2/agents") or []
        found = {}
        for ip in want_ips:
            for a in agents:
                if ip in agent_ips(a):
                    found[ip] = a
                    break
        if len(found) == len(want_ips):
            return found
        if time.time() - said > 45:
            log("waiting for agents: %s of %s registered (%s known to the controller)"
                % (len(found), len(want_ips), len(agents)))
            said = time.time()
        time.sleep(10)
    return found


def find_session(api, name):
    for s in api.call("GET", "/api/v2/sessions") or []:
        if s.get("name") == name:
            return s
    return None


def test_state(api, sid):
    return api.call("GET", "/api/v2/sessions/%s/test" % sid) or {}


def find_app_id(api, app_name):
    q = urllib.parse.quote(app_name)
    out = api.call("GET", "/api/v2/resources/apps?take=50&skip=0&exclude=links"
                          "&searchCol=Name&searchVal=%s&filterMode=and" % q)
    items = out.get("data", []) if isinstance(out, dict) else (out or [])
    for it in items:
        if it.get("Name") == app_name:
            return it.get("id")
    return items[0].get("id") if items else None


def build_session(api, name, client, server, app_name, mbps, duration):
    cfg = "/api/v2/sessions/%s/config/config"
    created = api.call("POST", "/api/v2/sessions", body={"configUrl": BASE_CONFIG})
    sess = created[0] if isinstance(created, list) else created
    sid = sess["id"]
    base = cfg % sid
    api.call("PATCH", "/api/v2/sessions/%s" % sid, body={"name": name})
    # traffic profile + one application
    api.call("POST", base + "/TrafficProfiles", body={})
    app_id = find_app_id(api, app_name)
    if not app_id:
        raise RuntimeError("application '%s' is not in this controller's library" % app_name)
    api.call("POST", base + "/TrafficProfiles/1/Applications", body={"ExternalResourceURL": app_id})
    api.call("PATCH", base + "/TrafficProfiles/1/ObjectivesAndTimeline/PrimaryObjective",
             body={"Type": "Throughput", "Unit": ""})
    api.call("PATCH", base + "/TrafficProfiles/1/ObjectivesAndTimeline/TimelineSegments/1",
             body={"Duration": duration, "PrimaryObjectiveValue": mbps, "PrimaryObjectiveUnit": "Mbps"})
    # segment 1 = client, segment 2 = server (the base config's order)
    for seg, agent in ((1, client), (2, server)):
        api.call("PATCH", base + "/NetworkProfiles/1/IPNetworkSegment/%s/agentAssignments" % seg,
                 body={"ByID": [{"agentId": agent["id"]}], "ByTag": []})
    return sid


def start_test(api, sid, max_seconds=600):
    st = test_state(api, sid)
    if str(st.get("status", "")).upper() in ("STARTED", "STARTING", "CONFIGURING"):
        log("the test is already %s; leaving it running" % st.get("status"))
    else:
        api.call("POST", "/api/v2/sessions/%s/test-run/operations/start" % sid, body={})
    deadline = time.time() + max_seconds
    last = ""
    while time.time() < deadline:
        st = test_state(api, sid)
        status = str(st.get("status", "")).upper()
        if status != last:
            log("test status: %s" % status)
            last = status
        if status == "STARTED":
            return True, st
        if status in ("ERROR", "STOPPED") and last:
            if status == "ERROR" or time.time() > deadline - max_seconds + 30:
                return False, st
        time.sleep(5)
    return False, st


def write_result(path, data):
    if not path:
        return
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--controller", required=True, help="controller address (IP or name)")
    ap.add_argument("--insecure", action="store_true", help="do not verify the controller certificate (it is self-signed)")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--client-ip", help="control-interface IP of the client agent")
    ap.add_argument("--server-ip", help="control-interface IP of the server agent")
    ap.add_argument("--mbps", type=int, default=10, help="throughput objective (default 10)")
    ap.add_argument("--duration", type=int, default=28800, help="test duration in seconds (default 8 hours)")
    ap.add_argument("--app", default="HTTP App", help="application from the CyPerf library (default 'HTTP App')")
    ap.add_argument("--session-name", default="CloudLens traffic")
    ap.add_argument("--license-qty", type=int, default=1, help="activation quantity (default 1: one per deployment)")
    ap.add_argument("--wait-api", type=int, default=1500, help="seconds to wait for the controller (default 25 min)")
    ap.add_argument("--wait-agents", type=int, default=600)
    ap.add_argument("--no-start", action="store_true", help="configure but do not start the test")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--release-license", action="store_true")
    ap.add_argument("--result-file", help="write a small JSON result here")
    args = ap.parse_args()

    api = Api(args.controller, verify=not args.insecure)
    api.user = args.user
    api.password = os.environ.get("CLOUDLENS_CYPERF_ADMIN_PASS") or FACTORY_PASS
    code = (os.environ.get("CLOUDLENS_CYPERF_LICENSE_CODE") or "").strip()
    result = {"controller": args.controller, "licensed": False, "agents": 0, "test": "not started"}

    quick = args.status or args.stop or args.release_license
    if not wait_ready(api, 120 if quick else args.wait_api):
        log("the controller at %s did not come up (login: %s)"
            % (args.controller, getattr(api, "last_login", "no answer")))
        write_result(args.result_file, result)
        return 1

    if args.release_license:
        codes = installed_codes(api)
        if not codes:
            log("no activation code is installed on this controller; nothing to release")
            return 0
        rc = 0
        for c, qty in codes:
            okay, msg = deactivate(api, c, qty)
            log("released %s x%s: %s" % (c[:4] + "-****", qty, "ok" if okay else "FAILED " + msg))
            rc = rc if okay else 1
        return rc

    if args.status or args.stop:
        licensed = is_licensed(api)
        agents = api.call("GET", "/api/v2/agents") or []
        sess = find_session(api, args.session_name)
        st = test_state(api, sess["id"]) if sess else {}
        if args.stop and sess and str(st.get("status", "")).upper() == "STARTED":
            api.call("POST", "/api/v2/sessions/%s/test-run/operations/stop" % sess["id"], body={})
            log("stop requested for '%s'" % args.session_name)
        print(json.dumps({"licensed": licensed, "agents": len(agents),
                          "session": bool(sess), "test": st.get("status", "none")}))
        return 0

    if not args.client_ip or not args.server_ip:
        log("--client-ip and --server-ip are required")
        return 1

    # licence
    licensed = is_licensed(api)
    if licensed:
        log("the controller already holds a licence; not activating again")
    elif code:
        okay, msg = activate(api, code, args.license_qty)
        if not okay:
            log("LICENCE REFUSED by the controller: %s" % msg)
            write_result(args.result_file, result)
            return 4
        log("licence activated (quantity %s)" % args.license_qty)
        licensed = True
    else:
        log("no activation code supplied: continuing without a licence")
    result["licensed"] = licensed

    # agents
    found = wait_agents(api, [args.client_ip, args.server_ip], args.wait_agents)
    result["agents"] = len(found)
    if len(found) < 2:
        missing = [ip for ip in (args.client_ip, args.server_ip) if ip not in found]
        log("agent(s) never registered: %s. Check that they reach the controller on "
            "TCP 443 and read /var/log/Appsec_init.log on the agent." % ", ".join(missing))
        write_result(args.result_file, result)
        return 1
    log("both agents are registered: client %s, server %s" % (args.client_ip, args.server_ip))

    # session
    sess = find_session(api, args.session_name)
    if sess:
        sid = sess["id"]
        log("session '%s' already exists; reusing it" % args.session_name)
    else:
        sid = build_session(api, args.session_name, found[args.client_ip], found[args.server_ip],
                            args.app, args.mbps, args.duration)
        log("session '%s' created: %s at %s Mbps for %s s" % (args.session_name, args.app, args.mbps, args.duration))
    result["session"] = sid

    if not licensed:
        result["test"] = "not started (no licence)"
        write_result(args.result_file, result)
        log("configured, NOT started: CyPerf will not run a test without a licence")
        return 3
    if args.no_start:
        result["test"] = "configured"
        write_result(args.result_file, result)
        return 0

    okay, st = start_test(api, sid)
    result["test"] = str(st.get("status", "unknown"))
    write_result(args.result_file, result)
    if not okay:
        log("the test did not reach STARTED: %s" % json.dumps(st)[:600])
        return 1
    log("traffic is running")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        log("ERROR: %s" % e)
        sys.exit(1)
