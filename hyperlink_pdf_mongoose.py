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


# CSV/formula-injection guard (OWASP): the "Matched Text" and "Detail"
# report columns are built from PDF-extracted text, which is untrusted if
# the source PDF itself is (e.g. testing a PDF handed to you rather than
# one of your own known-good books). A cell beginning with one of these
# characters is interpreted as a formula by Excel/Sheets when the report
# CSV is opened there -- prefixing with a literal quote forces plain-text
# interpretation without changing what a human reader sees.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


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
    # Mongoose's 1st-edition "numbered series" books put a generic
    # structural word + volume number BEFORE the real distinctive title,
    # separated by a colon -- confirmed real /Info Title metadata:
    # "Book 9: Robot", "Book 1: Mercenary Second Edition", "Alien Module
    # 1: Aslan". Extracting the first word without stripping this prefix
    # picks "book" or leaves "alien" ahead of the real title word --
    # "book" in particular is a common enough English word that it's a
    # fragile, silently-wrong trigger even though no false-positive skip
    # was observed in the three books that produced it (title_nearby()
    # never happened to fire there; title_after() caught all their real
    # cross-book citations instead). Strip the prefix before the normal
    # extraction below runs, same "low-impact but still wrong" standard
    # bug #27 already fixed the "2300AD" mid-token match to.
    title = re.sub(r"^\s*(?:[A-Za-z]+\s+){1,3}?\d+\s*:\s*", "", title)
    # An InDesign export can leave the source filename in /Title instead
    # of the real product name (confirmed: High Guard's is literally
    # 'High Guard Cover.indd') -- treat anything shaped like a filename
    # as unusable rather than trusting it, since 'high' (its first real
    # word) is not remotely the book's actual cross-reference trigger.
    is_filename = bool(re.search(r"\.\w{2,5}$", title.strip()))
    words = [] if is_filename else [w.lower() for w in re.findall(r"[A-Za-z']+", title)]
    words = [w for w in words if w not in _TRIGGER_STOPWORDS]
    if words:
        return words[0]
    # Metadata title is blank, filename-shaped, or otherwise unusable --
    # confirmed on real Mongoose Traveller PDFs, whose /Title is empty
    # (Core Rulebook, Companion) or an InDesign export artifact (High
    # Guard). Fall back to the book's own printed copyright line
    # ('Traveller (c)2026 Mongoose Publishing Ltd.'), scanning only the
    # first few pages so this doesn't accidentally pick up an unrelated
    # capitalized word from deep in the body.
    for i in range(min(6, doc.page_count)):
        text = doc[i].get_text()
        # \b anchors the start of the captured token, and the class
        # allows a leading digit -- without both, this can match INTO
        # the middle of a glued alphanumeric brand like "2300AD ©2021",
        # capturing the meaningless suffix "AD" instead of the real
        # word. Confirmed on all three 2300AD boxed-set books: their
        # copyright line reads "2300AD ©2021 Mongoose...", and the
        # original letters-only, unanchored pattern silently returned
        # "ad" for all three (a real, if low-impact, cross-book-trigger
        # bug -- "ad" is never going to appear before a real citation,
        # but neither would the correct answer here, "2300ad", not
        # "traveller": these books' own self-brand is the setting line,
        # not the rules-engine line).
        m = re.search(r"\b([A-Za-z0-9][A-Za-z0-9']*)\s*(?:©|\(c\))", text)
        if m:
            return m.group(1).lower()
    return None
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


def resolve_range_end(num, raw_end):
    """Resolve a range's second number, expanding an abbreviated ending
    like '152-3' (meaning 152-153) by keeping the leading digits of the
    first number and swapping in the written trailing digits -- a
    British-publishing convention confirmed in a real Mongoose book
    (Traveller Companion: 'pages 152-3'). Only kicks in when the written
    second number is shorter than the first AND numerically smaller,
    since a real non-abbreviated range never goes backward -- an
    ordinary range like '110-125' is left alone."""
    end = int(raw_end)
    if len(raw_end) < len(str(num)) and end < num:
        prefix_len = len(str(num)) - len(raw_end)
        candidate = int(str(num)[:prefix_len] + raw_end)
        if candidate > num:
            return candidate
    return end


def parse_ref_number(nxt):
    """Given the word immediately after a 'p.'/'pp.'/'page'/'pages'
    token, return (num, book_code, range_end), or None if it doesn't
    start with a number. Shared by both GURPS's abbreviated grammar and
    Mongoose's spelled-out one -- the token after the keyword looks the
    same either way."""
    code_m = BOOK_CODE_TOKEN.match(nxt)
    if code_m:
        return int(code_m.group(2)), code_m.group(1), None
    range_m = re.match(r'^(\d{1,4})[–—-](\d{1,4})', nxt)
    if range_m:
        num = int(range_m.group(1))
        return num, None, resolve_range_end(num, range_m.group(2))
    nm = re.match(r'^(\d{1,4})', nxt)
    if nm:
        return int(nm.group(1)), None, None
    return None


# ---------------------------------------------------------------------------
# Page-numbering detection
# ---------------------------------------------------------------------------

def bottom_words(page, band=45):
    h = page.rect.height
    return [w for w in page.get_text("words") if w[3] > h - band]


def detect_page_labels(doc):
    """Scan footers to find the arabic body-page offset and valid range."""
    offsets = []
    arabic_numbers_seen = []

    for i in range(doc.page_count):
        tokens = [w[4].strip(".,") for w in bottom_words(doc[i])]
        for t in tokens:
            if t.isdigit():
                n = int(t)
                offsets.append(i - n)
                arabic_numbers_seen.append(n)

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


def page_has_heading(page, keyword):
    """True if `keyword` (e.g. 'contents', 'index') appears on this page
    as a genuine section-opening heading, not incidental body text or a
    same-styled TOC list entry pointing at a DIFFERENT page. GURPS books
    repeat the marker as running footer/header text on every page of the
    section, which `bottom_words()` catches -- but real Mongoose PDFs
    (confirmed on all 3 test books) never do that; the word shows up
    exactly once, as a one-off page title positioned in the upper part
    of the page. Two independent signals, both derived from real
    extracted word data rather than assumed:
      (a) an anomalously huge bounding box (>50pt tall) -- confirmed on
          the actual Index-opening page of all 3 books (Core Rulebook
          p.263, High Guard p.289, Companion p.184); looks like MuPDF
          mis-measuring a decorative section-title treatment (same
          anomaly class as bug #15 in CLAUDE.md), but empirically a
          reliable positive signal here rather than noise.
      (b) a normal but clearly larger-than-body-text word (height
          >=17pt -- body text in these books is ~13pt, real headings
          are ~19-21pt) sitting in the top 40% of the page. This is
          what catches Core Rulebook/High Guard's plain 'CONTENTS'
          heading, while excluding an ordinary sentence that happens to
          contain the word ('...dump its contents on one...', ~13pt)
          and a same-sized TOC list entry further down the same page
          ('Index . . . 184', which sits well past the top-40% cutoff).
      (c) the SAME keyword rendered TWICE at byte-identical bounding-box
          coordinates on the page -- a faux-bold/shadow rendering trick
          some books use for a heading instead of a genuinely bigger
          font. Confirmed on Aliens of Charted Space vol. 2: its
          'CONTENTS' heading is only 14.7pt tall (fails check (b)'s
          17pt cutoff outright -- a font-size threshold tuned on one
          set of books doesn't necessarily hold for another from the
          same publisher), but is printed twice at the exact same (x0,
          y0, x1, y1). Ordinary body text is never printed twice at the
          identical pixel position, so this is safe to treat as a
          standalone positive signal without picking a size cutoff at
          all -- it doesn't replace (a)/(b), it catches what they miss.
    """
    h = page.rect.height
    matches = [w for w in page.get_text("words") if w[4].strip(".,:").lower() == keyword]
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


def detect_index_pages(doc):
    return {i for i in range(doc.page_count)
            if any(page_has_heading(doc[i], kw) for kw in INDEX_HEADER_WORDS)}


TOC_HEADER_WORDS = {"contents"}


def detect_toc_pages(doc):
    return {i for i in range(doc.page_count)
            if any(page_has_heading(doc[i], kw) for kw in TOC_HEADER_WORDS)}


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


def title_nearby(words, ref_word_idx, vocab=frozenset()):
    """If a '<TRIGGER> <Title...>' run sits immediately before this
    reference, return the matched text (signal to skip linking) --
    UNLESS the matched phrase is actually this book's own chapter/section
    name (checked against `vocab`, the same TOC+Index vocabulary
    italic_title_nearby() already uses), in which case it's a same-book
    reference, not a cross-book one. See title_after()'s docstring for
    why this check exists: a book can legitimately have its own trigger
    word start several of its own internal chapter titles."""
    lo = max(0, ref_word_idx - LOOKBACK_WORDS_FOR_TITLE)
    span = words[lo:ref_word_idx]
    for start in range(len(span)):
        bare_tok = span[start][4].strip("(),;:").lower()
        if bare_tok == TITLE_TRIGGER_WORD:
            between = span[start + 1:]
            if looks_like_title_span([w[4] for w in between]):
                phrase = " ".join(w[4] for w in span[start:])
                if normalize_title(phrase) in vocab:
                    continue
                return phrase
    return None


def title_after(words, ref_end_idx, vocab=frozenset()):
    """If 'of the <Title...>' (or 'of <Title...>'/'in the <Title...>')
    immediately FOLLOWS this reference, return the matched text.
    Mongoose's Traveller line cites other books with the title AFTER the
    page number ('page 149 of the Traveller Core Rulebook') rather than
    before it like GURPS's 'GURPS <Title>, p. N' -- a structurally
    different convention that title_nearby() (which only looks
    backward) can't catch, so this is a separate forward-looking layer.
    Requires at least 2 consecutive Title-Case words after the
    connector, not just 1, to avoid a stray capitalized word (e.g. 'of
    the Introduction') triggering a false skip -- confirmed real
    citations are always multi-word product names ('Traveller Core
    Rulebook', 'Traveller Companion', 'High Guard').

    `vocab` (this book's own TOC+Index terms, same as
    build_same_book_vocabulary() feeds italic_title_nearby()) guards
    against a DIFFERENT false-positive shape: a book citing its own
    chapter this same way. Confirmed on a real book (Alien Module 2:
    Vargr, whose own chapters are literally titled 'Vargr Character
    Generation' and 'Vargr Race' per its own Contents page): body text
    like 'page 32 of the Vargr Race chapter' is a SAME-book reference to
    its own chapter, not a citation of some other product -- the shape
    alone ('of the' + 2+ Title-Case words) can't tell the two apart, so
    without this check every one of these was being wrongly skipped as
    cross-book, silently losing real same-book links."""
    i = ref_end_idx + 1
    if i >= len(words):
        return None
    connector = words[i][4].strip(",;:").lower()
    if connector not in ("of", "in"):
        return None
    lead = [words[i][4]]
    i += 1
    if i < len(words) and words[i][4].strip(",;:").lower() == "the":
        lead.append(words[i][4])
        i += 1
    title_words = []
    steps = 0
    while i < len(words) and steps < LOOKBACK_WORDS_FOR_TITLE:
        bare = words[i][4].strip(",;:.()")
        # A single-letter token is never a real title word here -- it's
        # a decorative letter-spaced heading bleeding in (confirmed:
        # 'HIGH GUARD: ASLAN\nC\nH\nA\nP\nT\nE\nR...' in Aliens of Charted
        # Space, where a chapter-divider graphic sits right after a
        # genuine title). Stopping here keeps the reported match to the
        # real title instead of trailing off into 'ASLAN C H A P T'.
        #
        # A leading digit must also be allowed (matching
        # looks_like_title_span() and title_bare_before(), which both
        # already do this) -- 2300AD's own book titles literally start
        # with '2300AD', not a capital letter. Confirmed a real miss from
        # this gap: 'found on page 188 of the 2300AD Core Rulebook' in
        # Tools for Frontier Living wasn't recognized as a title span at
        # all (steps=0, returns None) because '2300AD'[0] is a digit, not
        # upper(). It only avoided becoming a wrong same-book link by
        # coincidence -- 188 also happened to exceed this citing book's
        # own valid page range -- so a citing book whose range DID reach
        # that far would have gotten a genuine wrong link.
        if not bare or len(bare) < 2 or not (bare[0].isupper() or bare[0].isdigit()):
            break
        title_words.append(words[i][4])
        i += 1
        steps += 1
    if len(title_words) < 2:
        return None
    if normalize_title(" ".join(title_words)) in vocab:
        return None
    return " ".join(lead + title_words)


# --- Third cross-book signal: a bare "<Title> page NN" citation with NO
# connector word and NO trigger word at all before the title -- neither
# title_nearby() (needs a trigger word like "traveller") nor title_after()
# (needs "of"/"in" right after the page number) can catch this shape.
# Confirmed in real 2300AD text: "(see Characters & Equipment page 38)"
# names the other book with nothing but the bare title sitting directly
# before the reference. Since there's no structural marker to key off at
# all here, this only checks against a small known-title allowlist --
# exactly the same tradeoff KNOWN_GURPS_TITLES already makes below, for
# the same reason (an unrecognized title just falls through as same-book,
# the safe failure direction, rather than risking false positives from
# matching any Title-Case span).
KNOWN_TRAVELLER_TITLES = {
    "traveller core rulebook", "traveller core book",
    # Bare "Core Rulebook" (no "Traveller"/"2300AD" brand prefix) is a
    # separate, real citation shape from the full-title entries above --
    # confirmed in two different sub-lines: Aliens of Charted Space
    # ("...roll on the Injury table of the Core Rulebook (page 47)") and
    # Tools for Frontier Living ("...the Organisations chapter in the
    # 2300AD Core Rulebook, (page 83)"). In the second case the growing
    # lookback in title_bare_before() walks straight through "2300AD"
    # and "the"/"in" (digits and TITLE_CONNECTORS are both allowed
    # through) but never matched anything, because neither the bare form
    # nor the brand-prefixed form was on the list -- only the "traveller
    # core rulebook"/"traveller core book" spellings were. Confirmed safe
    # to add: neither actual Core Rulebook PDF (1e or 2e) ever mentions
    # "Core Rulebook" adjacent to a page number anywhere in its own text
    # (both real mentions are cover-page/foreword prose), so this can't
    # cause a same-book self-citation to be wrongly skipped in the one
    # place that would matter.
    "core rulebook",
    "characters & equipment", "characters and equipment",
    "the worlds of 2300ad",
    "robot handbook", "central supply catalogue",
    "vehicle handbook", "aerospace engineer's handbook",
    "ships of the frontier", "tools for frontier living",
    "traveller companion", "high guard", "aliens of charted space",
}
KNOWN_TRAVELLER_TITLES_NORM = {normalize_title(t) for t in KNOWN_TRAVELLER_TITLES}


def title_bare_before(words, ref_word_idx):
    """Look immediately before the reference for a contiguous run of
    Title-Case words that, once assembled, matches a KNOWN Traveller/
    2300AD product title -- same lookback-and-grow approach as
    italic_title_nearby() below, but keyed on the known-title allowlist
    instead of italic styling (there's no reliable style cue for this
    citation shape). Stops and returns as soon as the accumulated run
    matches, so a longer preceding phrase ("the Colonies table in
    Characters & Equipment") doesn't get pulled in past the title's own
    start. "on" is also skippable here ("...present in the Vehicle
    Handbook on page 59") -- confirmed a real wrong link from its
    absence: Robot Handbook cites a different book this way, and without
    skipping past "on" the title never got reached at all, so this
    genuinely cross-book reference was linked into Robot Handbook's own
    page instead. Safe to skip unconditionally (not just after a title
    match) because the allowlist check below is what actually gates a
    match -- skipping "on" just lets the grown phrase reach as far back
    as a real title, it can't manufacture a match against ordinary
    prose like 'installation on vehicles on page 59' (grows into
    lowercase 'vehicles' and stops with no match, same as any other
    non-title phrase)."""
    i = ref_word_idx - 1
    while i >= 0 and words[i][4].strip("(),.;:").lower() in ("(", "see", "cf.", "in", "on"):
        i -= 1
    run = []
    steps = 0
    while i >= 0 and steps < LOOKBACK_WORDS_FOR_TITLE:
        bare = words[i][4].strip("(),.;:")
        # A mid-title connector ("&", "of", "the"...) isn't itself
        # Title-Case, but a real product name can contain one
        # ("Characters & Equipment") -- allow it through the same way
        # looks_like_title_span() does, rather than letting it wrongly
        # end the run before reaching the title's actual first word.
        if not bare:
            break
        if not (bare[0].isupper() or bare[0].isdigit()) and bare.lower() not in TITLE_CONNECTORS:
            break
        run.append(words[i][4])
        phrase = " ".join(reversed(run)).strip(" ,.;:()")
        if normalize_title(phrase) in KNOWN_TRAVELLER_TITLES_NORM:
            return phrase
        i -= 1
        steps += 1
    return None


# BookN shorthand: this boxed set's own way of citing a SIBLING PDF in the
# same product (e.g. "in Book2 page 23" inside Book 1, meaning Book 2's
# own page 23 -- which doesn't exist in this file at all). Always treated
# as cross-book regardless of which number, same rationale as GURPS's
# BOOK_CODE_TOKEN: a missed same-book link is a far smaller problem than
# wrongly linking into a page number that belongs to a different PDF.
BOOK_N_TOKEN = re.compile(r'^Book\d{1,2}$', re.IGNORECASE)


def book_n_nearby(words, ref_word_idx):
    i = ref_word_idx - 1
    if i < 0:
        return None
    tok = words[i][4].strip("(),;:")
    if BOOK_N_TOKEN.match(tok):
        return tok
    return None


# --- Another cross-book signal: an italicized product title cited WITHOUT
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
    "template toolkit", "power-ups", "pyramid",
    "gun fu", "vehicles", "steampunk",
    # NOTE: deliberately does NOT include "traveller", unlike the parent
    # GURPS script this allowlist was copied wholesale from. There it
    # means the unrelated 1990s "GURPS Traveller" crossover product; here,
    # in the Mongoose Traveller fork, every single book in this line IS
    # Traveller -- so an italicized "Traveller" right before a page ref
    # would be a same-book self-reference, and this allowlist entry would
    # wrongly flag it as citing a different product. Never observed to
    # fire yet (confirmed: 0 hits across the full 21-book test corpus,
    # despite italics being common -- 161 spans in one book alone), but
    # that's luck of which books happened to be tested, not protection;
    # same "silently wrong, technically harmless so far" class as bug #28.
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
            num2 = resolve_range_end(num, m.group(5)) if m.group(5) else None
            refs.append((j, j, num, code, num2))
            consumed.add(j)
            j += 1
            continue
        bare_tok = re.sub(r'^\W+', '', tok).lower()
        if bare_tok in ("p.", "pp.", "page", "pages") and j + 1 < len(words):
            parsed = parse_ref_number(words[j + 1][4])
            if parsed:
                num, code, num2 = parsed
                refs.append((j, j + 1, num, code, num2))
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
    offset, valid_range = detect_page_labels(doc)
    print(f"  Body pages: printed {valid_range[0]}-{valid_range[1]}, "
          f"pdf_index = printed_number + {offset}")

    index_pages = detect_index_pages(doc)
    print(f"  Detected {len(index_pages)} Index page(s) "
          f"(header word match: {sorted(INDEX_HEADER_WORDS)})")

    def printed_to_index(n):
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
        n_body_refs = len(all_refs)
        if i in index_pages:
            all_refs += find_index_references(words, page.rect.height, consumed)

        for ref_idx, (wi, wj, num, book_code, range_end) in enumerate(all_refs):
            # An Index entry is just "Term \n Number" -- there's no real
            # sentence around it, so the prose-shaped cross-book checks
            # below (trigger word/connector/bare-title lookback, italic
            # run) aren't testing genuine citation grammar here, only
            # whichever words happen to sit nearby in the alphabetized
            # list. Confirmed a real false positive from this: in the 1e
            # Core Rulebook's Index, the entry "Travel Codes 180" sits
            # immediately after the unrelated entry "Traveller 2" --
            # this book's own auto-detected trigger word -- so
            # title_nearby() matched "Traveller ... Travel Codes" as if
            # it were a real "<trigger> <title>" citation and wrongly
            # skipped a genuine same-book Index link. `book_code` is
            # unaffected (it only inspects the reference's own number
            # token, not surrounding words) and still applies to index
            # entries too.
            is_index_ref = ref_idx >= n_body_refs
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

            if not is_index_ref:
                book_n = book_n_nearby(words, wi)
                if book_n:
                    skipped_title += 1
                    report_rows.append((i + 1, text, num, "skipped",
                                         f"Traveller book-shorthand {book_n!r} nearby (different sibling PDF)"))
                    continue

                title = (title_nearby(words, wi, same_book_vocab)
                         or title_after(words, wj, same_book_vocab)
                         or title_bare_before(words, wi))
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
            probes = []
            if clean_start.startswith("page"):
                # Mongoose's spelled-out grammar -- plain space, no
                # period, so the probe text looks nothing like the
                # abbreviated 'p. NNN' form below.
                word_base = "pages" if clean_start.startswith("pages") else "page"
                if range_end is not None:
                    probes.append(f"{word_base} {num}-{range_end}")
                    probes.append(f"{word_base} {num}\u2013{range_end}")
                probes.append(f"{word_base} {num}")
            else:
                base = "pp" if clean_start.startswith("pp") else "p"
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

            # Re-check for "already linked" AFTER refinement, not just
            # before it. The pre-refinement check above compares each
            # reference's own ORIGINAL word rect, but the search_for()
            # refinement above can independently snap two DIFFERENT
            # original references onto the SAME final rect -- confirmed
            # on a real page (1e Core Rulebook p.141): the source PDF
            # renders an entire line twice at (nearly) identical
            # coordinates (same bold/shadow-effect duplication as the
            # faux-bold heading fix above, just applied to body text
            # instead of a heading), so two separate word-level matches
            # for "pages 165-166" exist a fraction of a point apart.
            # Both are within the 2pt y0 tolerance of BOTH duplicate
            # occurrences search_for() finds, so `near[0]` -- an
            # arbitrary first pick, not tied to which original word
            # triggered the search -- resolved to the identical rect
            # both times, producing two literal duplicate link
            # annotations stacked on each other. This check closes that
            # gap regardless of the precise duplication mechanism, by
            # verifying the rect actually being inserted doesn't already
            # overlap something in existing_links, not just the rect the
            # match started from.
            if any(rects_meaningfully_overlap(rect, r) for r in existing_links):
                skipped_existing += 1
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

            title = title_nearby(words, wi, same_book_vocab)
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
        writer.writerows(tuple(csv_safe(v) for v in row) for row in report_rows)

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

    # Path.rglob() follows symlinked subdirectories by default -- if
    # `in_dir` (e.g. an extracted third-party archive) contains a symlink
    # pointing outside itself, --recursive would otherwise walk and
    # process files anywhere else on disk the user can read, and mirror
    # that structure into out_dir. Resolve each candidate and drop any
    # whose real path lands outside in_dir's own resolved root.
    resolved_root = in_dir.resolve()
    globber = in_dir.rglob if args.recursive else in_dir.glob
    pdf_paths = []
    for p in globber("*"):
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        try:
            p.resolve().relative_to(resolved_root)
        except ValueError:
            print(f"Skipping {p} -- resolves outside {in_dir} (symlink?)", file=sys.stderr)
            continue
        pdf_paths.append(p)
    pdf_paths.sort()

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
