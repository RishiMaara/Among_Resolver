#!/usr/bin/env python3
"""
Scanned bank statements with a known answer, for the scan reader.

A scan is a picture of a statement: no text layer, slightly rotated, grey,
noisy. This draws statements in three layouts Indian banks print —

  split     Date | Narration | Ref | Withdrawal | Deposit | Balance
  dc        Date | Description | Debit | Credit | Balance
  marker    Txn Date | Particulars | Ref | Amount | Dr/Cr | Balance

(the last is the hard one: a single amount column, and the model has to use
the Dr/Cr marker to split it) — then degrades each image the way a scanner
or phone camera does, and saves it as an image-only PDF or a JPEG. The
figures are invented and balance line by line, and each statement's truth is
returned beside its bytes.

Pillow's built-in font is used so the images are the same on any machine.

From engine/:
    python scripts/make_scanned_statements.py          # the public sample scan
"""

from __future__ import annotations

import io
import os
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "public", "sample-data", "statements")
LAYOUTS = ("split", "dc", "marker")


@dataclass
class Truth:
    account: str
    opening: int
    closing: int
    lines: list[tuple[date, str, str, int, int]] = field(default_factory=list)  # date, desc, ref, amount, balance


def indian(p: int) -> str:
    whole, frac = f"{abs(p) / 100:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail]) + "." + frac


_PAYERS = ("SHARMA TRADERS", "GUPTA AND SONS", "KRISHNA ENTERPRISES", "MEHTA TEXTILES",
           "RAO ELECTRICALS", "IYER FOODS", "SINGH LOGISTICS", "PATEL HARDWARE")


def random_truth(rng: random.Random) -> Truth:
    start = date(2026, 8, 1) + timedelta(days=rng.randrange(40))
    opening = rng.randrange(2_00_000_00, 40_00_000_00)
    t = Truth(account=f"{rng.randrange(10**13, 10**14)}", opening=opening, closing=opening)
    bal, d = opening, start
    for i in range(rng.randrange(6, 13)):
        d += timedelta(days=rng.choice((0, 0, 1, 1, 2)))
        kind = rng.random()
        if kind < 0.25:
            ref = f"UTR{d:%Y%m%d}{rng.randrange(100, 999)}"
            amt = rng.randrange(20_000_00, 3_00_000_00)
            desc = f"NEFT CR {ref} RAZORPAY SOFTWARE PVT LTD"
        elif kind < 0.55:
            ref = f"{rng.randrange(10**11, 10**12)}"
            amt = rng.randrange(500_00, 60_000_00)
            desc = f"UPI CR {ref} {rng.choice(_PAYERS)}"
        elif kind < 0.8:
            ref = f"ACH{rng.randrange(10**6, 10**7)}"
            amt = -rng.randrange(1_000_00, 90_000_00)
            desc = rng.choice(("ACH DR ELECTRICITY BESCOM", "ACH DR GST PAYMENT",
                               "ACH DR LOAN EMI HDFC", "ACH DR TELECOM AIRTEL"))
        else:
            ref = f"IMPS{rng.randrange(10**8, 10**9)}"
            amt = -rng.randrange(2_000_00, 1_50_000_00)
            desc = f"IMPS DR {ref} {rng.choice(_PAYERS)}"
        if bal + amt < 0:
            amt = -amt
        bal += amt
        t.lines.append((d, desc, ref, amt, bal))
    t.closing = bal
    return t


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:        # Pillow < 10.1: fixed-size bitmap font
        return ImageFont.load_default()


def draw(t: Truth, layout: str) -> Image.Image:
    img = Image.new("L", (1700, 380 + 64 * len(t.lines)), 255)
    g = ImageDraw.Draw(img)
    big, small = _font(34), _font(24)
    first, last = t.lines[0][0], t.lines[-1][0]
    g.text((80, 50), "STATEMENT OF ACCOUNT", font=big, fill=0)
    g.text((80, 110), f"Account No: {t.account}    Period {first:%d/%m/%Y} to {last:%d/%m/%Y}"
           "    Currency INR", font=small, fill=0)
    g.text((80, 160), f"Opening Balance {indian(t.opening)}", font=small, fill=0)
    y = 230
    if layout == "split":
        cols = [(80, "Date"), (260, "Narration"), (860, "Ref"), (1130, "Withdrawal"),
                (1330, "Deposit"), (1520, "Balance")]
    elif layout == "dc":
        cols = [(80, "Date"), (260, "Description"), (1110, "Debit"), (1310, "Credit"),
                (1510, "Balance")]
    else:
        cols = [(80, "Txn Date"), (260, "Particulars"), (860, "Ref"), (1130, "Amount"),
                (1330, "Dr/Cr"), (1450, "Balance")]
    for x, h in cols:
        g.text((x, y), h, font=small, fill=0)
    g.line((70, y + 40, 1650, y + 40), fill=0, width=2)
    y += 60
    for d, desc, ref, amt, bal in t.lines:
        narration = desc if layout != "dc" else f"{desc} {ref}"
        cells = {"split": [f"{d:%d/%m/%Y}", narration[:38], ref,
                           indian(amt) if amt < 0 else "", indian(amt) if amt > 0 else "",
                           indian(bal)],
                 "dc": [f"{d:%d-%m-%Y}", narration[:62], indian(amt) if amt < 0 else "",
                        indian(amt) if amt > 0 else "", indian(bal)],
                 "marker": [f"{d:%d %b %Y}", desc[:38], ref, indian(amt),
                            "Dr" if amt < 0 else "Cr", indian(bal)]}[layout]
        for (x, _), text in zip(cols, cells):
            g.text((x, y), text, font=small, fill=0)
        y += 64
    g.line((70, y, 1650, y), fill=0, width=2)
    g.text((80, y + 20), f"Closing Balance {indian(t.closing)}", font=small, fill=0)
    return img


def degrade(img: Image.Image, rng: random.Random) -> Image.Image:
    """What a scanner or a phone does to a page."""
    img = img.rotate(rng.uniform(-1.4, 1.4), expand=True, fillcolor=255,
                     resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 0.9)))
    noise = Image.effect_noise(img.size, rng.uniform(8, 22))
    img = Image.blend(img, noise, 0.12)
    return img.point(lambda v: min(255, int(v * rng.uniform(0.9, 1.0) + 12)))


def to_bytes(img: Image.Image, kind: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if kind == "pdf":
        img.convert("RGB").save(buf, format="PDF", resolution=150)
        return buf.getvalue(), "statement.pdf"
    img.convert("RGB").save(buf, format="JPEG", quality=70)
    return buf.getvalue(), "statement.jpg"


def scans(n: int, seed: int = 11) -> list[tuple[bytes, str, Truth, str]]:
    """n scans with their truth: (bytes, filename, truth, layout)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        t = random_truth(rng)
        layout = LAYOUTS[i % len(LAYOUTS)]
        data, name = to_bytes(degrade(draw(t, layout), rng), "pdf" if i % 2 == 0 else "jpg")
        out.append((data, name, t, layout))
    return out


def sample() -> bytes:
    """The demo statement (make_statement_samples.py), scanned."""
    import make_statement_samples as mss  # pylint: disable=import-outside-toplevel
    t = Truth(account=mss.ACCOUNT, opening=mss.OPENING, closing=mss.CLOSING)
    bal = mss.OPENING
    for d, desc, ref, amt in mss.LINES:
        bal += amt
        t.lines.append((d, desc, ref, amt, bal))
    return to_bytes(degrade(draw(t, "split"), random.Random(2026)), "pdf")[0]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "statement_scanned.pdf")
    with open(path, "wb") as f:
        f.write(sample())
    print(f"wrote {path}")
