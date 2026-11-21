"""Draws screentime/assets/screentime.ico: the three glowing bars on a dark rounded tile.

Every size is drawn on its own grid instead of shrinking one big picture, so the bar edges
land on whole pixels and the gaps between bars stay sharp. Scaling one image down to, say,
30 px (the taskbar at 125%) smears them. Needs Pillow:  python packaging\\make_icon.py
"""
import os

from PIL import Image, ImageDraw

# Windows asks for these: 16-48 for the window, tray and taskbar at 100-200% scaling,
# the rest for Explorer's views. See "Construct icons" in the Windows app design guidelines.
SIZES = [16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 128, 256]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screentime", "assets", "screentime.ico")

# The design on a 256 grid
TILE = (14, 2, 69)
TILE_RADIUS = 51
BAR_WIDTH, BAR_GAP = 50, 12.5
BAR_BOTTOM = 202
BAR_TOPS = (129, 95, 54)  # left to right
BAR_RADIUS = 10
# The bars' fill by height on the 256 grid: lime at the top, cyan at the bottom
GRADIENT = [(54, (197, 252, 95)), (92, (138, 252, 127)), (128, (60, 252, 176)),
            (164, (5, 247, 209)), (202, (1, 222, 229))]

SS = 16  # supersampling: draw at 16x, then average down, so only curves get soft edges


def gradient(y):
    """The fill color at height y on the 256 grid."""
    if y <= GRADIENT[0][0]:
        return GRADIENT[0][1]
    for (y0, c0), (y1, c1) in zip(GRADIENT, GRADIENT[1:]):
        if y <= y1:
            t = (y - y0) / (y1 - y0)
            return tuple(round(a + (b - a) * t) for a, b in zip(c0, c1))
    return GRADIENT[-1][1]


def bars(n):
    """The bars as (left, top, right, bottom) in whole pixels of an n x n icon."""
    s = n / 256
    width = max(2, round(BAR_WIDTH * s))
    gap = max(1, round(BAR_GAP * s))
    left = (n - (3 * width + 2 * gap)) // 2
    bottom = round(BAR_BOTTOM * s)
    out = []
    for i, top in enumerate(BAR_TOPS):
        x = left + i * (width + gap)
        out.append((x, min(round(top * s), bottom - 2), x + width, bottom))
    return out


def draw(n):
    big = n * SS
    s = n / 256

    tile = Image.new("L", (big, big), 0)
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, big - 1, big - 1), radius=TILE_RADIUS * s * SS, fill=255)

    mask = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(mask)
    radius = BAR_RADIUS * s * SS
    for x0, y0, x1, y1 in bars(n):
        d.rounded_rectangle((x0 * SS, y0 * SS, x1 * SS - 1, y1 * SS - 1), radius=radius, fill=255)

    # One color per output row, so every bar gets the same shade at the same height
    fill = Image.new("RGB", (1, big))
    for y in range(big):
        fill.putpixel((0, y), gradient((y + 0.5) / SS / s))
    fill = fill.resize((big, big), Image.NEAREST)

    img = Image.composite(fill, Image.new("RGB", (big, big), TILE), mask).convert("RGBA")
    img.putalpha(tile)
    return img.resize((n, n), Image.BOX)


def main():
    frames = [draw(n) for n in SIZES]
    largest = frames[-1]
    # Pillow writes the given frames as they are when they're passed via append_images
    largest.save(OUT, format="ICO", sizes=[(n, n) for n in SIZES],
                 append_images=frames[:-1])
    print("wrote", os.path.normpath(OUT), "with sizes", SIZES)


if __name__ == "__main__":
    main()
