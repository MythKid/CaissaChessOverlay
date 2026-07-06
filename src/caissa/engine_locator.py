"""Finds the chess engine so the app is genuinely 'click and run'.

The engine binary ships *inside* the app, so the user never has to download
or configure anything. This module resolves an absolute path to it, whether
running from source (<project>/engine/stockfish.exe) or from a PyInstaller
build (unpacked under sys._MEIPASS/engine, or beside the .exe). An optional
custom path lets advanced users point at their own engine instead.

IMPORTANT: always return an ABSOLUTE path - launching an engine via a
relative path fails on Windows (WinError 2) through python-chess/asyncio.
"""
from __future__ import annotations

import os
import shutil
import sys

ENGINE_NAMES = ("stockfish.exe", "stockfish")


def _project_root() -> str:
    """Repository root when running from source: this file lives at
    <root>/src/caissa/engine_locator.py, so go up three levels."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))


def _search_dirs():
    """Directories that might contain the bundled engine, most specific first."""
    dirs = []
    # 1) PyInstaller onefile unpack dir.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "engine"))
    # 2) Next to the running .exe (onedir builds / engine placed beside app).
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "engine"))
    # 3) The project's engine/ folder (running from source).
    dirs.append(os.path.join(_project_root(), "engine"))
    return dirs


def find_bundled_engine() -> str | None:
    """Return an absolute path to the shipped engine, or None if missing."""
    for d in _search_dirs():
        for name in ENGINE_NAMES:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    # Last resort: an engine already on the system PATH.
    found = shutil.which("stockfish") or shutil.which("stockfish.exe")
    return os.path.abspath(found) if found else None


def resolve_engine(custom_path: str = "") -> str | None:
    """Prefer a valid user-supplied custom engine, else the bundled one."""
    if custom_path and os.path.isfile(custom_path):
        return os.path.abspath(custom_path)
    return find_bundled_engine()


def engine_status(custom_path: str = "") -> tuple[bool, str]:
    """(ready, human-readable message) for display in the UI."""
    path = resolve_engine(custom_path)
    if not path:
        return False, "No engine found"
    if custom_path and os.path.isfile(custom_path):
        return True, "Ready (your custom engine)"
    return True, "Ready (built-in)"
