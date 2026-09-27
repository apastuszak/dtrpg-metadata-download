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
barcode (`page_render.py`), page 2 is the back cover and the last page is
numbered normally (75 page-2 barcodes in the 490-book corpus, every one a
real back cover). A book can also have no back cover at all: a landscape
PDF page 1 in a portrait book is a single wraparound cover (Shadowrun),
and a blank white last page isn't a cover -- in both cases the last page
gets its number.

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

import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pikepdf
import pymupdf as fitz

from page_render import page_has_barcode, page_is_blank

BAND_FRACTION = 0.10
MAX_PAGE_NUMBER_DIGITS = 4
MIN_AGREEING_PAGES = 3
MIN_AGREEMENT = 0.5
MIN_SPAN = 0.5
COMPETING_MIN_PAGES = 10
COMPETING_FRACTION = 0.2
COVER_LABEL = "Cover"
WRAPAROUND_ASPECT = 1.1

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


def is_wraparound(doc: fitz.Document) -> bool:
    """PDF page 1 is landscape while the book itself is portrait: a single
    wraparound cover (front and back art on one page, e.g. Shadowrun), so
    the book has no separate back cover."""
    first = doc[0].rect
    body_width = statistics.median(page.rect.width for page in doc)
    body_height = statistics.median(page.rect.height for page in doc)
    return first.width > first.height * WRAPAROUND_ASPECT and body_width < body_height


def plan_page_labels(
    numbers: list[set[int]], back_cover_second: bool = False, no_back_cover: bool = False,
) -> tuple[PageLabelPlan | None, str]:
    """Pure: from each page's candidate numbers, the label plan -- or None
    and the reason it won't guess. `back_cover_second` puts the back cover
    at PDF page 2 instead of the last page (see page_render.py);
    `no_back_cover` means there isn't one, so the last page is numbered."""
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
    if no_back_cover:
        back_cover = None
    elif back_cover_second:
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
            wraparound = is_wraparound(doc)
        finally:
            doc.close()
    except Exception as exc:
        return None, f"couldn't read the PDF: {exc}"
    last_blank = len(numbers) > 1 and page_is_blank(path, len(numbers) - 1)
    if wraparound or last_blank:
        plan, reason = plan_page_labels(numbers, no_back_cover=True)
        if plan is not None:
            reason += "; no back cover (" + ("wraparound front cover" if wraparound else "last page is blank") + ")"
        return plan, reason
    plan, reason = plan_page_labels(numbers)
    if plan is not None and plan.page_count > 2 and 1 not in plan.agreeing and page_has_barcode(path, 1):
        plan, reason = plan_page_labels(numbers, back_cover_second=True)
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


def read_existing_labels(pdf: pikepdf.Pdf) -> list[tuple[str | None, str, int]] | None:
    """Each page's existing label as (style, prefix, number), or None if the
    PDF has no /PageLabels -- the decoded form reorder_page_labels() needs."""
    if "/PageLabels" not in pdf.Root:
        return None
    starts = sorted((int(key), entry) for key, entry in pikepdf.NumberTree(pdf.Root.PageLabels).items())
    per_page: list[tuple[str | None, str, int]] = []
    k = -1
    for index in range(len(pdf.pages)):
        while k + 1 < len(starts) and starts[k + 1][0] <= index:
            k += 1
        if k < 0:
            per_page.append(("/D", "", index + 1))  # no entry covers this page yet
            continue
        start, entry = starts[k]
        style = str(entry.S) if "/S" in entry else None
        prefix = str(entry.P) if "/P" in entry else ""
        first = int(entry.St) if "/St" in entry else 1
        per_page.append((style, prefix, first + index - start))
    return per_page


def write_existing_labels(pdf: pikepdf.Pdf, per_page: list[tuple[str | None, str, int]]) -> None:
    """Inverse of read_existing_labels(): one /PageLabels entry per run of
    pages that share style and prefix and count up by one."""
    nums = []
    previous = None
    for index, (style, prefix, value) in enumerate(per_page):
        continues = (
            previous is not None
            and previous[0] == style
            and previous[1] == prefix
            and (style is None or value == previous[2] + 1)
        )
        if not continues:
            entry = pikepdf.Dictionary()
            if style is not None:
                entry.S = pikepdf.Name(style)
                entry.St = value
            if prefix:
                entry.P = pikepdf.String(prefix)
            nums.extend([index, entry])
        previous = (style, prefix, value)
    pdf.Root.PageLabels = pdf.make_indirect(pikepdf.Dictionary(Nums=pikepdf.Array(nums)))

