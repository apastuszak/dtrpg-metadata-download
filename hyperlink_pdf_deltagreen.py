#!/usr/bin/env python3
"""
hyperlink_pdf_deltagreen.py -- hyperlinks a Delta Green PDF's own
cross-references (Arc Dream Publishing), the same job
hyperlink_pdf_universal.py (GURPS) and hyperlink_pdf_mongoose.py (Mongoose
Traveller) do for their lines, rewritten for how Delta Green books write:

  - Body references use the word "page": "See COMBAT on page 48",
    "(page 164)", "pages 12-13", "pages 294, 296 and 300". ("p."/"pp." are
    accepted too, but almost never used.)
  - Same-book references name a section in ALL CAPS ("See HUGE, page 60",
    "on page 177 in THE SENECA WHIRLWIND REPORT") and are linked.
  - Other-book references name a book in mixed case, usually italic, either
    after the number ("page 85 of the Agent's Handbook", "page 32 of
    Impossible Landscapes") or before it ("see the Handler's Guide, page
    312"). Those are skipped -- unless the title is this book's own.
  - "page 1 of 3" (an in-fiction document's own pagination on a handout)
    is skipped.

The Table of Contents and the Index are linked too, each only if it has no
internal links already. Nothing that is already a link is linked again,
so running this twice adds nothing.

Printed page numbers are read from each page's header/footer band and
voted into one offset (printed number -> PDF page), the same way the
other scripts and page_labels.py do.

USAGE:
    python3 hyperlink_pdf_deltagreen.py INPUT.pdf OUTPUT.pdf

Writes OUTPUT.pdf plus OUTPUT_link_report.csv (every reference found,
linked or skipped, and why).

REQUIREMENTS:
    pip install pymupdf
"""

import csv
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf as fitz  # PyMuPDF -- `import fitz` is the deprecated alias

BAND_FRACTION = 0.10
TOC_SEARCH_PAGES = 15
TOC_MIN_ENTRIES = 5
INDEX_MIN_NUMBERS = 20
MAX_TITLE_WORDS = 6

# Book titles Delta Green cites by name. Used for the "title before the
# page number" case, where a plain capitalized phrase could just as well be
# a same-book NPC ("see Sheryl Krieger, page 68"); a title after "of"/"in"
# is recognised by its mixed case alone.
KNOWN_TITLES = {
    "agentshandbook", "handlersguide", "impossiblelandscapes", "needtoknow",
    "blacksites", "controlgroup", "staticprotocol", "thecomplex", "thelabyrinth",
    "anightattheopera", "conspiracy", "countdown",
    "godsteeth", "iconoclasts", "extremophilia", "observereffect", "viscid",
    "sweetness", "kalighati", "loverintheice", "pxpokernight", "thelastequation",
    "hourglass", "exoblivione", "thechild", "musicfromadarkenedroom",
}

PAGE_WORD = re.compile(r"^\(?(pages?|pp?\.)$", re.I)
NUMBER = re.compile(r"^\(?(\d{1,3})(?:[–—-](\d{1,3}))?[.,;:)!?’”]*$")
LEADER_NUMBER = re.compile(r"^[.…\s]*(\d{1,3})$")
SOFT_BREAK = ("-", "­")


def normalize(title):
    return re.sub(r"[^a-z]", "", title.lower().replace("’", "").replace("'", ""))


def own_title_key(doc, path):
    """This book's own title, for recognising "page N of <this book>"."""
    title = (doc.metadata or {}).get("title") or ""
    key = normalize(title) or normalize(Path(path).stem.replace("deltagreen", ""))
    return key.replace("deltagreen", "") or key


# --- page numbering ---------------------------------------------------------

def page_offset(doc):
    """(offset, first_printed, last_printed): PDF index = printed + offset."""
    votes = defaultdict(set)
    for i, page in enumerate(doc):
        r = page.rect
        band = r.height * BAND_FRACTION
        for clip in (fitz.Rect(r.x0, r.y0, r.x1, r.y0 + band), fitz.Rect(r.x0, r.y1 - band, r.x1, r.y1)):
            for w in page.get_text("words", clip=clip):
                tok = w[4].strip(".,:;|()[]")
                if tok.isdigit() and 1 <= int(tok) <= 999:
                    votes[i - int(tok)].add(i)
    if not votes:
        raise ValueError("Couldn't detect any page numbers in headers/footers")
    offset, pages = max(votes.items(), key=lambda kv: (len(kv[1]), -kv[0]))
    if len(pages) < 3:
        raise ValueError("Couldn't detect a consistent page numbering")
    first = max(1, -offset)
    last = doc.page_count - 1 - offset
    return offset, first, last


# --- text helpers -------------------------------------------------------------

def italic_rects(page):
    rects = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                font = span.get("font", "").lower()
                if span.get("flags", 0) & 2 or "italic" in font or "oblique" in font:
                    rects.append(fitz.Rect(span["bbox"]))
    return rects


def is_italic(word, rects):
    r = fitz.Rect(word[:4])
    return any((r & s).get_area() > 0.5 * r.get_area() for s in rects if r.intersects(s))


def joined_words(words, start, step, limit):
    """Up to `limit` words from `start` walking by `step`, re-joining words
    hyphenated across a line break ("Impos-" + "sible")."""
    out = []
    i = start
    while 0 <= i < len(words) and len(out) < limit:
        text = words[i][4]
        if step > 0 and text.endswith(SOFT_BREAK) and i + 1 < len(words):
            text = text.rstrip("".join(SOFT_BREAK)) + words[i + 1][4]
            out.append((text, [i, i + 1]))
            i += 2
            continue
        if step < 0 and i - 1 >= 0 and words[i - 1][4].endswith(SOFT_BREAK):
            text = words[i - 1][4].rstrip("".join(SOFT_BREAK)) + text
            out.append((text, [i - 1, i]))
            i -= 2
            continue
        out.append((text, [i]))
        i += step
    return out


def is_caps(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def title_after(words, i, italics, own_key):
    """A book title right after a reference ("... of the Agent's Handbook")
    -> the title if it's another book, else None."""
    following = joined_words(words, i, 1, MAX_TITLE_WORDS + 2)
    if not following or following[0][0].lower().strip("(") not in ("of", "in"):
        return None
    rest = following[1:]
    if rest and rest[0][0].isdigit():
        return "in-document page count (\"page N of M\")"
    if rest and rest[0][0].lower() == "the":
        rest = rest[1:]
    phrase = []
    for text, idxs in rest:
        clean = text.strip(".,;:)(’”")
        if not clean or not (clean[0].isupper() or clean[0].isdigit()):
            break
        phrase.append((clean, idxs))
        if text.endswith((".", ",", ";", ":", ")")):
            break
    if not phrase:
        return None
    title = " ".join(t for t, _ in phrase)
    if all(is_caps(t) for t, _ in phrase):
        return None  # ALL CAPS: a section of this book
    if normalize(title).startswith(own_key) or (own_key and own_key in normalize(title)):
        return None
    return title


def title_before(words, i, italics, own_key):
    """A book title right before a reference ("see the Handler's Guide,
    page 312") -> the title if it's another book, else None."""
    if i == 0 or not words[i - 1][4].endswith(","):
        return None
    preceding = joined_words(words, i - 1, -1, MAX_TITLE_WORDS)
    phrase = []
    for text, idxs in preceding:
        clean = text.strip(".,;:)(’”“")
        if not clean or clean.lower() in ("see", "the", "in", "and", "also") or not (clean[0].isupper() or clean[0].isdigit()):
            break
        phrase.insert(0, (clean, idxs))
    if not phrase:
        return None
    title = " ".join(t for t, _ in phrase)
    if all(is_caps(t) for t, _ in phrase):
        return None
    key = normalize(title)
    if own_key and own_key in key:
        return None
    italic = all(is_italic(words[j], italics) for _, idxs in phrase for j in idxs)
    if italic or any(key.endswith(t) or t.endswith(key) and len(key) > 6 for t in KNOWN_TITLES):
        return title
    return None


def rects_overlap(a, b):
    inter = a & b
    return not inter.is_empty and inter.get_area() > 0.3 * min(a.get_area(), b.get_area())


# --- TOC and Index detection -------------------------------------------------

def line_groups(page):
    lines = defaultdict(list)
    for w in page.get_text("words"):
        lines[(w[5], w[6])].append(w)
    return [sorted(ws, key=lambda w: w[7]) for ws in lines.values()]


def toc_entries(page, last_printed):
    """(rect, printed number) for each "Entry ....... 12" line."""
    entries = []
    for ws in line_groups(page):
        if len(ws) == 1:
            # The entry, leader and number can come out as one word:
            # "Recovery..........75".
            m = re.match(r"^(.*?[^.…\s])[.…]{2,}(\d{1,3})$", ws[0][4])
            if m and int(m.group(2)) <= last_printed:
                entries.append((fitz.Rect(ws[0][:4]), int(m.group(2)), m.group(1)[:60]))
            continue
        m = LEADER_NUMBER.match(ws[-1][4].strip().lstrip(".").replace("…", "")) or re.match(r"^.*?\.{2,}(\d{1,3})$", ws[-1][4])
        if not m:
            continue
        num = int(m.group(1))
        text_words = [w for w in ws[:-1] if w[4].strip(".…")]
        if not text_words or num > last_printed:
            continue
        rect = fitz.Rect(text_words[0][:4])
        for w in ws:
            rect |= fitz.Rect(w[:4])
        entries.append((rect, num, " ".join(w[4] for w in text_words)[:60]))
    return entries


def detect_toc_pages(doc, last_printed):
    pages = []
    for i in range(min(doc.page_count, TOC_SEARCH_PAGES)):
        page = doc[i]
        has_heading = re.search(r"\bcontents\b", page.get_text()[:400], re.I) is not None
        entries = toc_entries(page, last_printed)
        if (has_heading or (pages and pages[-1] == i - 1)) and len(entries) >= TOC_MIN_ENTRIES:
            pages.append(i)
    return pages


def heading_is(page, word):
    spans = [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
    if not spans:
        return False
    big = max(s["size"] for s in spans)
    return any(s["text"].strip().strip("/ ").lower() == word and s["size"] >= max(14, big * 0.8) for s in spans)


def index_numbers(page, own_number=None):
    """Page numbers on an index page. Entries run right up into the
    header/footer band, so only the page's own printed number is skipped
    there, not every number in the band."""
    h = page.rect.height
    out = []
    for w in page.get_text("words"):
        m = NUMBER.match(w[4])
        if not m:
            continue
        in_band = w[1] < h * BAND_FRACTION or w[3] > h * (1 - BAND_FRACTION)
        if in_band and int(m.group(1)) == own_number:
            continue
        out.append((w, int(m.group(1))))
    return out


def detect_index_pages(doc):
    start = next((i for i in range(doc.page_count - 1, doc.page_count // 2, -1) if heading_is(doc[i], "index")), None)
    if start is None:
        return []
    pages = [start]
    for i in range(start + 1, doc.page_count):
        if len(index_numbers(doc[i])) < INDEX_MIN_NUMBERS:
            break
        pages.append(i)
    return pages


def has_internal_links(doc, pages):
    # Named-destination links count too: InDesign exports its TOC and
    # cross-reference links that way, and several Delta Green PDFs have them.
    internal = (fitz.LINK_GOTO, fitz.LINK_NAMED)
    return any(link.get("kind") in internal for i in pages for link in doc[i].get_links())


# --- main -------------------------------------------------------------------

def hyperlink_pdf(in_path, out_path):
    """Process a single PDF: copy it to out_path, add links in place, and
    write a CSV report alongside it."""
    shutil.copyfile(in_path, out_path)
    doc = fitz.open(out_path)
    offset, first_printed, last_printed = page_offset(doc)
    own_key = own_title_key(doc, in_path)

    def target(num):
        idx = num + offset
        return idx if first_printed <= num <= last_printed and 0 <= idx < doc.page_count else None

    print(f"Page numbering: printed {first_printed}-{last_printed}, PDF page = printed + {offset + 1}")
    toc_pages = detect_toc_pages(doc, last_printed)
    index_pages = detect_index_pages(doc)
    print(f"Table of Contents: {'PDF pages ' + ', '.join(str(p + 1) for p in toc_pages) if toc_pages else 'not found'}")
    print(f"Index: {'PDF pages ' + str(index_pages[0] + 1) + '-' + str(index_pages[-1] + 1) if index_pages else 'not found'}")

    report = []
    counts = Counter()

    def add_link(page, rect, num, text, existing, kind):
        idx = target(num)
        if idx is None:
            counts["skipped_range"] += 1
            report.append((page.number + 1, text, num, "skipped", "out of page-number range"))
            return
        if any(rects_overlap(rect, r) for r in existing):
            counts["skipped_existing"] += 1
            report.append((page.number + 1, text, num, "skipped", "already linked"))
            return
        # Trim a sliver off the top and bottom: tightly set index lines
        # otherwise overlap, and a click on the boundary hits the wrong line.
        inset = rect.height * 0.12
        rect = fitz.Rect(rect.x0, rect.y0 + inset, rect.x1, rect.y1 - inset)
        page.insert_link({"kind": fitz.LINK_GOTO, "page": idx, "from": rect, "to": fitz.Point(0, 0)})
        existing.append(rect)
        counts[kind] += 1
        report.append((page.number + 1, text, num, "added", f"pdf page {idx + 1}"))

    # Table of Contents
    if toc_pages and has_internal_links(doc, toc_pages):
        print("  Table of Contents already has links -- leaving it alone")
    else:
        for i in toc_pages:
            page = doc[i]
            existing = [l["from"] for l in page.get_links()]
            for rect, num, text in toc_entries(page, last_printed):
                add_link(page, rect, num, f"TOC: {text}", existing, "toc_added")

    # Index
    if index_pages and has_internal_links(doc, index_pages):
        print("  Index already has links -- leaving it alone")
    else:
        for i in index_pages:
            page = doc[i]
            existing = [l["from"] for l in page.get_links()]
            for w, num in index_numbers(page, own_number=i - offset):
                add_link(page, fitz.Rect(w[:4]), num, f"Index: {w[4]}", existing, "index_added")

    # Body references
    skip_pages = set(toc_pages) | set(index_pages)
    for page in doc:
        if page.number in skip_pages:
            continue
        words = page.get_text("words")
        if not words:
            continue
        existing = [l["from"] for l in page.get_links()]
        italics = italic_rects(page)
        i = 0
        while i < len(words):
            word = words[i][4]
            if not PAGE_WORD.match(word) or word[0].isupper() and word.lower().startswith("page"):
                i += 1
                continue
            # Consecutive numbers after the page word: "pages 12, 14 and 16".
            refs = []
            j = i + 1
            while j < len(words):
                m = NUMBER.match(words[j][4])
                if m:
                    refs.append((j, int(m.group(1))))
                    j += 1
                    if not words[j - 1][4].endswith(","):
                        if j < len(words) and words[j][4].lower() in ("and", "or", "&") and j + 1 < len(words) and NUMBER.match(words[j + 1][4]):
                            j += 1
                            continue
                        break
                    continue
                break
            if not refs:
                i += 1
                continue
            last_word = refs[-1][0]
            text = " ".join(w[4] for w in words[i:last_word + 1])
            other = title_after(words, last_word + 1, italics, own_key) or title_before(words, i, italics, own_key)
            if other:
                counts["skipped_title"] += len(refs)
                for _, num in refs:
                    report.append((page.number + 1, text, num, "skipped", f"other book: {other}"))
                i = last_word + 1
                continue
            for n, (j, num) in enumerate(refs):
                rect = fitz.Rect(words[j][:4])
                if n == 0 and abs(words[i][1] - words[j][1]) < 2:  # "page" on the same line: link "page 48" as one
                    rect |= fitz.Rect(words[i][:4])
                add_link(page, rect, num, text, existing, "added")
            i = last_word + 1

    doc.saveIncr()
    doc.close()

    report_path = Path(out_path).with_name(Path(out_path).stem + "_link_report.csv")
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["PDF Page", "Matched Text", "Ref Number", "Status", "Detail"])
        for row in report:
            writer.writerow([row[0], _csv_safe(row[1]), row[2], row[3], _csv_safe(row[4])])

    print(f"\nAdded {counts['added']} page-reference links")
    print(f"  Skipped {counts['skipped_existing']} (already linked)")
    print(f"  Skipped {counts['skipped_title']} (reference to another book)")
    print(f"  Skipped {counts['skipped_range']} (out of page-number range)")
    print(f"Added {counts['index_added']} Index links")
    print(f"Added {counts['toc_added']} Table of Contents links")
    print(f"Wrote {out_path}")
    print(f"Wrote report: {report_path}")
    return {
        "added": counts["added"] + counts["index_added"],
        "chapter_added": 0,
        "toc_added": counts["toc_added"],
    }


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in text copied from the PDF."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    in_path, out_path = sys.argv[1], sys.argv[2]
    if Path(in_path).resolve() == Path(out_path).resolve():
        sys.exit("Output path must be different from the input path.")
    hyperlink_pdf(in_path, out_path)


if __name__ == "__main__":
    main()
