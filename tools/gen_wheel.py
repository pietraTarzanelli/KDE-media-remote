#!/usr/bin/env python3
"""Genera web/wheel.svg e sincronizza le hit zone in web/index.html.

Fonte unica di verita per la click wheel: l'SVG e le percentuali CSS dei
pulsanti invisibili vengono entrambe calcolate dalla stessa geometria, cosi'
non possono divergere (i 4 laterali erano spostati in basso per anni).

Geometria misurata dal Figma "ipod kde remote" (canvas 956x908, centro ruota
a 634,311) confrontando campioni radiali di thumbnail.png con il rendering.

Uso:
    python3 tools/gen_wheel.py            # scrive SVG + CSS
    python3 tools/gen_wheel.py --check    # verifica senza scrivere
"""
import argparse
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SVG_PATH = os.path.join(ROOT, "web", "wheel.svg")
HTML_PATH = os.path.join(ROOT, "web", "index.html")

VB = 140.0                 # semi-viewBox: 280 unita' di disegno
HALF = VB * 2              # 280

R_BODY = 134.0             # bordo esterno del disco
R_BTN = 38.0               # pulsante centrale "Click"
R_IN, R_IN_O = 46.0, 76.0  # anello interno visibile
R_GROOVE = 78.5            # solco divisorio
R_OUT_I, R_OUT_O = 80.0, 133.0   # anello esterno visibile
GAP_HALF = 30.0            # gap 60 gradi fra un arco e il successivo
ARC_HALF = 45.0 - GAP_HALF  # 15 gradi: arco visibile largo 30 gradi

# Hit zone: piu' ampie degli archi visibili per facilitare il tocco.
# Il box del pulsante resta quello attuale (centra l'icona); un ::before
# trasparente allarga l'area sensibile senza spostare nulla a livello visivo.
#
# Nota: nell'HTML attuale i gruppi sono invertiti rispetto ai vecchi nomi:
#   .outer -> 4 frecce piene, anello interno, inviano mouse_start
#   .inner -> 4 chevron, anello esterno, inviano key_start (frecce tastiera)
TAP_INNER = 38.0          # .outer: box invariato 76x33 unita'
R_INNER_IN = 46.0
R_INNER_OUT = 79.0

TAP_OUTER = 50.0          # .inner: box icona invariato 100x53 unita'
R_OUTER_IN = 80.0
R_OUTER_OUT = 133.0
TAP_OUTER_HIT = 80.0      # .inner: area sensibile 160x60 unita' (~166x62 px)
R_OUTER_HIT_IN = 80.0
R_OUTER_HIT_OUT = 140.0

BEGIN = "/* wheel hit zones: GENERATO DA tools/gen_wheel.py - non modificare a mano */"
END = "/* fine wheel hit zones */"


def fmt(v):
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def pt(r, deg):
    a = math.radians(deg)
    return (r * math.cos(a), r * math.sin(a))


def annulus(r1, r2, a1, a2):
    """Settore anulare da a1 a a2 gradi, senso orario, 0 = destra."""
    large = 1 if (a2 - a1) > 180 else 0
    x1, y1 = pt(r2, a1)
    x2, y2 = pt(r2, a2)
    x3, y3 = pt(r1, a2)
    x4, y4 = pt(r1, a1)
    return ("M%s %sA%s %s 0 %d 1 %s %sL%s %sA%s %s 0 %d 0 %s %sZ"
            % (fmt(x1), fmt(y1), fmt(r2), fmt(r2), large, fmt(x2), fmt(y2),
               fmt(x3), fmt(y3), fmt(r1), fmt(r1), large, fmt(x4), fmt(y4)))


def arcs(r1, r2):
    return [annulus(r1, r2, c - ARC_HALF, c + ARC_HALF)
            for c in (0.0, 90.0, 180.0, 270.0)]


# ── hit zone ────────────────────────────────────────────────────────────────
# Un box rettangolare per ciascuna direzione. La coordinate sono espresse in
# unita' di disegno con origine nel centro, poi tradotte in percentuale
# rispetto al viewBox. top/left di un laterale DEVE valere lo stesso numero di
# left/top della verticale corrispondente: e' la condizione di simmetria.
def boxes(r_in, r_out, tap):
    """{direzione: (x0, y0, w, h)} in unita' di disegno, origine al CENTRO.

    verticali: estensione radiale lungo Y, tangenziale 2*tap lungo X
    laterali:  estensione radiale lungo X, tangenziale 2*tap lungo Y
    """
    radial = r_out - r_in
    return {
        "up":    (-tap,   -r_out, 2 * tap, radial),
        "down":  (-tap,    r_in,  2 * tap, radial),
        "left":  (-r_out, -tap,  radial, 2 * tap),
        "right": ( r_in,  -tap,  radial, 2 * tap),
    }


def pct_box(b):
    """(x, y, w, h) nel frame centrato -> (left, top, width, height) in % del viewBox."""
    x, y, w, h = b
    return (100 * (x + VB) / HALF,
            100 * (y + VB) / HALF,
            100 * w / HALF,
            100 * h / HALF)


def pseudo_insets(base, hit):
    """Inset (left, right, top, bottom) del ::before, in % del box base.

    Valori negativi = il pseudo si estende oltre il box del pulsante.
    """
    x, y, w, h = base
    X, Y, W, H = hit
    return (100 * (X - x) / w,
            100 * ((x + w) - (X + W)) / w,
            100 * (Y - y) / h,
            100 * ((y + h) - (Y + H)) / h)


def check_symmetry(boxes_map, label):
    """Il centro di ogni box deve cadere sugli assi cardinali."""
    for d, (x, y, w, h) in boxes_map.items():
        cx, cy = x + w / 2, y + h / 2
        on_x = abs(cx) < 1e-6
        on_y = abs(cy) < 1e-6
        axis = ("X" if on_x else "") + ("Y" if on_y else "")
        if d in ("up", "down"):
            ok = on_x
            expect = "X"
        else:
            ok = on_y
            expect = "Y"
        if not ok:
            raise SystemExit(
                "SIMMETRIA ROTTA [%s/%s]: centro (%.3f, %.3f), deve essere su %s"
                % (label, d, cx, cy, expect))


def check_band(boxes_map, r_in, r_out, label):
    """Il box deve coprire esattamente la banda radiale dell'anello."""
    for d, (x, y, w, h) in boxes_map.items():
        if d in ("up", "down"):
            lo, hi = y, y + h
        else:
            lo, hi = x, x + w
        if d in ("up", "left"):
            want = (-r_out, -r_in)
        else:
            want = (r_in, r_out)
        if abs(lo - want[0]) > 1e-6 or abs(hi - want[1]) > 1e-6:
            raise SystemExit(
                "BANDA SBAGLIATA [%s/%s]: radiale [%.2f, %.2f], atteso [%.2f, %.2f]"
                % (label, d, lo, hi, want[0], want[1]))


def check_overlap(inner, outer, center_half):
    """Le 9 zone non devono toccarsi."""
    rects = [("inner." + d, b) for d, b in inner.items()]
    rects += [("outer." + d, b) for d, b in outer.items()]
    rects.append(("center", (-center_half, -center_half, 2 * center_half, 2 * center_half)))
    for i, (ni, a) in enumerate(rects):
        for nj, b in rects[i + 1:]:
            w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
            h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
            if w > 0.5 and h > 0.5:
                raise SystemExit("SOVRAPPOSIZIONE: %s e %s (%.2f x %.2f)" % (ni, nj, w, h))


def css_block():
    # .outer (HTML): frecce piene -> box invariato, nessuna espansione
    inner = boxes(R_INNER_IN, R_INNER_OUT, TAP_INNER)
    # .inner (HTML): chevron tastiera -> box icona invariato + area sensibile
    outer_base = boxes(R_OUTER_IN, R_OUTER_OUT, TAP_OUTER)
    outer_hit = boxes(R_OUTER_HIT_IN, R_OUTER_HIT_OUT, TAP_OUTER_HIT)
    center_half = R_BTN

    check_symmetry(inner, "inner")
    check_symmetry(outer_base, "outer base")
    check_symmetry(outer_hit, "outer hit")
    check_band(inner, R_INNER_IN, R_INNER_OUT, "inner")
    check_band(outer_base, R_OUTER_IN, R_OUTER_OUT, "outer base")
    check_band(outer_hit, R_OUTER_HIT_IN, R_OUTER_HIT_OUT, "outer hit")
    check_overlap(inner, outer_hit, center_half)

    out = [BEGIN, ""]
    out.append("/* .outer -> anello interno R %s..%s (in HTML: frecce mouse) */"
               % (fmt(R_INNER_IN), fmt(R_INNER_OUT)))
    for d in ("up", "down", "left", "right"):
        l, t, w, h = pct_box(inner[d])
        out.append(".outer .%s{" % d)
        out.append("  left:%s%%;" % fmt(l))
        out.append("  top:%s%%;" % fmt(t))
        out.append("  width:%s%%;" % fmt(w))
        out.append("  height:%s%%;" % fmt(h))
        out.append("}")
    out.append("")
    out.append("/* .inner -> anello esterno R %s..%s (in HTML: frecce tastiera) */"
               % (fmt(R_OUTER_IN), fmt(R_OUTER_OUT)))
    for d in ("up", "down", "left", "right"):
        l, t, w, h = pct_box(outer_base[d])
        out.append(".inner .%s{" % d)
        out.append("  left:%s%%;" % fmt(l))
        out.append("  top:%s%%;" % fmt(t))
        out.append("  width:%s%%;" % fmt(w))
        out.append("  height:%s%%;" % fmt(h))
        out.append("}")
    out.append("")
    out.append("/* ::before invisibile: area sensibile R %s..%s, senza spostare l'icona */"
               % (fmt(R_OUTER_HIT_IN), fmt(R_OUTER_HIT_OUT)))
    for d in ("up", "down", "left", "right"):
        l, r, t, b = pseudo_insets(outer_base[d], outer_hit[d])
        out.append(".inner .%s::before{" % d)
        out.append('  content:"";')
        out.append("  position:absolute;")
        out.append("  left:%s%%;" % fmt(l))
        out.append("  right:%s%%;" % fmt(r))
        out.append("  top:%s%%;" % fmt(t))
        out.append("  bottom:%s%%;" % fmt(b))
        out.append("}")
    out.append(END)
    return "\n".join(out)


def build_svg():
    b = []
    b.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s %s %s %s" '
             'width="%s" height="%s" role="img" aria-label="Click wheel">'
             % (fmt(-VB), fmt(-VB), fmt(HALF), fmt(HALF), fmt(HALF), fmt(HALF)))
    b.append("""  <defs>
    <radialGradient id="body" cx="50%" cy="42%" r="62%">
      <stop offset="0" stop-color="#fbf8f0"/>
      <stop offset=".34" stop-color="#f8f5ea"/>
      <stop offset=".62" stop-color="#f0eee5"/>
      <stop offset=".84" stop-color="#dfe1de"/>
      <stop offset="1" stop-color="#d3d6d5"/>
    </radialGradient>
    <radialGradient id="ringIn" gradientUnits="userSpaceOnUse"
                    cx="0" cy="0" r="__R_IN_O__">
      <stop offset=".6" stop-color="#fffef8"/>
      <stop offset=".82" stop-color="#fbfaf3"/>
      <stop offset="1" stop-color="#f2f0e6"/>
    </radialGradient>
    <linearGradient id="ringOut" gradientUnits="userSpaceOnUse"
                    x1="0" y1="-__R_OUT_O__" x2="0" y2="__R_OUT_O__">
      <stop offset="0" stop-color="#fbfaf6"/>
      <stop offset=".22" stop-color="#f1f2ef"/>
      <stop offset=".5" stop-color="#e6e8e7"/>
      <stop offset=".78" stop-color="#dadcdb"/>
      <stop offset="1" stop-color="#ccd0cf"/>
    </linearGradient>
    <radialGradient id="btn" gradientUnits="userSpaceOnUse" cx="0" cy="-11" r="54">
      <stop offset="0" stop-color="#3d8cc8"/>
      <stop offset=".28" stop-color="#2f83c3"/>
      <stop offset=".55" stop-color="#287fc2"/>
      <stop offset="1" stop-color="#2577b9"/>
    </radialGradient>
    <radialGradient id="gloss" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#b0daf1" stop-opacity=".85"/>
      <stop offset=".5" stop-color="#5ba3d3" stop-opacity=".5"/>
      <stop offset="1" stop-color="#3f8dc8" stop-opacity="0"/>
    </radialGradient>
    <filter id="btnShadow" x="-40%" y="-40%" width="180%" height="180%">
      <feDropShadow dx="0" dy="1" stdDeviation="1.4"
                    flood-color="#122424" flood-opacity=".14"/>
    </filter>
  </defs>
""".replace("__R_IN_O__", fmt(R_IN_O)).replace("__R_OUT_O__", fmt(R_OUT_O)))

    # ombra di contorno del disco
    b.append('  <circle cx="0" cy="0" r="%s" fill="#122424" opacity=".10"/>' % fmt(R_BODY + 1.5))
    b.append('  <circle cx="0" cy="0" r="%s" fill="url(#body)"/>' % fmt(R_BODY))

    # anello esterno: 4 archi, gradiente verticale attenuato, nessuna ombra
    b.append('  <g fill="url(#ringOut)">')
    for p in arcs(R_OUT_I, R_OUT_O):
        b.append('    <path d="%s"/>' % p)
    b.append("  </g>")

    # solco divisorio
    b.append('  <circle cx="0" cy="0" r="%s" fill="none" stroke="#c9cccb" '
             'stroke-width="2.2" opacity=".9"/>' % fmt(R_GROOVE))
    b.append('  <circle cx="0" cy="0" r="%s" fill="none" stroke="#ffffff" '
             'stroke-width="1" opacity=".55"/>' % fmt(R_GROOVE + 1.6))

    # anello interno: 4 archi crema, nessuna ombra
    b.append('  <g fill="url(#ringIn)">')
    for p in arcs(R_IN, R_IN_O):
        b.append('    <path d="%s"/>' % p)
    b.append("  </g>")

    # luce sul bordo interno superiore
    b.append('  <path d="%s" fill="none" stroke="#ffffff" stroke-width="1.6" '
             'opacity=".7"/>' % annulus(R_IN - 0.8, R_IN + 0.8, -180, 0))

    # pulsante centrale: niente alone, niente disco di contorno
    b.append('  <circle cx="0" cy="0" r="%s" fill="url(#btn)" filter="url(#btnShadow)"/>'
             % fmt(R_BTN))
    b.append('  <ellipse cx="0" cy="-4" rx="11" ry="6" fill="url(#gloss)"/>')
    b.append('  <circle cx="0" cy="0" r="%s" fill="none" stroke="#ffffff" '
             'stroke-width="1" opacity=".18"/>' % fmt(R_BTN - 0.6))
    b.append("</svg>")
    return "\n".join(b) + "\n"


def apply_html(css):
    """Restituisce il nuovo HTML. Non scrive: va calcolato PRIMA di aprire il file
    in 'w', altrimenti un errore a meta' scrittura tronca il sorgente."""
    html = open(HTML_PATH, encoding="utf-8").read()
    if BEGIN not in html or END not in html:
        raise SystemExit("marcatori %r / %r non trovati in %s" % (BEGIN, END, HTML_PATH))
    return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda m: css, html, flags=re.S)


def write_atomic(path, data):
    """Scrive via file temporaneo + rename: un errore non lascia mai il file a meta'."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verifica che SVG e CSS siano gia' allineati, senza scrivere")
    args = ap.parse_args()

    # tutto calcolato e validato PRIMA di toccare il filesystem
    svg = build_svg()
    css = css_block()
    new_html = apply_html(css)

    if args.check:
        problems = []
        if open(SVG_PATH, encoding="utf-8").read() != svg:
            problems.append("web/wheel.svg non allineato")
        if open(HTML_PATH, encoding="utf-8").read() != new_html:
            problems.append("hit zone in web/index.html non allineate")
        for p in problems:
            print(p)
        print("check:", "tutto allineato" if not problems else "FUORI SINCRO")
        return 0 if not problems else 1

    write_atomic(SVG_PATH, svg)
    write_atomic(HTML_PATH, new_html)
    print("scritti: web/wheel.svg (%d byte) e hit zone in web/index.html" % len(svg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
