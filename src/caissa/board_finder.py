"""Automatic on-screen chessboard finder.

Architecture note
-----------------
This module only *proposes* candidate board regions - it never decides alone.
The analysis worker validates each candidate with the actual board reader
("is this a fresh game?" / "does this read as a legal position with high
confidence?"), so a wrong candidate is simply skipped. That validation gating
is what makes full-auto detection safe: the finder can afford to be greedy.

Pipeline per screenshot:
 1. coarse multi-scale checkerboard scoring on a downscaled image (vectorised
    with integral images) -> best few non-overlapping candidates;
 2. per candidate, a fine multi-scale pass on a small full-resolution window;
 3. a grid-line comb refinement that aligns the region to the lines BETWEEN
    squares (piece-robust - grid lines stay visible whatever sits on squares).
"""
from __future__ import annotations

import numpy as np
import cv2

DETECT_WIDTH = 480      # downscale width for the coarse search
MIN_DIFF = 12.0         # minimum light/dark brightness gap (0..255)

_PARITY = (np.add.outer(np.arange(8), np.arange(8)) % 2)


# --------------------------------------------------------------------------- #
# Checkerboard colour scoring (coarse localisation)
# --------------------------------------------------------------------------- #
def _block_mean_image(integ, p, H, W):
    """Mean of every p x p block, top-left indexed. Shape (H-p+1, W-p+1)."""
    S = integ
    return (S[0:H - p + 1, 0:W - p + 1] + S[p:H + 1, p:W + 1]
            - S[0:H - p + 1, p:W + 1] - S[p:H + 1, 0:W - p + 1]) / (p * p)


def _edge_offsets(s):
    """8 background sample patches per cell (4 edge-midpoints + 4 inset
    corners) plus the patch size. The median over 8 samples estimates the
    square's background colour whether pieces are sparse or bulky."""
    p = max(2, s // 7)
    d = max(1, int(round(s * 0.13)))
    c = (s - p) // 2
    e = s - d - p
    return [(d, c), (e, c), (c, d), (c, e),
            (d, d), (d, e), (e, d), (e, e)], p


def _score_map(integ, H, W, s):
    """Vectorised checkerboard score for every board origin at cell size s."""
    board = 8 * s
    oy, ox = H - board + 1, W - board + 1
    if oy <= 0 or ox <= 0:
        return None
    offsets, p = _edge_offsets(s)
    bmp = _block_mean_image(integ, p, H, W)

    inv = 1.0 / len(offsets)
    sum_e = np.zeros((oy, ox)); sq_e = np.zeros((oy, ox))
    sum_o = np.zeros((oy, ox)); sq_o = np.zeros((oy, ox))
    for i in range(8):
        for j in range(8):
            bg = None
            for ro, co in offsets:
                patch = bmp[i * s + ro:i * s + ro + oy, j * s + co:j * s + co + ox]
                bg = patch.copy() if bg is None else bg + patch
            bg *= inv
            if (i + j) % 2 == 0:
                sum_e += bg; sq_e += bg * bg
            else:
                sum_o += bg; sq_o += bg * bg

    me, mo = sum_e / 32.0, sum_o / 32.0
    ve = np.maximum(sq_e / 32.0 - me * me, 0.0)
    vo = np.maximum(sq_o / 32.0 - mo * mo, 0.0)
    diff = np.abs(me - mo)
    within = 0.5 * (np.sqrt(ve) + np.sqrt(vo))
    score = diff / (within + 6.0)
    score[diff < MIN_DIFF] = 0.0
    return score


# --------------------------------------------------------------------------- #
# Grid-line comb refinement (pixel-precise, piece-robust)
# --------------------------------------------------------------------------- #
def _best_comb(profile, approx_start, approx_cell):
    """Fit a comb of 9 equally-spaced lines (the 8x8 grid boundaries) to a 1-D
    gradient profile. Full-span grid lines dominate the summed gradient, so
    this is robust to pieces sitting on the squares."""
    n = len(profile)
    best = (-1.0, float(approx_start), float(approx_cell))
    cell = approx_cell * 0.90
    while cell <= approx_cell * 1.10:
        lo = int(approx_start - approx_cell * 0.5)
        hi = int(approx_start + approx_cell * 0.5)
        for start in range(max(0, lo), hi + 1):
            resp = 0.0
            ok = True
            for k in range(9):
                t = int(round(start + k * cell))
                if t < 1 or t >= n - 1:
                    ok = False
                    break
                resp += max(profile[t - 1], profile[t], profile[t + 1])
            if ok and resp > best[0]:
                best = (resp, float(start), float(cell))
        cell += 0.5
    return best[1], best[2]


def _snap_axis(profile, start, cell):
    """Sub-pixel snap: move each of the 9 expected grid lines to its nearest
    gradient peak (with parabolic interpolation), then least-squares fit
    position = start + k*cell over the snapped peaks."""
    n = len(profile)
    ys = []
    for k in range(9):
        t0 = int(round(start + k * cell))
        lo, hi = max(1, t0 - 3), min(n - 2, t0 + 3)
        if hi <= lo:
            ys.append(start + k * cell)
            continue
        p = lo + int(np.argmax(profile[lo:hi + 1]))
        denom = profile[p - 1] - 2.0 * profile[p] + profile[p + 1]
        delta = 0.0
        if abs(denom) > 1e-9:
            delta = 0.5 * (profile[p - 1] - profile[p + 1]) / denom
            delta = max(-1.0, min(1.0, delta))
        ys.append(p + delta)
    A = np.vstack([np.ones(9), np.arange(9)]).T
    (s, c), *_ = np.linalg.lstsq(A, np.asarray(ys), rcond=None)
    return float(s), float(c)


def _grid_refine(gray, x, y, board):
    """Align the region precisely to the board's grid lines.

    Returns (x, y, w, h) - width and height fitted independently so a slight
    aspect difference in the rendering doesn't skew the cells.
    """
    H, W = gray.shape
    m = max(6, board // 8)
    x0, x1 = max(0, x - m), min(W, x + board + m)
    y0, y1 = max(0, y - m), min(H, y + board + m)
    sub = gray[y0:y1, x0:x1].astype(np.float32)
    if sub.shape[0] < 24 or sub.shape[1] < 24:
        return x, y, board, board

    col = np.abs(cv2.Sobel(sub, cv2.CV_32F, 1, 0, ksize=3)).sum(axis=0)
    row = np.abs(cv2.Sobel(sub, cv2.CV_32F, 0, 1, ksize=3)).sum(axis=1)

    cell0 = board / 8.0
    sx, cx = _best_comb(col, x - x0, cell0)     # robust localisation
    sy, cy = _best_comb(row, y - y0, cell0)
    sx, cx = _snap_axis(col, sx, cx)            # sub-pixel snap
    sy, cy = _snap_axis(row, sy, cy)

    w = int(round(8 * cx))
    h = int(round(8 * cy))
    nx = int(round(x0 + sx))
    ny = int(round(y0 + sy))
    nx = max(0, min(nx, W - w))
    ny = max(0, min(ny, H - h))
    return nx, ny, w, h


# --------------------------------------------------------------------------- #
# Candidate proposal
# --------------------------------------------------------------------------- #
def overlap_frac(a, b):
    """Fraction of region a's area covered by region b ([x, y, w, h] each)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    if aw <= 0 or ah <= 0:
        return 0.0
    return (ix * iy) / float(aw * ah)


def find_candidates(bgr, max_candidates=3):
    """Propose up to `max_candidates` board-like regions in a BGR screenshot.

    Returns a list of ([x, y, w, h], score), best first, in the screenshot's
    own pixel coordinates. Callers MUST validate each candidate (fresh-game
    check / template read) before trusting it.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    scale = DETECT_WIDTH / float(W)
    small = cv2.resize(gray, (DETECT_WIDTH, max(1, int(round(H * scale)))))
    sH, sW = small.shape
    integ = cv2.integral(small.astype(np.float64))

    min_b = max(56, int(min(sH, sW) * 0.18))
    max_b = int(min(sH, sW) * 0.99)
    s_lo = max(6, min_b // 8)
    s_hi = max(s_lo, max_b // 8)

    hits = []
    for s in sorted(set(int(round(v)) for v in np.geomspace(s_lo, s_hi, 14))):
        sm = _score_map(integ, sH, sW, s)
        if sm is None:
            continue
        idx = int(np.argmax(sm))
        sc = float(sm.flat[idx])
        if sc <= 0.0:
            continue
        y, x = np.unravel_index(idx, sm.shape)
        hits.append((sc, int(x), int(y), 8 * s))
    hits.sort(key=lambda h: -h[0])

    # Keep the strongest non-overlapping few.
    picked = []
    for sc, x, y, b in hits:
        dup = False
        for _, (px, py, pb) in picked:
            ix = max(0, min(x + b, px + pb) - max(x, px))
            iy = max(0, min(y + b, py + pb) - max(y, py))
            if ix * iy > 0.4 * min(b * b, pb * pb):
                dup = True
                break
        if not dup:
            picked.append((sc, (x, y, b)))
        if len(picked) >= max_candidates:
            break

    # Map to full resolution and snap to the grid lines (fast, sub-pixel).
    results = []
    for sc, (x, y, b) in picked:
        xf, yf, bf = int(x / scale), int(y / scale), int(b / scale)
        gx, gy, gw, gh = _grid_refine(gray, xf, yf, bf)
        if (gw >= 64 and gh >= 64 and 0.8 < gw / max(gh, 1) < 1.25
                and gx >= 0 and gy >= 0 and gx + gw <= W and gy + gh <= H):
            results.append(([gx, gy, gw, gh], sc))
    return results
