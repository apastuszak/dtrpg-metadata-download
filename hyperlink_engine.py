#!/usr/bin/env python3
"""
hyperlink_engine.py -- one hyperlinking engine for RPG PDFs, with a rule set
per publisher in hyperlink_rules/ (one module each, found automatically).

It links a book's own cross-references to the pages they name ("p. 42",
"see page 48", "Chapter 3"), its Table of Contents and its Index, and skips
references to *other* books. Nothing that is already a link is linked
again, so running it twice adds nothing.

The engine does the work every publisher needs:

  - printed page number -> PDF page (`footer_vote()`, `band_vote()`)
  - finding the TOC and Index pages, and whether they're already linked
  - finding references (`abbreviated_refs()`, `page_word_list_refs()`,
    `bare_number_refs()`, `page_numbers_on()`, chapter references)
  - inserting links safely: page range, existing links (named destinations
    included), implausibly large rects, search_for() trimming of glued text
  - the CSV report, an incremental save, the counts callers get back

Cross-book checks (does this reference point at another book?) are in
hyperlink_checks.py, as building blocks a rule set lists by name.

A rule set is a `RuleSet` subclass, `Rules`, in its own module in
hyperlink_rules/, that mostly just sets attributes: which numbering,
TOC/Index and reference strategies its books need, the titles of other
books its publisher cites, and which checks to run. Adding a publisher
means adding one file there; see `RuleSet` for every setting and
hyperlink_rules/deltagreen.py for a complete example. Each rule-set module
carries the measurements behind its own choices; docs/HISTORY.md has the
longer story.

USAGE:
    python3 hyperlink_engine.py --rules RULES INPUT.pdf OUTPUT.pdf
    python3 hyperlink_engine.py --list

Writes OUTPUT.pdf plus OUTPUT_link_report.csv (every reference found,
linked or skipped, and why).

REQUIREMENTS:
    pip install pymupdf
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz  # PyMuPDF -- `import fitz` is the deprecated alias

import hyperlink_checks as checks  # noqa: F401 -- re-exported for rule sets
from hyperlink_checks import normalize_title, title_key
from provenance import is_filename_title

NBSP = " "
OTHER_SPACES = "   "  # regular space, nbsp, punctuation space
FOOTER_BAND_POINTS = 45

MAX_LINK_HEIGHT = 40       # a link should never span more than ~triple a line
MAX_LINK_WIDTH_FRAC = 0.9  # or more than ~a full line's width

# Spreadsheet formula-injection guard: report cells hold text copied out of
# the PDF, and a cell starting with one of these is run as a formula when
# the report is opened in Excel/Sheets/Numbers.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def word_rect(w):
    return fitz.Rect(w[0], w[1], w[2], w[3])


# ---------------------------------------------------------------------------
# Data passed between the engine and a rule set
# ---------------------------------------------------------------------------

@dataclass
class Ref:
    """One reference found in the text. `links` holds one (rect, printed
    number) per link to insert: usually one, several for a list like
    "pages 12, 14 and 16", whose numbers each get their own link.
    `start`/`end` are word indices of the whole reference, for the
    cross-book checks that look at the words around it. `probes` are
    search_for() strings tried in order to trim the rect to just the
    reference text (see `refine_rect()`)."""
    start: int
    end: int
    text: str
    links: list
    probes: list = field(default_factory=list)
    book_code: str | None = None
    is_index: bool = False


def page_words(page):
    """page.get_text("words"), extracted once per page and cached on the
    page's own document (so the cache goes away with the document, and a
    survey that opens one book after another can't mix them up). Building
    MuPDF's text page is the expensive part, and setup used to do it up to
    five times per page: numbering, TOC and Index detection, then linking.
    Returns a copy, so a caller sorting or filtering can't change the
    cache. Links added later don't change a page's words."""
    cache = getattr(page.parent, "_hyperlink_words", None)
    if cache is None:
        cache = page.parent._hyperlink_words = {}
    if page.number not in cache:
        cache[page.number] = page.get_text("words")
    return list(cache[page.number])


def page_search(page, text):
    """page.search_for(text), cached the same way: the same probe ("p. 34")
    is searched for again for every reference on a page."""
    cache = getattr(page.parent, "_hyperlink_searches", None)
    if cache is None:
        cache = page.parent._hyperlink_searches = {}
    key = (page.number, text)
    if key not in cache:
        cache[key] = page.search_for(text)
    return cache[key]


class PageText:
    """One page's words and existing link rects, plus italic spans on
    demand (building them means a full "dict" extraction, so it's done only
    for pages a cross-book check actually asks about)."""

    def __init__(self, page):
        self.page = page
        self.number = page.number
        self.words = page_words(page)
        self.existing = [l["from"] for l in page.get_links()]
        self._italic_spans = None

    def italic_spans(self):
        if self._italic_spans is None:
            spans = []
            for b in self.page.get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        font = s.get("font", "").lower()
                        italic = bool(s["flags"] & 2) or "italic" in font or "oblique" in font
                        spans.append((fitz.Rect(s["bbox"]), italic))
            self._italic_spans = spans
        return self._italic_spans

    def is_italic(self, word_idx):
        """Italic if the first span containing the word's centre is."""
        r = word_rect(self.words[word_idx])
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        for sr, italic in self.italic_spans():
            if sr.x0 - 0.5 <= cx <= sr.x1 + 0.5 and sr.y0 - 0.5 <= cy <= sr.y1 + 0.5:
                return italic
        return False


# ---------------------------------------------------------------------------
# Printed page number -> PDF page
# ---------------------------------------------------------------------------

class PageMap:
    """Printed page number -> 0-based PDF page index.

    Either an offset (PDF index = printed + offset) valid for printed
    numbers in [first, last], or an explicit dict built from the PDF's own
    /PageLabels."""

    def __init__(self, doc, offset=None, first=None, last=None, labels=None, description=""):
        self.page_count = doc.page_count
        self.offset = offset
        self.first = first
        self.last = last
        self.labels = labels
        self.description = description
        self._printed = {i: n for n, i in labels.items()} if labels is not None else None

    def target(self, n):
        if self.labels is not None:
            return self.labels.get(n)
        idx = n + self.offset
        if self.first <= n <= self.last and 0 <= idx < self.page_count:
            return idx
        return None

    def printed(self, i):
        """The printed number of PDF page `i`, or None."""
        if self._printed is not None:
            return self._printed.get(i)
        return i - self.offset


def bottom_words(page, band=FOOTER_BAND_POINTS):
    h = page.rect.height
    return [w for w in page_words(page) if w[3] > h - band]


def footer_observations(doc):
    """Every (pdf_index, printed_number) pair visible in a page's bottom
    band -- the raw evidence `footer_vote()` uses."""
    obs = []
    for i in range(doc.page_count):
        for w in bottom_words(doc[i]):
            t = w[4].strip(".,")
            if t.isdecimal():  # not isdigit(): "²" passes that, then int() fails
                obs.append((i, int(t)))
    return obs


def footer_vote(doc, use_labels=False):
    """The GURPS/Mongoose numbering detector: every digit word in the
    bottom 45pt votes for an offset; the valid printed range is the
    numbers actually seen with the winning offset.

    With `use_labels`, the PDF's own /PageLabels are used instead when they
    are trustworthy: a direct label -> index dict handles files with
    several offsets (e.g. a combined two-volume Basic Set) that a single
    offset can't. Trustworthy means the labels agree with most printed
    numbers seen in footers -- many PDFs carry only physical numbering as
    labels, and trusting those would put every link a few pages off -- and
    even then an individual label is used only where that page's own footer
    shows that number. (A /PageLabels entry can sit on a page with no
    printed number at all; on a real combined two-volume file one pointed
    a same-book-looking "p. 337" at an unrelated ad page.)"""
    obs = footer_observations(doc)
    if use_labels:
        label_to_index = {}
        for i in range(doc.page_count):
            lab = doc[i].get_label()
            if lab and lab.isdecimal():
                label_to_index.setdefault(int(lab), i)
        agree = sum(1 for i, n in obs if label_to_index.get(n) == i)
        if (len(label_to_index) >= max(10, doc.page_count // 2)
                and obs and agree / len(obs) >= 0.5):
            obs_set = set(obs)
            confirmed = {n: i for n, i in label_to_index.items() if (i, n) in obs_set}
            return PageMap(
                doc, labels=confirmed, first=min(confirmed), last=max(confirmed),
                description=(f"the PDF's own page labels: {len(confirmed)} confirmed by a "
                             f"visible footer number, printed {min(confirmed)}-{max(confirmed)} "
                             f"(labels agree with {agree}/{len(obs)} footer numbers)"))
    if not obs:
        raise RuntimeError(
            "Couldn't detect any page numbers in footers -- this assumes a "
            "printed page number appears near the bottom of most pages.")
    offsets = [i - n for i, n in obs]
    best_offset, _ = Counter(offsets).most_common(1)[0]
    valid = [n for (_, n), off in zip(obs, offsets) if off == best_offset]
    first, last = min(valid), max(valid)
    return PageMap(doc, offset=best_offset, first=first, last=last,
                   description=f"printed {first}-{last}, PDF page = printed + {best_offset + 1}")


def band_vote(doc, fraction=0.10):
    """The header-and-footer numbering detector (same model as
    page_labels.py): each page votes once per offset its top or bottom 10%
    shows a number for, so table values and years in the band can't swamp
    the vote the way a per-word count lets them. The valid printed range is
    every page the offset reaches, not just the ones with a visible number
    (chapter openers often have none)."""
    votes = defaultdict(set)
    for i, page in enumerate(doc):
        r = page.rect
        band = r.height * fraction
        for clip in (fitz.Rect(r.x0, r.y0, r.x1, r.y0 + band), fitz.Rect(r.x0, r.y1 - band, r.x1, r.y1)):
            for w in page.get_text("words", clip=clip):
                tok = w[4].strip(".,:;|()[]")
                if tok.isdecimal() and 1 <= int(tok) <= 999:
                    votes[i - int(tok)].add(i)
    if not votes:
        raise ValueError("Couldn't detect any page numbers in headers/footers")
    offset, pages = max(votes.items(), key=lambda kv: (len(kv[1]), -kv[0]))
    if len(pages) < 3:
        raise ValueError("Couldn't detect a consistent page numbering")
    first = max(1, -offset)
    last = doc.page_count - 1 - offset
    return PageMap(doc, offset=offset, first=first, last=last,
                   description=f"printed {first}-{last}, PDF page = printed + {offset + 1}")


# ---------------------------------------------------------------------------
# Finding the TOC and Index
# ---------------------------------------------------------------------------

def running_band_pages(doc, words):
    """Pages whose bottom band contains one of `words` -- GURPS repeats
    "Contents"/"Index" as running footer text on every page of the
    section."""
    found = set()
    for i in range(doc.page_count):
        tokens = {w[4].strip(".,:").lower() for w in bottom_words(doc[i])}
        if tokens & words:
            found.add(i)
    return found


def page_has_heading(page, keyword):
    """True if `keyword` ('contents', 'index') appears on this page as a
    section-opening heading, not body text or a TOC entry pointing at a
    different page. Books that print the word only once, as the section's
    title, need this instead of `running_band_pages()`. Three signals, all
    measured on real Mongoose PDFs:
      (a) an anomalously huge bounding box (>50pt tall) -- MuPDF
          mis-measuring a decorative section-title treatment; seen on the
          Index-opening page of Core Rulebook, High Guard and Companion.
      (b) a clearly larger-than-body word (>=17pt; body text ~13pt,
          headings ~19-21pt) in the top 40% of the page -- excludes "...dump
          its contents..." and an "Index . . . 184" TOC entry further down.
      (c) the word rendered twice at identical coordinates -- a faux-bold
          heading at body size (Aliens of Charted Space vol. 2's 14.7pt
          "CONTENTS"). Body text is never printed twice in the same place."""
    h = page.rect.height
    matches = [w for w in page_words(page) if w[4].strip(".,:").lower() == keyword]
    for w in matches:
        height = w[3] - w[1]
        if height > 50:
            return True
        if height >= 17 and w[1] < h * 0.4:
            return True
    for i, a in enumerate(matches):
        for b in matches[i + 1:]:
            if (abs(a[0] - b[0]) < 0.5 and abs(a[1] - b[1]) < 0.5
                    and abs(a[2] - b[2]) < 0.5 and abs(a[3] - b[3]) < 0.5):
                return True
    return False


def heading_pages(doc, keyword):
    return {i for i in range(doc.page_count) if page_has_heading(doc[i], keyword)}


def has_internal_links(doc, pages):
    """Any link to a page of this document. Named-destination links count
    too: InDesign exports its TOC and cross-reference links that way, and
    every one of 10 Delta Green test books had a TOC linked like that."""
    internal = (fitz.LINK_GOTO, fitz.LINK_NAMED)
    return any(link.get("kind") in internal for i in pages for link in doc[i].get_links())


def trailing_number_entries(page):
    """TOC entries in the 'Title . . . . 36' dot-leader format: each visual
    line's last word, if it's a bare page number. Returns (rect, number,
    text) with the rect over just the number. Skips the page's own footer
    band -- a running footer page number can otherwise pass for an entry."""
    words = page_words(page)
    h = page.rect.height
    lines = defaultdict(list)
    for w in words:
        if w[3] > h - FOOTER_BAND_POINTS:
            continue
        lines[(w[5], w[6])].append(w)
    results = []
    for key in sorted(lines.keys()):
        last_w = max(lines[key], key=lambda w: w[0])
        num_str = last_w[4].strip(".,")
        if num_str.isdecimal():
            results.append((word_rect(last_w), int(num_str), last_w[4]))
    return results


def links_cover_half(doc, pages):
    """Already linked if the existing links number at least half the
    entries `trailing_number_entries()` finds. (GURPS Space ships with a
    plain TOC; most GURPS books have a native linked one.)"""
    if not pages:
        return True
    candidates = sum(len(trailing_number_entries(doc[i])) for i in pages)
    links = sum(len(doc[i].get_links()) for i in pages)
    if candidates == 0:
        return True
    return links >= candidates * 0.5


LEADER_NUMBER = re.compile(r"^[.…\s]*(\d{1,3})$")
FRONT_MATTER_PAGES = 15
TOC_MIN_ENTRIES = 5
INDEX_MIN_NUMBERS = 20


def _line_groups(page):
    lines = defaultdict(list)
    for w in page_words(page):
        lines[(w[5], w[6])].append(w)
    return [sorted(ws, key=lambda w: w[7]) for ws in lines.values()]


def dot_leader_entries(page, last_printed):
    """TOC entries as whole lines, "Entry ....... 12": (rect over the line,
    printed number, report text). Handles the entry, leader and number
    coming out as one word ("Recovery..........75")."""
    entries = []
    for ws in _line_groups(page):
        if len(ws) == 1:
            m = re.match(r"^(.*?[^.…\s])[.…]{2,}(\d{1,3})$", ws[0][4])
            if m and int(m.group(2)) <= last_printed:
                entries.append((fitz.Rect(ws[0][:4]), int(m.group(2)), f"TOC: {m.group(1)[:60]}"))
            continue
        m = (LEADER_NUMBER.match(ws[-1][4].strip().lstrip(".").replace("…", ""))
             or re.match(r"^.*?\.{2,}(\d{1,3})$", ws[-1][4]))
        if not m:
            continue
        num = int(m.group(1))
        text_words = [w for w in ws[:-1] if w[4].strip(".…")]
        if not text_words or num > last_printed:
            continue
        rect = fitz.Rect(text_words[0][:4])
        for w in ws:
            rect |= fitz.Rect(w[:4])
        entries.append((rect, num, "TOC: " + " ".join(w[4] for w in text_words)[:60]))
    return entries


def front_matter_toc_pages(doc, last_printed):
    """TOC pages among the first 15: "Contents" near the top of a page with
    at least five dot-leader entries, plus the pages straight after it that
    also have five."""
    pages = []
    for i in range(min(doc.page_count, FRONT_MATTER_PAGES)):
        page = doc[i]
        has_heading = re.search(r"\bcontents\b", page.get_text()[:400], re.I) is not None
        if ((has_heading or (pages and pages[-1] == i - 1))
                and len(dot_leader_entries(page, last_printed)) >= TOC_MIN_ENTRIES):
            pages.append(i)
    return pages


def is_large_heading(page, word):
    """`word` set as a heading: at least 14pt and 80% of the page's largest text."""
    spans = [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
    if not spans:
        return False
    big = max(s["size"] for s in spans)
    return any(s["text"].strip().strip("/ ").lower() == word and s["size"] >= max(14, big * 0.8) for s in spans)


def page_numbers_on(page, own_number=None, band_fraction=0.10):
    """(word, number) for every page number on an Index page. Entries run
    right up into the header/footer band, so only the page's own printed
    number is skipped there, not every number in the band."""
    h = page.rect.height
    out = []
    for w in page_words(page):
        m = LIST_NUMBER.match(w[4])
        if not m:
            continue
        in_band = w[1] < h * band_fraction or w[3] > h * (1 - band_fraction)
        if in_band and int(m.group(1)) == own_number:
            continue
        out.append((w, int(m.group(1))))
    return out


def back_half_index_pages(doc):
    """The last "Index" heading in the second half of the book, plus the
    pages after it that still hold at least 20 page numbers."""
    start = next((i for i in range(doc.page_count - 1, doc.page_count // 2, -1)
                  if is_large_heading(doc[i], "index")), None)
    if start is None:
        return []
    pages = [start]
    for i in range(start + 1, doc.page_count):
        if len(page_numbers_on(doc[i])) < INDEX_MIN_NUMBERS:
            break
        pages.append(i)
    return pages


# ---------------------------------------------------------------------------
# Finding references
# ---------------------------------------------------------------------------

PP_SINGLE = re.compile(
    r'^\W*?(pp?)\.[' + re.escape(OTHER_SPACES) + NBSP + r']?([A-Z]{1,3})?(\d{1,4})'
    r'(?:[–—-]([A-Z]{1,3})?(\d{1,4}))?'
)
BARE_NUM = re.compile(r'^(\d{1,4})(?:[–—-](\d{1,4}))?[.,;)]*$')
WRAP_START = re.compile(r'^(\d{1,4})[–—-]$')
WRAP_CONT = re.compile(r'^(\d{1,4})')

# A page number prefixed with a book-code letter or two ("p. B123" = Basic
# Set p. 123, "p. MA45" = Martial Arts p. 45) is GURPS's shorthand for a
# page in a different book. `abbreviated_refs()` reports the code; it's up
# to the rule set whether that means "skip".
BOOK_CODE_TOKEN = re.compile(r'^([A-Z]{1,3})(\d{1,4})')


def resolve_range_end(num, raw_end):
    """Expand an abbreviated range end ('152-3' = 152-153, a British
    convention seen in the Traveller Companion). Only when the written end
    is shorter and smaller than the start, since a real range never goes
    backwards."""
    end = int(raw_end)
    if len(raw_end) < len(str(num)) and end < num:
        prefix_len = len(str(num)) - len(raw_end)
        candidate = int(str(num)[:prefix_len] + raw_end)
        if candidate > num:
            return candidate
    return end


def _parse_ref_number(nxt, resolve_ranges):
    """The word after 'p.'/'page' -> (num, book_code, range_end) or None."""
    code_m = BOOK_CODE_TOKEN.match(nxt)
    if code_m:
        return int(code_m.group(2)), code_m.group(1), None
    range_m = re.match(r'^(\d{1,4})[–—-](\d{1,4})', nxt)
    if range_m:
        num = int(range_m.group(1))
        end = resolve_range_end(num, range_m.group(2)) if resolve_ranges else int(range_m.group(2))
        return num, None, end
    nm = re.match(r'^(\d{1,4})', nxt)
    if nm:
        return int(nm.group(1)), None, None
    return None


def _ref_probes(text, num, range_end):
    """search_for() strings that trim a reference's rect to the reference
    itself, when the PDF glues following text into the same word ("(p. 10)
    - the" as one word, via narrow no-break spaces). The full range is
    tried first: the single-number form is a prefix of it and would
    otherwise always win, losing the range end."""
    clean_start = re.sub(r"^\W+", "", text).lower()
    probes = []
    if clean_start.startswith("page"):
        word_base = "pages" if clean_start.startswith("pages") else "page"
        if range_end is not None:
            probes.append(f"{word_base} {num}-{range_end}")
            probes.append(f"{word_base} {num}–{range_end}")
        probes.append(f"{word_base} {num}")
    else:
        base = "pp" if clean_start.startswith("pp") else "p"
        if range_end is not None:
            probes.append(f"{base}.{NBSP}{num}-{range_end})")
            probes.append(f"{base}.{NBSP}{num}–{range_end})")
            probes.append(f"{base}.{NBSP}{num}-{range_end}")
            probes.append(f"{base}.{NBSP}{num}–{range_end}")
        probes.append(f"{base}.{NBSP}{num})")
        probes.append(f"{base}.{NBSP}{num}")
    return probes


def abbreviated_refs(words, page_words=("p.", "pp."), resolve_ranges=False):
    """'p. 12' / 'pp. 12-13' / 'p.12' (one word) style references, plus
    'page 12' / 'pages 12-13' when `page_words` includes them. One link per
    reference, to its first page. Returns (refs, consumed word indices)."""
    refs = []
    consumed = set()
    j = 0
    while j < len(words):
        tok = words[j][4]
        m = PP_SINGLE.match(tok)
        if m:
            num = int(m.group(3))
            if m.group(5):
                end = resolve_range_end(num, m.group(5)) if resolve_ranges else int(m.group(5))
            else:
                end = None
            refs.append(_abbrev_ref(words, j, j, num, m.group(2), end))
            consumed.add(j)
            j += 1
            continue
        bare_tok = re.sub(r'^\W+', '', tok).lower()
        if bare_tok in page_words and j + 1 < len(words):
            parsed = _parse_ref_number(words[j + 1][4], resolve_ranges)
            if parsed:
                num, code, end = parsed
                refs.append(_abbrev_ref(words, j, j + 1, num, code, end))
                consumed.update((j, j + 1))
                j += 2
                continue
        j += 1
    return refs, consumed


def _abbrev_ref(words, wi, wj, num, code, range_end):
    text = " ".join(w[4] for w in words[wi:wj + 1])
    if abs(words[wi][1] - words[wj][1]) > 2:
        # "page" at the end of one line, the number on the next: one link
        # per line. (One rect from the first word's corner to the last
        # word's would span both lines and everything between.)
        return Ref(wi, wj, text, [(word_rect(words[wi]), num), (word_rect(words[wj]), num)], book_code=code)
    rect = fitz.Rect(words[wi][0], words[wi][1], words[wj][2], words[wj][3])
    return Ref(wi, wj, text, [(rect, num)], probes=_ref_probes(text, num, range_end), book_code=code)


def bare_number_refs(words, page_height, consumed):
    """Index entries: bare 'Term, 24.' numbers and ranges, plus a range
    wrapped across lines ('418-' / '425,'), whose two halves each get their
    own link to the first page. Skips the page's own footer band and any
    word a body reference already used."""
    refs = []
    consumed = set(consumed)
    for i, w in enumerate(words):
        if i in consumed:
            continue
        m = WRAP_START.match(w[4])
        if not m or i + 1 >= len(words) or (i + 1) in consumed:
            continue
        if not WRAP_CONT.match(words[i + 1][4]):
            continue
        num = int(m.group(1))
        for k in (i, i + 1):
            refs.append(Ref(k, k, words[k][4], [(word_rect(words[k]), num)], is_index=True))
        consumed.update((i, i + 1))
    for i, w in enumerate(words):
        if i in consumed or w[3] > page_height - FOOTER_BAND_POINTS:
            continue
        m = BARE_NUM.match(w[4])
        if m:
            refs.append(Ref(i, i, w[4], [(word_rect(w), int(m.group(1)))], is_index=True))
    return refs


LIST_NUMBER = re.compile(r"^\(?(\d{1,3})(?:[–—-](\d{1,3}))?[.,;:)!?’”]*$")


def page_word_list_refs(words, reference_words=("page", "pages", "p.", "pp."), skip_capitalized=True):
    """'page 48' / '(page 164)' / 'pages 12-13' / 'pages 294, 296 and 300'
    references: a reference word followed by one or more numbers, each linked
    on its own (the first together with the reference word, when they share
    a line). With `skip_capitalized`, a capitalized 'Page'/'PAGE' is skipped --
    in Delta Green that's an in-fiction document's own pagination."""
    page_word = re.compile(r"^\(?(?:" + "|".join(re.escape(w) for w in reference_words) + r")$", re.I)
    refs = []
    i = 0
    while i < len(words):
        word = words[i][4]
        bare = word.lstrip("(")  # "(Page 3 of 5)" is capitalized too
        if not page_word.match(word) or (skip_capitalized and bare[:1].isupper()
                                         and bare.lower().startswith("page")):
            i += 1
            continue
        nums = []
        j = i + 1
        while j < len(words):
            m = LIST_NUMBER.match(words[j][4])
            if not m:
                break
            nums.append((j, int(m.group(1))))
            j += 1
            if words[j - 1][4].endswith(","):
                continue
            if (j + 1 < len(words) and words[j][4].lower() in ("and", "or", "&")
                    and LIST_NUMBER.match(words[j + 1][4])):
                j += 1
                continue
            break
        if not nums:
            i += 1
            continue
        last = nums[-1][0]
        text = " ".join(w[4] for w in words[i:last + 1])
        links = []
        for n, (k, num) in enumerate(nums):
            rect = word_rect(words[k])
            if n == 0 and abs(words[i][1] - words[k][1]) < 2:  # link "page 48" as one
                rect |= word_rect(words[i])
            links.append((rect, num))
        refs.append(Ref(i, last, text, links))
        i = last + 1
    return refs


# ---------------------------------------------------------------------------
# Chapter references ("Chapter 3", "Chapters 2, 4, and 6")
# ---------------------------------------------------------------------------

CHAPTER_WORD_PAT = re.compile(r'^\W*(Chapters?)$')
CHAPTER_HYPHEN_START_PAT = re.compile(r'^\W*Chap\w*[\xad-]$')  # 'Chap\xad' or 'Chapt-'


def extract_chapters(doc, toc_pages, target):
    """(chapter_number, title, pdf_index) from the book's own TOC: top-level
    entries are lines whose first word is a lone 'N.' ('11. Combat . . .
    362'), the page number possibly on one of the next few lines."""
    chapters = []
    for i in sorted(toc_pages):
        lines = defaultdict(list)
        for w in page_words(doc[i]):
            lines[(w[5], w[6])].append(w)
        line_words = [sorted(lines[k], key=lambda w: w[0]) for k in sorted(lines)]

        def strip_trailing_number(tw):
            while tw and re.match(r'^\.+$', tw[-1]):
                tw = tw[:-1]
            if tw and tw[-1].strip(".,").isdecimal():
                return tw[:-1], tw[-1].strip(".,")
            return tw, None

        idx = 0
        while idx < len(line_words):
            m = re.match(r'^(\d{1,2})\.$', line_words[idx][0][4])
            if not m:
                idx += 1
                continue
            clean, pagenum = strip_trailing_number([w[4] for w in line_words[idx][1:]])
            scan_idx = idx
            while pagenum is None and scan_idx + 1 < len(line_words):
                scan_idx += 1
                nclean, pagenum = strip_trailing_number([w[4] for w in line_words[scan_idx]])
                clean += nclean
                if scan_idx - idx > 3:
                    break
            title = " ".join(w for w in clean if not re.match(r'^\.+$', w))
            title = re.sub(r"\s+", " ", title).strip(" .")
            if pagenum and title:
                t = target(int(pagenum))
                if t is not None:
                    chapters.append((int(m.group(1)), title, t))
            idx = scan_idx + 1
    return chapters


def _match_chapter_word(words, i):
    """'Chapter'/'Chapters' at word i, possibly with leading punctuation
    ('(Chapter') or split by a line-wrap hyphen ('Chap\xad' + 'ter').
    Returns its last word index, or None."""
    tok = words[i][4]
    if CHAPTER_WORD_PAT.match(tok):
        return i
    if CHAPTER_HYPHEN_START_PAT.match(tok) and i + 1 < len(words):
        combined = re.sub(r"[\xad-]$", "", re.sub(r"^\W+", "", tok)) + words[i + 1][4]
        if re.match(r"^(Chapters?)\b", combined):
            return i + 1
    return None


def chapter_number_refs(words):
    """'Chapter N' / 'Chapters N-M' / 'Chapters N and M' / 'Chapters N, M,
    and L' -> (chapter_word_idx, end_idx, groups), groups being (word_idx,
    [numbers]) per number word. A hyphen range is one group with two
    numbers; each member of a list is its own group, linked on its own.
    (Matching italic chapter *titles* instead was tried first and gave too
    many false positives from ordinary prose and stat-block labels.)"""
    refs = []
    i = 0
    while i < len(words):
        chap_end = _match_chapter_word(words, i)
        if chap_end is None:
            i += 1
            continue
        j = chap_end + 1
        num_m = re.match(r'^(\d{1,2})', words[j][4]) if j < len(words) else None
        if not num_m:
            i = chap_end + 1
            continue
        groups = [(j, [int(num_m.group(1))])]
        end_idx = j
        range_m = re.match(r'^[–—-](\d{1,2})', words[j][4][num_m.end():])
        if range_m:
            groups[0][1].append(int(range_m.group(1)))
        else:
            k = j + 1
            while k < len(words):
                tok = words[k][4]
                if tok.lower() == "and" and k + 1 < len(words):
                    m2 = re.match(r'^(\d{1,2})', words[k + 1][4])
                    if m2:
                        groups.append((k + 1, [int(m2.group(1))]))
                        end_idx = k + 1
                        k += 2
                        continue
                    break
                m3 = re.match(r'^(\d{1,2}),?$', tok)
                if m3:
                    groups.append((k, [int(m3.group(1))]))
                    end_idx = k
                    k += 1
                    continue
                break
        refs.append((i, end_idx, groups))
        i = end_idx + 1
    return refs


# ---------------------------------------------------------------------------
# Shared cross-book helpers
# ---------------------------------------------------------------------------

_TRIGGER_STOPWORDS = {"the", "a", "an", "of", "and"}



def detect_trigger_word(doc, default, strip_series_prefix=False, copyright_fallback=False):
    """The word that marks a citation of another book in the same line
    ('GURPS Magic, p. 12' -> 'gurps'), taken from the PDF's own title
    rather than hardcoded, skipping a leading article.

    `strip_series_prefix` drops a 'Book 9: ' / 'Alien Module 1: ' prefix
    first (Mongoose 1e titles), and a filename-shaped title ('High Guard
    Cover.indd', an InDesign export) is treated as no title. With
    `copyright_fallback`, a missing title falls back to the word before a
    '©' in the first six pages ('Traveller ©2026 Mongoose...',
    '2300AD ©2021...'); off for GURPS, whose copyright line would give
    'copyright'. Otherwise `default`."""
    title = (doc.metadata or {}).get("title", "") or ""
    if strip_series_prefix:
        title = re.sub(r"^\s*(?:[A-Za-z]+\s+){1,3}?\d+\s*:\s*", "", title)
    words = [] if is_filename_title(title) else [w.lower() for w in re.findall(r"[A-Za-z']+", title)]
    words = [w for w in words if w not in _TRIGGER_STOPWORDS]
    if words:
        return words[0]
    if copyright_fallback:
        for i in range(min(6, doc.page_count)):
            # \b plus a leading digit, so "2300AD ©2021" gives "2300ad", not "ad".
            m = re.search(r"\b([A-Za-z0-9][A-Za-z0-9']*)\s*(?:©|\(c\))", doc[i].get_text())
            if m:
                return m.group(1).lower()
    return default


def same_book_vocabulary(doc, toc_pages, index_pages):
    """This book's own TOC section titles and Index terms, normalized."""
    vocab = set()
    for i in toc_pages:
        lines = defaultdict(list)
        for w in page_words(doc[i]):
            lines[(w[5], w[6])].append(w)
        for key in sorted(lines):
            ws = sorted(lines[key], key=lambda w: w[0])
            norm = normalize_title(" ".join(w[4] for w in ws if not w[4].strip(".,").isdigit()).strip(" ."))
            if len(norm) > 2:
                vocab.add(norm)
    for i in index_pages:
        for line in doc[i].get_text().split("\n"):
            m = re.match(r"^([A-Za-z][A-Za-z0-9 ,'\"\-/&]*?)(?:,?\s*\d)", line.strip())
            if m:
                norm = normalize_title(m.group(1))
                if len(norm) > 2:
                    vocab.add(norm)
    return vocab


# ---------------------------------------------------------------------------
# Rule sets
# ---------------------------------------------------------------------------

class RuleSet:
    """A publisher's rules. A rule set is a module in `hyperlink_rules/`
    defining `Rules`, a subclass of this, that mostly just sets the
    attributes below. Methods can be overridden too, for something the
    settings can't express -- a new cross-book check that every rule set
    could use belongs in hyperlink_checks.py instead.

    A fresh instance is made for every book."""

    # Shown in the UIs and `--list`.
    label = ""
    description = ""

    # Printed page number -> PDF page:
    #   "footer"            digits in the bottom 45pt, one vote per number
    #   "footer_or_labels"  the same, but the PDF's own /PageLabels when the
    #                       footers confirm them (two numbering runs in one PDF)
    #   "header_and_footer" digits in the top and bottom 10%, one vote per page
    page_numbers = "header_and_footer"

    # Finding the TOC:
    #   "footer_word"  "Contents" in the bottom band of every TOC page (GURPS)
    #   "heading"      "Contents" as a heading (size, position or faux bold)
    #   "front_matter" "Contents" near the top of one of the first 15 pages
    #                  with at least five dot-leader entries, and the pages after
    toc = "heading"
    # Finding the Index: "footer_word", "heading", or "back_half_heading"
    # (the last large "Index" heading in the second half, and the pages after
    # it that still hold at least 20 page numbers).
    index = "heading"

    # TOC entries: "trailing_number" (link each line's last number) or
    # "dot_leader_lines" (link the whole "Entry ..... 12" line).
    toc_entries_style = "trailing_number"
    # Leave the TOC alone when "half_linked" (existing links number at least
    # half its entries) or "any_link" (it has any internal link at all).
    toc_linked_when = "any_link"
    # Leave the Index alone if it has any internal link.
    index_skip_if_linked = False
    # Index numbers: "outside_footer" (bare numbers and wrapped ranges, none
    # in the bottom 45pt) or "all_but_own_number" (every number, except the
    # page's own printed number in the header/footer band).
    index_numbers = "outside_footer"

    # Body references:
    #   "single" a reference word and one number or range ("p. 12",
    #          "pp. 12-13", "page 12"), plus "p.12" written as one word
    #   "list"   a reference word and every number after it ("pages 12, 14
    #          and 16"), each linked
    reference_style = "single"
    reference_words = ("p.", "pp.")
    # "152-3" means 152-153 (British style). Only affects how the link's
    # rect is trimmed; the link goes to the first page either way.
    expand_short_ranges = False
    # Skip a capitalized "Page"/"PAGE" ("list" style only).
    skip_capitalized_reference_word = False
    # Link "Chapter 3" / "Chapters 2, 4, and 6" to chapters found in the TOC.
    chapter_refs = False
    # Look for body references on TOC and Index pages too.
    body_refs_on_toc_and_index = True
    # Fraction trimmed off a link's top and bottom; tightly set index lines
    # otherwise overlap, and a click on the boundary hits the wrong line.
    link_inset = 0.0
    # Report status for a TOC link.
    toc_status = "added (TOC entry)"

    # The word that marks a citation of another book in the same line
    # ("GURPS Magic, p. 12"), from the PDF's title: None, or keyword
    # arguments for detect_trigger_word() ({"default": "gurps"}).
    trigger_word = None
    # The product line's name, removed from this book's own title so
    # "page N of <this book>" is recognised ("Delta Green").
    line_name = ""
    # Titles of other books this publisher cites by name.
    known_titles = ()
    # hyperlink_checks checks, in order; the first that matches skips the
    # reference. Only those marked on_index also run on Index entries.
    other_book_checks = ()
    # The same, for "Chapter N" references.
    chapter_checks = ()

    def page_map(self, doc):
        if self.page_numbers == "footer":
            return footer_vote(doc)
        if self.page_numbers == "footer_or_labels":
            return footer_vote(doc, use_labels=True)
        if self.page_numbers == "header_and_footer":
            return band_vote(doc)
        raise ValueError(f"unknown page_numbers {self.page_numbers!r}")

    def toc_pages(self, book):
        return self._section_pages(book, self.toc, "contents")

    def index_pages(self, book):
        return self._section_pages(book, self.index, "index")

    def _section_pages(self, book, how, word):
        if how is None:
            return set()
        if how == "footer_word":
            return running_band_pages(book.doc, {word})
        if how == "heading":
            return heading_pages(book.doc, word)
        if how == "front_matter" and word == "contents":
            return front_matter_toc_pages(book.doc, book.pages.last)
        if how == "back_half_heading" and word == "index":
            return back_half_index_pages(book.doc)
        raise ValueError(f"unknown {'toc' if word == 'contents' else 'index'} setting {how!r}")

    def toc_already_linked(self, book, pages):
        if self.toc_linked_when == "half_linked":
            return links_cover_half(book.doc, pages)
        return has_internal_links(book.doc, pages)

    def toc_entries(self, book, pt):
        """(rect, printed number, report text) per TOC entry."""
        if self.toc_entries_style == "dot_leader_lines":
            return dot_leader_entries(pt.page, book.pages.last)
        return trailing_number_entries(pt.page)

    def index_already_linked(self, book, pages):
        return self.index_skip_if_linked and has_internal_links(book.doc, pages)

    def body_refs(self, book, pt):
        """-> (list of Ref, set of word indices they used)."""
        if self.reference_style == "list":
            return page_word_list_refs(pt.words, self.reference_words,
                                       self.skip_capitalized_reference_word), set()
        return abbreviated_refs(pt.words, self.reference_words, self.expand_short_ranges)

    def index_refs(self, book, pt, consumed):
        if self.index_numbers == "all_but_own_number":
            own = book.pages.printed(pt.number)
            return [Ref(0, 0, f"Index: {w[4]}", [(word_rect(w), num)], is_index=True)
                    for w, num in page_numbers_on(pt.page, own_number=own)]
        return bare_number_refs(pt.words, pt.page.rect.height, consumed)

    def cross_book(self, book, pt, ref):
        """A reason string if `ref` points into another book, else None."""
        for check in self.other_book_checks:
            if ref.is_index and not check.on_index:
                continue
            reason = check(book, pt, ref)
            if reason:
                return reason
        return None

    def chapter_cross_book(self, book, pt, ref):
        for check in self.chapter_checks:
            reason = check(book, pt, ref)
            if reason:
                return reason
        return None


def available_rule_sets():
    """{name: (Rules class or None, error)} for every module in
    hyperlink_rules/ -- see that package."""
    import hyperlink_rules
    return hyperlink_rules.discover()


def load_rules(name):
    found = available_rule_sets()
    rules, error = found.get(name, (None, None))
    if rules is None:
        if error:
            raise ValueError(f"hyperlink rule set {name!r} failed to load: {error}")
        known = ", ".join(sorted(n for n, (r, _) in found.items() if r is not None))
        raise ValueError(f"unknown hyperlink rule set {name!r} (known: {known})")
    return rules


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def rects_meaningfully_overlap(a, b):
    """The overlap covers more than 30% of the smaller rect. Adjacent
    lines' word boxes routinely touch or overlap by a fraction of a point,
    so a plain intersects() test treated references next to a link on the
    line above or below as already linked. Measured against the smaller
    rect, not the new one: a publisher's link often covers just the number
    ("13") of a reference this engine would link as "page 13", which is
    under half of the new rect but still means the reference is linked
    (Delta Green Agent's Handbook, linked with named destinations)."""
    ix0, iy0 = max(a.x0, b.x0), max(a.y0, b.y0)
    ix1, iy1 = min(a.x1, b.x1), min(a.y1, b.y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    smaller = max(min(a.get_area(), b.get_area()), 1e-6)
    return (ix1 - ix0) * (iy1 - iy0) / smaller > 0.3


def is_reasonable_link_rect(rect, page):
    """Reject rects implausibly large for a short reference -- a rare
    extraction anomaly reports one 'word' (an image caption glued to a page
    reference) spanning a whole column."""
    return rect.height <= MAX_LINK_HEIGHT and rect.width <= page.rect.width * MAX_LINK_WIDTH_FRAC


def refine_rect(page, rect, probes):
    """Snap `rect` to the first probe text found on the same line, inside
    the reference's own words. A hit elsewhere on the line doesn't count:
    "p. 34" also matches the start of "p. 348" further along, and the old
    scripts linked that one to page 34 (seen in the combined Basic Set)."""
    for probe in probes:
        near = [r for r in page_search(page, probe) if abs(r.y0 - rect.y0) < 2 and r.intersects(rect)]
        if near:
            return near[0]
    return rect


class Book:
    """One run over one PDF."""

    def __init__(self, doc, rules, source_path):
        self.doc = doc
        self.rules = rules
        self.source_path = Path(source_path)  # the input, not the copy being linked
        self.pages = None
        self.toc_pages = set()
        self.index_pages = set()
        self.counts = Counter()
        self.report = []
        self.trigger = None
        self.own_title_key = ""
        self.own_known_title_key = ""
        self.known_titles_norm = frozenset(normalize_title(t) for t in rules.known_titles)
        self.known_title_keys = frozenset(title_key(t) for t in rules.known_titles)
        self._vocab = None

    @property
    def vocab(self):
        """This book's own TOC section titles and Index terms, normalized
        (built on first use)."""
        if self._vocab is None:
            self._vocab = frozenset(same_book_vocabulary(self.doc, self.toc_pages, self.index_pages))
        return self._vocab

    def _own_title(self):
        """This book's own title, letters only, minus the line's name --
        for recognising "page N of <this book>"."""
        title = (self.doc.metadata or {}).get("title") or ""
        if is_filename_title(title):  # "Agents Handbook.indd": no title
            title = ""
        key = title_key(title) or title_key(self.source_path.stem)
        line = title_key(self.rules.line_name)
        return (key.replace(line, "") or key) if line else key

    def target(self, n):
        return self.pages.target(n)

    def _own_known_title(self):
        """The longest of the rule set's `known_titles` this book's own title
        contains, compared without the line's name (own_title_key has it
        stripped): "Shadowrun, Sixth World" for the core rulebook, but
        "Sixth World Companion" -- the longer match -- for the Companion,
        whose title contains "Sixth World" too. Empty if none."""
        line = title_key(self.rules.line_name)

        def bare(key):
            return (key.replace(line, "") or key) if line else key

        found = [k for k in self.known_title_keys if bare(k) and bare(k) in self.own_title_key]
        return max(found, key=lambda k: len(bare(k)), default="")

    def skip(self, pt, text, num, counter, reason):
        self.counts[counter] += 1
        self.report.append((pt.number + 1, text, num, "skipped", reason))

    def link(self, pt, rect, num, text, kind, probes=(), status="added"):
        """Insert one link from `rect` to printed page `num`, unless it's
        out of range, implausibly large, or already linked. The overlap
        check runs on the final rect, after search_for() refinement: on a
        page that prints a line twice (a faux-bold effect), two references
        can refine onto the same rect and would otherwise stack two links."""
        idx = self.target(num)
        if idx is None:
            self.skip(pt, text, num, "skipped_range", "out of page-number range")
            return False
        rect = refine_rect(pt.page, rect, probes)
        if not is_reasonable_link_rect(rect, pt.page):
            self.skip(pt, text, num, "skipped_size",
                      "rejected: implausibly large link rect (likely a source PDF text-extraction anomaly)")
            return False
        if any(rects_meaningfully_overlap(rect, r) for r in pt.existing):
            self.skip(pt, text, num, "skipped_existing", "already linked")
            return False
        if self.rules.link_inset:
            inset = rect.height * self.rules.link_inset
            rect = fitz.Rect(rect.x0, rect.y0 + inset, rect.x1, rect.y1 - inset)
        pt.page.insert_link({"kind": fitz.LINK_GOTO, "page": idx, "from": rect, "to": fitz.Point(0, 0)})
        pt.existing.append(rect)
        self.counts[kind] += 1
        self.report.append((pt.number + 1, text, num, status, f"pdf page {idx + 1}"))
        return True

    def run(self, log=print):
        rules, doc = self.rules, self.doc
        self.pages = rules.page_map(doc)
        log(f"Page numbering: {self.pages.description}")
        self.toc_pages = set(rules.toc_pages(self))
        self.index_pages = set(rules.index_pages(self))
        log(f"Table of Contents: {_describe_pages(self.toc_pages)}")
        log(f"Index: {_describe_pages(self.index_pages)}")
        if rules.trigger_word is not None:
            self.trigger = detect_trigger_word(doc, **rules.trigger_word)
            log(f"Other-book trigger word: {self.trigger!r}")
        self.own_title_key = self._own_title()
        self.own_known_title_key = self._own_known_title()

        if self.toc_pages and rules.toc_already_linked(self, self.toc_pages):
            log("  Table of Contents already has links -- leaving it alone")
        else:
            for i in sorted(self.toc_pages):
                pt = PageText(doc[i])
                for rect, num, text in rules.toc_entries(self, pt):
                    self.link(pt, rect, num, text, "toc_added", status=rules.toc_status)

        link_index = bool(self.index_pages) and not rules.index_already_linked(self, self.index_pages)
        if self.index_pages and not link_index:
            log("  Index already has links -- leaving it alone")

        chapters = {}
        if rules.chapter_refs:
            found = extract_chapters(doc, self.toc_pages, self.target)
            chapters = {num: t for num, _, t in found}
            log(f"  Detected {len(found)} chapter(s) from the TOC")

        for page in doc:
            i = page.number
            special = i in self.toc_pages or i in self.index_pages
            pt = PageText(page)
            if not pt.words:
                continue
            refs, consumed = [], set()
            if rules.body_refs_on_toc_and_index or not special:
                refs, consumed = rules.body_refs(self, pt)
            if i in self.index_pages and link_index:
                refs = refs + rules.index_refs(self, pt, consumed)
            for ref in refs:
                self._link_ref(pt, ref)
            if chapters and not special:
                self._link_chapters(pt, chapters)

        return {
            "added": self.counts["added"] + self.counts["index_added"],
            "chapter_added": self.counts["chapter_added"],
            "toc_added": self.counts["toc_added"],
        }

    def _link_ref(self, pt, ref):
        # Already-linked references are left to link(), which checks overlap
        # on the trimmed rect and writes a report row. (An earlier check here
        # used the untrimmed rect and counted the skip without a report row.)
        reason = self.rules.cross_book(self, pt, ref)
        if reason:
            for _, num in ref.links:
                self.skip(pt, ref.text, num, "skipped_title", reason)
            return
        kind = "index_added" if ref.is_index else "added"
        for rect, num in ref.links:
            self.link(pt, rect, num, ref.text, kind, ref.probes)

    def _link_chapters(self, pt, chapters):
        page, words, i = pt.page, pt.words, pt.number
        for wi, end_idx, groups in chapter_number_refs(words):
            primary_word_idx, primary_nums = groups[0]
            valid_nums = [n for n in primary_nums if n in chapters]
            if not valid_nums:
                continue
            target = chapters[valid_nums[0]]
            run_text = " ".join(w[4] for w in words[wi:end_idx + 1])
            base = "Chapters" if re.sub(r"^\W+", "", words[wi][4]).lower().startswith("chapters") else "Chapter"

            reason = self.rules.chapter_cross_book(self, pt, Ref(wi, end_idx, run_text, []))
            if reason:
                self.skip(pt, run_text, "", "chapter_skipped_title", f"chapter ref, but {reason}")
                continue

            # "Chapters" through the first number, one rect per line so a
            # wrapped reference doesn't get a rect spanning both lines.
            if target != i:  # don't link a chapter to its own opening page
                line_groups, cur_key, cur = [], None, []
                for w in words[wi:primary_word_idx + 1]:
                    key = (w[5], w[6])
                    if key != cur_key and cur:
                        line_groups.append(cur)
                        cur = []
                    cur_key = key
                    cur.append(w)
                if cur:
                    line_groups.append(cur)
                rects = [fitz.Rect(min(w[0] for w in g), min(w[1] for w in g),
                                   max(w[2] for w in g), max(w[3] for w in g)) for g in line_groups]
                probes = []
                if len(valid_nums) >= 2:
                    n1, n2 = valid_nums[0], valid_nums[1]
                    probes += [f"{base} {n1}-{n2}", f"{base} {n1}–{n2}", f"{base} {n1} and {n2}"]
                probes.append(f"{base} {valid_nums[0]}")
                for probe in probes:
                    hits = [r for r in page_search(page, probe)
                            if any(abs(r.y0 - g.y0) < 2 and r.intersects(g) for g in rects)]
                    if hits:
                        rects = hits
                        break
                if (all(is_reasonable_link_rect(r, page) for r in rects)
                        and not any(rects_meaningfully_overlap(rect, r) for rect in rects for r in pt.existing)):
                    for rect in rects:
                        page.insert_link({"kind": fitz.LINK_GOTO, "page": target, "from": rect,
                                          "to": fitz.Point(0, 0)})
                    pt.existing.extend(rects)
                    self.counts["chapter_added"] += 1
                    self.report.append((i + 1, run_text, "", "added (chapter ref)", f"pdf page {target + 1}"))

            # Further list members ("Chapters 2, 4, and 6": the 4 and the 6).
            for word_idx, nums in groups[1:]:
                extra = [n for n in nums if n in chapters]
                if not extra or chapters[extra[0]] == i:
                    continue
                extra_target = chapters[extra[0]]
                w = words[word_idx]
                rect = word_rect(w)
                digits = re.match(r"^\d{1,2}", re.sub(r"^\W+", "", w[4]))
                if digits:
                    rect = refine_rect(page, rect, [digits.group(0)])
                if not is_reasonable_link_rect(rect, page):
                    continue
                if any(rects_meaningfully_overlap(rect, r) for r in pt.existing):
                    continue
                page.insert_link({"kind": fitz.LINK_GOTO, "page": extra_target, "from": rect,
                                  "to": fitz.Point(0, 0)})
                pt.existing.append(rect)
                self.counts["chapter_added"] += 1
                self.report.append((i + 1, w[4], "", "added (chapter ref, list member)",
                                    f"pdf page {extra_target + 1}"))


def _describe_pages(pages):
    if not pages:
        return "not found"
    pages = sorted(pages)
    if pages == list(range(pages[0], pages[-1] + 1)) and len(pages) > 2:
        return f"PDF pages {pages[0] + 1}-{pages[-1] + 1}"
    return "PDF pages " + ", ".join(str(p + 1) for p in pages)


def hyperlink_pdf(in_path, out_path, rules, log=print):
    """Copy `in_path` to `out_path`, add links there with rule set `rules`
    (a module name in hyperlink_rules/, or a RuleSet subclass -- e.g. a
    draft from hyperlink_survey.py that isn't installed yet), and write `<out stem>_link_report.csv` next to
    it. Progress goes to `log`. Returns {"added", "chapter_added",
    "toc_added"} (Index links count as "added"), plus "counts": every
    counter on its own ("index_added", "skipped_size", ...), for a caller
    that needs them apart (hyperlink_survey.py's trial run)."""
    rules_cls = rules if isinstance(rules, type) and issubclass(rules, RuleSet) else load_rules(rules)
    if Path(in_path).resolve() == Path(out_path).resolve():
        raise ValueError("output path must be different from the input path")
    shutil.copyfile(in_path, out_path)
    doc = fitz.open(out_path)
    book = Book(doc, rules_cls(), in_path)
    try:
        result = book.run(log)
        doc.saveIncr()
    finally:
        doc.close()

    report_path = Path(out_path).with_name(Path(out_path).stem + "_link_report.csv")
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["PDF Page", "Matched Text", "Ref Number", "Status", "Detail"])
        writer.writerows(tuple(csv_safe(v) for v in row) for row in book.report)

    c = book.counts
    log(f"Added {c['added']} page-reference links")
    log(f"  Skipped {c['skipped_existing']} (already linked)")
    log(f"  Skipped {c['skipped_title']} (reference to another book)")
    log(f"  Skipped {c['skipped_range']} (out of page-number range)")
    if c["index_added"]:
        log(f"Added {c['index_added']} Index links")
    if rules_cls.chapter_refs:
        log(f"Added {c['chapter_added']} chapter links "
              f"(skipped {c['chapter_skipped_title']} citing another book)")
    log(f"Added {c['toc_added']} Table of Contents links")
    log(f"Wrote {out_path}")
    log(f"Wrote report: {report_path}")
    return {**result, "counts": dict(c)}


def main():
    parser = argparse.ArgumentParser(description="Hyperlink a PDF's own page, chapter, TOC and Index references.")
    parser.add_argument("--rules", help="publisher rule set (a module name in hyperlink_rules/)")
    parser.add_argument("--list", action="store_true", help="list the available rule sets and exit")
    parser.add_argument("input", nargs="?")
    parser.add_argument("output", nargs="?")
    args = parser.parse_args()
    if args.list:
        found = available_rule_sets()
        width = max(map(len, found), default=0)
        for name, (rules, error) in sorted(found.items()):
            print(f"{name:{width}}  {rules.label} -- {rules.description}" if rules else f"{name:{width}}  FAILED TO LOAD: {error}")
        return
    if not (args.rules and args.input and args.output):
        parser.error("--rules, INPUT and OUTPUT are required (or --list)")
    try:
        hyperlink_pdf(args.input, args.output, args.rules)
    except (ValueError, RuntimeError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
