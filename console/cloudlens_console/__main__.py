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
import threading
import webbrowser

from . import server

# One definition, in the module that has to act on it: server.serve() uses this
# to decide whether the "no Origin means a local process" pairing exemption
# still holds, so a second copy here could drift away from the security
# decision it is supposed to describe.
is_loopback = server.is_loopback


# Both live in server.py, which is where they are acted on: serve() prints the
# startup banner (only it knows the port actually bound) and decides the pairing
# exemption from is_loopback. A second copy here could drift from either.
_banner_url = server._banner_url


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cloudlens_console", description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="bind host (loopback only by default)")
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--allow-remote", action="store_true",
                    help="permit a non-loopback --host (exposes real AWS deploys to the network)")
    ap.add_argument("--dev-origin", metavar="ORIGIN", default=None,
                    help="ALSO accept this one browser origin, e.g. "
                         "http://localhost:4173. For developing the public page "
                         "against a local console. It is NOT exempt from pairing. "
                         "Off unless passed.")
    args = ap.parse_args(argv)

    # Validated here rather than inside serve(): a typo must be an argparse
    # error before anything binds, not a traceback out of a listening server.
    if args.dev_origin:
        try:
            server.normalise_dev_origin(args.dev_origin)
        except ValueError as exc:
            ap.error("--dev-origin {!r}: {}".format(args.dev_origin, exc))

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

    # allow_remote is passed through, not just validated here: the Host guard
    # admits loopback names only, so without it a LAN client gets 403 and the
    # flag is documented but dead.
    # serve() prints the banner itself, pairing code included: it is the only
    # caller that knows the bound port, and the code has to be the live one.
    httpd = server.serve(args.host, args.port, allow_remote=args.allow_remote,
                         dev_origin=args.dev_origin)
    if not args.no_open:
        # From the BOUND port, never args.port: --port 0 means "any free port",
        # so the requested value is 0 and the browser would open
        # http://localhost:0/ next to a banner naming the real one.
        url = _banner_url(args.host, httpd.server_address[1])
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
