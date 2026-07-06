"""The always-on-top dashboard overlay window (friendly, click-and-run UI)."""
from __future__ import annotations

import math
import os

import chess
import chess.svg
from PyQt6.QtCore import Qt, QByteArray, QRectF
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFrame, QFileDialog, QDialog, QComboBox, QFormLayout, QLineEdit,
    QDialogButtonBox, QMessageBox, QSlider, QSizeGrip, QSizePolicy,
)

from .mini_board import MiniBoard
from .region_selector import RegionSelector
from ..analysis_worker import AnalysisWorker
from ..engine import default_threads
from ..engine_locator import engine_status

# Friendly presets that map to the technical values under the hood.
SENSITIVITIES = [("Low - fewer false moves", 0.18),
                 ("Medium (recommended)", 0.12),
                 ("High - catch subtle moves", 0.08)]

# Thinking-time slider bounds (tenths of a second): 0.3 s .. 8.0 s.
THINK_MIN, THINK_MAX = 3, 80


def _nearest_index(options, value, key=1):
    """Index of the preset whose numeric value (at tuple pos `key`) is
    closest to `value`."""
    return min(range(len(options)), key=lambda i: abs(options[i][key] - value))


# --- Palette -------------------------------------------------------------- #
# One calm green accent over a cohesive neutral-charcoal base. Everything that
# isn't the recommendation stays neutral grey, so the eye goes to the move.
SURFACE   = "#202225"   # window/card background (neutral charcoal)
SURFACE_2 = "#282a2e"   # raised elements (buttons, chips)
SURFACE_3 = "#31343a"   # hover
LINE      = "#33363c"   # hairline borders
TEXT      = "#e8e8ea"   # primary text
TEXT_DIM  = "#9a9ba1"   # secondary text
TEXT_MUTE = "#6d6e75"   # captions / status
ACCENT    = "#4f9d6b"   # the single accent (muted green)
ACCENT_HI = "#5cae79"   # accent hover
ACCENT_TX = "#7cc998"   # accent text on dark (brighter for legibility)

STYLE = f"""
#container {{
    background-color: {SURFACE};
    border-radius: 14px;
    border: 1px solid {LINE};
}}
QLabel {{ color: {TEXT}; font-family: 'Segoe UI'; }}
#title    {{ font-size: 13px; font-weight: 600; color: {TEXT}; }}
#caption  {{ color: {TEXT_MUTE}; font-size: 10px; font-weight: 600; letter-spacing: 1.3px; }}
#eval     {{ font-size: 27px; font-weight: 700; color: {TEXT}; }}
#moveCard {{
    background: #223026;
    border: 1px solid #3d6a4e;
    border-radius: 12px;
}}
#moveCardWait {{
    background: {SURFACE_2};
    border: 1px solid {LINE};
    border-radius: 12px;
}}
#moveSquares {{ font-size: 29px; font-weight: 800; color: {ACCENT_TX}; letter-spacing: 1px; }}
#moveDesc    {{ font-size: 14px; font-weight: 600; color: #dbe7df; }}
#moveWait    {{ font-size: 16px; font-weight: 700; color: {TEXT_DIM}; }}
#pv       {{ color: {TEXT_DIM}; font-size: 11px; }}
#status   {{ color: {TEXT_MUTE}; font-size: 10px; }}
#brand    {{ color: {TEXT_MUTE}; font-size: 9px; padding-top: 2px; }}
QLabel#chipPhase, QLabel#chipTurn {{
    background: {SURFACE_2}; color: {TEXT_DIM};
    border-radius: 7px; padding: 3px 9px; font-size: 11px; font-weight: 600;
}}
QPushButton {{
    background: {SURFACE_2}; color: {TEXT};
    border: none; border-radius: 8px; padding: 8px 10px; font-size: 12px;
}}
QPushButton:hover {{ background: {SURFACE_3}; }}
QPushButton:disabled {{ color: #5a5b61; background: {SURFACE}; }}
QPushButton#primary {{ background: {ACCENT}; color: #ffffff; font-weight: 600; padding: 9px; }}
QPushButton#primary:hover {{ background: {ACCENT_HI}; }}
QPushButton#close, QPushButton#help {{
    background: transparent; color: {TEXT_DIM}; font-size: 15px; padding: 0 6px;
}}
QPushButton#close:hover {{ color: #cf6b62; }}
QPushButton#help:hover {{ color: {ACCENT_TX}; }}
QSlider::groove:horizontal {{ height: 4px; background: {LINE}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
"""


# --------------------------------------------------------------------------- #
class PieceIcon(QWidget):
    """Renders one chess piece (vector, from chess.svg) - shows the user
    exactly WHICH piece the recommendation wants them to move."""

    def __init__(self, size=38, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._renderer: QSvgRenderer | None = None

    def set_piece(self, symbol):
        """symbol: 'N', 'q', ... or None to clear."""
        if not symbol:
            self._renderer = None
        else:
            try:
                svg = chess.svg.piece(chess.Piece.from_symbol(symbol), size=45)
                self._renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            except Exception:
                self._renderer = None
        self.setVisible(self._renderer is not None)
        self.update()

    def paintEvent(self, event):
        if self._renderer is None or not self._renderer.isValid():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._renderer.render(p, QRectF(0, 0, self.width(), self.height()))
        p.end()


# --------------------------------------------------------------------------- #
class EvalBar(QWidget):
    """A thin horizontal bar; the white portion grows with White's advantage."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(10)
        self.frac = 0.5

    def set_eval(self, cp, mate):
        if mate is not None:
            self.frac = 1.0 if mate > 0 else 0.0
        elif cp is None:
            self.frac = 0.5
        else:
            self.frac = 1.0 / (1.0 + math.exp(-cp / 400.0))
        self.update()

    def paintEvent(self, e):
        # A classic white/black advantage bar - neutral greys, no accent colour.
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0x30, 0x33, 0x39))          # black's side (track)
        p.drawRoundedRect(0, 0, w, h, 4, 4)
        p.setBrush(QColor(0xdd, 0xde, 0xe2))          # white's side
        p.drawRoundedRect(0, 0, max(6, int(w * self.frac)), h, 4, 4)
        p.end()


# --------------------------------------------------------------------------- #
class SettingsDialog(QDialog):
    def __init__(self, config, overlay=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.overlay = overlay
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(440)
        form = QFormLayout(self)

        ready, msg = engine_status(config.stockfish_path)
        engine_lbl = QLabel(("✓  " if ready else "✗  ") + msg)
        engine_lbl.setStyleSheet("color:%s;" % ("#6fd0a0" if ready else "#ff8080"))
        form.addRow("Chess engine:", engine_lbl)

        # Thinking-time slider: the search streams live either way, so this
        # only sets how long the engine may keep improving the answer.
        srow = QHBoxLayout()
        self.time_slider = QSlider(Qt.Orientation.Horizontal)
        self.time_slider.setRange(THINK_MIN, THINK_MAX)
        self.time_slider.setValue(
            int(round(getattr(config, "think_time", 2.5) * 10)))
        self.time_lbl = QLabel(f"{self.time_slider.value() / 10:.1f} s")
        self.time_lbl.setFixedWidth(44)
        self.time_slider.valueChanged.connect(
            lambda v: self.time_lbl.setText(f"{v / 10:.1f} s"))
        srow.addWidget(self.time_slider)
        srow.addWidget(self.time_lbl)
        srow_w = QWidget()
        srow_w.setLayout(srow)
        srow_w.setToolTip("How long I may keep improving the move. The first "
                          "suggestion always appears instantly; this caps the "
                          "deep polish. Higher = stronger.")
        form.addRow("Thinking time:", srow_w)

        # CPU-cores slider: how much of the machine the engine may use. The
        # default (half the cores) is polite to the game you're playing in;
        # more cores = faster/deeper, fewer = gentler on the system.
        self._cpu = max(1, os.cpu_count() or 2)
        self._cpu_default = default_threads()
        cur = getattr(config, "engine_threads", 0) or self._cpu_default
        crow = QHBoxLayout()
        self.cpu_slider = QSlider(Qt.Orientation.Horizontal)
        self.cpu_slider.setRange(1, self._cpu)
        self.cpu_slider.setValue(max(1, min(int(cur), self._cpu)))
        self.cpu_lbl = QLabel("")
        # Size to the widest label it can ever show ("NN / NN  ·  default") so
        # the text never clips and the slider width stays stable.
        self.cpu_lbl.setMinimumWidth(
            self.cpu_lbl.fontMetrics().horizontalAdvance(
                f"{self._cpu} / {self._cpu}  ·  default") + 8)
        self.cpu_lbl.setAlignment(Qt.AlignmentFlag.AlignRight
                                  | Qt.AlignmentFlag.AlignVCenter)
        self.cpu_slider.valueChanged.connect(self._update_cpu_lbl)
        self._update_cpu_lbl(self.cpu_slider.value())
        crow.addWidget(self.cpu_slider, 1)
        crow.addWidget(self.cpu_lbl, 0)
        crow_w = QWidget()
        crow_w.setLayout(crow)
        crow_w.setToolTip(f"CPU cores the engine may use (this PC has "
                          f"{self._cpu}). Default is {self._cpu_default} - "
                          f"leave it there unless you want it faster (more) or "
                          f"gentler on your system (fewer).")
        if self._cpu <= 1:
            self.cpu_slider.setEnabled(False)
        form.addRow("CPU cores:", crow_w)

        self.sens = QComboBox()
        self.sens.addItems([name for name, _ in SENSITIVITIES])
        self.sens.setCurrentIndex(_nearest_index(SENSITIVITIES, config.change_threshold))
        form.addRow("Move detection:", self.sens)

        # Advanced / optional: bring your own engine.
        adv = QHBoxLayout()
        self.path_edit = QLineEdit(config.stockfish_path)
        self.path_edit.setPlaceholderText("(optional) use my own engine .exe")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        adv.addWidget(self.path_edit)
        adv.addWidget(browse)
        adv_w = QWidget()
        adv_w.setLayout(adv)
        form.addRow("Custom engine:", adv_w)

        # Manual fallbacks - only needed if auto-detection struggles.
        fb = QHBoxLayout()
        b1 = QPushButton("Set board box")
        b1.setToolTip("Manually drag a box around the board")
        b1.clicked.connect(self._manual_region)
        b2 = QPushButton("Calibrate")
        b2.setToolTip("Learn pieces now from a fresh starting position")
        b2.clicked.connect(self._manual_calibrate)
        b3 = QPushButton("Debug shot")
        b3.setToolTip("Save what I'm capturing, for troubleshooting")
        b3.clicked.connect(self._manual_debug)
        for b in (b1, b2, b3):
            fb.addWidget(b)
        fb_w = QWidget()
        fb_w.setLayout(fb)
        form.addRow("Troubleshooting:", fb_w)

        # Footer: a small brand mark on the left, OK/Cancel on the right.
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        footer = QHBoxLayout()
        brand = QLabel("Caissa  |  nodenull.org")
        brand.setStyleSheet("color:#6d6e75; font-size:10px;")
        footer.addWidget(brand)
        footer.addStretch(1)
        footer.addWidget(bb)
        form.addRow(footer)

    def _manual_region(self):
        if self.overlay:
            self.accept()
            self.overlay.select_region()

    def _manual_calibrate(self):
        if self.overlay:
            self.accept()
            self.overlay.calibrate()

    def _manual_debug(self):
        if self.overlay:
            self.overlay.debug_shot()

    def _update_cpu_lbl(self, v):
        # The row is already labelled "CPU cores:", so keep the value compact.
        txt = f"{v} / {self._cpu}"
        if v == self._cpu_default:
            txt += "  ·  default"
        self.cpu_lbl.setText(txt)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a chess engine", "",
            "Executable (*.exe);;All files (*)")
        if path:
            self.path_edit.setText(path)

    def apply_to_config(self):
        self.config.think_time = self.time_slider.value() / 10.0
        # Store 0 (= automatic) when left at the default, else the explicit count.
        v = self.cpu_slider.value()
        self.config.engine_threads = 0 if v == self._cpu_default else v
        self.config.change_threshold = SENSITIVITIES[self.sens.currentIndex()][1]
        self.config.stockfish_path = self.path_edit.text().strip()
        self.config.save()


# --------------------------------------------------------------------------- #
class OverlayWindow(QWidget):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.worker: AnalysisWorker | None = None
        self._drag_pos = None
        self._selector = None
        self.last_fen = chess.STARTING_FEN

        self._min_locked = False
        self._apply_flags()
        self._build_ui()
        self.setStyleSheet(STYLE)
        self._lock_minimum_size()
        self._refresh_controls()

        # Fully automatic: start immediately. With no board locked yet the
        # worker scans the screen by itself and locks on when a board appears.
        self.start_analysis()

    def _lock_minimum_size(self):
        """Forbid shrinking below the size where every widget fits, so the
        resize grip can never compress the layout into overlapping widgets.
        The layout is activated first so minimumSizeHint is actually valid."""
        lay = self.layout()
        if lay is not None:
            lay.activate()
        self.setMinimumSize(self.minimumSizeHint())
        self._min_locked = True

    def showEvent(self, e):
        # Re-lock once the window is really shown (fonts/stylesheet fully
        # realised), which yields the correct minimum size.
        super().showEvent(e)
        self._lock_minimum_size()

    # ------------------------- window setup ----------------------------- #
    def _apply_flags(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Qt.Tool windows are excluded from quit-on-last-window-closed by
        # default, so without this the app's event loop keeps running as an
        # invisible zombie process after the ✕ button closes the window.
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, True)
        self.setWindowOpacity(0.97)

    def paintEvent(self, event):
        # A translucent, frameless window has no background of its own, so Qt
        # can leave stale pixels behind when children move/resize/relabel
        # (e.g. an old "Stop" button ghosting after Stop is pressed). Clearing
        # the dirty region to transparent every paint removes that ghosting;
        # the container frame and children then repaint cleanly on top.
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.end()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        container = QFrame()
        container.setObjectName("container")
        # Resizable: only a minimum is enforced; the mini-board soaks up any
        # extra space, so a bigger window = a bigger, clearer board.
        container.setMinimumWidth(286)
        # NOTE: no QGraphicsDropShadowEffect here on purpose - a graphics
        # effect on a translucent, frameless top-level window causes stale-pixel
        # "ghosting" on Windows when widgets move/resize/relabel. The rounded
        # border in the stylesheet gives a clean look without that risk.
        outer.addWidget(container)

        v = QVBoxLayout(container)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(7)

        # --- title bar -------------------------------------------------- #
        title_row = QHBoxLayout()
        title = QLabel("♞  Caissa")
        title.setObjectName("title")
        help_btn = QPushButton("?")
        help_btn.setObjectName("help")
        help_btn.setFixedWidth(24)
        help_btn.setToolTip("How to use this")
        help_btn.clicked.connect(self.show_help)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("close")
        close_btn.setFixedWidth(26)
        close_btn.clicked.connect(self.close)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(help_btn)
        title_row.addWidget(close_btn)
        v.addLayout(title_row)

        # --- phase / turn chips ---------------------------------------- #
        chips = QHBoxLayout()
        self.phase_lbl = QLabel("Opening")
        self.phase_lbl.setObjectName("chipPhase")
        self.turn_lbl = QLabel("White to move")
        self.turn_lbl.setObjectName("chipTurn")
        chips.addWidget(self.phase_lbl)
        chips.addWidget(self.turn_lbl)
        chips.addStretch(1)
        v.addLayout(chips)

        # --- evaluation ------------------------------------------------- #
        cap_eval = QLabel("EVALUATION")
        cap_eval.setObjectName("caption")
        v.addWidget(cap_eval)
        self.eval_lbl = QLabel("+0.00")
        self.eval_lbl.setObjectName("eval")
        v.addWidget(self.eval_lbl)
        self.eval_bar = EvalBar()
        v.addWidget(self.eval_bar)

        # --- best move CARD --------------------------------------------- #
        cap_best = QLabel("YOUR BEST MOVE")
        cap_best.setObjectName("caption")
        v.addWidget(cap_best)

        self.move_card = QFrame()
        self.move_card.setObjectName("moveCardWait")
        card = QVBoxLayout(self.move_card)
        card.setContentsMargins(12, 9, 12, 9)
        card.setSpacing(2)
        # Top row: the PIECE to move (vector icon) + big "E2 -> E4" squares.
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.piece_icon = PieceIcon(38)
        self.move_squares = QLabel("—")
        self.move_squares.setObjectName("moveSquares")
        self.move_squares.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_row.addStretch(1)
        top_row.addWidget(self.piece_icon)
        top_row.addWidget(self.move_squares)
        top_row.addStretch(1)
        card.addLayout(top_row)
        # Plain-English description beneath.
        self.move_desc = QLabel("Waiting for your move")
        self.move_desc.setObjectName("moveWait")
        self.move_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.move_desc.setWordWrap(True)
        card.addWidget(self.move_desc)
        v.addWidget(self.move_card)

        # --- mini board visualiser (grows with the window) -------------- #
        self.mini = MiniBoard()
        self.mini.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
        v.addWidget(self.mini, stretch=1)

        self.pv_lbl = QLabel("")
        self.pv_lbl.setObjectName("pv")
        self.pv_lbl.setWordWrap(True)
        v.addWidget(self.pv_lbl)

        # --- primary button --------------------------------------------- #
        self.primary_btn = QPushButton("Start")
        self.primary_btn.setObjectName("primary")
        self.primary_btn.clicked.connect(self.on_primary)
        v.addWidget(self.primary_btn)

        # --- the only controls you need with auto-detection ------------- #
        row1 = QHBoxLayout()
        self.colors_btn = QPushButton("Switch Colors")
        self.colors_btn.setToolTip("Tell me you're the OTHER side (board is "
                                   "flipped / you play from the top)")
        self.colors_btn.clicked.connect(self.switch_colors)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Re-find and re-read the board if I miss a "
                                    "move or get stuck")
        self.refresh_btn.clicked.connect(self.refresh_board)
        row1.addWidget(self.colors_btn)
        row1.addWidget(self.refresh_btn)
        v.addLayout(row1)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        v.addWidget(self.settings_btn)

        # --- status + resize grip --------------------------------------- #
        bottom = QHBoxLayout()
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("status")
        self.status_lbl.setWordWrap(True)
        grip = QSizeGrip(container)
        grip.setFixedSize(16, 16)
        grip.setToolTip("Drag to resize the window")
        bottom.addWidget(self.status_lbl, stretch=1)
        bottom.addWidget(grip, alignment=Qt.AlignmentFlag.AlignBottom)
        v.addLayout(bottom)

        # --- brand mark --------------------------------------------------
        # Plain, non-interactive text on its own row - kept well away from the
        # resize grip (no accidental clicks/browser launches while playing)
        # and off the live status line (which is updated from a dozen places
        # elsewhere and must never be overwritten by static text).
        brand = QLabel("Caissa  ·  nodenull.org  ·  © Methindu Damsara")
        brand.setObjectName("brand")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(brand)

    # ------------------------- control state ---------------------------- #
    def _running(self):
        return self.worker is not None and self.worker.isRunning()

    def _refresh_controls(self):
        running = self._running()
        self.primary_btn.setText("Stop" if running else "▶  Start")
        self.colors_btn.setEnabled(running)
        self.refresh_btn.setEnabled(running)
        if not running:
            self.status_lbl.setText("Paused. Press Start - I find the board and "
                                    "set up by myself.")
        # Force a clean full repaint so a relabelled button can't leave a
        # ghost of its old text behind on the translucent window.
        self.update()

    # ------------------------- actions ---------------------------------- #
    def on_primary(self):
        if self._running():
            self.stop_analysis()
        else:
            self.start_analysis()

    def switch_colors(self):
        if self._running():
            self.worker.request_switch_colors()

    def refresh_board(self):
        if self._running():
            self.worker.request_refresh()
            self.status_lbl.setText("Refreshing...")

    # --- manual fallbacks, reachable from Settings ---------------------- #
    def select_region(self):
        self._selector = RegionSelector()
        self._selector.region_selected.connect(self._on_region)
        self._selector.show()

    def _on_region(self, region):
        if region:
            self.config.region = region
            self.config.save()
            if not self._running():
                self.start_analysis()
            self.status_lbl.setText("Board area set manually - reading it now.")
        self._refresh_controls()

    def calibrate(self):
        if not self._running():
            self.start_analysis()
        if self.config.region:
            self.worker.request_calibrate()
            self.status_lbl.setText("Calibrating - make sure the board shows a "
                                    "fresh starting position.")

    def debug_shot(self):
        if not self._running():
            self.start_analysis()
        self.worker.request_debug_shot()
        self.status_lbl.setText("Saving a debug image...")

    def open_settings(self):
        dlg = SettingsDialog(self.config, overlay=self, parent=self)
        if dlg.exec():
            before = (self.config.stockfish_path,
                      getattr(self.config, "engine_threads", 0))
            dlg.apply_to_config()
            self.mini.orientation = self.config.orientation
            after = (self.config.stockfish_path,
                     getattr(self.config, "engine_threads", 0))
            # Only restart the engine when a setting that lives inside the
            # engine process changed (binary path / thread count). Think-time
            # and detection settings are read live - restarting for them would
            # throw away the engine's hash mid-game for nothing.
            if self._running() and after != before:
                self.worker.request_engine_reinit()
            self.status_lbl.setText("Settings saved.")

    def _push_exclude_rect(self):
        """Tell the worker where this window is (global PHYSICAL pixels) so
        the board scanner never locks onto the app's own mini-board."""
        if not self.worker:
            return
        g = self.frameGeometry()
        dpr = self.devicePixelRatioF()
        margin = 8  # a little slack around the window edges
        self.worker.set_exclude_rect([
            round((g.left() - margin) * dpr),
            round((g.top() - margin) * dpr),
            round((g.width() + 2 * margin) * dpr),
            round((g.height() + 2 * margin) * dpr),
        ])

    def start_analysis(self):
        if self._running():
            return
        self.worker = AnalysisWorker(self.config)
        self.worker.analysis_ready.connect(self.on_analysis)
        self.worker.status.connect(self.on_status)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self._refresh_controls)
        self._push_exclude_rect()
        self.worker.start()
        self._refresh_controls()

    # Keep the worker's self-exclusion rect current as the window moves.
    def moveEvent(self, e):
        self._push_exclude_rect()
        super().moveEvent(e)

    def resizeEvent(self, e):
        self._push_exclude_rect()
        super().resizeEvent(e)
        # Full repaint so widgets that shifted don't leave ghost pixels behind.
        self.update()

    def stop_analysis(self):
        if self.worker:
            self.worker.stop()
            # The worker may be mid-search. The abort callback stops the
            # engine at its next info line, but info lines can be sparse at
            # high depth - so wait out the full think-time budget (slider
            # allows up to 8 s) plus margin. Destroying a QThread that is
            # still running crashes the app on exit.
            budget_ms = int((getattr(self.config, "think_time", 2.5) + 3.0) * 1000)
            if not self.worker.wait(budget_ms):
                self.worker.wait(10000)   # engine time cap expires by then
        self._refresh_controls()

    def show_help(self):
        QMessageBox.information(
            self, "How to use Caissa",
            "Developed by Methindu Damsara  |  nodenull.org\n"
            "──────────────────────────────\n\n"
            "Fully automatic - just press Start and open a chess game.\n\n"
            "1.  Press Start. I scan the screen, find the board, learn its "
            "pieces and detect whether you're White or Black.\n"
            "2.  When it's your turn I show the best move - the piece to move, "
            "the squares (e.g. F3 → G5), and an arrow on the little board.\n"
            "3.  Play it, and I update automatically as the game goes on.\n\n"
            "The four buttons:\n"
            "•  Start / Stop  - turn me on or off.\n"
            "•  Switch Colors - press if I have you as the wrong side.\n"
            "•  Refresh       - re-read the board if I ever get stuck.\n"
            "•  Settings      - thinking time, CPU cores, and fallbacks.\n\n"
            "Tips: small diagram boards on web pages are ignored; only a real "
            "game board is picked up. Works on any flat 2-D board. The chess "
            "engine is built in - nothing to download.")

    # ------------------------- signal slots ----------------------------- #
    def on_analysis(self, data):
        self.last_fen = data["fen"]

        # The worker may have auto-detected which side we're on - reflect it.
        detected = data.get("orientation")
        if detected and detected != self.config.orientation:
            self.config.orientation = detected
            self.config.save()

        user_turn = data.get("is_user_turn", True)
        self.phase_lbl.setText(data["phase"])
        self.turn_lbl.setText("Your move" if user_turn else "Opponent to move")
        self.eval_bar.set_eval(data.get("eval_cp"), data.get("mate"))

        # Evaluation text.
        if data["game_over"]:
            self.eval_lbl.setText(data["result"] or "Game over")
        elif data["mate"] is not None:
            m = data["mate"]
            self.eval_lbl.setText(f"Mate in {abs(m)}")
        elif data["eval_cp"] is not None:
            self.eval_lbl.setText(f"{data['eval_cp'] / 100.0:+.2f}")
        else:
            self.eval_lbl.setText("—")

        # The move CARD - the thing you actually read.
        uci = data.get("best_uci", "-")
        if data["game_over"]:
            self.piece_icon.set_piece(None)
            self._set_card("moveCardWait", "Game over", data["result"] or "")
        elif user_turn and uci and uci != "-" and len(uci) >= 4:
            frm, to = uci[:2].upper(), uci[2:4].upper()
            thinking = data.get("thinking", False)
            depth = data.get("depth", 0)
            desc = data.get("best_desc") or ""
            if depth:
                desc += f"   ·  depth {depth}" + ("…" if thinking else "")
            elif thinking:
                desc += "   ·  thinking…"
            self.piece_icon.set_piece(data.get("best_piece"))
            self._set_card("moveCard", f"{frm}  →  {to}", desc)
        else:
            self.piece_icon.set_piece(None)
            self._set_card("moveCardWait", "· · ·", "Opponent to move")

        self.pv_lbl.setText("  ".join(data.get("pv", [])) if user_turn else "")

        last_move = self._safe_move(data.get("last_move"))
        best_move = self._safe_move(uci) if user_turn else None
        self.mini.update_state(data["fen"], self.config.orientation,
                               last_move, best_move)

        # Status line stays quiet now that the card is self-explanatory.
        if not data["game_over"]:
            self.status_lbl.setText("Your move." if user_turn
                                    else "Opponent to move…")

    @staticmethod
    def _repolish(widget):
        """Force a Qt style refresh after an objectName change."""
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _set_card(self, obj_name, squares, desc):
        """Update the move card (green when it's your move, grey when waiting)."""
        self.move_card.setObjectName(obj_name)
        self.move_desc.setObjectName("moveDesc" if obj_name == "moveCard" else "moveWait")
        self.move_squares.setText(squares)
        self.move_desc.setText(desc or "")
        self._repolish(self.move_card)
        self._repolish(self.move_desc)

    @staticmethod
    def _safe_move(uci):
        if not uci or uci == "-":
            return None
        try:
            return chess.Move.from_uci(uci)
        except (ValueError, TypeError):
            return None

    def on_status(self, text):
        self.status_lbl.setText(text)

    def on_error(self, text):
        self.status_lbl.setText(f"⚠ {text}")

    # ------------------------- window dragging -------------------------- #
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos and (e.buttons() & Qt.MouseButton.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def closeEvent(self, e):
        # Remember the window size the user chose (saved BEFORE the worker
        # wind-down so it persists even if the process has to be force-ended).
        self.config.window_w = self.width()
        self.config.window_h = self.height()
        self.config.save()
        if self.worker:
            self.worker.stop()
            # Short, bounded wind-down only: the abort callback stops any
            # running search almost immediately, but a wedged engine must
            # never hold the close hostage. app.main() force-terminates the
            # process after the event loop returns, so nothing can linger.
            self.worker.wait(2000)
        e.accept()
        # Closing the main HUD means quitting the app. Explicit, because
        # WA_QuitOnClose alone would keep the event loop alive if any other
        # window (a dialog, the region selector) happened to be open.
        QApplication.instance().quit()
