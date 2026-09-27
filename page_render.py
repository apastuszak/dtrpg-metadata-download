"""Everything that has to render a PDF page, run in a child process: the
barcode and blank-page checks used to find a book's back cover
(page_labels.py, back_cover.py), and page thumbnails for the "is this the
back cover?" prompt in tag.

A retail barcode is what marks a back cover: nothing else near the front
or back of an RPG book has one, and plenty of books have no back cover at
all. (PDF-only releases usually have no barcode either; tag asks about
those instead of guessing -- see back_cover.py.)

Two independent barcode checks on one 300 DPI grayscale render; either is
enough:

- `_repeating_stripes()`: a band of fine vertical stripes (>= 40 edges
  within 1.6in) whose whole window repeats nearly identically for >= 0.2in
  of height. Strict (no false positives on the middle page of 490 test
  books) and it handles barcodes that are only a blurry part of a
  flattened cover image (Delta Green), but it misses a barcode with
  varying text or art right beside it (Mongoose's rotated product code).
- `_decodes_ean13()`: actually decodes an EAN-13/UPC-A symbol (ISBNs are
  EAN-13) with a valid check digit, and requires the same code on >= 5
  rows. Line art occasionally passes the checksum on one or two rows by
  chance -- real barcodes decode on 11-132 rows (0.3-0.95in) -- hence the
  row count.

Rendering can crash MuPDF outright on some pages (a real Traveller PDF
aborts at every DPI), which Python can't catch, so callers use
page_has_barcode()/page_is_blank()/render_page_png(), which run in a
child process. See docs/HISTORY.md for the corpus measurements behind
every threshold here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pymupdf as fitz

DPI = 300
WINDOW_IN = 1.6
MIN_EDGES = 40
MIN_HEIGHT_IN = 0.2
MIN_SIMILARITY = 0.9
EAN_MIN_ROWS = 5
EAN_THRESHOLDS = (128, 90, 170)
MAX_PIXELS = 60_000_000
TIMEOUT_SECONDS = 120
BLANK_MIN_GRAY = 230

_DARK = bytes(1 if v < 128 else 0 for v in range(256))
_TABLES = [bytes(1 if v < t else 0 for v in range(256)) for t in EAN_THRESHOLDS]

_L = {"0001101": 0, "0011001": 1, "0010011": 2, "0111101": 3, "0100011": 4,
      "0110001": 5, "0101111": 6, "0111011": 7, "0110111": 8, "0001011": 9}
_R = {"".join("1" if c == "0" else "0" for c in k): v for k, v in _L.items()}
_G = {k[::-1]: v for k, v in _R.items()}
_PARITY = {"LLLLLL": 0, "LLGLGG": 1, "LLGGLG": 2, "LLGGGL": 3, "LGLLGG": 4,
           "LGGLLG": 5, "LGGGLL": 6, "LGLGLG": 7, "LGLGGL": 8, "LGGLGL": 9}
_MAX_DIGIT_ERROR = 1.6


def _runs_of(pattern: str) -> list[int]:
    out, count = [], 1
    for a, b in zip(pattern, pattern[1:]):
        if a == b:
            count += 1
        else:
            out.append(count)
            count = 1
    out.append(count)
    return out


_CANDIDATES = {
    "L": [(_runs_of(k), v) for k, v in _L.items()],
    "G": [(_runs_of(k), v) for k, v in _G.items()],
    "R": [(_runs_of(k), v) for k, v in _R.items()],
}


def _densest_span(bits: bytes, width: int, window: int) -> tuple[int, int, int]:
    """(edge count, first edge, last edge) of the densest `window`-wide run."""
    edges = [x for x in range(1, width) if bits[x] != bits[x - 1]]
    best, lo, hi, j = 0, 0, 0, 0
    for i in range(len(edges)):
        while edges[i] - edges[j] > window:
            j += 1
        if i - j + 1 > best:
            best, lo, hi = i - j + 1, edges[j], edges[i]
    return best, lo, hi


def _repeating_stripes(width: int, height: int, samples: bytes) -> bool:
    window = int(WINDOW_IN * DPI)
    rows_needed = int(MIN_HEIGHT_IN * DPI / 2)  # every 2nd row is sampled
    drift = DPI // 25
    run, previous = 0, None
    for y in range(0, height, 2):
        bits = samples[y * width:(y + 1) * width].translate(_DARK)
        best, start, _ = _densest_span(bits, width, window)
        if best < MIN_EDGES:
            run, previous = 0, None
            continue
        segment = bits[start:start + window]
        if previous is not None and abs(previous[0] - start) <= drift:
            n = min(len(previous[1]), len(segment))
            same = sum(1 for k in range(n) if previous[1][k] == segment[k])
            run = run + 1 if n and same / n >= MIN_SIMILARITY else 1
        else:
            run = 1
        previous = (start, segment)
        if run >= rows_needed:
            return True
    return False


def _match_digit(widths: list[int], kinds: tuple[str, ...]) -> tuple[float, str, int] | None:
    total = sum(widths)
    norm = [w * 7 / total for w in widths]
    best = None
    for kind in kinds:
        for runs, value in _CANDIDATES[kind]:
            err = sum(abs(a - b) for a, b in zip(norm, runs))
            if best is None or err < best[0]:
                best = (err, kind, value)
    return best if best is not None and best[0] <= _MAX_DIGIT_ERROR else None


def decode_ean13(runs: list[tuple[int, int]]) -> str | None:
    """An EAN-13/UPC-A code with a valid check digit from a row's
    (bit, width) runs, or None."""
    for s in range(len(runs) - 58):
        if runs[s][0] != 1:
            continue
        seg = [w for _, w in runs[s:s + 59]]
        module = sum(seg) / 95
        if any(abs(w - module) > module * 0.8 for w in seg[0:3] + seg[27:32] + seg[56:59]):
            continue  # start, middle and end guards are one module each
        digits, parity = [], ""
        for d in range(6):
            m = _match_digit(seg[3 + d * 4:7 + d * 4], ("L", "G"))
            if m is None:
                break
            digits.append(m[2])
            parity += m[1]
        if len(digits) != 6 or parity not in _PARITY:
            continue
        for d in range(6):
            m = _match_digit(seg[32 + d * 4:36 + d * 4], ("R",))
            if m is None:
                break
            digits.append(m[2])
        if len(digits) != 12:
            continue
        digits.insert(0, _PARITY[parity])
        check = (10 - (sum(digits[0:12:2]) + 3 * sum(digits[1:12:2])) % 10) % 10
        if check == digits[12]:
            return "".join(map(str, digits))
    return None


def _decodes_ean13(width: int, height: int, samples: bytes) -> bool:
    window = int(WINDOW_IN * DPI)
    rows_by_code: dict[str, int] = {}
    for y in range(0, height, 2):
        row = samples[y * width:(y + 1) * width]
        for table in _TABLES:
            bits = row.translate(table)
            best, lo, hi = _densest_span(bits, width, window)
            if best < MIN_EDGES:
                continue
            a, b = max(0, lo - window // 2), min(width, hi + window // 2)
            runs, start = [], a
            for x in range(a + 1, b + 1):
                if x == b or bits[x] != bits[x - 1]:
                    runs.append((bits[start], x - start))
                    start = x
            code = decode_ean13(runs)
            if code:
                rows_by_code[code] = rows_by_code.get(code, 0) + 1
                if rows_by_code[code] >= EAN_MIN_ROWS:
                    return True
                break  # this row is done; don't count it once per threshold
    return False


def has_barcode(page: fitz.Page) -> bool:
    r = page.rect
    if (r.width / 72 * DPI) * (r.height / 72 * DPI) > MAX_PIXELS:
        return False  # poster-sized maps, never a back cover
    pix = page.get_pixmap(dpi=DPI, colorspace=fitz.csGRAY, alpha=False)
    return _repeating_stripes(pix.width, pix.height, pix.samples) or _decodes_ean13(pix.width, pix.height, pix.samples)


def is_blank(page: fitz.Page) -> bool:
    """No text and nothing darker than near-white when rendered -- a page
    that just pads a book out, never a cover."""
    if page.get_text("text").strip():
        return False
    pix = page.get_pixmap(dpi=20, colorspace=fitz.csGRAY, alpha=False)
    return min(pix.samples, default=255) >= BLANK_MIN_GRAY


_CHECKS = {"barcode": has_barcode, "blank": is_blank}


def _check_in_child(check: str, path: Path, index: int) -> bool | None:
    """Runs a rendering check on one page in a child process with a
    timeout, so a MuPDF abort or hang can't take down the caller. None
    means it couldn't tell (crash or timeout)."""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), check, str(path), str(index)],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"1": True, "0": False}.get(result.stdout.strip())


def page_has_barcode(path: Path, index: int) -> bool | None:
    """has_barcode() in a child process; callers treat None as "no barcode"."""
    return _check_in_child("barcode", path, index)


def page_is_blank(path: Path, index: int) -> bool | None:
    """is_blank() in a child process; callers treat None as "not blank"."""
    return _check_in_child("blank", path, index)


def render_page_png(path: Path, index: int, out: Path, height_px: int = 480) -> bool:
    """Writes a thumbnail of one page to `out`, in a child process. False if
    it couldn't (crash, timeout, unreadable page)."""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "png", str(path), str(index), str(out), str(height_px)],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "1" and out.exists()


if __name__ == "__main__":
    # Entry point for the child processes above only.
    with fitz.open(sys.argv[2]) as _doc:
        _page = _doc[int(sys.argv[3])]
        if sys.argv[1] == "png":
            _scale = int(sys.argv[5]) / _page.rect.height
            _page.get_pixmap(matrix=fitz.Matrix(_scale, _scale)).save(sys.argv[4])
            print("1")
        else:
            print("1" if _CHECKS[sys.argv[1]](_page) else "0")
