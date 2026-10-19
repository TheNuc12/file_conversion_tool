"""Draw the Rotary knob icon: packaging/rotary.ico (Windows) and packaging/rotary.png.

Run from the repo root: python packaging/make_icon.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).parent
SIZE = 1024
ACCENT = (232, 84, 40)


def radial(size: int, inner: tuple, outer: tuple, center: tuple[float, float]) -> Image.Image:
    """A radial gradient, used for the knob's lit face."""
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    cx, cy = center[0] * size, center[1] * size
    reach = size * 0.95
    for y in range(size):
        for x in range(size):
            t = min(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / reach, 1)
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(inner, outer))
    return grad


def draw() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    c = SIZE / 2

    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((70, 110, SIZE - 70, SIZE - 30), fill=(0, 0, 0, 110))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))

    d = ImageDraw.Draw(img)
    edge_r, face_r = 440, 360
    d.ellipse((c - edge_r, c - edge_r, c + edge_r, c + edge_r), fill=(62, 60, 57, 255))
    # Knurled grip around the edge.
    for i in range(48):
        a = i / 48 * 2 * math.pi
        x1, y1 = c + math.sin(a) * 375, c - math.cos(a) * 375
        x2, y2 = c + math.sin(a) * 432, c - math.cos(a) * 432
        d.line((x1, y1, x2, y2), fill=(38, 37, 35, 255), width=14)

    face = radial(SIZE, (238, 235, 229), (165, 160, 152), (0.4, 0.3))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).ellipse((c - face_r, c - face_r, c + face_r, c + face_r), fill=255)
    img.paste(face, (0, 0), mask)

    # The pointer, at twelve o'clock.
    d.rounded_rectangle((c - 50, c - 335, c + 50, c - 120), radius=50, fill=ACCENT + (255,))
    return img


def main() -> None:
    art = draw()
    art.resize((512, 512), Image.LANCZOS).save(HERE / "rotary.png")
    art.save(HERE / "rotary.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)])
    print("wrote", HERE / "rotary.ico", "and", HERE / "rotary.png")


if __name__ == "__main__":
    main()
