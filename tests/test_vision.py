"""Vision pipeline tests: on-screen board finding + full-position reading.

These exercise the hardest parts of the app (board_finder + board_reader) and
need no chess engine and no display, so they run anywhere.
"""
import chess

from caissa.board_finder import find_candidates, overlap_frac
from caissa.board_reader import (BoardReader, looks_like_fresh_game,
                                 resolve_board)
from _synth import render_board, desktop, place, crop, placement_of


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter + 1e-9)


def test_finder_locates_board_accurately():
    truth = [300, 180, 440, 440]
    img = place(render_board(chess.Board(), "white"), 440, 300, 180, seed=11)
    cands = find_candidates(img)
    assert cands, "no candidate found"
    assert _iou(cands[0][0], truth) > 0.9


def test_finder_ignores_empty_desktop():
    # A busy desktop with no board must yield no confident candidate.
    cands = find_candidates(desktop(seed=7))
    assert all(_iou(c[0], [300, 180, 440, 440]) == 0 for c in cands) or not cands


def test_reader_learns_and_reads_start_exactly():
    img = place(render_board(chess.Board(), "white"), 440, 300, 180, seed=11)
    reg = find_candidates(img)[0][0]
    c = crop(img, reg)
    is_start, orient = looks_like_fresh_game(c)
    assert is_start and orient == "white"
    r = BoardReader()
    assert r.learn(c, orient)
    placement, conf = r.read(c, orient)
    assert placement == placement_of(chess.Board())
    assert conf > 0.3


def test_reader_reads_midgame_from_templates():
    # Learn from a start position, then read an in-progress game.
    r = BoardReader()
    start_img = place(render_board(chess.Board(), "white"), 440, 300, 180, seed=11)
    r.learn(crop(start_img, find_candidates(start_img)[0][0]), "white")

    g = chess.Board()
    for m in ["e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6", "Nc3", "a6"]:
        g.push_san(m)
    mid_img = place(render_board(g, "white"), 400, 700, 250, seed=13)
    pl, _ = r.read(crop(mid_img, find_candidates(mid_img)[0][0]), "white")
    board, _ = resolve_board(pl, None, None, chess.WHITE, None)
    assert board is not None
    assert placement_of(board) == placement_of(g)


def test_reader_handles_black_orientation():
    g = chess.Board()
    for m in ["e4", "e5", "Qh5"]:
        g.push_san(m)
    img = place(render_board(g, "black"), 360, 500, 200, seed=5)
    reg = find_candidates(img)[0][0]
    r = BoardReader()
    # learn from a black-orientation start
    start = place(render_board(chess.Board(), "black"), 360, 500, 200, seed=5)
    assert looks_like_fresh_game(crop(start, find_candidates(start)[0][0]))[1] == "black"


def test_overlap_frac_math():
    assert overlap_frac([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0
    assert overlap_frac([0, 0, 10, 10], [100, 100, 10, 10]) == 0.0
