"""Persistent configuration for Caissa Chess Overlay.

User data (settings, the learned board templates and debug captures) is kept
in the OS user-data directory - %LOCALAPPDATA%\\Caissa on Windows - NOT next
to the executable. That keeps the application folder clean and follows normal
Windows app conventions (the install folder stays read-only, per-user state
lives under the user's profile).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict

APP_DIR_NAME = "Caissa"


PORTABLE_MARKER = "portable.txt"
PORTABLE_DATA_DIRNAME = "Caissa-data"


def _app_dir() -> str:
    """Folder that contains the app itself (the .exe when frozen, else the
    project root when running from source)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))   # src/caissa -> project root


def _is_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".writetest")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _user_data_dir() -> tuple[str, bool]:
    """Return (data_dir, portable).

    Portable mode: if a 'portable.txt' marker sits next to the app, keep all
    data in a 'Caissa-data' folder right beside it - so settings travel with
    the app (e.g. on a USB stick) and nothing is written to the host machine.
    Falls back to the per-user location if that spot isn't writable.
    """
    app_dir = _app_dir()
    if os.path.isfile(os.path.join(app_dir, PORTABLE_MARKER)):
        portable = os.path.join(app_dir, PORTABLE_DATA_DIRNAME)
        if _is_writable(portable):
            return portable, True

    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_DIR_NAME)
    elif sys.platform == "darwin":
        path = os.path.expanduser(f"~/Library/Application Support/{APP_DIR_NAME}")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        path = os.path.join(base, APP_DIR_NAME.lower())
    if _is_writable(path):
        return path, False
    return os.path.expanduser("~"), False


DATA_DIR, PORTABLE = _user_data_dir()
DEFAULT_CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
# Learned board templates (calibrate once, reused forever) and debug captures.
TEMPLATES_PATH = os.path.join(DATA_DIR, "board_templates.npz")
DEBUG_DIR = os.path.join(DATA_DIR, "debug_shots")


CONFIG_VERSION = 4


@dataclass
class Config:
    # Absolute path to a custom engine executable (optional; blank = built-in).
    stockfish_path: str = ""
    # Screen region of the board as [left, top, width, height] in global px.
    region: list | None = None
    # Which colour sits at the BOTTOM of the board on screen:
    #   "white" -> normal orientation (default), "black" -> board is flipped.
    orientation: str = "white"
    # Engine search depth cap in plies. Generous on purpose: the think-time
    # cap is what bounds latency, and a low depth cap would waste the time
    # budget in easy positions (the engine would stop early instead of
    # searching deeper).
    depth: int = 30
    # Hard time cap (seconds) for the deep search. The engine stops at depth
    # OR time, whichever comes first - this is what bounds recommendation lag.
    # Exposed as the "Thinking time" slider in Settings.
    think_time: float = 2.5
    # Number of CPU cores the engine may use. 0 = automatic (half the cores),
    # which is polite to the game/browser you're playing in. Exposed as the
    # "CPU cores" slider in Settings.
    engine_threads: int = 0
    # How often (seconds) to poll the screen for changes.
    poll_interval: float = 0.12
    # Per-square change sensitivity (0..1). Lower = more sensitive (High preset).
    change_threshold: float = 0.08
    # Consecutive polls the read must repeat before a move is committed
    # (1 = two identical consecutive reads; keeps latency low).
    stability_frames: int = 1
    # A detected board must span at least this fraction of the smaller screen
    # dimension to count as the real game - filters out small example/diagram
    # boards on article pages.
    min_board_frac: float = 0.30
    # Remembered overlay window size (0 = use the built-in default).
    window_w: int = 0
    window_h: int = 0
    # Config schema version (used for one-time default upgrades).
    version: int = CONFIG_VERSION

    # ------------------------------------------------------------------ #
    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        """Write the current settings to disk as JSON."""
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str = DEFAULT_CONFIG_PATH) -> "Config":
        """Load settings from disk, falling back to defaults on any error."""
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                # Only accept keys we actually know about.
                known = {k: v for k, v in data.items()
                         if k in cls.__dataclass_fields__}
                cfg = cls(**known)
                # One-time upgrades for configs written by older versions.
                old = data.get("version", 0)
                if old < 3:
                    cfg.change_threshold = 0.08
                    cfg.think_time = 2.5
                    cfg.poll_interval = 0.12
                    cfg.stability_frames = 1
                if old < 4:
                    cfg.depth = 30   # depth cap; think_time bounds latency
                if old < CONFIG_VERSION:
                    cfg.version = CONFIG_VERSION
                return cfg
            except (json.JSONDecodeError, TypeError, ValueError, OSError):
                pass
        return cls()
