"""The deploy profile: the ONE contract between the wizard and deploy-stack.sh.

A profile is a KEY="value" file of the interview's CLOUDLENS_* answers. The
console writes one from its forms; the script replays it with --profile and
asks no question a key already answers. The key list lives in
deploy/profile-keys.txt and is read by both sides, so a key added to one is
added to the other by construction (test_profile.py holds the script's
built-in fallback copy to the same list).

The script's loader strips one pair of outer quotes from a value and never
unescapes, so render() refuses the two things it could not write faithfully,
a double quote and a line break, instead of escaping them.
"""
import os
import re
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KEYS_FILE = os.path.join(REPO, "deploy", "profile-keys.txt")
_KEY = re.compile(r"^CLOUDLENS_[A-Z0-9_]+$")
_BAD_IN_VALUE = re.compile(r'["\r\n]')


def allowed_keys():
    """The keys a profile may set, in the file's order. Comments and blank
    lines are skipped; anything else that is not a CLOUDLENS_* name is a
    corrupted file and an error, never silently a key. A line that changes
    under strip() (a CR from a CRLF save, a trailing space, an indented key)
    is an error too: the script matches whole lines with grep -x, so such a
    line would refuse the key there while this side still wrote it."""
    keys = []
    # newline="": line endings come back untranslated, so a CR is seen.
    with open(KEYS_FILE, newline="") as fh:
        for raw in fh:
            line = raw[:-1] if raw.endswith("\n") else raw
            if line != line.strip():
                raise ValueError("%s: whitespace or CR around %r (LF endings, no trailing space)"
                                 % (KEYS_FILE, line))
            if not line or line.startswith("#"):
                continue
            if not _KEY.match(line):
                raise ValueError("%s: not a profile key: %r" % (KEYS_FILE, line))
            keys.append(line)
    return keys


def render(plan):
    """plan: {KEY: value}. Returns the profile text.

    Unknown keys are dropped, never an error: the UI builds plans from forms
    and the script enforces the same list. A None value is left out (the
    script then asks, or takes its default); an empty string is written, as
    the interview does for a field it did not use. A value the loader could
    not read back unchanged raises ValueError. Shape only, not vocabulary:
    that a value is true/false, a region or a CIDR is the forms' job."""
    keys = allowed_keys()
    lines = [
        "# CloudLens deploy profile, written %s by the CloudLens console."
        % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "# Replay (no questions asked): deploy-stack.sh --profile <this file>",
        "# Secrets (licence codes, AWS keys, project keys) are never stored here; they are asked.",
    ]
    for k in keys:
        if k not in plan or plan[k] is None:
            continue
        v = str(plan[k])
        if _BAD_IN_VALUE.search(v):
            raise ValueError("%s: a profile value may not contain a double quote or a line break: %r" % (k, v))
        lines.append('%s="%s"' % (k, v))
    return "\n".join(lines) + "\n"
