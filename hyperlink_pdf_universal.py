#!/usr/bin/env python3
"""
hyperlink_pdf_v2_chapters.py -- Everything hyperlink_pdf.py does, PLUS:
detects references to this book's own CHAPTER NAMES (e.g. an italicized
"Combat" or "Tactical Combat" appearing in running text, with or
without a page number nearby) and links them to that chapter's actual
starting page.

Chapter names are extracted straight from this book's own Table of
Contents (top-level numbered entries like "11. Combat . . . 362"), not
hardcoded -- so this should adapt to a different GURPS book's own
chapter list automatically.

Matching is deliberately conservative to avoid false positives:
  - Only a CONTIGUOUS italicized run whose text EXACTLY equals a
    chapter title counts as a match -- not a substring. This is what
    keeps "Combat Reflexes" or "Close Combat" from falsely matching
    the "Combat" chapter; the full italic span has to equal "Combat"
    with nothing else italicized alongside it.
  - Reuses the same "GURPS <Title>" cross-book detection as the page-
    number linker -- "GURPS Magic" (a different book) won't get linked
    to this book's own "Magic" chapter.
  - Never creates a link where one already exists (same rule as the
    rest of the pipeline), so a chapter name immediately next to an
    already-linked page reference doesn't get double-linked.

USAGE:
    python3 hyperlink_pdf_v2_chapters.py INPUT.pdf OUTPUT.pdf

REQUIREMENTS:
    pip install pymupdf
"""

import sys
import re
import csv
import shutil
import argparse
from pathlib import Path
import pymupdf as fitz  # PyMuPDF -- `import fitz` is the deprecated alias
from collections import Counter


NBSP = "\u202f"
OTHER_SPACES = " \u00a0\u2008"  # regular space, nbsp, punctuation space
DEFAULT_TRIGGER_WORD = "gurps"   # fallback if title-based auto-detection below fails
TITLE_TRIGGER_WORD = DEFAULT_TRIGGER_WORD
_TRIGGER_STOPWORDS = {"the", "a", "an", "of", "and"}


def detect_title_trigger_word(doc):
    """Derive the word that flags a reference to a DIFFERENT book from this
    PDF's OWN title metadata (e.g. 'GURPS Basic Set, Fourth Edition
    Revised' -> 'gurps'), instead of a hardcoded brand name. This is what
    makes the cross-book filter work on a book from a different publisher
    without editing the script. Skips a leading article/connector word so
    a title like 'The Great Wide World' picks 'great', not 'the'.
    Returns None if the PDF has no usable title -- callers should treat
    that as "cross-book filtering disabled" rather than guessing, since a
    wrong trigger word actively suppresses real links."""
    title = (doc.metadata or {}).get("title", "") or ""
    words = [w.lower() for w in re.findall(r"[A-Za-z']+", title)]
    words = [w for w in words if w not in _TRIGGER_STOPWORDS]
    return words[0] if words else None
INDEX_HEADER_WORDS = {"index"}   # footer/header word(s) that mark an Index page
TITLE_CONNECTORS = {"and", "of", "the", "in", "for", "to", "a", "an", "or", "&"}
LOOKBACK_WORDS_FOR_TITLE = 10

PP_SINGLE = re.compile(
    r'^\W*?(pp?)\.[' + re.escape(OTHER_SPACES) + NBSP + r']?([A-Z]{1,3})?(\d{1,4})'
    r'(?:[\u2013\u2014-]([A-Z]{1,3})?(\d{1,4}))?'
)
BARE_NUM = re.compile(r'^(\d{1,4})(?:[\u2013\u2014-](\d{1,4}))?[.,;)]*$')
WRAP_START = re.compile(r'^(\d{1,4})[\u2013\u2014-]$')
WRAP_CONT = re.compile(r'^(\d{1,4})')

# GURPS's own formal cross-reference shorthand: a page reference prefixed
# with a book-code letter (or short letter combo) means "this page in a
# DIFFERENT book" -- e.g. "p. B123" = Basic Set p.123, "p. MA45" = Martial
# Arts p.45 -- regardless of which specific code it is. This is a much
# more reliable cross-book signal than inferring from italics or a nearby
# "GURPS <Title>" phrase (which is still kept as a fallback for books that
# spell the citation out in prose instead of using the shorthand). We
# don't try to detect "this book's own code" and allow self-references
# through it -- deliberately conservative: a missed same-book link is a
# far smaller problem than a wrongly-linked cross-book reference, which is
# exactly what this feature exists to prevent.
BOOK_CODE_TOKEN = re.compile(r'^([A-Z]{1,3})(\d{1,4})')


# ---------------------------------------------------------------------------
# Page-numbering detection
# ---------------------------------------------------------------------------

def bottom_words(page, band=45):
    h = page.rect.height
    return [w for w in page.get_text("words") if w[3] > h - band]


def footer_observations(doc):
    """Every (pdf_index, printed_number) pair visible in a page's
    header/footer band -- the raw evidence both numbering detectors use."""
    obs = []
    for i in range(doc.page_count):
        for w in bottom_words(doc[i]):
            t = w[4].strip(".,")
            if t.isdigit():
                obs.append((i, int(t)))
    return obs


def detect_page_labels(doc, obs=None):
    """Scan footers to find the arabic body-page offset and valid range."""
    if obs is None:
        obs = footer_observations(doc)
    offsets = [i - n for i, n in obs]
    arabic_numbers_seen = [n for _, n in obs]

    if not offsets:
        raise RuntimeError(
            "Couldn't detect any page numbers in footers/headers -- "
            "this script assumes a printed page number appears near the "
            "top or bottom of most pages."
        )

    offset_counts = Counter(offsets)
    best_offset, _ = offset_counts.most_common(1)[0]
    valid_numbers = [n for n, off in zip(arabic_numbers_seen, offsets) if off == best_offset]
    return best_offset, (min(valid_numbers), max(valid_numbers))


def detect_index_pages(doc):
    """Pages whose footer/header band contains an Index marker word."""
    index_pages = set()
    for i in range(doc.page_count):
        tokens = {w[4].strip(".,:").lower() for w in bottom_words(doc[i])}
        if tokens & INDEX_HEADER_WORDS:
            index_pages.add(i)
    return index_pages


TOC_HEADER_WORDS = {"contents"}


def detect_toc_pages(doc):
    toc_pages = set()
    for i in range(doc.page_count):
        tokens = {w[4].strip(".,:").lower() for w in bottom_words(doc[i])}
        if tokens & TOC_HEADER_WORDS:
            toc_pages.add(i)
    return toc_pages


def extract_chapters(doc, toc_pages, printed_to_index):
    """Pull (chapter_number, title, target_pdf_index) from this book's
    own TOC -- top-level entries are lines whose first word is a lone
    'N.' (e.g. '11. Combat . . . 362')."""
    from collections import defaultdict

    chapters = []
    for i in sorted(toc_pages):
        page = doc[i]
        words = page.get_text("words")
        lines = defaultdict(list)
        for w in words:
            lines[(w[5], w[6])].append(w)
        sorted_keys = sorted(lines.keys())
        line_word_lists = [sorted(lines[k], key=lambda w: w[0]) for k in sorted_keys]

        def strip_trailing_number(tw):
            while tw and re.match(r'^\.+$', tw[-1]):
                tw = tw[:-1]
            if tw and tw[-1].strip(".,").isdigit():
                return tw[:-1], tw[-1].strip(".,")
            return tw, None

        idx = 0
        while idx < len(line_word_lists):
            ws = line_word_lists[idx]
            first = ws[0][4]
            m = re.match(r'^(\d{1,2})\.$', first)
            if not m:
                idx += 1
                continue
            title_words = [w[4] for w in ws[1:]]
            clean, pagenum = strip_trailing_number(title_words)
            scan_idx = idx
            while pagenum is None and scan_idx + 1 < len(line_word_lists):
                scan_idx += 1
                nxt_words = [w[4] for w in line_word_lists[scan_idx]]
                nclean, npagenum = strip_trailing_number(nxt_words)
                clean += nclean
                pagenum = npagenum
                if scan_idx - idx > 3:
                    break

            title = " ".join(w for w in clean if not re.match(r'^\.+$', w))
            title = re.sub(r"\s+", " ", title).strip(" .")
            if pagenum and title:
                target = printed_to_index(int(pagenum))
                if target is not None:
                    chapters.append((int(m.group(1)), title, target))
            idx = scan_idx + 1

    return chapters


def find_toc_page_number_words(page):
    """For each visual line on a TOC page, find the trailing word if it's
    a bare page number (the typical 'Title . . . . 36' dot-leader
    format). Returns list of (word_idx, page_number). Excludes the
    page's own footer band -- a running footer page number (e.g. '2')
    can otherwise be mistaken for a genuine dot-leader entry."""
    from collections import defaultdict
    words = page.get_text("words")
    h = page.rect.height
    lines = defaultdict(list)
    for idx, w in enumerate(words):
        if w[3] > h - 45:
            continue
        lines[(w[5], w[6])].append((idx, w))

    results = []
    for key in sorted(lines.keys()):
        ws = sorted(lines[key], key=lambda p: p[1][0])
        last_idx, last_w = ws[-1]
        num_str = last_w[4].strip(".,")
        if num_str.isdigit():
            results.append((last_idx, int(num_str)))
    return results, words


def toc_already_hyperlinked(doc, toc_pages):
    """Some books' TOC pages already have working native links (common
    when the publisher built them into the original file); others
    (confirmed: GURPS Space) ship with a plain, unlinked TOC. Compare how
    many TOC lines look like page-number entries against how many
    existing links are actually there -- if links cover less than half
    of the apparent entries, treat the TOC as unlinked."""
    if not toc_pages:
        return True  # no TOC found at all -- nothing to add either way
    candidate_count = 0
    link_count = 0
    for i in toc_pages:
        page = doc[i]
        entries, _ = find_toc_page_number_words(page)
        candidate_count += len(entries)
        link_count += len(page.get_links())
    if candidate_count == 0:
        return True
    return link_count >= candidate_count * 0.5


CHAPTER_WORD_PAT = re.compile(r'^\W*(Chapters?)$')
CHAPTER_HYPHEN_START_PAT = re.compile(r'^\W*Chap\w*[\xad-]$')  # e.g. 'Chap\xad' or 'Chapt-'


def match_chapter_word(words, i):
    """Detect a 'Chapter'/'Chapters' token starting at index i, which may
    have leading punctuation attached ('(Chapter') or be split across a
    line-wrap hyphen ('Chap\xad' + 'ter'). Returns (end_idx, plural) or None."""
    tok = words[i][4]
    m = CHAPTER_WORD_PAT.match(tok)
    if m:
        return i, m.group(1).endswith("s")
    if CHAPTER_HYPHEN_START_PAT.match(tok) and i + 1 < len(words):
        stripped = re.sub(r"^\W+", "", tok)
        combined = re.sub(r"[\xad-]$", "", stripped) + words[i + 1][4]
        m2 = re.match(r"^(Chapters?)\b", combined)
        if m2:
            return i + 1, m2.group(1).endswith("s")
    return None


def find_chapter_number_refs(words):
    """Find explicit 'Chapter N' / 'Chapters N-M' / 'Chapters N and M' /
    'Chapters N, M, and L' references -- this book's own actual
    cross-reference convention (confirmed against real text;
    italicized-title matching alone was tried first and produced too
    many false positives from ordinary prose, stat-block labels like
    'Advantages: 15 points', and the like).

    Returns (chapter_word_idx, end_word_idx, groups) where groups is a
    list of (word_idx, [numbers]) -- one entry per distinct word
    position. A hyphen range ('11-13') is a single group with two
    numbers sharing one word (linked together as one reference, same as
    before). A comma/and-separated list ('2, 4, and 6') produces
    multiple groups, one per number, each individually linkable to its
    own chapter's page."""
    refs = []
    i = 0
    while i < len(words):
        m = match_chapter_word(words, i)
        if not m:
            i += 1
            continue
        chap_end, plural = m
        j = chap_end + 1
        if j >= len(words):
            i = chap_end + 1
            continue
        num_m = re.match(r'^(\d{1,2})', words[j][4])
        if not num_m:
            i = chap_end + 1
            continue

        groups = [(j, [int(num_m.group(1))])]
        rest = words[j][4][num_m.end():]
        end_idx = j

        range_m = re.match(r'^[\u2013\u2014-](\d{1,2})', rest)
        if range_m:
            groups[0][1].append(int(range_m.group(1)))
        else:
            # Comma/and-separated list: "2, 4, and 6" or "2, 4, 6".
            # Each additional number is its own separate word token, so
            # each can get its own individual link later.
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


def normalize_title(s):
    s = re.sub(r"[^\w\s]", "", s).strip().lower()
    return re.sub(r"\s+", " ", s)


# ---------------------------------------------------------------------------
# Cross-book title detection
# ---------------------------------------------------------------------------

def looks_like_title_span(words):
    for w in words:
        bare = w.strip(",;:")
        if not bare:
            continue
        if bare.lower() in TITLE_CONNECTORS:
            continue
        if not (bare[0].isupper() or bare[0].isdigit()):
            return False
        if "." in bare[:-1]:
            return False
    return True


def title_nearby(words, ref_word_idx):
    """If a '<TRIGGER> <Title...>' run sits immediately before this
    reference, return the matched text (signal to skip linking)."""
    lo = max(0, ref_word_idx - LOOKBACK_WORDS_FOR_TITLE)
    span = words[lo:ref_word_idx]
    for start in range(len(span)):
        bare_tok = span[start][4].strip("(),;:").lower()
        if bare_tok == TITLE_TRIGGER_WORD:
            between = span[start + 1:]
            if looks_like_title_span([w[4] for w in between]):
                return " ".join(w[4] for w in span[start:])
    return None


# --- Second cross-book signal: an italicized product title cited WITHOUT
# the literal trigger word, e.g. "High-Tech, pp. 13-15" (a supplement
# citing its own parent book by name alone, since the trigger word would
# be redundant within a book that's already part of that line). GURPS
# italicizes book/product titles consistently, so an italicized phrase
# immediately before a reference that ISN'T part of THIS book's own
# vocabulary (its TOC section titles + Index terms) is a strong signal
# it's naming a different book. Deliberately requires NOT being in a
# large known-good vocabulary, rather than matching a specific pattern,
# since that's what keeps this from re-flagging this book's own many
# genuine italicized same-book cross-references (a plain "exact title
# match" version of this was tried first and produced false positives
# on ordinary prose -- see hyperlink_pdf.py's docstring history).

def build_italic_spans(page):
    spans = []
    d = page.get_text("dict")
    for b in d["blocks"]:
        if "lines" not in b:
            continue
        for l in b["lines"]:
            for s in l["spans"]:
                spans.append((fitz.Rect(s["bbox"]), bool(s["flags"] & 2)))
    return spans


def word_is_italic(rect, spans):
    cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    for r, italic in spans:
        if r.x0 - 0.5 <= cx <= r.x1 + 0.5 and r.y0 - 0.5 <= cy <= r.y1 + 0.5:
            return italic
    return False


def build_same_book_vocabulary(doc, toc_pages, index_pages):
    """This book's own section titles + Index terms, normalized. Used to
    tell 'this italicized phrase is part of THIS book' from 'this
    italicized phrase names some OTHER book.'"""
    vocab = set()

    for i in toc_pages:
        page = doc[i]
        words = page.get_text("words")
        from collections import defaultdict
        lines = defaultdict(list)
        for w in words:
            lines[(w[5], w[6])].append(w)
        for key in sorted(lines.keys()):
            ws = sorted(lines[key], key=lambda w: w[0])
            text_words = [w[4] for w in ws if not w[4].strip(".,").isdigit()]
            title = " ".join(text_words).strip(" .")
            norm = normalize_title(title)
            if len(norm) > 2:
                vocab.add(norm)

    for i in index_pages:
        page = doc[i]
        text = page.get_text()
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^([A-Za-z][A-Za-z0-9 ,'\"\-/&]*?)(?:,?\s*\d)", line)
            if m:
                norm = normalize_title(m.group(1))
                if len(norm) > 2:
                    vocab.add(norm)

    return vocab


# Known GURPS 4th-edition product titles that get cited by name alone
# (no "GURPS" prefix) within other GURPS books, since the line is already
# established. This list is necessarily incomplete -- SJG publishes many
# supplements -- but an incomplete allowlist is the SAFE failure mode
# here: an unrecognized title just falls through to being treated as
# same-book (a missed cross-book skip), rather than the alternative of
# guessing from "not in this book's own vocabulary," which was tested
# and produced 85 false positives on a single 55-page supplement (its
# own same-book cross-reference headings, like "Wet Cell" or "Secondary
# Batteries," aren't all captured by the TOC/Index and got wrongly
# flagged as other-book citations). Extend this list if a real bare
# citation is found that isn't caught.
KNOWN_GURPS_TITLES = {
    "basic set", "characters", "campaigns",
    "low-tech", "high-tech", "ultra-tech", "bio-tech",
    "dungeon fantasy", "powers", "magic", "psionic powers",
    "martial arts", "horror", "space", "social engineering",
    "zombies", "monster hunters", "action", "banestorm",
    "infinite worlds", "thaumatology", "mysteries", "supers",
    "traveller", "template toolkit", "power-ups", "pyramid",
    "gun fu", "vehicles", "steampunk",
}


KNOWN_GURPS_TITLES_NORM = {normalize_title(t) for t in KNOWN_GURPS_TITLES}


def italic_title_nearby(words, spans, ref_word_idx, vocab):
    """Look immediately before the reference for a contiguous italicized
    run that matches a KNOWN other GURPS product title -- signal that
    it's citing a different book by name alone (e.g. 'High-Tech, pp.
    13-15' inside a High-Tech supplement, where the line context makes
    the usual 'GURPS <Title>' trigger word feel redundant to the author).
    Deliberately conservative: only matches a known title list, not any
    unrecognized italicized phrase (see KNOWN_GURPS_TITLES docstring)."""
    i = ref_word_idx - 1
    while i >= 0 and words[i][4].strip("(),.;:").lower() in ("(", "see", "cf."):
        i -= 1
    run = []
    steps = 0
    while i >= 0 and steps < LOOKBACK_WORDS_FOR_TITLE:
        rect = fitz.Rect(words[i][0], words[i][1], words[i][2], words[i][3])
        if word_is_italic(rect, spans):
            run.append(words[i][4])
            i -= 1
            steps += 1
        else:
            break
    if not run:
        return None
    phrase = " ".join(reversed(run)).strip(" ,.;:()")
    if normalize_title(phrase) in KNOWN_GURPS_TITLES_NORM:
        return phrase
    return None


# ---------------------------------------------------------------------------
# Reference matching
# ---------------------------------------------------------------------------

MAX_LINK_HEIGHT = 40   # a link should never span more than ~triple a line
MAX_LINK_WIDTH_FRAC = 0.9  # or more than ~a full line's width


def is_reasonable_link_rect(rect, page):
    """Safety net: reject rects that are implausibly large for a short
    text reference. Found in testing: a rare PDF text-extraction anomaly
    where a single 'word' (an image caption run glued to a page
    reference) reports a bounding box spanning an entire page column --
    not something fixable at the source, so we just refuse to insert a
    link that big rather than create a giant wrong-looking clickable
    area over unrelated text."""
    if rect.height > MAX_LINK_HEIGHT:
        return False
    if rect.width > page.rect.width * MAX_LINK_WIDTH_FRAC:
        return False
    return True


def rects_meaningfully_overlap(a, b):
    """True only if these rects substantially overlap, not just touch at
    a shared edge. Adjacent lines' word bounding boxes routinely touch
    or overlap by a fraction of a point (tight line-leading), which
    made a plain rect.intersects() check produce false positives -- a
    reference would get incorrectly treated as 'already linked' because
    an unrelated reference on the line directly above or below happened
    to share a boundary pixel."""
    ix0, iy0 = max(a.x0, b.x0), max(a.y0, b.y0)
    ix1, iy1 = min(a.x1, b.x1), min(a.y1, b.y1)
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    overlap_area = (ix1 - ix0) * (iy1 - iy0)
    a_area = max((a.x1 - a.x0) * (a.y1 - a.y0), 1e-6)
    return (overlap_area / a_area) > 0.5


def find_body_references(words):
    """'p. NNN' / 'pp. NNN-NNN' style references, any spacing variant.
    Returns (start_idx, end_idx, number, book_code, range_end) --
    book_code is None for a normal same-book reference, or the letter
    code (e.g. 'B') if this reference used GURPS's cross-book shorthand
    ('p. B123'). range_end is None for a single-page reference, or the
    second number for a range ('pp. 10-11' -> range_end=11)."""
    refs = []
    consumed = set()
    j = 0
    while j < len(words):
        if j in consumed:
            j += 1
            continue
        tok = words[j][4]
        m = PP_SINGLE.match(tok)
        if m:
            code = m.group(2)
            num = int(m.group(3))
            num2 = int(m.group(5)) if m.group(5) else None
            refs.append((j, j, num, code, num2))
            consumed.add(j)
            j += 1
            continue
        bare_tok = re.sub(r'^\W+', '', tok).lower()
        if bare_tok in ("p.", "pp.") and j + 1 < len(words):
            nxt = words[j + 1][4]
            code_m = BOOK_CODE_TOKEN.match(nxt)
            if code_m:
                refs.append((j, j + 1, int(code_m.group(2)), code_m.group(1), None))
                consumed.add(j)
                consumed.add(j + 1)
                j += 2
                continue
            range_m = re.match(r'^(\d{1,4})[\u2013\u2014-](\d{1,4})', nxt)
            if range_m:
                num = int(range_m.group(1))
                num2 = int(range_m.group(2))
                refs.append((j, j + 1, num, None, num2))
                consumed.add(j)
                consumed.add(j + 1)
                j += 2
                continue
            nm = re.match(r"^(\d{1,4})", nxt)
            if nm:
                num = int(nm.group(1))
                refs.append((j, j + 1, num, None, None))
                consumed.add(j)
                consumed.add(j + 1)
                j += 2
                continue
        j += 1
    return refs, consumed


def find_index_references(words, h, already_consumed):
    """Bare 'Term, 24.' style references, plus line-wrapped ranges.
    Returns 5-tuples matching find_body_references' shape:
    (start_idx, end_idx, number, book_code, range_end)."""
    refs = []
    consumed = set(already_consumed)

    # wrapped ranges: "418-" at end of line, "425," continuing
    for i, w in enumerate(words):
        if i in consumed:
            continue
        m = WRAP_START.match(w[4])
        if not m or i + 1 >= len(words) or (i + 1) in consumed:
            continue
        cont_m = WRAP_CONT.match(words[i + 1][4])
        if not cont_m:
            continue
        num = int(m.group(1))
        num2 = int(cont_m.group(1))
        refs.append((i, i, num, None, None))       # first half: own tight rect
        refs.append((i + 1, i + 1, num, None, None))  # second half: same target, own rect
        consumed.add(i)
        consumed.add(i + 1)

    # plain bare numbers/ranges (skip the page's own footer band)
    for i, w in enumerate(words):
        if i in consumed:
            continue
        if w[3] > h - 45:
            continue
        m = BARE_NUM.match(w[4])
        if not m:
            continue
        num2 = int(m.group(2)) if m.group(2) else None
        refs.append((i, i, int(m.group(1)), None, num2))

    return refs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def hyperlink_pdf(in_path, out_path):
    """Process a single PDF: copy it to out_path, add all detected links
    in place, and write a CSV report alongside it. Split out of main() so
    both single-file and --batch modes share exactly one code path."""
    report_path = out_path.rsplit(".", 1)[0] + "_link_report.csv"

    shutil.copyfile(in_path, out_path)
    doc = fitz.open(out_path)

    # Reset to the default before detecting -- in --batch mode this
    # function runs once per book, and without a reset a book with no
    # usable title metadata would silently inherit the PREVIOUS book's
    # trigger word instead of falling back to the documented default.
    global TITLE_TRIGGER_WORD
    TITLE_TRIGGER_WORD = DEFAULT_TRIGGER_WORD
    detected_trigger = detect_title_trigger_word(doc)
    if detected_trigger:
        TITLE_TRIGGER_WORD = detected_trigger
        print(f"Cross-book trigger word (from this PDF's own title): {TITLE_TRIGGER_WORD!r}")
    else:
        print(f"Could not read a title from this PDF's metadata -- "
              f"falling back to {TITLE_TRIGGER_WORD!r}. If this book isn't "
              f"a GURPS title, cross-book filtering may not work; consider "
              f"setting TITLE_TRIGGER_WORD manually near the top of the script.")

    print("Detecting page-numbering scheme...")
    # Prefer the PDF's own /PageLabels when they are trustworthy: a direct
    # label -> index dict handles files with several offsets (e.g. a
    # combined two-volume Basic Set) that the single-offset footer vote
    # below cannot. "Trustworthy" means the labels agree with the printed
    # numbers actually seen in footers on most pages -- many PDFs carry
    # only the default physical numbering as labels, and trusting those
    # would put every link a few pages off. Fall back to the vote otherwise.
    obs = footer_observations(doc)
    label_to_index = {}
    for i in range(doc.page_count):
        lab = doc[i].get_label()
        if lab and lab.isdigit():
            label_to_index.setdefault(int(lab), i)
    agree = sum(1 for i, n in obs if label_to_index.get(n) == i)
    use_labels = (len(label_to_index) >= max(10, doc.page_count // 2)
                  and obs and agree / len(obs) >= 0.5)
    if use_labels:
        # Even once the labels pass the whole-book agreement check above,
        # trust an INDIVIDUAL label -> index mapping only when that exact
        # page's own footer visibly shows that same number. A /PageLabels
        # entry can exist on a page with no printed folio at all -- a
        # title/credits page counted as the nominal start of a numbering
        # run, or (confirmed on a real combined two-volume file) a
        # back-matter ad page whose label just continues the boxed set's
        # overall page count even though it holds none of that volume's
        # real content. Trusting an unconfirmed label sent a same-book-
        # looking "p. 337" reference straight to an unrelated ad insert on
        # that file. An unconfirmed number simply isn't resolvable via the
        # label path -- it falls through exactly like an out-of-range page
        # already does, rather than risk linking into whatever the label
        # happens to point at.
        obs_set = set(obs)
        label_to_index = {
            n: i for n, i in label_to_index.items() if (i, n) in obs_set
        }
        offset, valid_range = None, (min(label_to_index), max(label_to_index))
        print(f"  Using the PDF's own page labels: {len(label_to_index)} "
              f"confirmed by a visible footer number, printed "
              f"{valid_range[0]}-{valid_range[1]} "
              f"(labels agree with {agree}/{len(obs)} footer numbers)")
    else:
        if label_to_index:
            print(f"  Ignoring the PDF's page labels: they agree with only "
                  f"{agree}/{len(obs)} footer numbers")
        offset, valid_range = detect_page_labels(doc, obs)
        print(f"  Body pages: printed {valid_range[0]}-{valid_range[1]}, "
              f"pdf_index = printed_number + {offset}")

    index_pages = detect_index_pages(doc)
    print(f"  Detected {len(index_pages)} Index page(s) "
          f"(header word match: {sorted(INDEX_HEADER_WORDS)})")

    def printed_to_index(n):
        if use_labels:
            return label_to_index.get(n)
        idx = n + offset
        if valid_range[0] <= n <= valid_range[1]:
            return idx
        return None

    toc_pages = detect_toc_pages(doc)
    chapters = extract_chapters(doc, toc_pages, printed_to_index)
    print(f"  Detected {len(chapters)} chapter(s) from the TOC "
          f"(header word match: {sorted(TOC_HEADER_WORDS)})")
    chapter_by_title = {normalize_title(title): target for _, title, target in chapters}
    chapter_by_number = {num: target for num, title, target in chapters}
    for num, title, target in chapters:
        print(f"    {num}. {title!r} -> pdf page {target + 1}")

    same_book_vocab = build_same_book_vocabulary(doc, toc_pages, index_pages)
    print(f"  Built same-book vocabulary: {len(same_book_vocab)} terms "
          f"(from TOC + Index) -- used to catch other-book titles cited "
          f"without the {TITLE_TRIGGER_WORD!r} trigger word")

    added, skipped_range, skipped_title, skipped_existing = 0, 0, 0, 0
    chapter_added, chapter_skipped_title, chapter_skipped_existing = 0, 0, 0
    toc_added = 0
    report_rows = []

    # Some books ship with a plain, unlinked Table of Contents (confirmed:
    # GURPS Space) rather than the native hyperlinked TOC most GURPS books
    # have. Detect that and add the missing links ourselves -- one link
    # per dot-leader entry, from its trailing page number to that page.
    if toc_pages and not toc_already_hyperlinked(doc, toc_pages):
        print("  TOC has no (or very few) existing links -- adding them...")
        for i in toc_pages:
            page = doc[i]
            existing_links = [l["from"] for l in page.get_links()]
            entries, toc_words = find_toc_page_number_words(page)
            for word_idx, num in entries:
                target = printed_to_index(num)
                if target is None:
                    continue
                w = toc_words[word_idx]
                rect = fitz.Rect(w[0], w[1], w[2], w[3])
                if any(rects_meaningfully_overlap(rect, r) for r in existing_links):
                    continue
                if not is_reasonable_link_rect(rect, page):
                    continue
                page.insert_link({
                    "kind": fitz.LINK_GOTO,
                    "page": target,
                    "from": rect,
                    "to": fitz.Point(0, 0),
                })
                existing_links.append(rect)
                toc_added += 1
                report_rows.append((i + 1, w[4], num, "added (TOC entry)", f"pdf page {target + 1}"))
        print(f"  Added {toc_added} TOC links")
    elif toc_pages:
        print("  TOC already has links -- leaving it alone")

    for i in range(doc.page_count):
        page = doc[i]
        words = page.get_text("words")
        existing_links = [l["from"] for l in page.get_links()]
        italic_spans = build_italic_spans(page)

        body_refs, consumed = find_body_references(words)
        all_refs = list(body_refs)
        if i in index_pages:
            all_refs += find_index_references(words, page.rect.height, consumed)

        for wi, wj, num, book_code, range_end in all_refs:
            rect = fitz.Rect(words[wi][0], words[wi][1], words[wj][2], words[wj][3])
            text = " ".join(w[4] for w in words[wi:wj + 1])

            if any(rects_meaningfully_overlap(rect, r) for r in existing_links):
                skipped_existing += 1
                continue

            if book_code:
                skipped_title += 1
                report_rows.append((i + 1, text, num, "skipped",
                                     f"GURPS cross-book code {book_code!r} (e.g. p. {book_code}{num})"))
                continue

            title = title_nearby(words, wi)
            if title:
                skipped_title += 1
                report_rows.append((i + 1, text, num, "skipped", f"other-book title nearby: {title}"))
                continue

            italic_title = italic_title_nearby(words, italic_spans, wi, same_book_vocab)
            if italic_title:
                skipped_title += 1
                report_rows.append((i + 1, text, num, "skipped",
                                     f"italicized other-book title nearby (no vocab match): {italic_title}"))
                continue

            target = printed_to_index(num)
            if target is None:
                skipped_range += 1
                report_rows.append((i + 1, text, num, "skipped", "out of page-number range"))
                continue

            # Trim glued trailing text (e.g. a token like "(p. 10) - the"
            # where the number, closing paren, dash, and following word
            # are all one PDF "word" due to narrow-no-break-space joins).
            # search_for operates on the text stream rather than word
            # tokens and naturally stops where the probe text ends.
            #
            # Try the FULL range first when one exists ("p. 10-11") --
            # trying the single-number form first would find it too,
            # since it's a literal prefix of the range text, silently
            # truncating the link to just the first number and losing
            # the range end entirely. Order matters here.
            clean_start = re.sub(r"^\W+", "", text).lower()
            base = "pp" if clean_start.startswith("pp") else "p"
            probes = []
            if range_end is not None:
                probes.append(f"{base}.{NBSP}{num}-{range_end})")
                probes.append(f"{base}.{NBSP}{num}\u2013{range_end})")
                probes.append(f"{base}.{NBSP}{num}-{range_end}")
                probes.append(f"{base}.{NBSP}{num}\u2013{range_end}")
            probes.append(f"{base}.{NBSP}{num})")
            probes.append(f"{base}.{NBSP}{num}")
            for probe in probes:
                found = page.search_for(probe)
                near = [r for r in found if abs(r.y0 - rect.y0) < 2]
                if near:
                    rect = near[0]
                    break

            if not is_reasonable_link_rect(rect, page):
                skipped_range += 1
                report_rows.append((i + 1, text, num, "skipped",
                                     "rejected: implausibly large link rect (likely a source PDF text-extraction anomaly)"))
                continue

            page.insert_link({
                "kind": fitz.LINK_GOTO,
                "page": target,
                "from": rect,
                "to": fitz.Point(0, 0),
            })
            existing_links.append(rect)
            added += 1
            report_rows.append((i + 1, text, num, "added", f"pdf page {target + 1}"))

        # Chapter-name pass: skip the TOC itself and the Index (neither
        # should get chapter-name links -- TOC already has its own
        # native links, and Index entries are terms, not prose).
        if i in toc_pages or i in index_pages:
            continue

        for wi, end_idx, groups in find_chapter_number_refs(words):
            primary_word_idx, primary_nums = groups[0]
            valid_nums = [n for n in primary_nums if n in chapter_by_number]
            if not valid_nums:
                continue
            target = chapter_by_number[valid_nums[0]]

            ref_words = words[wi:end_idx + 1]
            run_text = " ".join(w[4] for w in ref_words)

            chap_word_clean = re.sub(r"^\W+", "", words[wi][4])
            base = "Chapters" if chap_word_clean.lower().startswith("chapters") else "Chapter"

            title = title_nearby(words, wi)
            italic_title = None if title else italic_title_nearby(words, italic_spans, wi, same_book_vocab)
            if title or italic_title:
                chapter_skipped_title += 1
                which = title or italic_title
                report_rows.append((i + 1, run_text, "", "skipped",
                                     f"chapter ref, but other-book title nearby: {which}"))
                continue

            # --- Primary reference: "Chapters" word through the first
            # number (and its range partner, if any). Unchanged from
            # before: group by (block, line) so a line-wrapped reference
            # gets one tight rect per line instead of one rect spanning
            # both full lines, then refine via search_for to trim any
            # glued trailing text.
            if target != i:  # don't link a chapter to its own opening page
                primary_span = words[wi:primary_word_idx + 1]
                line_groups = []
                cur_key, cur_words = None, []
                for w in primary_span:
                    key = (w[5], w[6])
                    if key != cur_key and cur_words:
                        line_groups.append(cur_words)
                        cur_words = []
                    cur_key = key
                    cur_words.append(w)
                if cur_words:
                    line_groups.append(cur_words)

                rects = []
                for group in line_groups:
                    x0 = min(w[0] for w in group)
                    y0 = min(w[1] for w in group)
                    x1 = max(w[2] for w in group)
                    y1 = max(w[3] for w in group)
                    rects.append(fitz.Rect(x0, y0, x1, y1))

                probes = []
                if len(valid_nums) >= 2:
                    n1, n2 = valid_nums[0], valid_nums[1]
                    probes.append(f"{base} {n1}-{n2}")
                    probes.append(f"{base} {n1}\u2013{n2}")
                    probes.append(f"{base} {n1} and {n2}")
                probes.append(f"{base} {valid_nums[0]}")

                for probe in probes:
                    found = page.search_for(probe)
                    probe_hits = [r for r in found if any(
                        abs(r.y0 - g.y0) < 2 for g in rects
                    )]
                    if probe_hits:
                        rects = probe_hits
                        break

                approx_rect = fitz.Rect(
                    min(r.x0 for r in rects), min(r.y0 for r in rects),
                    max(r.x1 for r in rects), max(r.y1 for r in rects),
                )
                if (all(is_reasonable_link_rect(r, page) for r in rects)
                        and not any(rects_meaningfully_overlap(approx_rect, r) for r in existing_links)):
                    for rect in rects:
                        page.insert_link({
                            "kind": fitz.LINK_GOTO,
                            "page": target,
                            "from": rect,
                            "to": fitz.Point(0, 0),
                        })
                    existing_links.append(approx_rect)
                    chapter_added += 1
                    report_rows.append((i + 1, run_text, "", "added (chapter ref)", f"pdf page {target + 1}"))

            # --- Additional list members ("Chapters 2, 4, and 6" -- the
            # "4" and "6" here): each is its own standalone word, linked
            # individually to its own chapter, independent of the
            # primary reference above.
            for word_idx, nums in groups[1:]:
                extra_valid = [n for n in nums if n in chapter_by_number]
                if not extra_valid:
                    continue
                extra_target = chapter_by_number[extra_valid[0]]
                if extra_target == i:
                    continue
                w = words[word_idx]
                extra_rect = fitz.Rect(w[0], w[1], w[2], w[3])
                # trim any glued trailing text on this number word too
                probe = re.sub(r"^\W+", "", w[4])
                probe = re.match(r"^\d{1,2}", probe)
                if probe:
                    found = page.search_for(probe.group(0))
                    near = [r for r in found if abs(r.y0 - extra_rect.y0) < 2]
                    if near:
                        extra_rect = near[0]
                if not is_reasonable_link_rect(extra_rect, page):
                    continue
                if any(rects_meaningfully_overlap(extra_rect, r) for r in existing_links):
                    continue
                page.insert_link({
                    "kind": fitz.LINK_GOTO,
                    "page": extra_target,
                    "from": extra_rect,
                    "to": fitz.Point(0, 0),
                })
                existing_links.append(extra_rect)
                chapter_added += 1
                report_rows.append((i + 1, w[4], "", "added (chapter ref, list member)",
                                     f"pdf page {extra_target + 1}"))

    doc.saveIncr()

    with open(report_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["PDF Page", "Matched Text", "Ref Number", "Status", "Detail"])
        writer.writerows(report_rows)

    print(f"\nAdded {added} page-reference links")
    print(f"  Skipped {skipped_existing} (already linked)")
    print(f"  Skipped {skipped_title} (other-book title nearby)")
    print(f"  Skipped {skipped_range} (out of page-number range)")
    print(f"\nAdded {chapter_added} chapter-name links")
    print(f"  Skipped {chapter_skipped_existing} (already linked)")
    print(f"  Skipped {chapter_skipped_title} (other-book title nearby)")
    print(f"\nAdded {toc_added} Table of Contents links")
    print(f"Wrote {out_path}")
    print(f"Wrote report: {report_path}")

    return {
        "added": added,
        "chapter_added": chapter_added,
        "toc_added": toc_added,
    }


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Auto-hyperlink in-text page/chapter references in a PDF.")
    parser.add_argument("input", nargs="?",
                         help="Input PDF (single-file mode) or input directory (--batch mode)")
    parser.add_argument("output", nargs="?",
                         help="Output PDF (single-file mode only; ignored with --batch)")
    parser.add_argument("--batch", action="store_true",
                         help="Process every PDF in a directory instead of a single file. "
                              "INPUT must be a directory; OUTPUT is not used -- results go "
                              "into a new '<input dir>-hyperlinked' directory next to it.")
    parser.add_argument("--recursive", "--recursives", dest="recursive", action="store_true",
                         help="With --batch, also descend into subdirectories, "
                              "mirroring the same subdirectory structure in the output.")
    parser.add_argument("--rename", action="store_true",
                         help="With --batch, append '-hyperlinked' to each output filename "
                              "(e.g. Basic Set.pdf -> Basic Set-hyperlinked.pdf).")
    return parser


def run_batch(args):
    in_dir = Path(args.input)
    if not in_dir.is_dir():
        print(f"Error: {in_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    out_dir = in_dir.parent / f"{in_dir.name}-hyperlinked"
    out_dir.mkdir(parents=True, exist_ok=True)

    globber = in_dir.rglob if args.recursive else in_dir.glob
    pdf_paths = sorted(p for p in globber("*") if p.is_file() and p.suffix.lower() == ".pdf")

    if not pdf_paths:
        print(f"No PDFs found in {in_dir}"
              f"{' (recursively)' if args.recursive else ''}")
        return

    print(f"Found {len(pdf_paths)} PDF(s) in {in_dir} "
          f"-- writing results to {out_dir}\n")

    succeeded, failed = 0, 0
    for pdf_path in pdf_paths:
        rel = pdf_path.relative_to(in_dir)
        out_name = f"{rel.stem}-hyperlinked{rel.suffix}" if args.rename else rel.name
        dest_path = out_dir / rel.parent / out_name
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        print(f"=== {rel} ===")
        try:
            hyperlink_pdf(str(pdf_path), str(dest_path))
            succeeded += 1
        except Exception as e:
            failed += 1
            print(f"FAILED: {rel}: {e}", file=sys.stderr)
        print()

    print(f"Batch complete: {succeeded} succeeded, {failed} failed, "
          f"output in {out_dir}")


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.batch:
        if not args.input:
            parser.error("--batch requires INPUT (a directory)")
        run_batch(args)
        return

    if args.recursive or args.rename:
        parser.error("--recursive and --rename only apply with --batch")

    if not args.input or not args.output:
        parser.error("INPUT and OUTPUT are required unless --batch is used")

    hyperlink_pdf(args.input, args.output)


if __name__ == "__main__":
    main()
