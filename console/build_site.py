"""Generate the self-contained static site (docs/index.html) from the console.

The public site is static GitHub Pages, so it can't run the Python backend. This
bundles the flow metadata + the real captured fixtures inline and drives the whole
premium experience CLIENT-SIDE (no server, no AWS). It is the "watch it happen"
demo anyone can open. The Python console (`python3 -m cloudlens_console`) remains
the tool that runs REAL deploys with live SSE.

Run:  cd console && python3 build_site.py
"""
import os, re, json, sys
sys.path.insert(0, os.path.dirname(__file__))
from cloudlens_console import flows as F

HERE = os.path.dirname(__file__)
WEB = os.path.join(HERE, "cloudlens_console", "web")
FX = os.path.join(HERE, "fixtures")
OUT = os.path.abspath(os.path.join(HERE, "..", "docs", "console.html"))

# 1. flow metadata (same shape the /flows endpoint returns)
FLOWS = {"order": F.ORDER, "flows": {
    fid: {k: F.FLOWS[fid][k] for k in ("id", "name", "script", "subtitle", "inputs", "nodes", "wires")}
    for fid in F.ORDER}}

# 2. the real captured fixtures
FIXTURES = {fid: json.load(open(os.path.join(FX, fid + ".json"))) for fid in F.ORDER}

# 3. take the CSS + body from the console UI and inline the SAME scripts it loads
#
# There is deliberately no second implementation here. The static site used to
# carry its own copy of the UI, which meant the page the world sees and the page
# the console serves could drift apart silently - and the whole point of the
# bridge is that they are one page in three states. app.js probes 127.0.0.1 on
# load, degrades to the replay below when nothing answers, and offers pairing
# when a console does.
src = open(os.path.join(WEB, "index.html")).read()
style = re.search(r"<style>.*?</style>", src, re.S).group(0)

SCRIPTS = ["strings.js", "bridge.js", "app.js"]

data = ('<script>window.__FLOWS__=' + json.dumps(FLOWS, separators=(",", ":")) +
        ';window.__FIXTURES__=' + json.dumps(FIXTURES, separators=(",", ":")) + ';</script>')
inline = data + "".join(
    '<script>' + open(os.path.join(WEB, name)).read() + '</script>' for name in SCRIPTS)

body = re.search(r"<body>.*?</body>", src, re.S).group(0)
# One <script src> block becomes the inlined bundle; the rest are dropped so a
# GitHub Pages visitor never requests /web/ paths that do not exist there.
body = re.sub(r'\s*<script src="/web/[^"]+"></script>', "", body).replace(
    "</body>", inline + "\n</body>")

html = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        "<title>CloudLens Autopilot for AWS · Deployment Console</title>\n"
        "<script>try{document.documentElement.setAttribute('data-theme',localStorage.getItem('cl-theme')||'light');}catch(e){document.documentElement.setAttribute('data-theme','light');}</script>\n"
        + style + "\n</head>\n" + body + "\n</html>\n")

os.makedirs(os.path.dirname(OUT), exist_ok=True)
open(OUT, "w").write(html)
print("wrote", OUT, "(", len(html), "bytes,", len(F.ORDER), "flows,",
      sum(len(v) for v in FIXTURES.values()), "fixture frames )")
