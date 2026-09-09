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
             and reach /api/run as kvo_codes from an array wizard.js closes over;
             the page's CODE,QTY rule is api.CODE_QTY, quantity digits and all
  values     the vocabularies wizard.js offers for the choice keys are the
             words deploy-stack.sh accepts (parsed from its own messages)
  routes     every /api/..., /events/, /run, /stop/ and /flows literal in the
             three scripts is a path server.py routes (parsed from its source)
  ids        every id app.js, wizard.js and watch.js look up exists in index.html
  buttons    begin() hands the quick-flow Run button back, so a wizard launch
             does not strand it disabled and reading "Running..."
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
SCRIPTS = ("app.js", "wizard.js", "plan.js", "watch.js", "operate.js", "licences.js", "teardown.js")
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
def _own_keys(src):
    """wizard.js declares var OWN_KEYS = [..]: the keys it writes, and the
    only ones a plan restored from localStorage keeps."""
    block = re.search(r"var OWN_KEYS\s*=\s*\[(.*?)\];", src, re.S)
    assert block, "wizard.js declares var OWN_KEYS = [...]"
    return set(re.findall(r'"(CLOUDLENS_[A-Z0-9_]+)"', block.group(1)))


def test_every_profile_key_literal_in_wizard_js_is_a_profile_key():
    """The contract that catches a typo without a browser: a key the wizard
    writes that is not in deploy/profile-keys.txt is one /api/plan refuses.

    And OWN_KEYS is exactly that set of literals: localStorage is not the
    wizard's, so a restored plan keeps these keys and drops the rest. A key
    the file writes but leaves out of OWN_KEYS would not survive a reload;
    a key in OWN_KEYS that no screen writes any more would ride an old
    cl-plan into /api/plan and fail it for a key nothing on the page can
    reach."""
    keys = set(P.allowed_keys())
    src = _read("wizard.js")
    found = set(KEY.findall(src))
    assert found, "wizard.js writes the plan by CLOUDLENS_* keys"
    assert found <= keys, "not profile keys: %s" % sorted(found - keys)
    own = _own_keys(src)
    assert own == found, "OWN_KEYS and the keys the file writes differ: %s" % sorted(own ^ found)


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


def test_the_page_and_the_api_agree_on_a_code_with_a_quantity():
    """CODE_QTY_RE read [0-9]{1,4} while api.CODE_QTY reads {1,6}, and the
    comment above it claimed the two matched: a five or six digit quantity
    the engine accepts came back from the page as "not an activation code",
    with no way past it.

    Both rules are written in syntax the two regex engines share, so the
    page's is compiled here and the pair is run over one table."""
    src = _read("wizard.js")
    m = re.search(r"var CODE_QTY_RE\s*=\s*/(.+?)/;", src)
    assert m, "wizard.js declares var CODE_QTY_RE = /.../;"
    page, engine = m.group(1), api.CODE_QTY.pattern
    qty = re.compile(r"\[0-9\]\{1,(\d+)\}")
    on_page, in_api = qty.search(page), qty.search(engine)
    assert on_page and in_api, (page, engine)
    assert on_page.group(1) == in_api.group(1), (
        "the page takes a quantity of %s digits, the API %s" % (on_page.group(1), in_api.group(1)))
    rule = re.compile(page)
    for value in ("AAAA", "AAAA,1", "AAAA,1234", "AAAA,12345", "AAAA,123456", "AAAA,1234567",
                  "AAAA,", "AAAA,x", "AAAA,12,3", "-AAA", "AAA", "A" * 64, "A" * 65):
        assert bool(rule.fullmatch(value)) == bool(api.CODE_QTY.fullmatch(value)), value
    assert rule.fullmatch("AAAA,123456"), "six digits is a quantity the engine takes"
    # the field's label and the refusal say the number the rule enforces
    said = "a quantity of 1 to %s digits" % on_page.group(1)
    assert said in src, "the refusal text says %r" % said
    assert said in _read("index.html"), "the field's label says %r" % said


def _licensing_block(html):
    m = re.search(r'<section class="page" id="page-licensing".*?</section>', html, re.S)
    assert m, "index.html carries the Licensing page"
    return m.group(0)


def test_the_licensing_screen_takes_codes_the_same_way_the_wizard_does():
    """Same reasoning, same shape: a textarea shows every code in clear on
    a browser that ignores -webkit-text-security, so the codes go in
    through a password input and show as chips of their last four
    characters. This screen has a second password field (the KVO's own),
    which is NOT a data-secret: api.SECRET_ENV is the list of names the
    deploy script reads from the environment, and the KVO password here
    goes in a request body instead."""
    block = _licensing_block(_read("index.html"))
    assert "<textarea" not in block, "a textarea shows the codes in clear on Firefox"
    entry = [t for t in re.findall(r"<input\b[^>]*>", block) if 'id="licEntry"' in t]
    assert len(entry) == 1, entry
    assert 'type="password"' in entry[0] and "value=" not in entry[0], entry[0]
    pw = [t for t in re.findall(r"<input\b[^>]*>", block) if 'id="licPass"' in t]
    assert len(pw) == 1 and 'type="password"' in pw[0], pw
    assert not [t for t in re.findall(r"<input\b[^>]*>", block) if "data-secret" in t], (
        "the KVO password is not one of the script's environment secrets")
    for i in ("licAdd", "licList", "licCount"):
        assert 'id="%s"' % i in block, i
    js = _read("licences.js")
    m = re.search(r"var CODE_QTY_RE\s*=\s*/(.+?)/;", js)
    assert m, "licences.js declares var CODE_QTY_RE = /.../;"
    rule = re.compile(m.group(1))
    for value in ("AAAA", "AAAA,1", "AAAA,123456", "AAAA,1234567", "AAAA,", "AAAA,x", "-AAA", "AAA",
                  "A" * 64, "A" * 65, "has space"):
        assert bool(rule.fullmatch(value)) == bool(api.CODE_QTY.fullmatch(value)), value
    assert "codeTail" in js, "a code is shown by its last four characters, never whole"
    assert not re.search(r"localStorage\s*\.", js), (
        "the KVO password and its codes are stored nowhere: no localStorage call in this file")


def test_the_teardown_screen_gates_on_the_typed_name_and_points_at_licensing():
    """The order this screen exists to enforce: the read-only audit, the
    licence warning, the typed name, the run. The button starts disabled
    in the page itself, so a script that failed to load leaves it off
    rather than armed."""
    html = _read("index.html")
    m = re.search(r'<section class="page" id="page-teardown".*?</section>', html, re.S)
    assert m
    block = m.group(0)
    run = [t for t in re.findall(r"<button\b[^>]*>", block) if 'id="tdRun"' in t]
    assert len(run) == 1 and "disabled" in run[0], run
    assert "--orphans" in block, "the audit says which flag it is"
    js = _read("teardown.js")
    assert "confirm_name:model.typed" in js, "the typed name is what the API is given"
    assert "orphans_only:true" in js, "the audit is the read-only run"
    assert 'clNav.show("licensing")' in js, "the warning points at the screen that fixes it"
    assert "licences_released:gate.licencesReleased" in js


# ---------------------------------------------------------------- buttons
def _js_function(src, name):
    """The text of `function name(...){ ... }`, braces matched. Every body
    read this way is checked to hold no brace inside a string literal, which
    is the one thing that would fool the count."""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{" % re.escape(name), src)
    assert m, "%s() is not declared in the file" % name
    depth, i = 0, m.end() - 1
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                body = src[m.start():i + 1]
                for literal in re.findall(r"'[^'\n]*'|\"[^\"\n]*\"", body):
                    assert "{" not in literal and "}" not in literal, (
                        "%s() holds a brace in a string: the brace count cannot read it" % name)
                return body
        i += 1
    raise AssertionError("%s() has unbalanced braces" % name)


def test_a_wizard_launch_hands_the_quick_flow_run_button_back():
    """Reproduced: click Run on a quick flow, then launch a run from the
    wizard. The wizard's run finishes, the pill reads complete, and the Run
    button is still disabled reading "Running...". finish() is right not to
    relabel it (the button did not start that run: quickRun is false), so the
    restore belongs where begin() clears quickRun, on the way in.

    Read as text because the fault only shows across two runs in a browser."""
    src = _read("app.js")
    body = _js_function(src, "begin")
    assert "quickRun=false" in body, "begin() clears quickRun"
    assert "armRunBtn()" in body, (
        "begin() restores the Run button where it clears quickRun, or a wizard "
        "launch strands it: " + body)
    arm = _js_function(src, "armRunBtn")
    assert '$("runBtn").disabled=false' in arm, arm
    assert "Run this flow" in arm, "the button goes back to its own label: " + arm
    fin = _js_function(src, "finish")
    assert "if(quickRun)" in fin, "finish() relabels only the button that started the run"


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


def test_the_operations_screens_use_the_routes_they_are_faces_on():
    """Operate reads /api/status and replays through /api/run; Licensing
    speaks only to /api/licences/*; Teardown audits and runs through
    /api/teardown, follows the audit on /events/ and proves the result
    with /api/verify-empty. A path typed wrong in any of them is a button
    that does nothing, silently."""
    assert {"/api/status", "/api/run"} <= _script_paths(_read("operate.js"))
    lic = _script_paths(_read("licences.js"))
    assert lic and all(p.startswith("/api/licences") for p in lic), sorted(lic)
    assert {"/api/teardown", "/api/verify-empty", "/events/"} <= _script_paths(_read("teardown.js"))


def test_the_audit_report_listens_for_the_frames_an_unwired_run_sends():
    """teardown-stack.sh runs unwired: it has no events channel, so the
    console's own frames are all there are (narrate for the command line,
    log per output line, done or error for the verdict). A named SSE event
    with no listener is never delivered, so a type missing here is a
    report that silently stops at the command line."""
    from cloudlens_console import events as E
    js = _read("teardown.js")
    heard = set(re.findall(r'addEventListener\("(\w+)"', js))
    heard |= set(re.findall(r'\["([\w",]+)"\]\.forEach\(function\(type\)', js))
    heard = {w for chunk in heard for w in chunk.replace('"', "").split(",")}
    assert {E.NARRATE, E.LOG, E.DONE, E.ERROR} <= heard, sorted(heard)
    assert "phase" not in heard, "the audit has no phases: that screen is Watch's"


def test_the_wizard_uses_the_discovery_plan_run_and_doctor_routes():
    paths = _script_paths(_read("wizard.js"))
    assert {"/api/doctor", "/api/discover/vpcs", "/api/discover/subnets", "/api/discover/workloads",
            "/api/discover/eks", "/api/plan", "/api/run"} <= paths, sorted(paths)


# -------------------------------------------------------------------- ids
def _ids_a_script_names(src):
    """Every id a script names: $("x"), and any #id inside a string literal,
    which is how the selector calls name theirs (querySelector('#opsNav
    [data-page]'), "#wSteps [data-step]", "#secrets [data-secret]"). A
    renamed id in one of those is as broken as one in $(), and silently:
    querySelectorAll finds nothing and no line of the page ever runs."""
    ids = set(re.findall(r'\$\("([^"]+)"\)', src))
    for dq, sq in re.findall(r'"([^"\n]*)"|\'([^\'\n]*)\'', src):
        ids.update(re.findall(r"#([A-Za-z][\w-]*)", dq or sq))
    return ids


@pytest.mark.parametrize("name", ("app.js", "wizard.js", "watch.js", "operate.js", "licences.js",
                                 "teardown.js"))
def test_every_id_a_script_looks_up_exists_in_the_page(name):
    ids = set(re.findall(r'id="([^"]+)"', _read("index.html")))
    wanted = _ids_a_script_names(_read(name))
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


def test_a_pick_table_chooses_with_a_button_not_with_the_row(name="wizard.js"):
    """A <tr role="button"> replaces the row's own role, and a table whose
    rows are buttons stops being a table to a screen reader: the columns,
    the headers and the row count all go. The choice is a real button in
    the first cell instead, wearing the row's type; the row keeps the
    hover, the picked background and the mouse click."""
    js = _read(name)
    assert 'setAttribute("role","button")' not in js, "a row is a row"
    assert 'class="pickb"' in js, "the pick is a button in the first cell"
    assert "button.pickb" in _read("index.html"), "the button wears the row's type"


def test_each_tab_names_the_panel_it_controls():
    html = _read("index.html")
    for page in ("preflight", "deploy", "watch", "operate", "licensing", "teardown"):
        assert 'aria-controls="page-%s"' % page in html, page
        assert 'id="page-%s" role="tabpanel" aria-labelledby="tab-%s"' % (page, page) in html, page


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
