"""Generates the original Chess Overlay app icon (procedural, not traced art).

Design: a dark rounded badge (matching the app's own dashboard colours), a
bold knight-head silhouette, and a small glowing accent "eye" (nods to the
computer-vision detection at the app's core). Kept deliberately simple - no
fine reticle/bar details - so it still reads clearly at 16x16 taskbar size.

Run:  python assets/generate_icon.py
Produces: assets/icon.png (1024, window/taskbar icon) and assets/icon.ico
(multi-resolution, used as the .exe file icon).
"""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

BG_TOP = (21, 24, 34, 255)      # #151822
BG_BOTTOM = (31, 36, 51, 255)   # #1f2433
BORDER = (255, 255, 255, 30)
KNIGHT = (245, 246, 250, 255)   # near-white
KNIGHT_OUTLINE = (12, 13, 18, 255)
ACCENT_BLUE = (58, 122, 254)    # #3a7afe
ACCENT_GREEN = (111, 208, 160)  # #6fd0a0


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def vertical_gradient(size, top, bottom):
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    for y in range(size):
        t = y / (size - 1)
        col = [int(top[i] + (bottom[i] - top[i]) * t) for i in range(4)]
        arr[y, :, :] = col
    return Image.fromarray(arr, "RGBA")


def _bezier(p0, p1, p2, p3, n=16):
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


# Hand-designed knight head+neck profile (local 0-200 x, 0-200 y box, nose to
# the left). A short list of anchor points (straight edges) with a few
# targeted bezier curves for the forehead/muzzle, so proportions stay
# predictable: distinct snout, small ear, jagged mane down an arched neck.
def knight_polygon(scale, ox, oy):
    pts = [(176, 208), (66, 208)]                       # base: chest -> back of neck
    pts += _bezier((66, 208), (56, 196), (50, 188), (46, 180))   # chest -> throat
    pts += _bezier((46, 180), (38, 178), (32, 175), (28, 172))   # throat -> chin (no dip)
    pts += _bezier((28, 172), (14, 166), (4, 158), (2, 148))     # chin -> rounded nose tip
    pts += _bezier((2, 148), (8, 136), (16, 130), (24, 128))     # nose tip -> upper lip
    pts.append((17, 120))                                # mouth corner notch (subtle)
    pts += _bezier((17, 120), (14, 105), (10, 92), (12, 78))     # forehead bulges outward/left
    pts += _bezier((12, 78), (16, 60), (34, 54), (50, 48))       # sweeps back to the poll
    pts += _bezier((50, 48), (50, 34), (52, 20), (54, 12))       # near-vertical rise to the ear
    pts += [
        (68, 42),                                            # back of ear
        (78, 50),                                             # crest / mane start
        (94, 60), (82, 76),                                    # mane zigzag 1
        (102, 88), (88, 102),                                  # mane zigzag 2
        (108, 112), (96, 126),                                 # mane zigzag 3
    ]
    pts += _bezier((96, 126), (120, 140), (152, 160), (176, 208))  # neck sweep to base
    return [(ox + x * scale, oy + y * scale) for x, y in pts]


def _fit_and_center(raw_pts, canvas_size, fill_frac):
    """Uniformly scale raw_pts (any coordinate space) so its longest side is
    `fill_frac` of canvas_size, then centre it. Removes manual guesswork."""
    xs = [p[0] for p in raw_pts]
    ys = [p[1] for p in raw_pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = maxx - minx, maxy - miny
    scale = (canvas_size * fill_frac) / max(w, h)
    px_w, px_h = w * scale, h * scale
    ox = (canvas_size - px_w) / 2 - minx * scale
    oy = (canvas_size - px_h) / 2 - miny * scale
    return [(ox + x * scale, oy + y * scale) for x, y in raw_pts], scale, ox, oy


def build():
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # --- background badge -------------------------------------------------
    bg = vertical_gradient(SIZE, BG_TOP, BG_BOTTOM)
    mask = rounded_mask(SIZE, radius=int(SIZE * 0.22))
    canvas.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        [4, 4, SIZE - 5, SIZE - 5], radius=int(SIZE * 0.22),
        outline=BORDER, width=4)

    # --- knight silhouette, auto-fit to fill most of the badge --------------
    raw = knight_polygon(1.0, 0.0, 0.0)
    poly, scale, ox, oy = _fit_and_center(raw, SIZE, fill_frac=0.72)

    # soft drop shadow for depth
    shadow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    shadow_poly = [(x + SIZE * 0.012, y + SIZE * 0.018) for x, y in poly]
    sd.polygon(shadow_poly, fill=(0, 0, 0, 130))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(SIZE * 0.012))
    canvas.alpha_composite(shadow_layer)

    draw = ImageDraw.Draw(canvas)
    draw.polygon(poly, fill=KNIGHT, outline=KNIGHT_OUTLINE, width=max(3, int(SIZE * 0.005)))

    # --- glowing "eye" (computer-vision accent) -----------------------------
    eye_cx, eye_cy = ox + 24 * scale, oy + 100 * scale
    eye_r = SIZE * 0.024
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([eye_cx - eye_r * 2.4, eye_cy - eye_r * 2.4,
               eye_cx + eye_r * 2.4, eye_cy + eye_r * 2.4],
              fill=(*ACCENT_BLUE[:3], 170))
    glow = glow.filter(ImageFilter.GaussianBlur(SIZE * 0.016))
    canvas.alpha_composite(glow)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse([eye_cx - eye_r, eye_cy - eye_r, eye_cx + eye_r, eye_cy + eye_r],
                fill=ACCENT_BLUE)
    draw.ellipse([eye_cx - eye_r * 0.42, eye_cy - eye_r * 0.42,
                 eye_cx + eye_r * 0.42, eye_cy + eye_r * 0.42],
                fill=(255, 255, 255, 235))

    return canvas


def main():
    icon = build()
    png_path = os.path.join(OUT_DIR, "icon.png")
    icon.save(png_path)

    ico_path = os.path.join(OUT_DIR, "icon.ico")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save(ico_path, format="ICO", sizes=sizes)

    print(f"Wrote {png_path}")
    print(f"Wrote {ico_path}")


if __name__ == "__main__":
    main()
