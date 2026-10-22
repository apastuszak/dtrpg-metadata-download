"""Cross-book checks for hyperlink_engine.py: each decides whether a page
reference points into *another* book, so it shouldn't be linked.

A rule set lists the checks its publisher needs, with settings:

    other_book_checks = [checks.book_code(), checks.title_after(min_words=2)]

Each factory returns a `Check`, called as `check(book, pt, ref)` and
returning a reason (for the link report) or None. `book` is the engine's
per-book state (trigger word, the book's own section names, its own title,
the rule set's known titles), `pt` the page's words, `ref` the reference.

A check that a new publisher needs and nobody has written yet goes here, so
every rule set can use it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

TITLE_CONNECTORS = {"and", "of", "the", "in", "for", "to", "a", "an", "or", "&"}
LOOKBACK_WORDS_FOR_TITLE = 10
SOFT_BREAK = ("-", "­")


def normalize_title(s):
    """Lowercase, punctuation dropped, single spaces: 'High-Tech' -> 'hightech'."""
    s = re.sub(r"[^\w\s]", "", s).strip().lower()
    return re.sub(r"\s+", " ", s)


def title_key(title):
    """Letters only, "&" read as "and": "Agent's Handbook" -> 'agentshandbook',
    "Hack & Slash" -> 'hackandslash'. Survives a title hyphenated or spaced
    differently across a line break."""
    title = title.lower().replace("’", "").replace("'", "").replace("&", "and")
    return re.sub(r"[^a-z]", "", title)


@dataclass
class Check:
    name: str
    fn: Callable
    # Also run on Index entries. Only checks that look at the reference
    # itself should: an Index entry has no sentence around it, so a check
    # reading the words before or after it only reads the neighbouring
    # entry ("Traveller 2" right before "Travel Codes 180" in a Traveller
    # index; "magic, 34" under Advantages in the GURPS Basic Set's).
    on_index: bool = False

    def __call__(self, book, pt, ref):
        return self.fn(book, pt, ref)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def looks_like_title_span(words):
    for w in words:
        bare = w.strip(",;:")
        if not bare or bare.lower() in TITLE_CONNECTORS:
            continue
        if not (bare[0].isupper() or bare[0].isdigit()):
            return False
        if "." in bare[:-1]:
            return False
    return True


def joined_words(words, start, step, limit):
    """Up to `limit` (text, [word indices]) from `start` walking by `step`,
    re-joining words hyphenated across a line break ("Impos-" + "sible")."""
    out = []
    i = start
    while 0 <= i < len(words) and len(out) < limit:
        text = words[i][4]
        if step > 0 and text.endswith(SOFT_BREAK) and i + 1 < len(words):
            out.append((text.rstrip("".join(SOFT_BREAK)) + words[i + 1][4], [i, i + 1]))
            i += 2
            continue
        if step < 0 and i - 1 >= 0 and words[i - 1][4].endswith(SOFT_BREAK):
            out.append((words[i - 1][4].rstrip("".join(SOFT_BREAK)) + text, [i - 1, i]))
            i -= 2
            continue
        out.append((text, [i]))
        i += step
    return out


def sentence_ends_at(words, i):
    """The word at `i` ends a sentence or clause ("(p. 174)." / "p. 12;"),
    so what follows isn't about the reference: "(p. 174). In 1967, ..."."""
    return words[i][4].rstrip("\u2019\u201d").endswith((".", ";", ":", "!", "?", ")"))


def is_caps(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def book_code():
    """A page number with a book-code prefix: "p. B123" (GURPS Basic Set
    page 123), "p. MA45". Always another book. Runs on Index entries too,
    since it reads only the reference itself."""
    def fn(book, pt, ref):
        if ref.book_code:
            return f"book-code prefix {ref.book_code!r} (p. {ref.book_code}{ref.links[0][1]}): another book"
        return None
    return Check("book_code", fn, on_index=True)


def sibling_volume(pattern=r"^Book\d{1,2}$"):
    """A token naming another volume of the same boxed set right before
    the reference ("in Book2 page 23", Mongoose 2300AD)."""
    rx = re.compile(pattern, re.IGNORECASE)

    def fn(book, pt, ref):
        if ref.start < 1:
            return None
        tok = pt.words[ref.start - 1][4].strip("(),;:")
        return f"sibling volume {tok!r} nearby" if rx.match(tok) else None
    return Check("sibling_volume", fn)


def trigger_word_before(ignore_own_sections=False):
    """'<trigger> <Title...>' just before the reference ("GURPS Magic,
    p. 12"), the trigger word coming from the PDF's own title (see the
    rule set's `trigger_word`). With `ignore_own_sections`, a phrase that is
    one of this book's own TOC/Index entries doesn't count -- a book can
    start its own chapter titles with the line's name. (Off for GURPS,
    whose index can list "GURPS Magic" itself.)"""
    def fn(book, pt, ref):
        trigger = book.trigger
        if not trigger:
            return None
        vocab = book.vocab if ignore_own_sections else frozenset()
        words = pt.words
        lo = max(0, ref.start - LOOKBACK_WORDS_FOR_TITLE)
        span = words[lo:ref.start]
        for start in range(len(span)):
            if span[start][4].strip("(),;:").lower() == trigger:
                if looks_like_title_span([w[4] for w in span[start + 1:]]):
                    phrase = " ".join(w[4] for w in span[start:])
                    if normalize_title(phrase) in vocab:
                        continue
                    return f"other-book title nearby: {phrase}"
        return None
    return Check("trigger_word_before", fn)


def italic_known_title_before():
    """A contiguous italic run just before the reference that is one of the
    rule set's `known_titles` ("High-Tech, pp. 13-15"). Only a known list,
    never "any italic phrase": guessing from "not in this book's own
    vocabulary" gave 85 false positives on one 55-page GURPS supplement,
    while an incomplete list only misses a skip."""
    def fn(book, pt, ref):
        words = pt.words
        i = ref.start - 1
        while i >= 0 and words[i][4].strip("(),.;:").lower() in ("(", "see", "cf."):
            i -= 1
        run = []
        while i >= 0 and len(run) < LOOKBACK_WORDS_FOR_TITLE and pt.is_italic(i):
            run.append(words[i][4])
            i -= 1
        if not run:
            return None
        phrase = " ".join(reversed(run)).strip(" ,.;:()")
        if normalize_title(phrase) in book.known_titles_norm:
            return f"italicized other-book title nearby: {phrase}"
        return None
    return Check("italic_known_title_before", fn)


def known_title_before():
    """One of the rule set's `known_titles` right before the reference with
    nothing marking it ("see Characters & Equipment page 38", "in the
    Vehicle Handbook on page 59"). Grows the phrase backwards a word at a
    time and stops at the first match; connectors inside a title are
    allowed through ("Characters & Equipment")."""
    def fn(book, pt, ref):
        words = pt.words
        i = ref.start - 1
        while i >= 0 and words[i][4].strip("(),.;:").lower() in ("(", "see", "cf.", "in", "on"):
            i -= 1
        run = []
        while i >= 0 and len(run) < LOOKBACK_WORDS_FOR_TITLE:
            bare = words[i][4].strip("(),.;:")
            if not bare:
                break
            if not (bare[0].isupper() or bare[0].isdigit()) and bare.lower() not in TITLE_CONNECTORS:
                break
            run.append(words[i][4])
            phrase = " ".join(reversed(run)).strip(" ,.;:()")
            if normalize_title(phrase) in book.known_titles_norm:
                return f"other-book title nearby: {phrase}"
            i -= 1
        return None
    return Check("known_title_before", fn)


def title_after(connectors=("of", "in"), min_words=2):
    """'of the <Title...>' right after the reference ("page 149 of the
    Traveller Core Rulebook"): at least `min_words` capitalized words
    (real citations are multi-word product names; "of the Introduction"
    isn't one), in the same sentence, and not one of this book's own sections ("page 32 of the
    Vargr Race chapter", in Alien Module 2: Vargr). A one-letter word ends
    the title (a letter-spaced divider heading, "C H A P T E R"); a
    leading digit doesn't ("2300AD Core Rulebook"). Words hyphenated across
    a line break are re-joined first ("Impossi-" / "ble Landscapes")."""
    def fn(book, pt, ref):
        if sentence_ends_at(pt.words, ref.end):
            return None
        following = joined_words(pt.words, ref.end + 1, 1, LOOKBACK_WORDS_FOR_TITLE + 2)
        if not following or following[0][0].strip(",;:").lower() not in connectors:
            return None
        lead = [following[0][0]]
        rest = following[1:]
        if rest and rest[0][0].strip(",;:").lower() == "the":
            lead.append(rest[0][0])
            rest = rest[1:]
        title_words = []
        for text, _ in rest:
            bare = text.strip(",;:.()")
            if not bare or len(bare) < 2 or not (bare[0].isupper() or bare[0].isdigit()):
                break
            title_words.append(text)
            if text.endswith((".", ",", ";", ":", ")", "!", "?")):
                break  # the title ends here: "(p. 233) in WWI: EWt. 27.5"
        title_words = title_words[:LOOKBACK_WORDS_FOR_TITLE]
        if len(title_words) < min_words or normalize_title(" ".join(title_words)) in book.vocab:
            return None
        return "other-book title nearby: " + " ".join(lead + title_words)
    return Check("title_after", fn)


def mixed_case_title_after(max_words=6):
    """A title right after the reference ("page 85 of the Agent's
    Handbook"), where ALL CAPS means a section of this book ("on page 177
    in THE SENECA WHIRLWIND REPORT") and mixed case means another book --
    unless it's this book's own title. "page 1 of 3" (a handout's own
    pagination) counts as not-this-book too. Titles hyphenated across lines
    are re-joined first."""
    def fn(book, pt, ref):
        if sentence_ends_at(pt.words, ref.end):
            return None
        following = joined_words(pt.words, ref.end + 1, 1, max_words + 2)
        if not following or following[0][0].lower().strip("(") not in ("of", "in"):
            return None
        rest = following[1:]
        if rest and rest[0][0].isdigit():
            return "other book: in-document page count (\"page N of M\")"
        if rest and rest[0][0].lower() == "the":
            rest = rest[1:]
        phrase = []
        for text, _ in rest:
            clean = text.strip(".,;:)(’”")
            if not clean or not (clean[0].isupper() or clean[0].isdigit()):
                break
            phrase.append(clean)
            if text.endswith((".", ",", ";", ":", ")")):
                break
        if not phrase or all(is_caps(t) for t in phrase):
            return None
        if not any(t[0].isalpha() for t in phrase):
            return None  # a year or number, not a title: "(p. 174) ... in 1967"
        title = " ".join(phrase)
        if book.own_title_key and book.own_title_key in title_key(title):
            return None
        return f"other book: {title}"
    return Check("mixed_case_title_after", fn)


def title_before_comma(max_words=6):
    """A title right before the reference, ending in a comma ("see the
    Handler's Guide, page 312"), that is italic or one of the rule set's
    `known_titles`. A plain capitalized name is more often a same-book
    character ("see Sheryl Krieger, page 68"). ALL CAPS and this book's own
    title don't count."""
    def fn(book, pt, ref):
        words = pt.words
        i = ref.start
        if i == 0 or not words[i - 1][4].endswith(","):
            return None
        phrase = []
        for text, idxs in joined_words(words, i - 1, -1, max_words):
            clean = text.strip(".,;:)(’”“")
            if (not clean or clean.lower() in ("see", "the", "in", "and", "also")
                    or not (clean[0].isupper() or clean[0].isdigit())):
                break
            phrase.insert(0, (clean, idxs))
        if not phrase or all(is_caps(t) for t, _ in phrase):
            return None
        title = " ".join(t for t, _ in phrase)
        key = title_key(title)
        if book.own_title_key and book.own_title_key in key:
            return None
        italic = all(pt.is_italic(j) for _, idxs in phrase for j in idxs)
        if italic or any(key.endswith(t) or t.endswith(key) and len(key) > 6 for t in book.known_title_keys):
            return f"other book: {title}"
        return None
    return Check("title_before_comma", fn)


CONTINUATION = re.compile(r"^(?:\d[\d\u2013\u2014-]*[,;]?|[\u2013\u2014-]|and|&|or)$")  # "121 – 124,"


def name_after_comma(words, end, max_words=6):
    """The name after the page number(s) inside the same parentheses --
    "SR6" in "(p. 94 and 96, SR6)", "Firing Squad" in "(p. 86 and 89-90,
    Firing Squad)." -- or None. Shared by title_in_parentheses() and
    hyperlink_survey.py, so the survey measures exactly what the check reads."""
    def closes(text):
        return text.rstrip(".,;:").endswith(")")

    last = end
    if closes(words[last][4]):
        return None
    j = last + 1
    while j < len(words) and CONTINUATION.match(words[j][4]):   # "p. 161, 179, SR6"
        last, j = j, j + 1
    if not words[last][4].endswith(",") or closes(words[last][4]):
        return None
    phrase = []
    for text, _ in joined_words(words, j, 1, max_words):
        if ")" in text:   # "SR6)for": the PDF glued the next word on
            phrase.append(text.split(")")[0] + ")")
            break
        phrase.append(text)
        # "p. 133, SR6. Slay..." ends at the period
        if text.endswith((",", ";", ".")):
            break
    # No closing punctuation at all (a table cell: "p. 74, SR6" then the
    # next cell's text) still returns the run; callers match its start.
    return " ".join(phrase).split(",")[0].rstrip(").;:").strip() or None


def known_title_prefix(book, words):
    """The longest start of `words` that is one of the rule set's
    `known_titles`, other than this book's own title -- or None."""
    for n in range(len(words), 0, -1):
        part = " ".join(words[:n])
        key = title_key(part)
        if key and key in book.known_title_keys and not (book.own_title_key and key in book.own_title_key):
            return part.rstrip(".,;:)")
    return None


def title_in_parentheses(codes=(), max_words=6):
    """A book named inside the same parentheses as the page number, after
    it -- "(p. 94 and 96, SR6)", "(p. 124, 6WC)", "(pp. 132-3, Sixth World
    Companion)" -- or before it, "(SR6, p. 72)". Catalyst's Shadowrun
    convention: 709 times in 13 books. Further page numbers in a list are
    skipped first. Only one of `codes` (exact, e.g. "SR6") or the rule
    set's `known_titles` counts, so "(p. 36, Teamwork Tests)", a section of
    this book, is still linked."""
    code_set = set(codes)

    def is_other_book(book, name):
        """The name, or its start (a table cell runs on into the next:
        "SR6 Always Late", "Firing Squad Bad Memories"), is a code or a
        known title -- returns the matched part, or None."""
        words = name.split()
        if words and words[0].rstrip(").;:") in code_set:
            return words[0].rstrip(").;:")
        return known_title_prefix(book, words)

    def fn(book, pt, ref):
        name = name_after_comma(pt.words, ref.end, max_words)
        found = name and is_other_book(book, name)
        if found:
            return f"other book: {found}"
        if ref.start >= 1:   # "(SR6, p. 72)"
            before = pt.words[ref.start - 1][4]
            if before.startswith("(") and before.endswith(","):
                found = is_other_book(book, before.strip("(,"))
                if found:
                    return f"other book: {found}"
        return None
    return Check("title_in_parentheses", fn)


def known_title_after(connectors=("of", "in"), max_words=6):
    """One of the rule set's `known_titles` right after the reference,
    following "of"/"in" and an optional "the" ("p. 283 of Shadowrun, Sixth
    World"). Unlike `title_after()`, a one-word or comma-split title counts,
    because only listed titles match."""
    def fn(book, pt, ref):
        if sentence_ends_at(pt.words, ref.end):
            return None
        following = joined_words(pt.words, ref.end + 1, 1, max_words + 2)
        if not following or following[0][0].strip(",;:").lower() not in connectors:
            return None
        rest = [text for text, _ in following[1:]]
        if rest and rest[0].lower() == "the":
            rest = rest[1:]
        found = known_title_prefix(book, rest[:max_words])
        return f"other book: {found}" if found else None
    return Check("known_title_after", fn)
