"""Make the `caissa` package importable when running the tests from the repo
root (e.g. `python -m pytest`), without installing the package."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))
