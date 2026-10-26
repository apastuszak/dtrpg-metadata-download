#!/usr/bin/env python3
"""
hyperlink_survey.py -- drafts a hyperlink rule set for a publisher from a
folder of that publisher's PDFs.

It measures, across the books:

  - where page numbers are printed (footer, header, or both), and whether
    the PDFs' own /PageLabels can be trusted;
  - how the Table of Contents and the Index can be found, whether they're
    already linked, and how tightly the Index is set;
  - how references are written ("p. 12", "page 12", "pages 12, 14 and 16",
    "Chapter 3") and how often each form appears;
  - what sits next to references that might name another book: a book-code
    prefix ("p. B123"), "of the <Title>" after the number (ALL CAPS or mixed
    case), an italic or comma-ended title before it, the line's name
    ("GURPS Magic, p. 12"), a sibling volume ("Book2").

From that it writes a draft rule-set module (hyperlink_rules format) whose
comments give the evidence for each choice, and a Markdown report with
sample sentences for every cross-book pattern, since deciding whether "page
85 of the Agent's Handbook" means another book takes reading examples.
Finally it runs the draft over copies of the same books and reports what it
linked and skipped.

The draft is a starting point to review, not a finished rule set: check the
known-titles list and the samples in the report, then copy the module into
hyperlink_rules/ (or use --install).

The input PDFs are only read. Validation runs on temporary copies.

USAGE:
    python3 hyperlink_survey.py --name pelgrane ~/RPGs/Pelgrane/
    python3 hyperlink_survey.py --name pelgrane a.pdf b.pdf c.pdf --install

Writes <out>/<name>.py (the draft) and <out>/<name>_survey.md (the report),
<out> defaulting to ./hyperlink-survey-<name>/.

REQUIREMENTS:
    pip install pymupdf
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import keyword
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz

import hyperlink_engine as engine
from hyperlink_checks import is_caps, joined_words, name_after_comma, normalize_title, sentence_ends_at, title_key

REFERENCE_WORDS = ("p.", "pp.", "pg.", "pgs.", "page", "pages")
TITLE_STOPWORDS = {"the", "a", "an", "of", "and"}
SAMPLES_PER_PATTERN = 8
CONTEXT_WORDS = 8


# ---------------------------------------------------------------------------
# What the survey records
# ---------------------------------------------------------------------------

@dataclass
class BookSurvey:
    path: Path
    title: str
    pages: int
    numbering: str = ""          # description, or the error
    numbering_ok: bool = False
    top_hits: int = 0            # pages whose printed number shows in the top 10%
    bottom_hits: int = 0         # ... in the bottom 45pt
    labels_trusted: bool = False
    second_run: bool = False     # a second numbering run (two volumes in one PDF)
    toc: dict = field(default_factory=dict)      # strategy -> pages
    index: dict = field(default_factory=dict)
    toc_entries: dict = field(default_factory=dict)  # style -> count
    toc_linked: bool = False
    index_linked: bool = False
    index_numbers: int = 0
    index_numbers_in_band: int = 0
    index_overlapping: int = 0
    chapters: int = 0
    refs: Counter = field(default_factory=Counter)
    vocab: frozenset = frozenset()   # this book's own TOC/Index entries
    page_headers: list = field(default_factory=list)  # (pdf page, N, word before) for capitalized "Page N" ending a line


@dataclass
class Survey:
    books: list = field(default_factory=list)
    forms: Counter = field(default_factory=Counter)        # "page" / "Page" / "p." ...
    patterns: Counter = field(default_factory=Counter)     # cross-book evidence
    samples: dict = field(default_factory=lambda: defaultdict(list))
    cited_titles: Counter = field(default_factory=Counter)  # "of the <Title>" phrases, not this book's own
    italic_candidates: Counter = field(default_factory=Counter)  # italic phrases before a number, for the report
    comma_names: Counter = field(default_factory=Counter)  # "(p. 94, SR6)": codes and titles after the number
    trigger_hits: Counter = field(default_factory=Counter)

    def sample(self, pattern, book, page_number, words, start, end):
        self.patterns[pattern] += 1
        if len(self.samples[pattern]) < SAMPLES_PER_PATTERN:
            lo, hi = max(0, start - CONTEXT_WORDS), min(len(words), end + CONTEXT_WORDS + 1)
            text = " ".join(w[4] for w in words[lo:hi])
            self.samples[pattern].append(f"{book.path.name} p.{page_number + 1}: …{text}…")


def book_title(doc, path):
    """The PDF's /Title, or a tidied file name: product codes, dates,
    edition and copy markers dropped ("2300AD_MGT_MGP20023_Ships_of_the_
    Frontier_OEF2022_05_31 (1)" -> "2300AD Ships of the Frontier")."""
    title = (doc.metadata or {}).get("title") or ""
    if not title.strip() or re.search(r"\.\w{2,5}$", title.strip()):
        title = re.sub(r"[_]+|(?<=\w)-(?=\w)", " ", path.stem)
        title = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", title)
    title = re.sub(r"\bOEF\s?\d{4}\s\d{2}\s\d{2}\b|\b(?:19|20)\d{2}(?:\s\d{2}\s\d{2}|[A-Za-z]*)\b"
                   r"|\b[A-Z]{2,5}\s?\d{3,}\b|\bOEF\S*|\b\d[Ee]\b|\b(?:MgT2?|MgP|MGT)\b"
                   r"|\(\d+\)|\[[^\]]*\]", " ", title)
    return re.sub(r"\s+", " ", title).strip(" -–—:")


# ---------------------------------------------------------------------------
# Measuring one book
# ---------------------------------------------------------------------------

def survey_book(path, survey):
    doc = fitz.open(path)
    bs = BookSurvey(path, book_title(doc, path), doc.page_count)
    survey.books.append(bs)
    try:
        pages = engine.band_vote(doc)
        bs.numbering, bs.numbering_ok = pages.description, True
    except (ValueError, RuntimeError) as exc:
        bs.numbering = str(exc)
        doc.close()
        return
    try:
        bs.labels_trusted = engine.footer_vote(doc, use_labels=True).labels is not None
    except (ValueError, RuntimeError):
        pass
    bs.second_run = second_numbering_run(doc, pages)
    for page in doc:
        n = str(pages.printed(page.number))
        r = page.rect
        top = page.get_text("words", clip=fitz.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * 0.10))
        bottom = engine.bottom_words(page)
        bs.top_hits += any(w[4].strip(".,:;|()[]") == n for w in top)
        bs.bottom_hits += any(w[4].strip(".,") == n for w in bottom)

    book = engine.Book(doc, engine.RuleSet(), path)
    book.pages = pages
    rules = engine.RuleSet()
    for how in ("footer_word", "heading", "front_matter"):
        bs.toc[how] = sorted(rules._section_pages(book, how, "contents"))
    for how in ("footer_word", "heading", "back_half_heading"):
        bs.index[how] = sorted(rules._section_pages(book, how, "index"))

    toc_pages = best_section_guess(bs.toc, doc, toc=True)
    index_pages = best_section_guess(bs.index, doc, toc=False)
    bs.toc_entries = {
        "trailing_number": sum(len(engine.trailing_number_entries(doc[i])) for i in toc_pages),
        "dot_leader_lines": sum(len(engine.dot_leader_entries(doc[i], pages.last)) for i in toc_pages),
    }
    bs.toc_linked = bool(toc_pages) and engine.has_internal_links(doc, toc_pages)
    bs.index_linked = bool(index_pages) and engine.has_internal_links(doc, index_pages)
    for i in index_pages:
        page = doc[i]
        found = engine.page_numbers_on(page, own_number=pages.printed(i))
        bs.index_numbers += len(found)
        h = page.rect.height
        bs.index_numbers_in_band += sum(1 for w, _ in found if w[1] < h * 0.10 or w[3] > h - engine.FOOTER_BAND_POINTS)
        rects = [engine.word_rect(w) for w, _ in found]
        for a in rects:
            if any(a is not b and a.y0 < b.y0 < a.y1 and min(a.x1, b.x1) > max(a.x0, b.x0) for b in rects):
                bs.index_overlapping += 1
    bs.chapters = len(engine.extract_chapters(doc, toc_pages, pages.target))
    bs.vocab = frozenset(engine.same_book_vocabulary(doc, toc_pages, index_pages))

    skip = set(toc_pages) | set(index_pages)
    for page in doc:
        if page.number not in skip:
            survey_page(engine.PageText(page), bs, survey)
    # In-document pagination: "Report Page 1" on one PDF page and "Report
    # Page 2" on the next. A heading that merely ends with "(PAGE 63)" or a
    # blurb ending "Page 29" doesn't step like that.
    headers = {(pg, n, before): span for pg, n, before, span in bs.page_headers}
    for (pg, n, before) in headers:
        if (pg + 1, n + 1, before) in headers:
            survey.patterns["page_header"] += 1
            if len(survey.samples["page_header"]) < SAMPLES_PER_PATTERN:
                survey.samples["page_header"].append(
                    f"{path.name} p.{pg + 1} and p.{pg + 2}: …{before} Page {n}… / …{before} Page {n + 1}…")
    doc.close()


def second_numbering_run(doc, pages):
    """True if at least 15% of pages agree on some offset other than the
    main one -- a combined two-volume PDF, which needs the page labels."""
    other = Counter()
    for page in doc:
        r = page.rect
        for clip in (fitz.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * 0.10),
                     fitz.Rect(r.x0, r.y1 - r.height * 0.10, r.x1, r.y1)):
            for off in {page.number - int(t) for t in
                        (w[4].strip(".,:;|()[]") for w in page.get_text("words", clip=clip))
                        if t.isdecimal() and 1 <= int(t) <= 999}:  # not isdigit(): "²" crashes int()
                if off != pages.offset:
                    other[off] += 1
    return bool(other) and other.most_common(1)[0][1] >= 0.15 * doc.page_count


def best_section_guess(found, doc, toc):
    """The strategy whose pages look most like a TOC (front of the book, a
    few pages) or an Index (back half, many numbers) -- only to pick which
    pages to measure entries on; the rule-set choice is made later, across
    all books."""
    def plausible(pages):
        if not pages or len(pages) > (15 if toc else 40):
            return False
        return pages[0] < doc.page_count * 0.25 if toc else pages[0] > doc.page_count * 0.5
    for pages in found.values():
        if plausible(pages):
            return pages
    return []


def survey_page(pt, bs, survey):
    words = pt.words
    for i, w in enumerate(words):
        tok = w[4]
        bare = re.sub(r"^\W+", "", tok)
        if engine.PP_SINGLE.match(tok):
            form = "p." if not bare.lower().startswith("pp") else "pp."
            nums = [(i, i)]
        elif bare.lower() in REFERENCE_WORDS and i + 1 < len(words) and re.match(r"^\(?[A-Z]{0,3}\d", words[i + 1][4]):
            form = bare
            nums = [(i, i + 1)]
            j = i + 1
            number = re.compile(r"^\d+(?:[\u2013\u2014-]\d+)?[,;.)]*$")   # not "6WC)"
            while True:   # "pages 12, 14 and 16"
                if words[j][4].endswith(",") and j + 1 < len(words) and number.match(words[j + 1][4]):
                    j += 1
                elif (j + 2 < len(words) and words[j + 1][4].lower() in ("and", "or", "&")
                      and number.match(words[j + 2][4])):
                    j += 2
                else:
                    break
                nums.append((j, j))
        elif re.match(r"^\W*Chapters?$", tok) and i + 1 < len(words) and re.match(r"^\d{1,2}\b", words[i + 1][4]):
            bs.refs["chapter"] += 1
            continue
        else:
            continue
        start, end = i, nums[-1][1]
        key = form.lower() if form.lower() in REFERENCE_WORDS else form
        bs.refs[key] += 1
        survey.forms[form] += 1
        if form[:1].isupper() and form.lower().startswith("page"):
            survey.sample("capitalized", bs, pt.number, words, start, end)
            last = nums[0][1]
            num = re.match(r"\(?(\d+)", words[last][4])
            if num and (last + 1 >= len(words) or words[last + 1][5:7] != words[last][5:7]):
                before = words[start - 1][4] if start else ""
                bs.page_headers.append((pt.number, int(num.group(1)), before, (start, end)))
        if len(nums) > 1:
            survey.sample("list", bs, pt.number, words, start, end)
        number_tok = words[nums[0][1]][4] if nums[0][1] != i else tok
        if re.search(r"\d[A-Z]{1,3}|[.\s (][A-Z]{1,3}\d", number_tok) or re.match(r"^\(?[A-Z]{1,3}\d", number_tok):
            survey.sample("book_code", bs, pt.number, words, start, end)
        m = re.search(r"(\d{2,4})[–—-](\d{1,3})\b", number_tok)
        if m and len(m.group(2)) < len(m.group(1)) and int(m.group(2)) < int(m.group(1)):
            survey.sample("short_range_end", bs, pt.number, words, start, end)
        survey_context(pt, bs, survey, start, end)


def survey_context(pt, bs, survey, start, end):
    words = pt.words
    # "(p. 94 and 96, SR6)" / "(pp. 132-3, Sixth World Companion)"
    name = name_after_comma(words, end)
    if name and not name[:1].isdigit() or (name and re.match(r"^\d[A-Z]{2,4}$", name)):
        if normalize_title(name) not in bs.vocab and not all(c.islower() for c in name if c.isalpha()):
            survey.sample("title_in_parentheses", bs, pt.number, words, start, end)
            first = name.split()[0].rstrip(").;:")
            # A code is one word ("SR6"); a run-on table cell keeps only that.
            survey.comma_names[first if re.match(r"^[A-Z0-9]{2,5}$", first) else name] += 1
    # After: "of the <Title>" / "in <Title>" / "of 3"
    after = [t for t, _ in joined_words(words, end + 1, 1, CONTEXT_WORDS)]
    # Only when the sentence carries on: "(p. 174). In 1967, ..." is a new
    # sentence, not "page 174 in <something>". The checks' own rule, so
    # the survey counts exactly what they'll read.
    if after and not sentence_ends_at(words, end) and after[0].lower().strip("(") in ("of", "in"):
        rest = after[1:]
        if rest and rest[0][:1].isdigit():
            if after[0].lower() == "of":
                survey.sample("page_n_of_m", bs, pt.number, words, start, end)
        else:
            if rest and rest[0].lower() == "the":
                rest = rest[1:]
            phrase = []
            for t in rest:
                clean = t.strip(".,;:)(’”")
                if not clean or not (clean[0].isupper() or clean[0].isdigit()) or len(clean) < 2:
                    break
                phrase.append(clean)
                if t.endswith((".", ",", ";", ":", ")")):
                    break
            if phrase:
                if all(is_caps(t) for t in phrase):
                    if len(phrase) >= 2:   # "in WWI" / "in GURPS" aren't section titles
                        survey.sample("caps_title_after", bs, pt.number, words, start, end)
                else:
                    kind = "mixed_title_after" if len(phrase) >= 2 else "one_word_title_after"
                    survey.sample(kind, bs, pt.number, words, start, end)
                    title = " ".join(phrase)
                    if len(phrase) >= 2 and normalize_title(title) not in bs.vocab:
                        survey.cited_titles[title] += 1
    # Before
    if start >= 1:
        prev = words[start - 1][4].strip("(),;:")
        if re.match(r"^Book\d{1,2}$", prev, re.I):
            survey.sample("sibling_volume", bs, pt.number, words, start, end)
    run, i = [], start - 1
    while i >= 0 and words[i][4].strip("(),.;:").lower() in ("", "see", "cf", "in", "on"):
        i -= 1
    while i >= 0 and len(run) < 6:
        clean = words[i][4].strip("(),.;:’”“")
        if not clean or not (clean[0].isupper() or clean[0].isdigit()):
            break
        run.insert(0, (clean, i))
        i -= 1
    if run:
        italic = all(pt.is_italic(j) for _, j in run)
        phrase = " ".join(t for t, _ in run)
        comma = start >= 1 and words[start - 1][4].endswith(",")
        if italic and not all(is_caps(t) for t, _ in run):
            survey.sample("italic_title_before_comma" if comma else "italic_title_before",
                          bs, pt.number, words, start, end)
            # Only suggested in the report: publishers italicize their own
            # section names this way too ("see Influence Rolls, p. 359").
            if normalize_title(phrase) not in bs.vocab:
                survey.italic_candidates[phrase] += 1
        elif comma and not all(is_caps(t) for t, _ in run):
            survey.sample("plain_title_before_comma", bs, pt.number, words, start, end)
        first = run[0][0].lower()
        if len(run) >= 2 and first not in TITLE_STOPWORDS:
            survey.trigger_hits[first] += 1


# ---------------------------------------------------------------------------
# Turning measurements into settings
# ---------------------------------------------------------------------------

@dataclass
class Choice:
    attr: str
    value: object
    why: str
    source: str | None = None   # Python source for the value, when repr() won't do


def decide(survey):
    books = [b for b in survey.books if b.numbering_ok]
    n = max(len(books), 1)
    choices = []
    p = survey.patterns

    # Page numbering
    top = sum(b.top_hits for b in books)
    bottom = sum(b.bottom_hits for b in books)
    labels = sum(b.labels_trusted for b in books)
    runs = sum(b.second_run for b in books)
    where = f"numbers seen in the header on {top} pages, the footer on {bottom}"
    if runs and labels:
        choices.append(Choice("page_numbers", "footer_or_labels",
                              f"{runs}/{n} books have a second numbering run (two volumes in one PDF), and the "
                              f"PDF's own page labels agree with the footers in {labels}/{n}; {where}"))
    else:
        choices.append(Choice("page_numbers", "header_and_footer",
                              f"one numbering run per book; {where}"))

    # TOC and Index finding: the strategy that finds a plausible section in most books
    def pick(attr, options, plausible, label):
        scores = {how: sum(1 for b in books if plausible(b, getattr(b, attr)[how])) for how in options}
        best = max(options, key=lambda how: scores[how])
        detail = ", ".join(f"{how}: {scores[how]}/{n}" for how in options)
        return best, scores[best], detail

    def toc_ok(b, pages):
        return 0 < len(pages) <= 15 and pages[0] < b.pages * 0.25
    def index_ok(b, pages):
        return 0 < len(pages) <= 40 and pages[0] > b.pages * 0.5

    # Ties go to the stricter method (listed first).
    toc, toc_score, detail = pick("toc", ("footer_word", "front_matter", "heading"), toc_ok, "TOC")
    choices.append(Choice("toc", toc if toc_score else None, f"books where each method found a TOC -- {detail}"))
    index, index_score, detail = pick("index", ("footer_word", "back_half_heading", "heading"), index_ok, "Index")
    choices.append(Choice("index", index if index_score else None, f"books where each method found an Index -- {detail}"))

    trailing = sum(b.toc_entries.get("trailing_number", 0) for b in books)
    leaders = sum(b.toc_entries.get("dot_leader_lines", 0) for b in books)
    style = "dot_leader_lines" if leaders > trailing else "trailing_number"
    choices.append(Choice("toc_entries_style", style,
                          f"TOC entries found: {trailing} as a trailing number, {leaders} as whole dot-leader lines"))
    toc_linked = sum(b.toc_linked for b in books)
    choices.append(Choice("toc_linked_when", "any_link",
                          f"{toc_linked}/{n} books already have a linked TOC; never add a second set"))
    index_linked = sum(b.index_linked for b in books)
    choices.append(Choice("index_skip_if_linked", bool(index_linked),
                          f"{index_linked}/{n} books already have a linked Index"))
    numbers = sum(b.index_numbers for b in books)
    in_band = sum(b.index_numbers_in_band for b in books)
    band_style = "all_but_own_number" if numbers and in_band > 0.01 * numbers else "outside_footer"
    choices.append(Choice("index_numbers", band_style,
                          f"{in_band} of {numbers} Index numbers sit in the header/footer band"))
    overlapping = sum(b.index_overlapping for b in books)
    inset = 0.12 if numbers and overlapping > 0.05 * numbers else 0.0
    choices.append(Choice("link_inset", inset,
                          f"{overlapping} of {numbers} Index numbers' boxes overlap the line below"))

    # References
    forms = survey.forms
    lower = Counter()
    for form, count in forms.items():
        lower[form.lower()] += count
    total = sum(lower.values())
    words = {w for w in REFERENCE_WORDS if lower[w] >= 3}
    for a, b in (("p.", "pp."), ("page", "pages"), ("pg.", "pgs.")):
        if a in words or b in words:   # singular and plural go together
            words |= {a, b}
    words = tuple(w for w in REFERENCE_WORDS if w in words)
    form_detail = ", ".join(f'"{f}": {c}' for f, c in forms.most_common())
    lists = p["list"]
    spelled = lower["page"] + lower["pages"]
    # List finding is built for spelled-out references; the single style
    # also knows glued "p.12" and book codes ("p. B123").
    style = "list" if lists >= 5 and spelled > total / 2 else "single"
    choices.append(Choice("reference_style", style,
                          f'{lists} of {total} references list several numbers ("pages 12, 14 and 16"); '
                          f'{spelled} use "page"/"pages"'))
    choices.append(Choice("reference_words", words or ("p.", "pp."), f"forms seen -- {form_detail}"))
    capitalized = sum(c for f, c in forms.items() if f[:1].isupper())
    if style == "list":
        skip_caps = p["page_n_of_m"] >= 3 or p["page_header"] >= 1
        choices.append(Choice("skip_capitalized_reference_word", skip_caps,
                              f'{capitalized} capitalized ("Page 12"); {p["page_header"]} in-document page '
                              f'headers numbered 1, 2, ... on consecutive pages; {p["page_n_of_m"]} "page N of M"'))
        choices.append(Choice("body_refs_on_toc_and_index", False,
                              "list-style references are only looked for outside the TOC and Index"))
    else:
        choices.append(Choice("expand_short_ranges", p["short_range_end"] >= 1,
                              f'{p["short_range_end"]} ranges with a shortened end ("152-3")'))
    chapter_refs = sum(b.refs["chapter"] for b in books)
    chapters = sum(b.chapters for b in books)
    choices.append(Choice("chapter_refs", style == "single" and chapter_refs >= 10 and chapters >= 3,
                          f'{chapter_refs} "Chapter N" references; {chapters} numbered chapters in the TOCs'
                          + ("" if style == "single" else " (only supported with single-style references)")))

    # The line's name and the trigger word
    titles = [b.title for b in survey.books]
    line = common_prefix_words(titles)
    trigger = None
    if line:
        candidate = next((w.lower() for w in re.findall(r"[A-Za-z0-9']+", line) if w.lower() not in TITLE_STOPWORDS), None)
        if candidate and survey.trigger_hits[candidate] >= 3:
            trigger = candidate
    choices.append(Choice("line_name", line, f"words shared by most book titles: {line!r}" if line
                          else "no common leading words in the book titles"))
    if trigger:
        choices.append(Choice("trigger_word", {"default": trigger},
                              f'"{trigger} <Title>" just before {survey.trigger_hits[trigger]} references'))

    # Known titles: the surveyed books themselves, and phrases cited as other books more than once
    known, keys = [], set()

    def add(t):
        t = t.replace("\u2019", "'").strip(" :-–—_,.")
        while line and t.lower().startswith(line.lower()):
            t = t[len(line):].strip(" :-–—_,.")
        t = re.sub(r"\s*\([^)]*\)$", "", t)   # "Double Clutch (Core Rigger Rulebook)"
        t = re.sub(r"\s+", " ", t)
        key = title_key(t)
        if len(key) >= 3 and key not in keys and not t.endswith(("-", "\u00ad")):
            keys.add(key)
            known.append(t)
    for b in survey.books:
        add(b.title)
    codes = [n for n, c in survey.comma_names.most_common() if c >= 2 and re.match(r"^[A-Z0-9]{2,5}$", n)]
    for name, count in survey.comma_names.most_common():
        if count >= 2 and name not in codes:
            add(name)
    for phrase, count in survey.cited_titles.most_common():
        words = phrase.split()
        if (count >= 2 and not is_caps(phrase) and words[0].lower() not in ("chapter", "chapters", "page", "pages")
                and not any(w.strip(".,").isdigit() for w in words)):
            add(phrase)
    known = known[:60]
    # The vocabulary build above already left out each citing book's own
    # sections; italic phrases before a number go to the report only.
    choices.append(Choice("known_titles", tuple(known),
                          "the surveyed books' own titles, then \"of the <Title>\" phrases cited at least "
                          "twice that aren't the citing book's own sections -- REVIEW THIS LIST; the report "
                          "suggests more"))

    # Cross-book checks
    checks = []
    why = []
    if p["book_code"] >= 5:
        checks.append("checks.book_code()")
        why.append(f'book-code prefixes ("p. B123"): {p["book_code"]}')
    if p["sibling_volume"] >= 3:
        checks.append('checks.sibling_volume(r"^Book\\d{1,2}$")')
        why.append(f'sibling volumes ("Book2 page 23"): {p["sibling_volume"]}')
    caps, mixed = p["caps_title_after"], p["mixed_title_after"] + p["one_word_title_after"]
    of_m = p["page_n_of_m"]
    if (caps >= 5 or of_m >= 3) and mixed >= 5:
        # Also skips "page N of M", and ALL CAPS after the number reads as
        # this book's own section.
        checks.append("checks.mixed_case_title_after()")
        why.append(f'title after the number: {mixed} mixed case, {caps} ALL CAPS (this book\'s sections), '
                   f'{of_m} "page N of M"')
    elif p["mixed_title_after"] >= 5:
        checks.append("checks.title_after(min_words=2)")
        why.append(f'"of the <Title>" after the number: {p["mixed_title_after"]} of two words or more')
    italic_comma, italic_bare = p["italic_title_before_comma"], p["italic_title_before"]
    if italic_comma >= 5 and italic_comma >= italic_bare:
        checks.append("checks.title_before_comma()")
        why.append(f'italic title before the number: {italic_comma} ending in a comma, {italic_bare} not')
    elif italic_comma + italic_bare >= 5:
        checks.append("checks.italic_known_title_before()")
        why.append(f'italic title before the number: {italic_comma + italic_bare} (only known titles are skipped)')
    if p["title_in_parentheses"] >= 5:
        checks.append(f"checks.title_in_parentheses(codes={tuple(codes)!r})")
        why.append(f'a book named after the number inside the parentheses ("(p. 94, SR6)"): '
                   f'{p["title_in_parentheses"]}; codes seen at least twice: {", ".join(codes) or "none"}')
    if trigger:
        checks.append("checks.trigger_word_before()")
        why.append(f'"{trigger} <Title>" before the number: {survey.trigger_hits[trigger]}')
    choices.append(Choice("other_book_checks", None, "; ".join(why) or "no cross-book pattern seen often enough",
                          source="(" + "".join(f"\n        {c}," for c in checks) + ("\n    )" if checks else ")")))
    if any(c.attr == "chapter_refs" and c.value for c in choices):
        chapter_checks = [c for c in checks if "trigger_word" in c or "italic" in c]
        choices.append(Choice("chapter_checks", None, "the before-the-reference checks above",
                              source="(" + "".join(f"\n        {c}," for c in chapter_checks)
                              + ("\n    )" if chapter_checks else ")")))
    return choices


def common_prefix_words(titles):
    """Leading words shared by at least 60% of titles ("Delta Green")."""
    split = [re.findall(r"[A-Za-z0-9']+", t) for t in titles if t]
    if not split:
        return ""
    best = ""
    for k in range(1, 4):
        heads = Counter(" ".join(w[:k]) for w in split if len(w) > k)
        if not heads:
            break
        head, count = heads.most_common(1)[0]
        if count >= 0.6 * len(split):
            best = head
        else:
            break
    return best


# ---------------------------------------------------------------------------
# Writing the draft and the report
# ---------------------------------------------------------------------------

def render_module(name, label, choices, survey):
    lines = [
        f'"""{label} -- DRAFT from hyperlink_survey.py, {len(survey.books)} books surveyed.',
        "",
        "Review before use: the known_titles list, and the samples in the survey",
        "report for each cross-book check. Every setting below says why it was",
        "chosen; RuleSet in hyperlink_engine.py documents what each one does.",
        '"""',
        "",
        "from hyperlink_engine import RuleSet, checks",
        "",
        "",
        "class Rules(RuleSet):",
        f"    label = {label!r}",
        f"    description = {('DRAFT: ' + summary(choices))!r}",
        "",
    ]
    for c in choices:
        if c.value is None and c.source is None:
            lines.append(f"    # {c.attr}: not found in any book ({c.why})")
            lines.append(f"    {c.attr} = None")
            continue
        for part in wrap_comment(c.why):
            lines.append(f"    # {part}")
        if c.source is not None:
            lines.append(f"    {c.attr} = {c.source}")
        elif isinstance(c.value, tuple) and len(c.value) > 3:
            lines.append(f"    {c.attr} = (")
            lines.extend(f"        {v!r}," for v in c.value)
            lines.append("    )")
        else:
            lines.append(f"    {c.attr} = {c.value!r}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def wrap_comment(text, width=72):
    out, line = [], ""
    for word in text.split():
        if line and len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return out + ([line] if line else [])


def summary(choices):
    get = {c.attr: c.value for c in choices}
    words = "/".join(f'"{w}"' for w in get.get("reference_words", ()))
    extra = [x for x, on in (('"Chapter N"', get.get("chapter_refs")), ("TOC", get.get("toc")),
                             ("Index", get.get("index"))) if on]
    return ", ".join([f"{words} references"] + extra)


def render_report(name, choices, survey, validation, module_path):
    out = [f"# Hyperlink survey: {name}", "",
           f"{len(survey.books)} books surveyed. Draft rule set: `{module_path.name}`.", "",
           "## Settings chosen", "", "| Setting | Value | Why |", "|---|---|---|"]
    for c in choices:
        value = " ".join(c.source.split()).replace("( ", "(").replace(", )", ")") if c.source else repr(c.value)
        if c.attr == "known_titles":
            value = f"{len(c.value)} titles (see the draft)"
        out.append(f"| `{c.attr}` | `{value}` | {c.why} |")
    out += ["", "## Books", "", "| Book | Pages | Numbering | Header / footer hits | TOC (footer word / heading / front matter) | Index (footer word / heading / back half) | References |",
            "|---|---|---|---|---|---|---|"]
    for b in survey.books:
        toc = " / ".join(fmt_pages(b.toc.get(k, [])) for k in ("footer_word", "heading", "front_matter"))
        idx = " / ".join(fmt_pages(b.index.get(k, [])) for k in ("footer_word", "heading", "back_half_heading"))
        refs = ", ".join(f"{k}: {v}" for k, v in b.refs.most_common())
        out.append(f"| {b.path.name} | {b.pages} | {b.numbering} | {b.top_hits} / {b.bottom_hits} | {toc} | {idx} | {refs} |")
    out += ["", "## Cross-book patterns, with samples", "",
            "Read these before trusting the checks: each pattern is a guess at how this publisher names "
            "*other* books. A pattern that's really this book's own sections must not become a check.", ""]
    names = {
        "book_code": 'Book-code prefix ("p. B123")',
        "sibling_volume": 'Sibling volume ("Book2 page 23")',
        "caps_title_after": "ALL CAPS title after the number (usually this book's own section)",
        "mixed_title_after": "Mixed-case title of two or more words after the number",
        "one_word_title_after": "Mixed-case single word after the number",
        "title_in_parentheses": 'A book named after the number, inside the parentheses ("(p. 94, SR6)")',
        "italic_title_before_comma": "Italic title right before the number, ending in a comma",
        "italic_title_before": "Italic title right before the number, no comma",
        "plain_title_before_comma": "Plain capitalized phrase before the number, ending in a comma (often a character or place)",
        "page_n_of_m": '"page N of M" (a document\'s own pagination)',
        "list": 'Several numbers after one reference word ("pages 12, 14 and 16")',
        "capitalized": 'Capitalized "Page 12" (skipped only with evidence of in-document pagination, below)',
        "page_header": 'A document\'s own page headers ("Report Page 1", then "Report Page 2" on the next page)',
        "short_range_end": 'Range with a shortened end ("152-3")',
    }
    for key, title in names.items():
        if not survey.patterns[key]:
            continue
        out += [f"### {title}: {survey.patterns[key]}", ""]
        out += [f"- {s}" for s in survey.samples[key]]
        out.append("")
    if survey.italic_candidates:
        out += ["## Italic phrases before a page number", "",
                "Not added to `known_titles` automatically: publishers italicize their own section names "
                "this way as often as other books' titles, and a wrong entry skips real links. Add the ones "
                "that are titles of *other* books (phrases in the citing book's own TOC or Index are "
                "already left out).", ""]
        out += [f"- {phrase} ({count})" for phrase, count in survey.italic_candidates.most_common(40)]
        out.append("")
    if validation:
        out += ["## Trial run with the draft", "",
                "The draft run over temporary copies of the same books (the originals weren't touched).", "",
                "| Book | Page refs | Index | TOC | Chapters | Skipped: other book | Skipped: out of range | Skipped: already linked |",
                "|---|---|---|---|---|---|---|---|"]
        for row in validation:
            out.append("| " + " | ".join(str(v) for v in row) + " |")
        out.append("")
    out += ["## Before installing", "",
            "1. Check `known_titles` in the draft: remove anything that isn't really a book title, and add "
            "titles this publisher cites that weren't in the surveyed books.",
            "2. Read the samples above for each check the draft uses.",
            "3. Look at a few `_link_report.csv` files from a trial run (`hyperlink_engine.py --rules ...`) "
            "for wrong links.",
            "4. Copy the draft into `hyperlink_rules/`, and give it a clearer `description`.", ""]
    return "\n".join(out)


def fmt_pages(pages):
    if not pages:
        return "–"
    if len(pages) > 3:
        return f"{pages[0] + 1}–{pages[-1] + 1} ({len(pages)})"
    return ", ".join(str(p + 1) for p in pages)


# ---------------------------------------------------------------------------
# Trial run
# ---------------------------------------------------------------------------

def load_draft(module_path):
    spec = importlib.util.spec_from_file_location(f"hyperlink_survey_draft_{module_path.stem}", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Rules


def trial_run(rules_cls, pdfs, log):
    rows = []
    with tempfile.TemporaryDirectory(prefix="hyperlink-survey-") as tmp:
        for path in pdfs:
            out = Path(tmp) / f"{path.stem}.pdf"
            book_counts = Counter()
            try:
                engine.hyperlink_pdf(str(path), str(out), rules_cls, log=lambda line: None)
                report = Path(tmp) / f"{path.stem}_link_report.csv"
                with open(report, newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        status, detail = r["Status"], r["Detail"]
                        if status.startswith("added"):
                            kind = ("toc" if "TOC" in status or r["Matched Text"].startswith("TOC:")
                                    else "chapter" if "chapter" in status
                                    else "index" if r["Matched Text"].startswith("Index:") else "body")
                            book_counts[kind] += 1
                        elif detail == "already linked":
                            book_counts["existing"] += 1
                        elif "range" in detail:
                            book_counts["range"] += 1
                        else:
                            book_counts["other"] += 1
                rows.append((path.name, book_counts["body"], book_counts["index"], book_counts["toc"],
                             book_counts["chapter"], book_counts["other"], book_counts["range"], book_counts["existing"]))
            except Exception as exc:
                rows.append((path.name, f"failed: {exc}", "", "", "", "", "", ""))
            log(f"  trial: {path.name}")
            for p in Path(tmp).iterdir():
                p.unlink()
    return rows


# ---------------------------------------------------------------------------

def collect_pdfs(paths):
    pdfs = []
    for p in map(Path, paths):
        if p.is_dir():
            pdfs += sorted(q for q in p.rglob("*.pdf") if not q.name.startswith("."))
        elif p.suffix.lower() == ".pdf":
            pdfs.append(p)
    return pdfs


def main():
    parser = argparse.ArgumentParser(description="Draft a hyperlink rule set from a publisher's PDFs.")
    parser.add_argument("paths", nargs="+", help="PDFs, or folders of PDFs, all from one publisher")
    parser.add_argument("--name", required=True, help="rule-set name (the module name; letters, digits, _)")
    parser.add_argument("--label", help='shown in the GUI, e.g. "Pelgrane Press" (default: from --name)')
    parser.add_argument("--out", help="output folder (default: ./hyperlink-survey-NAME)")
    parser.add_argument("--install", action="store_true",
                        help="also copy the draft into hyperlink_rules/ (refuses to overwrite)")
    parser.add_argument("--no-trial", action="store_true", help="skip the trial run")
    args = parser.parse_args()

    if not re.match(r"^[a-z][a-z0-9_]*$", args.name) or keyword.iskeyword(args.name):
        parser.error("--name must be lowercase letters, digits and underscores, starting with a letter")
    pdfs = collect_pdfs(args.paths)
    if not pdfs:
        parser.error("no PDFs found")
    out = Path(args.out or f"hyperlink-survey-{args.name}")
    out.mkdir(parents=True, exist_ok=True)
    label = args.label or args.name.replace("_", " ").title()

    survey = Survey()
    for i, path in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] surveying {path.name}")
        try:
            survey_book(path, survey)
        except Exception as exc:
            print(f"  skipped: {exc}")
    choices = decide(survey)
    module_path = out / f"{args.name}.py"
    module_path.write_text(render_module(args.name, label, choices, survey), encoding="utf-8")
    print(f"Wrote draft rule set: {module_path}")

    validation = None
    if not args.no_trial:
        print("Trial run of the draft on copies of the books...")
        validation = trial_run(load_draft(module_path), pdfs, print)
    report_path = out / f"{args.name}_survey.md"
    report_path.write_text(render_report(args.name, choices, survey, validation, module_path), encoding="utf-8")
    print(f"Wrote survey report: {report_path}")

    if args.install:
        dest = Path(engine.__file__).parent / "hyperlink_rules" / module_path.name
        if dest.exists():
            sys.exit(f"Not installed: {dest} already exists")
        shutil.copyfile(module_path, dest)
        print(f"Installed: {dest} (now --hyperlink {args.name})")


if __name__ == "__main__":
    main()
