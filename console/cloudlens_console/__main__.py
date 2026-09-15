"""CloudLens live deployment console.

    python3 -m cloudlens_console            # start, open the browser
    python3 -m cloudlens_console --port 8890
    python3 -m cloudlens_console --no-open  # don't auto-open a browser

The page runs the REAL deploy scripts against your shell's AWS identity and
streams live progress. Pick a flow, fill in the inputs, and watch it happen.
Replay mode (a "Demo" toggle in the UI) needs neither AWS nor boto3.
"""
from __future__ import annotations
import sys
import time
import argparse
import threading
import webbrowser

from . import server
from . import orchestrator


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cloudlens_console", description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="bind host (loopback only by default)")
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args(argv)

    httpd = server.serve(args.host, args.port)
    url = "http://{}:{}/".format("localhost" if args.host == "127.0.0.1" else args.host, args.port)
    print("\n  CloudLens live console  ->  {}".format(url))
    print("  Loopback only. Runs the real deploy scripts against your AWS identity.")
    print("  Ctrl-C to stop.\n")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        # The engines do not see this Ctrl-C: run_engine starts each one in
        # its own session, so the terminal's SIGINT stops at the console. A
        # deploy blocked on a prompt would otherwise outlive the page that
        # was going to answer it, forever. Stop them here, as a group each,
        # and give them the grace period before the process exits.
        n = _stop_jobs()
        print("\n  stopped." + ("  ({} running job{} stopped)".format(n, "" if n == 1 else "s") if n else ""))
        httpd.shutdown()
    return 0


def _stop_jobs():
    # list(): a /run request racing this shutdown may still add to JOBS.
    # running(), not alive(): a job whose leader exited but whose subshell
    # still holds the pipe is still a run, and its group still needs the
    # signal.
    running = [j for j in list(server.JOBS.values()) if j.running()]
    for j in running:
        j.stop()
    # stop() arms a daemon Timer for the KILL escalation. A daemon thread
    # does not survive the interpreter, but this wait outlasts the grace
    # period, so the Timer has fired (or the run ended first) before main
    # returns; the daemon flag costs nothing here.
    deadline = time.monotonic() + orchestrator.STOP_GRACE_SECS + 0.5
    for j in running:
        while j.running() and time.monotonic() < deadline:
            time.sleep(0.1)
    return len(running)


if __name__ == "__main__":
    sys.exit(main())
