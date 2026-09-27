"""Automatic /PageLabels -- the page "names" a PDF viewer shows in its page
box and thumbnail panel (Cover, i, ii, 1, 2, ...), independent of the
physical page index.

Scheme: the front cover (PDF page 1) and back cover (the last page) are
labeled "Cover"; pages before printed page 1 get sequential lowercase roman
numerals (i, ii, iii, ...); every other page gets the page number actually
printed in the book.

Detection reads every digit-only word in the top and bottom
`BAND_FRACTION` of each page (headers and footers both, since some books
number pages in the header) and votes, per page, on the offset between
the physical page index and the printed number. One offset explains nearly
every numbered page in a normal book, so the winner gives every page's
printed number -- including pages that show no number at all (title
pages, full-page art, ads), and pages before the first visible number:
if the first number seen is "5", the pages before it are worked back to
4, 3, 2, 1, and anything before printed page 1 is front matter (roman).
Counting agreement per *page* rather than per digit matters: a 10% band
also catches table values, years and product codes, which would swamp a
per-digit vote.

A cover is dropped from its slot if that page itself shows a page number
agreeing with the winning offset -- i.e. the PDF has no separate cover
there. The label then follows the printed number, even when the publisher
counts the cover as page 1 (Call of Cthulhu's first interior page prints
"2", so it's labeled "2", not "1").

Some publishers (Steve Jackson Games, Arc Dream's Delta Green) put the back
cover second, right after the front cover, with an ad or a form as the
last page. A back cover carries a retail barcode; nothing else near the
front of a book does. So when PDF page 2 shows no page number and has a
barcode (`has_barcode()`), page 2 is the back cover and the last page is
numbered normally. Checked against page 2 of all 401 labeled test books:
66 barcodes found, every one a real back cover.

Deliberately refuses rather than guesses (returning a reason, never
raising) when the evidence doesn't support one consistent numbering:
- too few agreeing pages, or under half the numbered pages agreeing
  (scanned books whose numbers aren't text, or aren't in a header/footer);
- the agreeing pages span less than half the book (e.g. only one booklet
  of a boxed set is numbered);
- a second offset has a real following of its own (a merged multi-volume
  PDF, where numbering jumps partway through) -- a single offset would
  mislabel everything after the jump.
Existing /PageLabels are left untouched in all of those cases.

Tuned against 490 real RPG PDFs: 400 labeled, and on books whose existing
labels were more than physical page numbers, 99.7% of pages matched the
publisher's own labels. The skips were scans with no text-layer page
numbers, boxed sets, and merged volumes -- see docs/HISTORY.md.

Derived from fix_page_labels.py in the gurps_4e_revised_hyperlink sibling
project, rather than vendored: that script is an interactive wizard, only
reads footers, and anchors arabic numbering to "printed page 1" by
counting pages, so it can't represent a book whose cover is counted as
page 1.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pikepdf
import pymupdf as fitz

BAND_FRACTION = 0.10
MAX_PAGE_NUMBER_DIGITS = 4
MIN_AGREEING_PAGES = 3
MIN_AGREEMENT = 0.5
MIN_SPAN = 0.5
COMPETING_MIN_PAGES = 10
COMPETING_FRACTION = 0.2
COVER_LABEL = "Cover"

# Barcode detection: a retail barcode renders as a band of fine vertical
# stripes whose pattern repeats nearly identically row after row. EAN-13
# has 30 bars (~60 edges) in ~1-1.5in; SJG's are only ~0.33in tall, which
# is why the height minimum is low and the resolution is high.
BARCODE_DPI = 300
BARCODE_WINDOW_IN = 1.6
BARCODE_MIN_EDGES = 40
BARCODE_MIN_HEIGHT_IN = 0.2
BARCODE_MIN_SIMILARITY = 0.9
_DARK = bytes(1 if v < 128 else 0 for v in range(256))

_ROMAN = (
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
    (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
)


def to_roman(n: int) -> str:
    out = []
    for value, symbol in _ROMAN:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


@dataclass(frozen=True)
class LabelRange:
    """A run of consecutive pages sharing one /PageLabels entry. `start`/
    `end` are 0-based, inclusive; `kind` is "cover", "roman" or "arabic";
    `first_value` is the first page's number (unused for covers)."""

    start: int
    end: int
    kind: str
    first_value: int

    def label(self, page: int) -> str:
        if self.kind == "cover":
            return COVER_LABEL
        value = self.first_value + (page - self.start)
        return to_roman(value) if self.kind == "roman" else str(value)


@dataclass
class PageLabelPlan:
    page_count: int
    ranges: list[LabelRange]
    agreeing: frozenset[int]
    numbered_pages: int

    @property
    def agreeing_pages(self) -> int:
        return len(self.agreeing)

    @property
    def labels(self) -> list[str]:
        return [r.label(i) for r in self.ranges for i in range(r.start, r.end + 1)]

    def summary(self) -> str:
        return f"page numbers read from {self.agreeing_pages} of {self.numbered_pages} numbered pages"

    def describe(self) -> list[str]:
        """One line per range, 1-based PDF page numbers, e.g.
        "PDF pages 2-3: i to ii"."""
        lines = []
        for r in self.ranges:
            first, last = r.label(r.start), r.label(r.end)
            if r.start == r.end:
                lines.append(f"PDF page {r.start + 1}: {first}")
            elif r.kind == "cover":
                lines.append(f"PDF pages {r.start + 1}-{r.end + 1}: {first}")
            else:
                lines.append(f"PDF pages {r.start + 1}-{r.end + 1}: {first} to {last}")
        return lines


def read_page_numbers(doc: fitz.Document) -> list[set[int]]:
    """Every digit-only word (1-4 digits, >= 1) in each page's header and
    footer band -- candidates, not answers; the vote decides which ones
    are page numbers."""
    numbers = []
    for page in doc:
        r = page.rect
        band = r.height * BAND_FRACTION
        found: set[int] = set()
        for clip in (fitz.Rect(r.x0, r.y0, r.x1, r.y0 + band), fitz.Rect(r.x0, r.y1 - band, r.x1, r.y1)):
            for word in page.get_text("words", clip=clip):
                token = word[4].strip(".,:;|()[]")
                if token.isdigit() and len(token) <= MAX_PAGE_NUMBER_DIGITS and int(token) >= 1:
                    found.add(int(token))
        numbers.append(found)
    return numbers


def has_barcode(page: fitz.Page) -> bool:
    """True if the page shows a retail barcode anywhere -- see the constants
    above for what that means in pixels."""
    pix = page.get_pixmap(dpi=BARCODE_DPI, colorspace=fitz.csGRAY, alpha=False)
    width, height, samples = pix.width, pix.height, pix.samples
    window = int(BARCODE_WINDOW_IN * BARCODE_DPI)
    rows_needed = int(BARCODE_MIN_HEIGHT_IN * BARCODE_DPI / 2)  # every 2nd row is sampled
    drift = BARCODE_DPI // 25
    run, previous = 0, None
    for y in range(0, height, 2):
        bits = samples[y * width:(y + 1) * width].translate(_DARK)
        edges = [x for x in range(1, width) if bits[x] != bits[x - 1]]
        best, best_start, j = 0, 0, 0
        for i in range(len(edges)):
            while edges[i] - edges[j] > window:
                j += 1
            if i - j + 1 > best:
                best, best_start = i - j + 1, edges[j]
        if best < BARCODE_MIN_EDGES:
            run, previous = 0, None
            continue
        segment = bits[best_start:best_start + window]
        if previous is not None and abs(previous[0] - best_start) <= drift:
            n = min(len(previous[1]), len(segment))
            same = sum(1 for k in range(n) if previous[1][k] == segment[k])
            run = run + 1 if n and same / n >= BARCODE_MIN_SIMILARITY else 1
        else:
            run = 1
        previous = (best_start, segment)
        if run >= rows_needed:
            return True
    return False


def plan_page_labels(numbers: list[set[int]], back_cover_second: bool = False) -> tuple[PageLabelPlan | None, str]:
    """Pure: from each page's candidate numbers, the label plan -- or None
    and the reason it won't guess. `back_cover_second` puts the back cover
    at PDF page 2 instead of the last page (see has_barcode())."""
    page_count = len(numbers)
    pages_by_offset: dict[int, set[int]] = defaultdict(set)
    numbered = 0
    for index, found in enumerate(numbers):
        if found:
            numbered += 1
        for value in found:
            pages_by_offset[index - value].add(index)
    if not pages_by_offset:
        return None, "no page numbers found in any header or footer"

    ranked = sorted(pages_by_offset.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    offset, agreeing = ranked[0]
    if len(agreeing) < MIN_AGREEING_PAGES or len(agreeing) / numbered < MIN_AGREEMENT:
        return None, f"no consistent page numbering (only {len(agreeing)} of {numbered} numbered pages agree)"
    if (max(agreeing) - min(agreeing) + 1) / page_count < MIN_SPAN:
        return None, "page numbers only found on a small part of the book"
    for _, other in ranked[1:4]:
        if len(other - agreeing) >= max(COMPETING_MIN_PAGES, COMPETING_FRACTION * len(agreeing)):
            return None, "page numbering changes partway through (a merged or multi-part PDF?)"

    front_cover = 0 if 0 not in agreeing else None
    if back_cover_second:
        back_cover = 1 if page_count > 1 and 1 not in agreeing else None
    else:
        back_cover = page_count - 1 if page_count > 1 and (page_count - 1) not in agreeing else None

    per_page: list[tuple[str, int]] = []
    roman = 0
    for index in range(page_count):
        if index in (front_cover, back_cover):
            per_page.append(("cover", 0))
        elif index - offset >= 1:
            per_page.append(("arabic", index - offset))
        else:
            roman += 1
            per_page.append(("roman", roman))

    ranges: list[LabelRange] = []
    start = 0
    for index in range(1, page_count + 1):
        kind, value = per_page[index - 1]
        continues = (
            index < page_count
            and per_page[index][0] == kind
            and (kind == "cover" or per_page[index][1] == value + 1)
        )
        if not continues:
            ranges.append(LabelRange(start, index - 1, per_page[start][0], per_page[start][1]))
            start = index

    plan = PageLabelPlan(page_count, ranges, frozenset(agreeing), numbered)
    summary = plan.summary() + ("; back cover is PDF page 2" if back_cover == 1 else "")
    return plan, summary


def detect_page_labels(path: Path) -> tuple[PageLabelPlan | None, str]:
    """Read-only. Never raises -- an unreadable PDF is just one more reason
    to leave its labels alone."""
    try:
        doc = fitz.open(path)
        try:
            numbers = read_page_numbers(doc)
            if not numbers:
                return None, "the PDF has no pages"
            plan, reason = plan_page_labels(numbers)
            if plan is not None and plan.page_count > 2 and 1 not in plan.agreeing and has_barcode(doc[1]):
                plan, reason = plan_page_labels(numbers, back_cover_second=True)
        finally:
            doc.close()
    except Exception as exc:
        return None, f"couldn't read the PDF: {exc}"
    return plan, reason


def set_page_labels(pdf: pikepdf.Pdf, plan: PageLabelPlan) -> None:
    """Replaces pdf's /PageLabels with the plan (the caller saves). Raises
    ValueError if the page count no longer matches what was read."""
    if len(pdf.pages) != plan.page_count:
        raise ValueError(f"page count changed ({plan.page_count} read, {len(pdf.pages)} now)")
    nums = []
    for r in plan.ranges:
        entry = pikepdf.Dictionary()
        if r.kind == "cover":
            entry.P = pikepdf.String(COVER_LABEL)
        else:
            entry.S = pikepdf.Name.r if r.kind == "roman" else pikepdf.Name.D
            entry.St = r.first_value
        nums.extend([r.start, entry])
    pdf.Root.PageLabels = pdf.make_indirect(pikepdf.Dictionary(Nums=pikepdf.Array(nums)))
