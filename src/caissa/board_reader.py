"""Full-board reader: reconstructs the ENTIRE position from the screen on
every scan (no fragile incremental move-tracking).

Why this design
---------------
Tracking moves by diffing frames requires catching every move perfectly - one
miss and the internal board desyncs forever (can't join mid-game, can't
recover). Instead we recognise what piece sits on each of the 64 squares and
rebuild the position from scratch each scan. That self-corrects, joins games
in progress, and survives missed moves.

How piece recognition works without a trained model
---------------------------------------------------
You calibrate once on a *starting position* (which we know exactly). From that
one frame we learn, per piece type, what its sprite looks like - and by
compositing the sprite onto both light and dark empty squares we get a template
for every (piece, square-colour) combination. Templates are saved to disk, so
calibration is a one-time thing per board theme; afterwards any position on
that board can be read, even across app restarts.
"""
from __future__ import annotations

import os

import numpy as np
import cv2
import chess

SQ = 48            # each square is resampled to SQ x SQ for matching
TRIM = 0.10        # trim this fraction off each square edge (grid lines/labels)

# Piece placement of the standard starting position (square -> symbol/None).
_START_BOARD = chess.Board()
START_PLACEMENT = {
    sq: (_START_BOARD.piece_at(sq).symbol() if _START_BOARD.piece_at(sq) else None)
    for sq in chess.SQUARES
}


def grid_to_square(row: int, col: int, orientation: str) -> int:
    """Image grid (row from top, col from left) -> python-chess square."""
    if orientation == "white":
        file_, rank = col, 7 - row
    else:
        file_, rank = 7 - col, row
    return rank * 8 + file_


def square_is_light(sq: int) -> int:
    """1 if the square is light, 0 if dark (a1 is dark)."""
    return (chess.square_file(sq) + chess.square_rank(sq)) % 2


def detect_orientation(img) -> str:
    """Guess which colour is at the bottom from a starting-position image
    (White pieces are bright, Black dark; brighter bottom rows => White below)."""
    cells = BoardReader._cells(img)
    top = float(np.mean(cells[0:2]))
    bottom = float(np.mean(cells[6:8]))
    return "white" if bottom >= top else "black"


def looks_like_fresh_game(img):
    """Template-free check for the standard opening set-up and its orientation.

    A starting position has its two outer rows full of pieces and the four
    middle rows empty. Piece squares have busy interiors (high pixel variance);
    empty squares are smooth. Returns (is_start, orientation) — orientation is
    only meaningful when is_start is True. Used to auto-detect a new game and
    which colour the user is, with no manual calibration.
    """
    cells = BoardReader._cells(img)
    cstd = np.array([[np.std(cells[r, c]) for c in range(8)] for r in range(8)])
    if cstd.max() <= 0:
        return False, None

    # Use the (assumed-empty) middle rows as the "empty square" baseline; any
    # square clearly busier than that holds a piece. Keyed off the baseline
    # rather than the busiest piece, so low-contrast pieces (e.g. white pawns
    # on light squares, where only the outline is visible) still count.
    mid = cstd[2:6].flatten()
    thr = float(np.median(mid)) + max(3.5, 3.0 * float(np.std(mid)))
    busy = cstd > thr
    row_busy = busy.sum(axis=1)

    outer_full = all(row_busy[r] >= 6 for r in (0, 1, 6, 7))
    middle_empty = all(row_busy[r] <= 1 for r in (2, 3, 4, 5))
    if not (outer_full and middle_empty):
        return False, None

    brightness = [float(np.mean(cells[r])) for r in range(8)]
    top = (brightness[0] + brightness[1]) / 2.0
    bottom = (brightness[6] + brightness[7]) / 2.0
    return True, ("white" if bottom >= top else "black")


# --------------------------------------------------------------------------- #
class BoardReader:
    def __init__(self):
        self.empty = None      # {colour: SQxSQ float array}
        self._stacks = None    # {colour: (labels list, (K,SQ,SQ) array)}
        self.ready = False

    # ---- image -> per-square grayscale cells ------------------------------
    @staticmethod
    def _cells(img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        cells = np.zeros((8, 8, SQ, SQ), dtype=np.float32)
        for r in range(8):
            for c in range(8):
                y0, y1 = r * h / 8, (r + 1) * h / 8
                x0, x1 = c * w / 8, (c + 1) * w / 8
                ty, tx = (y1 - y0) * TRIM, (x1 - x0) * TRIM
                sub = gray[int(y0 + ty):int(y1 - ty), int(x0 + tx):int(x1 - tx)]
                if sub.size == 0:
                    continue
                cells[r, c] = cv2.resize(sub, (SQ, SQ)).astype(np.float32)
        return cells

    # ---- learn templates from a starting-position frame -------------------
    def learn(self, img, orientation) -> bool:
        cells = self._cells(img)
        empties = {0: [], 1: []}
        samples = {}   # symbol -> {colour: [cells]}

        for r in range(8):
            for c in range(8):
                sq = grid_to_square(r, c, orientation)
                colour = square_is_light(sq)
                sym = START_PLACEMENT[sq]
                if sym is None:
                    empties[colour].append(cells[r, c])
                else:
                    samples.setdefault(sym, {0: [], 1: []})[colour].append(cells[r, c])

        if len(empties[0]) == 0 or len(empties[1]) == 0 or len(samples) < 10:
            return False   # doesn't look like a start position

        empty = {col: np.mean(np.stack(v), axis=0) for col, v in empties.items()}

        # Prefer DIRECT samples per (piece, square-colour): at the start most
        # pieces appear on both colours (rooks/knights/bishops/pawns), so their
        # templates come straight from real pixels. Only the king/queen lack
        # one colour - composite those by masking the sprite onto the missing
        # colour's empty square.
        templates = {0: {}, 1: {}}
        for sym, by_col in samples.items():
            for tcol in (0, 1):
                if by_col[tcol]:
                    templates[tcol][sym] = np.mean(np.stack(by_col[tcol]), axis=0)
            for tcol in (0, 1):
                if sym not in templates[tcol]:
                    ocol = 1 - tcol
                    cell = templates[ocol][sym]
                    mask = np.abs(cell - empty[ocol]) > 22.0
                    templates[tcol][sym] = np.where(mask, cell, empty[tcol])

        self.empty = empty
        self._build_stacks(empty, templates)
        self.ready = True
        return True

    def validate_start(self, img, orientation):
        """Check the frame really is a fresh starting position AND the box is
        aligned to the 8x8 grid. Returns (ok, message)."""
        cells = self._cells(img)
        empties = {0: [], 1: []}
        should_empty, should_piece = [], []
        for r in range(8):
            for c in range(8):
                sq = grid_to_square(r, c, orientation)
                col = square_is_light(sq)
                cell = cells[r, c]
                if START_PLACEMENT[sq] is None:
                    empties[col].append(cell)
                    should_empty.append((cell, col))
                else:
                    should_piece.append((cell, col))

        if not empties[0] or not empties[1]:
            return False, "Couldn't read the 8x8 grid - is the box exactly on the board?"

        # Median empty square per colour (robust to a stray label/piece).
        empty = {col: np.median(np.stack(v), axis=0) for col, v in empties.items()}

        def mad(a, b):
            return float(np.mean(np.abs(a - b)))

        empty_bad = sum(1 for cell, col in should_empty if mad(cell, empty[col]) > 22)
        piece_bad = sum(1 for cell, col in should_piece if mad(cell, empty[col]) < 12)

        if empty_bad > 8:
            return False, ("This doesn't look like a fresh game - the middle of "
                           "the board should be empty. Start a new game, then "
                           "press Calibrate.")
        if piece_bad > 10:
            return False, ("I can't see all the starting pieces. Make sure the "
                           "box covers the WHOLE 8x8 board (use Debug Shot to "
                           "check), then Calibrate.")
        return True, "ok"

    def _build_stacks(self, empty, templates):
        self._stacks = {}
        for col in (0, 1):
            labels = [None]
            arrs = [empty[col]]
            for sym in sorted(templates[col]):
                labels.append(sym)
                arrs.append(templates[col][sym])
            self._stacks[col] = (labels, np.stack(arrs))

    # ---- read the current position ----------------------------------------
    def read(self, img, orientation):
        """Classify every square from scratch.

        Returns (placement, confidence) where placement is {square: symbol|None}
        and confidence is 0..1 (mean relative margin between the best and
        second-best template per square). Low confidence => this board doesn't
        match what was calibrated (wrong theme/size, or not calibrated).
        """
        cells = self._cells(img)
        placement = {}
        margins = []
        for r in range(8):
            for c in range(8):
                sq = grid_to_square(r, c, orientation)
                labels, stack = self._stacks[square_is_light(sq)]
                mad = np.mean(np.abs(stack - cells[r, c]), axis=(1, 2))
                order = np.argsort(mad)
                placement[sq] = labels[int(order[0])]
                best = mad[order[0]]
                second = mad[order[1]] if len(order) > 1 else best + 1.0
                margins.append((second - best) / (second + 1e-6))
        confidence = float(np.mean(margins)) if margins else 0.0
        return placement, confidence

    def read_placement(self, img, orientation) -> dict:
        """Convenience wrapper returning just the placement."""
        return self.read(img, orientation)[0]

    # ---- persistence (calibrate once, reuse forever) ----------------------
    def save(self, path):
        data = {"empty0": self.empty[0], "empty1": self.empty[1]}
        for col in (0, 1):
            labels, stack = self._stacks[col]
            for sym, arr in zip(labels[1:], stack[1:]):
                data[f"t{col}_{sym}"] = arr
        np.savez_compressed(path, **data)

    def load(self, path) -> bool:
        if not os.path.exists(path):
            return False
        try:
            data = np.load(path, allow_pickle=False)
            empty = {0: data["empty0"], 1: data["empty1"]}
            templates = {0: {}, 1: {}}
            for key in data.files:
                if key.startswith("t0_") or key.startswith("t1_"):
                    col = int(key[1])
                    sym = key[3:]
                    templates[col][sym] = data[key]
            if not templates[0] or not templates[1]:
                return False
            self.empty = empty
            self._build_stacks(empty, templates)
            self.ready = True
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# Position reconstruction and side-to-move resolution.
# --------------------------------------------------------------------------- #
def placement_to_board(placement: dict, turn: bool) -> chess.Board:
    board = chess.Board.empty()
    for sq, sym in placement.items():
        if sym:
            board.set_piece_at(sq, chess.Piece.from_symbol(sym))
    board.turn = turn

    rights = ""
    if board.piece_at(chess.E1) == chess.Piece.from_symbol("K"):
        if board.piece_at(chess.H1) == chess.Piece.from_symbol("R"):
            rights += "K"
        if board.piece_at(chess.A1) == chess.Piece.from_symbol("R"):
            rights += "Q"
    if board.piece_at(chess.E8) == chess.Piece.from_symbol("k"):
        if board.piece_at(chess.H8) == chess.Piece.from_symbol("r"):
            rights += "k"
        if board.piece_at(chess.A8) == chess.Piece.from_symbol("r"):
            rights += "q"
    board.set_castling_fen(rights or "-")
    return board


def _plausible(placement: dict, turn: bool):
    board = placement_to_board(placement, turn)
    return board if board.is_valid() else None


def infer_mover_turn(prev: dict, new: dict):
    """From a transition, return the side that is NOW to move, or None."""
    if not prev:
        return None
    movers = set()
    for sq in chess.SQUARES:
        n = new.get(sq)
        if n and n != prev.get(sq):
            movers.add(n.isupper())          # True => a white piece landed here
    if len(movers) == 1:
        white_moved = movers.pop()
        return chess.BLACK if white_moved else chess.WHITE
    return None


def resolve_board(placement, prev_placement, prev_turn, user_color, forced_turn):
    """Rebuild a valid board and decide whose move it is.

    Returns (board, turn) or (None, None) if the read isn't a legal position
    (e.g. a momentary misread) so the caller can simply wait for the next scan.
    """
    valid = [t for t in (chess.WHITE, chess.BLACK) if _plausible(placement, t)]
    if not valid:
        return None, None

    if forced_turn is not None and forced_turn in valid:
        turn = forced_turn
    elif len(valid) == 1:
        turn = valid[0]
    elif placement == START_PLACEMENT:
        turn = chess.WHITE
    else:
        inferred = infer_mover_turn(prev_placement, placement)
        if inferred in valid:
            turn = inferred
        elif prev_placement is not None and placement != prev_placement and prev_turn is not None:
            turn = not prev_turn            # a move happened - flip
        elif prev_turn is not None:
            turn = prev_turn
        else:
            turn = user_color if user_color in valid else valid[0]

    return placement_to_board(placement, turn), turn
