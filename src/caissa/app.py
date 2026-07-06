"""Caissa Chess Overlay - application entry point.

The window floats always-on-top. Everything (the chess engine included) is
built in - just run it and open a chess game anywhere on screen.

Diagnostic:  run with  --engine-selftest  to verify the built-in engine can
launch and analyse; the result is written to
%TEMP%/caissa_selftest.json  (no window is shown).
"""
from __future__ import annotations

import os
import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from .config import Config
from .ui.overlay import OverlayWindow

APP_NAME = "Caissa Chess Overlay"


def resource_path(relative_path: str) -> str:
    """Resolve a bundled resource whether running from source or a frozen
    build. Frozen resources unpack under sys._MEIPASS; from source they live
    in the project's resources/ folder (two levels above this package file)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, relative_path)
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(here))   # src/caissa -> root
    return os.path.join(project_root, relative_path)


def _engine_selftest() -> int:
    """Verify the built-in engine resolves and analyses. Writes JSON so it
    works even in a windowed (no-console) build. Returns a process exit code."""
    import json
    import tempfile
    import chess
    import chess.engine

    from .engine_locator import resolve_engine
    from .config import DATA_DIR, PORTABLE

    out = {"data_dir": DATA_DIR, "portable": PORTABLE}
    try:
        path = resolve_engine("")
        out["engine_path"] = path
        engine = chess.engine.SimpleEngine.popen_uci(path)
        board = chess.Board()
        info = engine.analyse(board, chess.engine.Limit(depth=10))
        out["best_move"] = board.san(info["pv"][0])
        out["eval_cp"] = info["score"].white().score()
        engine.quit()
        out["ok"] = True
    except Exception as e:  # noqa: BLE001 - report anything back to the file
        out["ok"] = False
        out["error"] = repr(e)

    report = os.path.join(tempfile.gettempdir(), "caissa_selftest.json")
    with open(report, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    return 0 if out.get("ok") else 1


def main():
    if "--engine-selftest" in sys.argv:
        sys.exit(_engine_selftest())
    # The chess engine ships inside the app - the worker locates it
    # automatically (see engine_locator.resolve_engine).
    config = Config.load()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(True)

    icon = QIcon(resource_path(os.path.join("resources", "icon.png")))
    app.setWindowIcon(icon)

    win = OverlayWindow(config)
    win.setWindowIcon(icon)
    # Restore the size the user last chose (the window is freely resizable
    # via the grip in its bottom-right corner).
    win.resize(config.window_w or 330, config.window_h or 700)
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
