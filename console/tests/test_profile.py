"""The deploy profile is the one contract between the console and
deploy-stack.sh: the console writes deploy-profile-<stack>.env from a form and
the script replays it with --profile. Both sides read deploy/profile-keys.txt;
the script also carries a built-in copy of the list for a bare curl|bash run
before the repo is cloned. These tests hold the file, the built-in copy, the
interview's own writer and the console's writer to the same 44 keys, prove
that what the console writes is what the script loads, and that no keys file
(edited, planted beside a decoy, or absent) can ever widen what a profile may
set.
Run:  cd console && python3 -m pytest tests/test_profile.py -q
      and from a terminal: script -q /dev/null python3 -m pytest tests/test_profile.py -q
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from cloudlens_console import profile as P  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(REPO, "deploy", "deploy-stack.sh")
HOSTILE = "CLOUDLENS_ADMIN_USER"  # reaches ssh as an option; a profile must never set it

# Names a profile must never be able to set: each picks an image, an IAM role,
# an installer that is downloaded and run, the ssh username, a file the script
# reads, a pipe it answers on, or a secret. The list may grow; it may never
# gain one of these.
NEVER = {
    "CLOUDLENS_ADMIN_USER", "CLOUDLENS_WINDOWS_INSTANCE_PROFILE",
    "CLOUDLENS_VCONTROLLER_AMI", "CLOUDLENS_KVO_AMI", "CLOUDLENS_VPB_AMI",
    "CLOUDLENS_WINDOWS_INSTALLER_URL", "CLOUDLENS_KEY_PEM", "CLOUDLENS_VC_CREDS_FILE",
    "CLOUDLENS_PROMPT_PIPE", "CLOUDLENS_EVENTS_FILE",
    "CLOUDLENS_MIRROR_ACCESS_KEY", "CLOUDLENS_MIRROR_SECRET_KEY",
    "CLOUDLENS_VC_PASSWORD", "CLOUDLENS_KVO_ADMIN_PASS",
}

# A value holding every character the loader must pass through untouched: a
# single quote, a hash, a backslash, a $VAR and a backtick command. The plan
# has to print it back byte for byte; any expansion would be a bug.
TRICKY = "it's #1 \\ $HOME `id`"

# The credential-free environment of deploy/tests/test_events.sh, so a dry run
# can never reach STS or sign in with whatever the developer's shell carries.
# HOME is the temp dir too: the script reads ~/.aws and ~/.ssh.
_NOCREDS = ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN")


def _run(script, args, cwd, stdin_argv0=None):
    """Run the script from cwd (its state file lands in cwd, which must be the
    temp dir, never the repo). By absolute path normally; with stdin_argv0 the
    script is fed on stdin and bash is named that way in argv[0], the exact
    shape of `curl ... | bash` (or `| /bin/bash`), where BASH_SOURCE is empty
    and SCRIPT_DIR is only a guess (cwd for a bare name, /bin for the path).

    start_new_session: the script re-attaches its stdin to /dev/tty when it
    has one, so a run started from a terminal (a developer, not CI) would sit
    on its first question until the timeout. In a new session there is no
    controlling terminal to re-attach, stdin stays /dev/null, and the run is
    non-interactive wherever pytest was started.

    Every CLOUDLENS_* variable is dropped as well: they are the script's own
    inputs, and one exported in the developer's shell would change what the
    dry run resolves."""
    env = {k: v for k, v in os.environ.items()
           if k not in _NOCREDS and not k.startswith("CLOUDLENS_")}
    env.update(HOME=str(cwd), AWS_CONFIG_FILE="/dev/null",
               AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_EC2_METADATA_DISABLED="true")
    kw = dict(cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
              start_new_session=True)
    if stdin_argv0:
        with open(script) as fh:
            return subprocess.run([stdin_argv0, "-s", "--"] + list(args),
                                  executable="/bin/bash", stdin=fh, **kw)
    return subprocess.run(["/bin/bash", script] + list(args), stdin=subprocess.DEVNULL, **kw)


def _function_body(name):
    src = open(SCRIPT).read()
    m = re.search(r"^%s\(\) \{\n(.*?)^\}" % re.escape(name), src, re.S | re.M)
    assert m, "%s() not found in deploy-stack.sh" % name
    return m.group(1)


def _case_keys():
    """The keys of the script's built-in case: the fallback for a bare curl|bash."""
    body = _function_body("profile_key_allowed")
    case = re.search(r'case "\$1" in\n(.*?)\besac\b', body, re.S)
    assert case, "profile_key_allowed() has no built-in case"
    return set(re.findall(r"CLOUDLENS_[A-Z0-9_]+", case.group(1)))


def _hostile_profile(tmp_path):
    prof = tmp_path / "hostile.env"
    prof.write_text("%s=-oProxyCommand=x\nCLOUDLENS_REGION=us-east-1\n" % HOSTILE)
    return str(prof)


def test_keys_match_the_script():
    keys = P.allowed_keys()
    assert "CLOUDLENS_REGION" in keys and HOSTILE not in keys
    assert not NEVER & set(keys), NEVER & set(keys)
    assert len(keys) == len(set(keys)), "duplicate key in profile-keys.txt"
    for k in keys:
        assert re.match(r"^CLOUDLENS_[A-Z0-9_]+$", k), k
    # the script matches whole lines (grep -x): a CR or a trailing space on
    # any line would refuse that key there while the console still wrote it
    raw = open(P.KEYS_FILE, "rb").read()
    assert b"\r" not in raw, "profile-keys.txt has CRLF line endings"
    for line in raw.decode().split("\n"):
        assert line == line.strip(), "whitespace around %r in profile-keys.txt" % line
    # the file and the built-in case are the same list, both directions
    assert set(keys) == _case_keys()
    # and the interview's own writer saves exactly that list, so a profile it
    # wrote replays with no key reported as ignored
    written = set(re.findall(r"CLOUDLENS_[A-Z0-9_]+", _function_body("write_profile")))
    assert written == set(keys)


def test_allowed_keys_is_as_strict_as_grep_x(tmp_path, monkeypatch):
    # The Python side refuses exactly what grep -x would fail to match, so a
    # CRLF or trailing-space file is an error here rather than a list the
    # console honours and the script does not.
    f = tmp_path / "keys.txt"
    monkeypatch.setattr(P, "KEYS_FILE", str(f))
    for bad in (b"CLOUDLENS_REGION\r\nCLOUDLENS_STACK_NAME\r\n", b"CLOUDLENS_REGION \n",
                b" CLOUDLENS_REGION\n", b"# a comment\r\n", b"CLOUDLENS_REGION\r"):
        f.write_bytes(bad)
        with pytest.raises(ValueError):
            P.allowed_keys()
    # comments, blank lines and a missing final newline are all fine
    f.write_bytes(b"# c\n\nCLOUDLENS_REGION\nCLOUDLENS_STACK_NAME")
    assert P.allowed_keys() == ["CLOUDLENS_REGION", "CLOUDLENS_STACK_NAME"]


def test_render_only_allowlisted_and_round_trips(tmp_path):
    plan = {"CLOUDLENS_REGION": "us-east-1", "CLOUDLENS_STACK_NAME": "demo",
            HOSTILE: "-oProxyCommand=x", "CLOUDLENS_TAPPING": "both",
            "CLOUDLENS_DEPLOY_KVO": "true", "CLOUDLENS_KVO_NAME": TRICKY,
            "CLOUDLENS_EXISTING_VPC_ID": None}
    text = P.render(plan)
    assert HOSTILE not in text and 'CLOUDLENS_TAPPING="both"' in text
    assert 'CLOUDLENS_KVO_NAME="%s"' % TRICKY in text
    assert "CLOUDLENS_EXISTING_VPC_ID" not in text, "a None value must not be written"
    p = tmp_path / "test-profile.env"
    p.write_text(text)
    out = _run(SCRIPT, ["--dry-run", "--profile", str(p), "--key-name", "k"], tmp_path)
    combined = out.stdout + out.stderr
    assert "5 setting(s) applied" in out.stdout, combined
    assert "ignored keys" not in combined, combined
    assert out.returncode == 0, combined
    # the plan prints the name back verbatim: no $HOME expanded (HOME is
    # tmp_path in that environment), no `id` run, no quote or backslash lost
    names = [l.split("KVO name:", 1)[1].strip() for l in out.stdout.splitlines() if "KVO name:" in l]
    assert names == [TRICKY], (names, combined)
    assert str(tmp_path) not in out.stdout.split("KVO name:", 1)[1].split("\n", 1)[0]


def test_render_rejects_values_the_loader_cannot_read():
    # The loader strips one pair of outer quotes and never unescapes, so a
    # double quote inside a value cannot round-trip; a newline would end the
    # line early. Both are refused rather than written wrong.
    for bad in ('say "hi"', "a\nb", "a\rb"):
        with pytest.raises(ValueError):
            P.render({"CLOUDLENS_STACK_NAME": bad})
    # an empty value is fine: the interview writes them for unused fields
    assert 'CLOUDLENS_EXISTING_VPC_ID=""' in P.render({"CLOUDLENS_EXISTING_VPC_ID": ""})
    # and a single quote, a hash or a backslash pass through untouched
    assert "CLOUDLENS_KVO_NAME=\"it's #1\\\"" in P.render({"CLOUDLENS_KVO_NAME": "it's #1\\"})


def test_hostile_key_is_ignored_with_and_without_the_keys_file(tmp_path):
    # With the file next to the script (a clone) and without it (a bare
    # curl|bash, stood in for by the script copied out alone), the same
    # profile is refused the same way. --help exits right after the loader, so
    # nothing past it runs.
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(SCRIPT, alone / "deploy-stack.sh")
    assert not (alone / "profile-keys.txt").exists()
    prof = _hostile_profile(tmp_path)
    for script in (SCRIPT, str(alone / "deploy-stack.sh")):
        out = _run(script, ["--profile", prof, "--help"], tmp_path)
        assert "ignored keys" in out.stderr and HOSTILE in out.stderr, out.stderr
        assert "1 setting(s) applied" in out.stdout, out.stdout + out.stderr
        assert out.returncode == 0, out.stdout + out.stderr


def test_keys_file_governs_when_present_and_only_narrows(tmp_path):
    # A key the built-in case knows but the file does not is refused: the
    # file is consulted whenever it sits beside a script that came from disk.
    # And a line in the file that is not a CLOUDLENS_* name widens nothing:
    # a profile still cannot set PATH.
    d = tmp_path / "withfile"
    d.mkdir()
    shutil.copy(SCRIPT, d / "deploy-stack.sh")
    (d / "profile-keys.txt").write_text("# narrowed\nCLOUDLENS_REGION\nPATH\n")
    prof = tmp_path / "narrow.env"
    prof.write_text("CLOUDLENS_REGION=us-east-1\nCLOUDLENS_STACK_NAME=demo\nPATH=/x\n")
    out = _run(str(d / "deploy-stack.sh"), ["--profile", str(prof), "--help"], tmp_path)
    assert "1 setting(s) applied" in out.stdout, out.stdout + out.stderr
    assert "CLOUDLENS_STACK_NAME" in out.stderr and " PATH" in out.stderr, out.stderr
    assert out.returncode == 0, out.stdout + out.stderr


def test_keys_file_beside_the_script_cannot_widen(tmp_path):
    # An edited profile-keys.txt that lists a key the built-in case does not
    # know adds nothing: a key has to pass BOTH the file and the case. The
    # file can only ever narrow the list.
    d = tmp_path / "widened"
    d.mkdir()
    shutil.copy(SCRIPT, d / "deploy-stack.sh")
    (d / "profile-keys.txt").write_text("CLOUDLENS_REGION\n%s\n" % HOSTILE)
    out = _run(str(d / "deploy-stack.sh"), ["--profile", _hostile_profile(tmp_path), "--help"], tmp_path)
    assert "ignored keys" in out.stderr and HOSTILE in out.stderr, out.stdout + out.stderr
    assert "1 setting(s) applied" in out.stdout, out.stdout + out.stderr
    assert out.returncode == 0, out.stdout + out.stderr


def test_keys_file_that_refuses_every_key_is_named(tmp_path):
    # A CRLF keys file beside the script matches nothing under grep -x, so
    # every key is refused; the error names the file rather than blaming the
    # profile.
    d = tmp_path / "crlf"
    d.mkdir()
    shutil.copy(SCRIPT, d / "deploy-stack.sh")
    (d / "profile-keys.txt").write_bytes(b"CLOUDLENS_REGION\r\n")
    prof = tmp_path / "ok.env"
    prof.write_text("CLOUDLENS_REGION=us-east-1\n")
    out = _run(str(d / "deploy-stack.sh"), ["--profile", str(prof), "--help"], tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "no usable settings" in out.stderr and "check its line endings" in out.stderr, out.stderr
    assert str(d / "profile-keys.txt") in out.stderr, out.stderr


@pytest.mark.parametrize("argv0", ["bash", "/bin/bash"])
def test_planted_cwd_does_not_govern_a_script_fed_on_stdin(tmp_path, argv0):
    # `curl ... | bash` run from a directory someone prepared: a decoy
    # deploy-stack.sh beside a profile-keys.txt that lists the hostile key.
    # The running code never came from that directory (BASH_SOURCE is empty;
    # SCRIPT_DIR is cwd when bash was named bare, /bin when named by path), so
    # the file must not become the list. The first guard looked for a
    # deploy-stack.sh beside the file, and this exact layout got
    # CLOUDLENS_ADMIN_USER applied.
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "deploy-stack.sh").write_text("# decoy\n")
    (planted / "profile-keys.txt").write_text("CLOUDLENS_REGION\n%s\n" % HOSTILE)
    out = _run(SCRIPT, ["--profile", _hostile_profile(tmp_path), "--help"], planted, stdin_argv0=argv0)
    assert "ignored keys" in out.stderr and HOSTILE in out.stderr, out.stdout + out.stderr
    assert "1 setting(s) applied" in out.stdout, out.stdout + out.stderr
    assert out.returncode == 0, out.stdout + out.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
