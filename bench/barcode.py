"""Frame-number barcode burned into every fixture (OCR-free timestamp verification).

Layout (in source pixels, any 16:9 resolution): the top 1/9 of the picture is an 18-cell strip.
Row A (top 1/18 of height): cell i is white iff bit i of the frame number N is 1.
Row B (next 1/18): the complement of row A (used as a validity check).
N is the source frame index (0-based, constant 24 fps), so presentation time = N / 24.
18 bits -> up to 262143 frames (182 min at 24 fps).

At 320x180 each cell is ~17.8 x 10 px, which survives bilinear downscale + JPEG q~5.
"""
from __future__ import annotations

BITS = 18
FPS = 24

# geq expression for a BITS x 2 gray canvas (scaled up with flags=neighbor afterwards)
GEQ_LUM = (
    "if(eq(Y\\,0)\\,1\\,0)*255*mod(floor(N/pow(2\\,X))\\,2)"
    "+if(eq(Y\\,1)\\,1\\,0)*255*(1-mod(floor(N/pow(2\\,X))\\,2))"
)


def barcode_source(width: int, height: int, fps: int = FPS) -> str:
    """lavfi graph producing the barcode strip sized for a WxH picture."""
    strip_h = height // 9
    return (
        f"color=black:s={BITS}x2:r={fps},format=gray,geq=lum='{GEQ_LUM}',"
        f"scale={width}:{strip_h}:flags=neighbor"
    )


def decode(img, box=None):
    """Decode the frame number from a PIL image.

    box = (x, y, w, h) of the active picture inside img (for letterboxed output).
    Returns (frame_number or None, min_contrast).
    """
    g = img.convert("L")
    if box is None:
        box = (0, 0, g.width, g.height)
    x0, y0, w, h = box
    px = g.load()
    n = 0
    min_contrast = 255
    for i in range(BITS):
        cx = x0 + (i + 0.5) * w / BITS
        vals = []
        for row in (0, 1):
            cy = y0 + (row * 2 + 1) * h / 36.0
            acc = 0
            cnt = 0
            for dx in (-2, 0, 2):
                for dy in (-1, 0, 1):
                    xx = min(max(int(cx + dx), 0), g.width - 1)
                    yy = min(max(int(cy + dy), 0), g.height - 1)
                    acc += px[xx, yy]
                    cnt += 1
            vals.append(acc / cnt)
        a, b = vals
        contrast = abs(a - b)
        min_contrast = min(min_contrast, contrast)
        if contrast < 50:
            return None, contrast
        if a > b:
            n |= 1 << i
    return n, min_contrast


def decode_file(path, box=None):
    from PIL import Image

    with Image.open(path) as im:
        return decode(im, box)
