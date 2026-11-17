"""Verifica geometria dei tracciati SVG ('d').

Parser minimale dei comandi usati in questo progetto: M/m L/l H/h V/v A/a Z/z,
con ripetizione implicita delle coppie di coordinate. Gli archi sono campionati
con la conversione endpoint->centro, quindi il bounding box e' reale.
"""

import math
import re

TOK = re.compile(r"[MmLlHhVvAaZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
NARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "A": 7, "Z": 0}


def arc_points(x0, y0, rx, ry, rot, laf, sf, x1, y1, n=32):
    if rx == 0 or ry == 0 or (x0 == x1 and y0 == y1):
        return [(x1, y1)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rot)
    cosp, sinp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p = cosp * dx2 + sinp * dy2
    y1p = -sinp * dx2 + cosp * dy2
    lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if laf == sf:
        co = -co
    cxp = co * rx * y1p / ry
    cyp = -co * ry * x1p / rx
    cx = cosp * cxp - sinp * cyp + (x0 + x1) / 2.0
    cy = sinp * cxp + cosp * cyp + (y0 + y1) / 2.0

    def ang(ux, uy, vx, vy):
        n1 = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, (ux * vx + uy * vy) / n1))) if n1 else 0.0
        return -a if (ux * vy - uy * vx) < 0 else a

    th1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry,
              (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sf and dth > 0:
        dth -= 2 * math.pi
    elif sf and dth < 0:
        dth += 2 * math.pi
    return [(cosp * rx * math.cos(th1 + dth * k / n) - sinp * ry * math.sin(th1 + dth * k / n) + cx,
             sinp * rx * math.cos(th1 + dth * k / n) + cosp * ry * math.sin(th1 + dth * k / n) + cy)
            for k in range(n + 1)]


def path_points(d):
    toks = TOK.findall(d)
    i = 0
    x = y = sx = sy = 0.0
    pts = []
    cur = None
    while i < len(toks):
        if re.match(r"[A-Za-z]", toks[i]):
            cur = toks[i]
            i += 1
        if cur is None:
            raise ValueError("path senza comando iniziale: %r" % d[:40])
        n = NARGS[cur.upper()]
        if n == 0:
            x, y = sx, sy
            pts.append((x, y))
            cur = None
            continue
        if i + n > len(toks):
            raise ValueError("argomenti insufficienti per %r in %r" % (cur, d))
        a = [float(v) for v in toks[i:i + n]]
        i += n
        rel = cur.islower()
        if cur in "Mm":
            x, y = (x + a[0], y + a[1]) if rel else (a[0], a[1])
            sx, sy = x, y
        elif cur in "Ll":
            x, y = (x + a[0], y + a[1]) if rel else (a[0], a[1])
        elif cur in "Hh":
            x = x + a[0] if rel else a[0]
        elif cur in "Vv":
            y = y + a[0] if rel else a[0]
        elif cur in "Aa":
            ex, ey = (x + a[5], y + a[6]) if rel else (a[5], a[6])
            pts.extend(arc_points(x, y, a[0], a[1], a[2], int(a[3]), int(a[4]), ex, ey))
            x, y = ex, ey
            continue
        pts.append((x, y))
        if cur == "M":
            cur = "L"
        elif cur == "m":
            cur = "l"
    return pts


def bbox(d):
    pts = path_points(d)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))
