#!/usr/bin/env python3
"""Genera le icone della PWA tastiera (icon-keyboard-*.png).

Parte da icon-192.png / icon-512.png e sovrappone al glifo blu
centrale una tastiera bianca con i tasti in blu, così le due PWA
restano riconoscibili a colpo d'occhio ma condividono lo stesso
stile dell'icona originale.

Uso (serve Pillow, non e' nel requirements del server — e' un tool
di sviluppo, le icone sono gia' generate e commitgate in web/):

    python3 tools/gen_keyboard_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parent.parent / "web"
SIZES = (192, 512)

# Stessi colori dell'icona originale
WHITE = (255, 255, 255, 255)
KEY_BLUE = (47, 127, 190, 255)


def build(size: int) -> Path:
    base = Image.open(WEB / f"icon-{size}.png").convert("RGBA")
    draw = ImageDraw.Draw(base)

    # Geometria espressa a 512px e scalata alla dimensione richiesta
    s = size / 512
    cx = cy = size / 2

    body_w, body_h = 100 * s, 64 * s
    x0, y0 = cx - body_w / 2, cy - body_h / 2
    x1, y1 = x0 + body_w, y0 + body_h

    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=10 * s,
        fill=WHITE,
    )

    margin = 9 * s
    inner_w = body_w - 2 * margin
    inner_h = body_h - 2 * margin
    ix, iy = x0 + margin, y0 + margin

    cols, rows = 5, 4
    gap = 4 * s
    key_w = (inner_w - gap * (cols - 1)) / cols
    key_h = (inner_h - gap * (rows - 1)) / rows
    key_r = max(1, 2 * s)

    for r in range(rows):
        ky = iy + r * (key_h + gap)
        if r == rows - 1:
            # ultima riga: un solo spacebar al centro
            bar_w = key_w * 3 + gap * 2
            bx = ix + (inner_w - bar_w) / 2
            draw.rounded_rectangle(
                [bx, ky, bx + bar_w, ky + key_h],
                radius=key_r,
                fill=KEY_BLUE,
            )
            continue
        for c in range(cols):
            kx = ix + c * (key_w + gap)
            draw.rounded_rectangle(
                [kx, ky, kx + key_w, ky + key_h],
                radius=key_r,
                fill=KEY_BLUE,
            )

    out = WEB / f"icon-keyboard-{size}.png"
    base.save(out, optimize=True)
    return out


def main() -> None:
    for size in SIZES:
        path = build(size)
        print(f"generata {path}")


if __name__ == "__main__":
    main()
