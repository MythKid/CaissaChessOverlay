"""Synthetic 'desktop + chess board' renderer used by the vision tests.

Deliberately engine-free and GUI-free (only numpy/opencv/python-chess), so the
whole test suite runs headless in CI without Stockfish or a display. Filename
starts with an underscore so pytest does not collect it as a test module.
"""
import random

import numpy as np
import cv2
import chess

from caissa.board_reader import grid_to_square, square_is_light

CELL = 72
LETT = {"p": "P", "n": "N", "b": "B", "r": "R", "q": "Q", "k": "K"}


def render_board(board: chess.Board, orient: str = "white") -> np.ndarray:
    """Draw a chess.com-like board with outlined glyph 'pieces'."""
    img = np.zeros((CELL * 8, CELL * 8, 3), np.uint8)
    for row in range(8):
        for col in range(8):
            sq = grid_to_square(row, col, orient)
            light = square_is_light(sq)
            y0, x0 = row * CELL, col * CELL
            img[y0:y0 + CELL, x0:x0 + CELL] = (208, 236, 235) if light else (82, 149, 115)
            pc = board.piece_at(sq)
            if pc:
                white = pc.symbol().isupper()
                fg = (249, 249, 249) if white else (25, 22, 20)
                out = (30, 30, 34) if white else (240, 240, 240)
                org = (x0 + int(CELL * 0.24), y0 + int(CELL * 0.72))
                cv2.putText(img, LETT[pc.symbol().lower()], org,
                            cv2.FONT_HERSHEY_SIMPLEX, CELL * 0.032, out, 11, cv2.LINE_AA)
                cv2.putText(img, LETT[pc.symbol().lower()], org,
                            cv2.FONT_HERSHEY_SIMPLEX, CELL * 0.032, fg, 4, cv2.LINE_AA)
    return img


def desktop(w: int = 1600, h: int = 900, seed: int | None = None) -> np.ndarray:
    """A busy, non-board 'desktop' background."""
    rng = random.Random(seed)
    bg = np.zeros((h, w, 3), np.uint8)
    for y in range(h):
        bg[y, :] = (38 + y * 26 // h, 42 + y * 20 // h, 50)
    for _ in range(35):
        x0, y0 = rng.randint(0, w - 1), rng.randint(0, h - 1)
        x1 = min(w, x0 + rng.randint(30, 380))
        y1 = min(h, y0 + rng.randint(12, 70))
        cv2.rectangle(bg, (x0, y0), (x1, y1),
                      tuple(rng.randint(25, 215) for _ in range(3)), -1)
    return bg


def place(board_img: np.ndarray, size: int, x: int, y: int, seed=1) -> np.ndarray:
    bg = desktop(seed=seed)
    bg[y:y + size, x:x + size] = cv2.resize(board_img, (size, size))
    return bg


def crop(img: np.ndarray, region) -> np.ndarray:
    x, y, w, h = region
    return img[y:y + h, x:x + w]


def placement_of(board: chess.Board) -> dict:
    return {sq: (board.piece_at(sq).symbol() if board.piece_at(sq) else None)
            for sq in chess.SQUARES}
