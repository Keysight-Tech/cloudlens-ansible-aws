"""The deploy profile is the one contract between the console and
deploy-stack.sh: the console writes deploy-profile-<stack>.env from a form and
the script replays it with --profile. Both sides read deploy/profile-keys.txt;
the script also carries a built-in copy of the list for a bare curl|bash run
before the repo is cloned. These tests hold the file, the built-in copy, the
interview's own writer and the console's writer to the same 44 keys, and prove
that what the console writes is what the script loads.
Run:  cd console && python3 -m pytest tests/test_profile.py -q
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

# The credential-free environment of deploy/tests/test_events.sh, so a dry run
# can never reach STS or sign in with whatever the developer's shell carries.
# HOME is the temp dir too: the script reads ~/.aws and ~/.ssh.
_NOCREDS = ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN")


def _run(script, args, cwd):
    """Run the script by absolute path from cwd (its state file lands in cwd,
    which must be the temp dir, never the repo)."""
    env = {k: v for k, v in os.environ.items() if k not in _NOCREDS}
    env.update(HOME=str(cwd), AWS_CONFIG_FILE="/dev/null",
               AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_EC2_METADATA_DISABLED="true")
    return subprocess.run(["/bin/bash", script] + list(args), cwd=str(cwd), env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


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


def test_keys_match_the_script():
    keys = P.allowed_keys()
    assert "CLOUDLENS_REGION" in keys and HOSTILE not in keys
    assert len(keys) == len(set(keys)), "duplicate key in profile-keys.txt"
    for k in keys:
        assert re.match(r"^CLOUDLENS_[A-Z0-9_]+$", k), k
    # the file and the built-in case are the same list, both directions
    assert set(keys) == _case_keys()
    # and the interview's own writer saves exactly that list, so a profile it
    # wrote replays with no key reported as ignored
    written = set(re.findall(r"CLOUDLENS_[A-Z0-9_]+", _function_body("write_profile")))
    assert written == set(keys)


def test_render_only_allowlisted_and_round_trips(tmp_path):
    plan = {"CLOUDLENS_REGION": "us-east-1", "CLOUDLENS_STACK_NAME": "demo",
            HOSTILE: "-oProxyCommand=x", "CLOUDLENS_TAPPING": "both",
            "CLOUDLENS_EXISTING_VPC_ID": None}
    text = P.render(plan)
    assert HOSTILE not in text and 'CLOUDLENS_TAPPING="both"' in text
    assert "CLOUDLENS_EXISTING_VPC_ID" not in text, "a None value must not be written"
    p = tmp_path / "test-profile.env"
    p.write_text(text)
    out = _run(SCRIPT, ["--dry-run", "--profile", str(p), "--key-name", "k"], tmp_path)
    combined = out.stdout + out.stderr
    assert "3 setting(s) applied" in out.stdout, combined
    assert "ignored keys" not in combined, combined
    assert out.returncode == 0, combined


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
    prof = tmp_path / "hostile.env"
    prof.write_text("%s=-oProxyCommand=x\nCLOUDLENS_REGION=us-east-1\n" % HOSTILE)
    for script in (SCRIPT, str(alone / "deploy-stack.sh")):
        out = _run(script, ["--profile", str(prof), "--help"], tmp_path)
        assert "ignored keys" in out.stderr and HOSTILE in out.stderr, out.stderr
        assert "1 setting(s) applied" in out.stdout, out.stdout + out.stderr
        assert out.returncode == 0, out.stdout + out.stderr


def test_keys_file_governs_when_present_and_only_narrows(tmp_path):
    # A key the built-in case knows but the file does not is refused: the
    # file is consulted, not the case, whenever it sits beside the script.
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
