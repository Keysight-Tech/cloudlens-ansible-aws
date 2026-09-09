"""Puts console/ (the directory above tests/) on sys.path once, so every test
module imports the package under test the same way with no shim of its own.
pytest loads this file before collecting the modules beside it."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
