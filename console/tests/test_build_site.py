"""build_site.py: the public page is the console's page with the operations
console cut out of it.

That cut is one unanchored substitution between two comment markers, and
what it removes is everything that only works against the Python backend:
the wizard's six screens, the scripts that drive them, every /api/ path,
the secret fields and the activation-code entry. GitHub Pages serves no
backend, so a marker that moved or a regex that stopped matching would
publish a wizard whose every button is dead, and a set of password fields
with nowhere to send what is typed into them, silently.

build_site.py asserts both markers before the cut and the result after it.
This runs the real script into a temp OUT (never docs/console.html) and
reads what it wrote.

Run:  cd console && python3 -m pytest tests/test_build_site.py -q
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.abspath(os.path.join(HERE, ".."))
BUILD = os.path.join(CONSOLE, "build_site.py")
WEB = os.path.join(CONSOLE, "cloudlens_console", "web")
MARKERS = ("<!-- ops:start -->", "<!-- ops:end -->")

# strings the operations console owns and the static page must not carry
OPS_ONLY = ("data-screen", "ui.js", "wizard.js", "plan.js", "watch.js", "operate.js", "licences.js",
            "teardown.js", "/api/", "/events/", "data-secret", "codeEntry", "licEntry", "opCodes")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    """The built page, once for this file: OUT in a temp directory, the
    environment otherwise the caller's, and the script's own assertions
    left to fail the run."""
    out = tmp_path_factory.mktemp("site") / "console.html"
    proc = subprocess.run([sys.executable, BUILD], cwd=CONSOLE, capture_output=True, text=True,
                          timeout=120, env=dict(os.environ, OUT=str(out)))
    assert proc.returncode == 0, proc.stderr
    assert str(out) in proc.stdout, proc.stdout
    assert out.exists(), "the build printed a path it did not write"
    return out.read_text(encoding="utf-8")


def test_the_static_page_carries_nothing_the_operations_console_owns(page):
    for gone in OPS_ONLY:
        assert gone not in page, "the static page still carries %r" % gone
    for marker in MARKERS:
        assert marker not in page, marker


def test_the_static_page_is_the_quick_flows_opened_on_their_captured_runs(page):
    assert 'quickFlows" open' in page, "the fold the whole page is about stays shut"
    assert "window.__FLOWS__" in page and "window.__FIXTURES__" in page
    assert page.startswith("<!doctype html>"), page[:40]


def test_index_html_carries_exactly_one_of_each_ops_marker():
    """What build_site.py asserts before it cuts, said here so a renamed or
    duplicated marker fails a test and not only a build."""
    with open(os.path.join(WEB, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    for marker in MARKERS:
        assert html.count(marker) == 1, "%d of %s" % (html.count(marker), marker)
    assert html.index(MARKERS[0]) < html.index(MARKERS[1]), "the markers are the wrong way round"
