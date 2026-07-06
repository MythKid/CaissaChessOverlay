"""Build Caissa.exe.

Produces a single self-contained Caissa.exe at the project root (the chess
engine and icon are bundled inside). Build scratch goes to build/ and is safe
to delete.

    python -m pip install -r requirements.txt pyinstaller
    python build.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SEP = ";" if os.name == "nt" else ":"


def _p(*parts):
    return os.path.join(ROOT, *parts)


# Absolute paths throughout: PyInstaller resolves relative --add-data sources
# against the spec file's location (which we place in build/), so relative
# source paths would break.
ARGS = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--name", "Caissa",
    "--onefile",
    "--windowed",
    "--icon", _p("resources", "icon.ico"),
    # Windows version resource: product name, version and copyright shown in
    # Explorer's Properties -> Details for Caissa.exe.
    "--version-file", _p("resources", "version_info.txt"),
    "--paths", _p("src"),
    "--add-data", f"{_p('resources', 'icon.png')}{SEP}resources",
    "--add-data", f"{_p('engine', 'stockfish.exe')}{SEP}engine",
    "--distpath", ROOT,           # put Caissa.exe at the project root
    "--workpath", _p("build"),
    "--specpath", _p("build"),
    _p("run.py"),
]


def main() -> int:
    os.chdir(ROOT)
    rc = subprocess.call(ARGS)
    if rc == 0:
        # Tidy the scratch folder; the finished exe is Caissa.exe at the root.
        shutil.rmtree(os.path.join(ROOT, "build"), ignore_errors=True)
        print("\nBuilt:", os.path.join(ROOT, "Caissa.exe"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
