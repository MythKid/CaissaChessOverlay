"""Fullscreen overlay that lets the user drag a rectangle around the board.

Covers the whole virtual desktop (all monitors), dims everything, and lets the
user rubber-band the exact board area. Emits the selected region in global
screen coordinates (or an empty list if cancelled).
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QRect, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QPen, QGuiApplication
from PyQt6.QtWidgets import QWidget


class RegionSelector(QWidget):
    region_selected = pyqtSignal(list)   # [left, top, width, height] or []

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)

        # Cover the entire virtual desktop (spanning every monitor).
        self.setGeometry(QGuiApplication.primaryScreen().virtualGeometry())

        self._origin = None
        self._rect = QRect()

    def mousePressEvent(self, e):
        self._origin = e.position().toPoint()
        self._rect = QRect(self._origin, self._origin)
        self.update()

    def mouseMoveEvent(self, e):
        if self._origin is not None:
            self._rect = QRect(self._origin, e.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if self._origin is None:
            return
        r = self._rect.normalized()
        g = self.geometry()   # widget-local -> global (LOGICAL) screen coords

        # The screen grabber (mss) works in PHYSICAL pixels, but Qt reports
        # logical pixels. On a scaled display (125%/150%/...) these differ, so
        # convert with the device pixel ratio or the capture is the wrong size
        # and offset (only grabbing "part" of the selection).
        dpr = self.devicePixelRatioF()
        left = round((g.left() + r.left()) * dpr)
        top = round((g.top() + r.top()) * dpr)
        width = round(r.width() * dpr)
        height = round(r.height() * dpr)

        if width > 12 and height > 12:
            self.region_selected.emit([left, top, width, height])
        else:
            self.region_selected.emit([])
        self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.region_selected.emit([])
            self.close()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if not self._rect.isNull():
            # Punch a clear hole so the user sees the actual board underneath.
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(self._rect, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(QColor(0, 200, 120), 2))
            p.drawRect(self._rect)
        p.end()
