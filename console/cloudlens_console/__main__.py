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
import argparse
import ipaddress
import threading
import webbrowser

from . import server


def is_loopback(host):
    """True only for addresses that cannot be reached from another machine.

    An empty host means INADDR_ANY, and 0.0.0.0 / :: are wildcards: all three
    bind every interface, so none of them is loopback. Anything that is not a
    literal IP (a hostname or interface name) is treated as remote, because we
    cannot know what it resolves to at bind time.
    """
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cloudlens_console", description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="bind host (loopback only by default)")
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--allow-remote", action="store_true",
                    help="permit a non-loopback --host (exposes real AWS deploys to the network)")
    args = ap.parse_args(argv)

    if not is_loopback(args.host) and not args.allow_remote:
        ap.error(
            "refusing to bind {!r}: that is reachable from the network, and this "
            "console runs real deploys against your AWS identity. A LAN caller "
            "sends no Origin on a GET, so origin checks do not stop them. Pass "
            "--allow-remote if you truly mean it.".format(args.host))
    if not is_loopback(args.host):
        print("\n  !!  WARNING: bound to {} - NOT loopback.".format(args.host))
        print("  !!  Anyone who can reach this port can drive real AWS deploys")
        print("  !!  under your credentials. The pairing code is the only thing")
        print("  !!  standing in their way. Stop this as soon as you are done.\n")

    httpd = server.serve(args.host, args.port)
    url = "http://{}:{}/".format("localhost" if args.host == "127.0.0.1" else args.host, args.port)
    print("\n  CloudLens live console  ->  {}".format(url))
    print("  {} Runs the real deploy scripts against your AWS identity.".format(
        "Loopback only." if is_loopback(args.host) else "REACHABLE FROM THE NETWORK."))
    print("  Ctrl-C to stop.\n")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
