"""Screen capture and board-to-grid sampling.

Design note
-----------
The vision layer is deliberately **engine-agnostic**. It does NOT try to
classify individual pieces (which is brittle across the countless board
themes on chess.com, Lichess, desktop GUIs, streaming overlays, DGT boards,
etc.). Trying to recognise "this glyph is a black knight" would tie the app
to specific piece sets.

Instead it:
  1. captures the board region,
  2. slices it into an 8x8 grid,
  3. builds a compact grayscale "fingerprint" for each square, and
  4. reports which squares changed between two frames.

`board_state.match_move()` then matches the *set of changed squares* against
the **legal moves** reported by python-chess. Using the rules of chess as a
constraint makes move detection robust on essentially any 2D board rendering,
regardless of platform or piece theme.
"""
from __future__ import annotations

import numpy as np
import cv2
import mss

# Each square is resampled to this many pixels per side before diffing.
CELL_SAMPLE = 32
# Fraction of each square kept when sampling (centre crop). Dropping the
# outer ~14% avoids square borders, move-highlight rings and rank/file labels.
CENTER_CROP = 0.72


class ScreenCapturer:
    """Thin wrapper around mss for grabbing a fixed screen rectangle.

    NOTE: mss objects must be created and used on the *same* thread, so this
    is instantiated inside the analysis worker thread, never on the UI thread.
    """

    def __init__(self):
        self._sct = mss.mss()

    def grab(self, region) -> np.ndarray:
        """region = [left, top, width, height] -> BGR numpy image."""
        left, top, width, height = region
        monitor = {"left": int(left), "top": int(top),
                   "width": int(width), "height": int(height)}
        raw = self._sct.grab(monitor)
        # mss returns BGRA; drop alpha, keep BGR for OpenCV.
        img = np.asarray(raw)[:, :, :3]
        return np.ascontiguousarray(img)

    def grab_full(self):
        """Grab the entire virtual desktop (all monitors).

        Returns (BGR image, (left, top) offset) so detections in image
        coordinates can be mapped back to global screen coordinates.
        """
        mon = self._sct.monitors[0]     # index 0 = full virtual screen
        raw = self._sct.grab(mon)
        img = np.asarray(raw)[:, :, :3]
        return np.ascontiguousarray(img), (int(mon["left"]), int(mon["top"]))

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


class BoardVision:
    """Converts a captured board image into per-square fingerprints and
    computes which squares changed between two captures."""

    def __init__(self, orientation: str = "white"):
        self.orientation = orientation

    def set_orientation(self, orientation: str):
        self.orientation = orientation

    # ------------------------------------------------------------------ #
    def board_to_grid(self, img: np.ndarray) -> np.ndarray:
        """Slice a board image into an 8x8 grid of grayscale fingerprints.

        Returns an array of shape (8, 8, CELL_SAMPLE, CELL_SAMPLE) indexed
        [row][col], where row 0 is the TOP of the captured image and col 0
        is the LEFT (i.e. raw image orientation, before any board flip).
        """
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cells = np.zeros((8, 8, CELL_SAMPLE, CELL_SAMPLE), dtype=np.float32)
        for r in range(8):
            for c in range(8):
                y0, y1 = int(round(r * h / 8)), int(round((r + 1) * h / 8))
                x0, x1 = int(round(c * w / 8)), int(round((c + 1) * w / 8))
                cell = gray[y0:y1, x0:x1]
                ch, cw = cell.shape[:2]
                my = int(ch * (1 - CENTER_CROP) / 2)
                mx = int(cw * (1 - CENTER_CROP) / 2)
                if ch > 2 * my and cw > 2 * mx:
                    cell = cell[my:ch - my, mx:cw - mx]
                if cell.size == 0:
                    continue
                cell = cv2.resize(cell, (CELL_SAMPLE, CELL_SAMPLE))
                cells[r, c] = cell.astype(np.float32)
        return cells

    # ------------------------------------------------------------------ #
    @staticmethod
    def cell_diff(a: np.ndarray, b: np.ndarray) -> float:
        """Normalised (0..1) mean absolute difference between two cells."""
        return float(np.mean(np.abs(a - b)) / 255.0)

    def changed_squares(self, ref_grid, cur_grid, threshold) -> dict:
        """Return {square_index: change_score} for squares whose fingerprint
        changed by more than `threshold`. square_index is in python-chess
        numbering (a1=0 ... h8=63)."""
        changed = {}
        for r in range(8):
            for c in range(8):
                score = self.cell_diff(ref_grid[r, c], cur_grid[r, c])
                if score > threshold:
                    changed[self.grid_to_square(r, c)] = score
        return changed

    def grid_to_square(self, row: int, col: int) -> int:
        """Map image grid (row from top, col from left) to a python-chess
        square index (0..63, a1=0), honouring board orientation."""
        if self.orientation == "white":
            # White at bottom: top row is rank 8, left column is file a.
            file_, rank = col, 7 - row
        else:
            # Black at bottom: board rotated 180 degrees.
            file_, rank = 7 - col, row
        return rank * 8 + file_

    @staticmethod
    def grid_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Overall similarity (0..1, 1 = identical) between two full grids."""
        return 1.0 - float(np.mean(np.abs(a - b)) / 255.0)


def detect_orientation_from_start(grid: np.ndarray) -> str:
    """Guess which colour is at the bottom from a *starting-position* grid.

    At the start both armies fill their two rows, so occupancy is symmetric -
    but White pieces are near-white and Black pieces near-black in virtually
    every theme. Whichever of the top/bottom two image rows is brighter is the
    White army, so 'bottom brighter' => White is at the bottom.

    `grid` is the (8, 8, N, N) fingerprint array with row 0 at the top.
    """
    top = float(np.mean(grid[0:2]))     # top two image rows (one army)
    bottom = float(np.mean(grid[6:8]))  # bottom two image rows (other army)
    return "white" if bottom >= top else "black"
