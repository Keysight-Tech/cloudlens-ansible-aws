"""The web files, held to their contracts without a browser.

The wizard writes a plan keyed by the profile keys and posts it to routes
server.py dispatches; a typo in either is a form that silently does nothing
until someone clicks it. These tests read the files as text (stdlib + re)
and hold them to the same lists the server reads:

  screens    index.html carries data-screen 1..6 and the Launch button
  keys       every CLOUDLENS_* literal in wizard.js is in profile.allowed_keys();
             the secret fields in index.html are named after api.SECRET_ENV and
             nothing else in the page names a key outside the allowlist
  codes      activation codes enter through a password input, never a textarea,
             and reach /api/run as kvo_codes from an array wizard.js closes over
  values     the vocabularies wizard.js offers for the choice keys are the
             words deploy-stack.sh accepts (parsed from its own messages)
  routes     every /api/..., /events/, /run, /stop/ and /flows literal in the
             three scripts is a path server.py routes (parsed from its source)
  ids        every id app.js and wizard.js look up exists in index.html
  labels     every static input, select and textarea has a label or an aria-label
  syntax     node --check on each script (skipped, with the reason, without node)
  style      no em dash in any web file

Run:  cd console && python3 -m pytest tests/test_web_static.py -q
"""
import os
import re
import shutil
import subprocess

import pytest

from cloudlens_console import api, profile as P

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "..", "cloudlens_console")
WEB = os.path.join(PKG, "web")
SERVER = os.path.join(PKG, "server.py")
DEPLOY = os.path.join(HERE, "..", "..", "deploy", "deploy-stack.sh")
SCRIPTS = ("app.js", "wizard.js", "plan.js")
KEY = re.compile(r"CLOUDLENS_[A-Z0-9_]+")


def _read(name, base=WEB):
    with open(os.path.join(base, name), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- screens
def test_the_wizard_has_six_screens_and_a_launch_button():
    html = _read("index.html")
    for n in range(1, 7):
        assert 'data-screen="%d"' % n in html, "screen %d is missing" % n
    assert 'id="launchBtn"' in html


def test_the_top_navigation_names_every_page_and_each_page_exists():
    html = _read("index.html")
    nav = re.findall(r'data-page="([a-z]+)"', html)
    pages = set(re.findall(r'data-page-body="([a-z]+)"', html))
    assert set(nav) == pages == {"preflight", "deploy", "watch", "operate", "licensing", "teardown"}
    assert len(nav) == len(set(nav)), "a page is in the navigation twice"


# ------------------------------------------------------------------- keys
def test_every_profile_key_literal_in_wizard_js_is_a_profile_key():
    """The contract that catches a typo without a browser: a key the wizard
    writes that is not in deploy/profile-keys.txt is one /api/plan refuses."""
    keys = set(P.allowed_keys())
    found = set(KEY.findall(_read("wizard.js")))
    assert found, "wizard.js writes the plan by CLOUDLENS_* keys"
    assert found <= keys, "not profile keys: %s" % sorted(found - keys)


def _secret_fields(html):
    names = []
    for tag in re.findall(r"<(?:input|textarea)\b[^>]*>", html):
        if "data-secret" not in tag:
            continue
        m = re.search(r'\bname="([^"]*)"', tag)
        assert m, "a secret field needs a name: %s" % tag
        names.append((m.group(1), tag))
    return names


def test_secret_fields_are_named_after_the_secrets_the_script_reads():
    """A secret reaches the engine as environment under a name in
    api.SECRET_ENV and nowhere else; the wizard collects each one under
    that name, as type=password. Every other CLOUDLENS_* literal in the
    page is a profile key."""
    html = _read("index.html")
    fields = _secret_fields(html)
    names = {n for n, _ in fields}
    assert names == set(api.SECRET_ENV), (sorted(names), sorted(api.SECRET_ENV))
    for name, tag in fields:
        assert 'type="password"' in tag, "%s is not a password field: %s" % (name, tag)
        assert "value=" not in tag, "a secret field never carries a value in the page"
    others = set(KEY.findall(html)) - names
    assert others <= set(P.allowed_keys()), sorted(others - set(P.allowed_keys()))


# ------------------------------------------------------------------ codes
def _secrets_block(html):
    m = re.search(r'<div class="subsec" id="secrets">(.*?)<div class="launch">', html, re.S)
    assert m, "index.html carries the secrets block before the Launch row"
    return m.group(1)


def test_activation_codes_enter_through_a_password_input_never_a_textarea():
    """A textarea masked by -webkit-text-security shows every code in clear
    on Firefox, which ignores that property. The codes go in through a
    password input, which every browser masks, one at a time or pasted as
    a list; each shows as a chip of its last four characters, with a
    remove button and a count line. The input carries no data-secret: a
    code is not environment, it rides the argv as --kvo-codes, and no
    secret field ever carries a value in the page."""
    html = _read("index.html")
    block = _secrets_block(html)
    assert "<textarea" not in block, "a textarea shows the codes in clear on Firefox"
    entry = [t for t in re.findall(r"<input\b[^>]*>", block) if 'id="codeEntry"' in t]
    assert len(entry) == 1, entry
    assert 'type="password"' in entry[0], entry[0]
    assert "data-secret" not in entry[0] and "value=" not in entry[0], entry[0]
    for i in ("codeAdd", "codeList", "codeCount"):
        assert 'id="%s"' % i in block, i
    assert "text-security" not in html, "the masking is the input type, never a CSS property"
    js = _read("wizard.js")
    assert "kvo_codes:codesNow()" in js, "Launch sends the codes as kvo_codes"
    assert 'getData("text")' in js, "a paste is read from the clipboard before the input sanitises it"


# ----------------------------------------------------------------- values
def _script_vocab():
    """The words deploy-stack.sh accepts for each choice key, read from the
    script's own refusal messages and comments so a renamed value there
    fails here."""
    src = _read(os.path.basename(DEPLOY), os.path.dirname(DEPLOY))
    vocab = {}
    m = re.search(r"--tapping must be '(\w+)', '(\w+)', '(\w+)', or '(\w+)'", src)
    vocab["CLOUDLENS_TAPPING"] = set(m.groups())
    m = re.search(r"--sensor-mode must be '(\w+)', '(\w+)', or '(\w+)'", src)
    vocab["CLOUDLENS_SENSOR_MODE"] = set(m.groups())
    m = re.search(r"--eks-mode must be (\w+) or (\w+)", src)
    vocab["CLOUDLENS_EKS_MODE"] = set(m.groups())
    m = re.search(r'VCONTROLLER_ALLOWED="([^"]+)"', src)
    vocab["CLOUDLENS_VCONTROLLER_TYPE"] = set(m.group(1).split())
    m = re.search(r'INFRA_CHOICE="\$\{CLOUDLENS_INFRA:-\}"\s+#\s*([\w |]+)', src)
    vocab["CLOUDLENS_INFRA"] = {w.strip() for w in m.group(1).split("|")}
    m = re.search(r'WORKLOAD_CHOICE="\$\{CLOUDLENS_WORKLOAD_CHOICE:-\}"\s+#\s*([\w |]+)', src)
    vocab["CLOUDLENS_WORKLOAD_CHOICE"] = {w.strip() for w in m.group(1).split("|")}
    return vocab


def _wizard_vocab():
    """wizard.js declares VALUES = {KEY: [..words..]}: what each choice
    screen can write. Parsed as text, one key per line."""
    src = _read("wizard.js")
    block = re.search(r"var VALUES\s*=\s*\{(.*?)\};", src, re.S)
    assert block, "wizard.js declares var VALUES = {...}"
    vocab = {}
    for key, words in re.findall(r"(CLOUDLENS_[A-Z_]+)\s*:\s*\[([^\]]*)\]", block.group(1)):
        vocab[key] = set(re.findall(r'"([^"]+)"', words))
    return vocab


def test_the_choice_values_the_wizard_offers_are_the_words_the_script_accepts():
    script, wizard = _script_vocab(), _wizard_vocab()
    assert set(wizard) == set(script), (sorted(wizard), sorted(script))
    for key in script:
        assert wizard[key], "%s offers nothing" % key
        assert wizard[key] <= script[key], "%s: %s is not a word the script accepts (it takes %s)" % (
            key, sorted(wizard[key] - script[key]), sorted(script[key]))
    # the one word the wizard never writes: "none" is a sensor mode the
    # tapping choice already expresses
    assert wizard["CLOUDLENS_SENSOR_MODE"] == {"standalone", "kvo"}
    for key in ("CLOUDLENS_TAPPING", "CLOUDLENS_EKS_MODE", "CLOUDLENS_VCONTROLLER_TYPE", "CLOUDLENS_INFRA",
                "CLOUDLENS_WORKLOAD_CHOICE"):
        assert wizard[key] == script[key], key


def test_the_booleans_the_wizard_writes_are_the_scripts_true_and_false():
    """DEPLOY_KVO, DEPLOY_VPB, DEPLOY_EKS and EKS_SAMPLE are compared to the
    literal "true" in the script ([[ "$DEPLOY_KVO" == "true" ]]), never to
    yes or 1."""
    src = _read("wizard.js")
    for key in ("CLOUDLENS_DEPLOY_KVO", "CLOUDLENS_DEPLOY_VPB", "CLOUDLENS_DEPLOY_EKS", "CLOUDLENS_EKS_SAMPLE"):
        values = set(re.findall(key + r'[^;\n]*?["\'](true|false|yes|no|1|0)["\']', src))
        assert values and values <= {"true", "false"}, (key, values)


# ----------------------------------------------------------------- routes
def _server_routes():
    """(exact, prefixes) as server.py compares them: `path == "/x"` and
    `path.startswith("/x/")`. The bare /api/ guard is not a route."""
    src = _read(os.path.basename(SERVER), os.path.dirname(SERVER))
    exact = set(re.findall(r'path\s*==\s*"(/[^"]*)"', src))
    prefixes = set(re.findall(r'path\.startswith\("(/[^"]*)"\)', src)) - {"/api/"}
    assert exact and prefixes
    return exact, prefixes


def _script_paths(src):
    return set(re.findall(r"""["'](/(?:api|events|flows|run|stop)(?:/[^"'?\s]*)?)["'?]""", src))


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_path_a_script_calls_is_one_the_server_routes(name):
    exact, prefixes = _server_routes()
    paths = _script_paths(_read(name))
    if name != "plan.js":
        assert paths, "%s calls the server" % name
    for p in sorted(paths):
        assert p in exact or any(p.startswith(x) for x in prefixes), "%s calls %s, which server.py does not route" % (
            name, p)


def test_the_wizard_uses_the_discovery_plan_run_and_doctor_routes():
    paths = _script_paths(_read("wizard.js"))
    assert {"/api/doctor", "/api/discover/vpcs", "/api/discover/subnets", "/api/discover/workloads",
            "/api/discover/eks", "/api/plan", "/api/run"} <= paths, sorted(paths)


# -------------------------------------------------------------------- ids
@pytest.mark.parametrize("name", ("app.js", "wizard.js"))
def test_every_id_a_script_looks_up_exists_in_the_page(name):
    ids = set(re.findall(r'id="([^"]+)"', _read("index.html")))
    wanted = set(re.findall(r'\$\("([^"]+)"\)', _read(name)))
    assert wanted, "%s looks elements up by id" % name
    assert wanted <= ids, "%s looks up ids the page does not have: %s" % (name, sorted(wanted - ids))


def test_page_ids_are_unique():
    ids = re.findall(r'id="([^"]+)"', _read("index.html"))
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, dupes


# ----------------------------------------------------------------- labels
def test_every_static_form_control_has_a_label():
    html = _read("index.html")
    labelled = set(re.findall(r'<label\b[^>]*\bfor="([^"]+)"', html))
    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
        m = re.search(r'\bid="([^"]+)"', tag)
        assert m, "a form control needs an id a label can point at: %s" % tag
        assert m.group(1) in labelled or "aria-label=" in tag, "no label: %s" % tag


def test_interactive_things_are_buttons_not_divs():
    html = _read("index.html")
    for tag in re.findall(r"<div\b[^>]*>", html):
        assert "onclick" not in tag, tag
        if 'role="button"' in tag:
            assert False, "a clickable thing is a <button>: %s" % tag


# ----------------------------------------------------------------- syntax
@pytest.mark.parametrize("name", SCRIPTS)
def test_the_script_parses(name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: `node --check %s` needs it" % name)
    proc = subprocess.run([node, "--check", os.path.join(WEB, name)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_each_script_is_a_strict_iife(name):
    src = _read(name)
    assert src.lstrip().startswith("(function(){"), name
    assert '"use strict";' in src[:200], name


# ------------------------------------------------------------------ style
@pytest.mark.parametrize("name", sorted(os.listdir(WEB)))
def test_no_em_dash_in_any_web_file(name):
    assert "\u2014" not in _read(name), "%s carries an em dash" % name
