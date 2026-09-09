"""Puts console/ (the directory above tests/) on sys.path once, so every test
module imports the package under test the same way with no shim of its own.
pytest loads this file before collecting the modules beside it.

It also keeps tests/browser out of the default run. Those tests drive a real
chromium through a real server: they are slower than the rest of the suite
put together and they need a browser build that most machines running
`pytest tests` do not have. They are collected only when the command line
names them:

    python3 -m pytest tests -q            the fast suite, no browser
    python3 -m pytest tests/browser -q    the browser smoke tests
    python3 -m pytest tests tests/browser -q      both

An exclusion rather than a marker on purpose: a marker still imports the
module, and importing it imports playwright."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BROWSER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser")


def _asked_for(config):
    """Whether the command line names something inside tests/browser. A
    file, a directory or a single test id (path::test) all count; nothing
    else does, so `pytest`, `pytest tests` and `pytest tests/test_api.py`
    all leave the browser suite alone."""
    for arg in config.args:
        target = os.path.abspath(str(arg).split("::")[0])
        if target == BROWSER or target.startswith(BROWSER + os.sep):
            return True
    return False


def pytest_ignore_collect(collection_path, config):
    """True skips a path entirely: it is never imported, so a machine with
    no playwright still runs the default suite clean."""
    path = os.path.abspath(str(collection_path))
    if path != BROWSER and not path.startswith(BROWSER + os.sep):
        return None
    return None if _asked_for(config) else True


def pytest_report_header(config):
    """Say it in the header, so a run that quietly left the browser tests
    out says so at the top rather than being read as the whole suite."""
    if _asked_for(config):
        return "browser tests: collected (tests/browser was named on the command line)"
    return ("browser tests: NOT collected (tests/browser is left out unless it is named: "
            "run `python3 -m pytest tests tests/browser -q` for both)")
