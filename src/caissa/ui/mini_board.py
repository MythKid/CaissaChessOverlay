"""A compact chessboard widget that mirrors a python-chess Board.

Rendered as vector SVG via chess.svg (piece artwork ships inside python-chess),
NOT as Unicode font glyphs - font fallback on Windows silently drops some
chess glyphs (emoji substitution / missing glyph), which showed up as
"missing pieces". SVG rendering is deterministic on every machine.
"""
from __future__ import annotations

import chess
import chess.svg
from PyQt6.QtCore import QByteArray, QRectF
from PyQt6.QtGui import QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QWidget

# Chess.com-like palette to match what most users play on.
BOARD_COLORS = {
    "square light": "#e9edcf",
    "square dark": "#6f8f5a",
    "square light lastmove": "#e7e08a",
    "square dark lastmove": "#a9b96a",
}
# NOTE: QtSvg (SVG Tiny) does not parse 8-digit #rrggbbaa colours - use
# 6-digit hex only, or strokes silently fail to render.
ARROW_COLOR = "#e8a33c"      # best-move arrow (warm amber, reads on green)
FROM_FILL = "#e3c85a"        # softened amber: the piece to pick up
TO_FILL = "#5fb87f"          # green: where it should go


class MiniBoard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.board = chess.Board()
        self.orientation = "white"
        self.last_move = None    # chess.Move | None
        self.best_move = None    # chess.Move | None (arrow + square fills)
        self._renderer: QSvgRenderer | None = None
        self.setMinimumSize(200, 200)
        self._rebuild()

    def update_state(self, fen, orientation="white", last_move=None, best_move=None):
        try:
            self.board = chess.Board(fen)
        except Exception:
            pass
        self.orientation = orientation
        self.last_move = last_move
        self.best_move = best_move
        self._rebuild()
        self.update()

    # ------------------------------------------------------------------ #
    def _rebuild(self):
        arrows = []
        fills = {}
        if self.best_move:
            arrows = [chess.svg.Arrow(self.best_move.from_square,
                                      self.best_move.to_square,
                                      color=ARROW_COLOR)]
            # Triple cue: yellow = pick this piece up, green = put it here,
            # arrow = the path. Impossible to miss which piece moves where.
            fills = {self.best_move.from_square: FROM_FILL,
                     self.best_move.to_square: TO_FILL}
        svg = chess.svg.board(
            board=self.board,
            orientation=chess.WHITE if self.orientation == "white" else chess.BLACK,
            lastmove=self.last_move,
            arrows=arrows,
            fill=fills,
            coordinates=False,
            colors=BOARD_COLORS,
        )
        self._renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))

    def paintEvent(self, event):
        if self._renderer is None or not self._renderer.isValid():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        x = (self.width() - side) / 2
        y = (self.height() - side) / 2
        self._renderer.render(p, QRectF(x, y, side, side))
        p.end()
