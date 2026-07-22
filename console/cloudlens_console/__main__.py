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
        print("\n  stopped.")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
