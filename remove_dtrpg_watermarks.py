#!/usr/bin/env python3
import os
import re
import sys
from collections import Counter, defaultdict

import pymupdf as fitz

# Watermark zone: the lower-left corner, as a fraction of the page size.
# A line qualifies if it starts in the left CORNER_X_FRACTION of the page
# and its bottom edge is inside the bottom CORNER_Y_FRACTION of the page.
CORNER_X_FRACTION = 0.25
CORNER_Y_FRACTION = 0.10

# Auto-detect (blank prompt): a corner line counts as a watermark candidate
# if the exact same text appears on at least this fraction of pages.
AUTO_MIN_PAGE_FRACTION = 0.90

# a top-level text object in a PDF content stream; a false match here is
# harmless because every removal is verified against the page's extracted
# text afterward and reverted if the result isn't exactly what we expect
TEXT_OBJECT = re.compile(rb"(?<![A-Za-z])BT(?![A-Za-z]).*?(?<![A-Za-z])ET(?![A-Za-z])", re.S)

# --repair: fraction of a document's pages that must have the old-output
# shape (see old_output_pages) before the script treats it as old-method
# output -- both to offer a repair and, in normal mode, to hint that
# --repair might be what's actually wanted.
OLD_OUTPUT_MIN_FRACTION = 0.90

# --repair: a page content stream produced by the OLD (pre-stream-removal)
# version of this script: "/<name> Do q Q" drawing a single rewritten Form
# XObject, optionally followed by more drawing (see repair_page).
OLD_OUTPUT_RE = re.compile(rb"\A\s*/(\S+)\s+Do\s+q\s+Q")

# --repair: resource subdictionaries whose entries make up a Form XObject's
# "signature" for matching a rewritten form back to its pre-redaction original.
XOBJECT_RESOURCE_SUBDICTS = ("Font", "ColorSpace", "ExtGState", "Pattern", "Shading", "Properties")

# --repair: a repair candidate's rendered page is compared against the
# damaged page's own render at this DPI, to catch a false structural match.
REPAIR_RENDER_DPI = 20

# --repair: maximum mean per-byte render difference (at REPAIR_RENDER_DPI) to
# accept a candidate as the real original. Calibrated against the sample
# PDFs: genuine matches scored <= 0.037, while the closest pair of
# genuinely different pages in either book scored 3.37 -- 0.5 leaves a wide
# margin without being tight enough for rendering noise to reject a real match.
REPAIR_MATCH_THRESHOLD = 0.5


def in_corner(page_rect, bbox):
    bb = fitz.Rect(bbox)
    return (
        bb.x0 <= page_rect.x0 + page_rect.width * CORNER_X_FRACTION
        and bb.y1 >= page_rect.y1 - page_rect.height * CORNER_Y_FRACTION
    )


def page_lines(page):
    """Every line on the page as (text, rounded bbox), for before/after comparison."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            bbox = tuple(round(v, 1) for v in line["bbox"])
            out.append((text, bbox))
    return out


def collect_lines(doc):
    """Read every page once: [(text, fitz.Rect, in_corner), ...] per page.

    Shared by detect() and scan() so the (slow, ~5s/book) text extraction
    happens exactly once, regardless of which mode (auto-detect vs. partial
    text) the user picks.
    """
    lines_by_page = []
    for page in doc:
        page_out = []
        for block in page.get_text("dict")["blocks"]:
            if "lines" not in block:
                continue  # image block, no text to match
            for line in block["lines"]:
                text = "".join(span["text"] for span in line["spans"])
                rect = fitz.Rect(line["bbox"])
                page_out.append((text, rect, in_corner(page.rect, rect)))
        lines_by_page.append(page_out)
    return lines_by_page


def detect(lines_by_page):
    """Find corner lines whose exact text repeats on >= AUTO_MIN_PAGE_FRACTION of pages.

    Returns [(text, page_count), ...], highest count first then by text. Only
    exact, identical text counts -- this is what excludes page numbers, which
    differ page to page even though they also sit in the corner.
    """
    page_counts = Counter()
    for page_out in lines_by_page:
        texts_on_page = {text.strip() for text, _, corner in page_out if corner and text.strip()}
        page_counts.update(texts_on_page)

    threshold = AUTO_MIN_PAGE_FRACTION * len(lines_by_page)
    candidates = [(text, count) for text, count in page_counts.items() if count >= threshold]
    candidates.sort(key=lambda tc: (-tc[1], tc[0]))
    return candidates


def scan(lines_by_page, match):
    """Find every line whose text satisfies `match(text)`.

    Returns (matches, outside_pages) where matches is a list of
    (page_index, fitz.Rect, line_text) for lines in the lower-left corner,
    and outside_pages is the set of 0-based page indexes where the text
    matched but the line was outside the corner (left alone).
    """
    matches = []
    outside_pages = set()

    for page_index, page_out in enumerate(lines_by_page):
        for text, rect, corner in page_out:
            if not match(text):
                continue
            if corner:
                matches.append((page_index, rect, text))
            else:
                outside_pages.add(page_index)

    return matches, outside_pages


def strip_from_streams(doc, page, targets, usage):
    """Try to delete each target line's text object directly from the page's
    own content stream, leaving everything else byte-for-byte untouched.

    This avoids apply_redactions() rewriting the whole page's text (which can
    subtly reflow/shift unrelated text -- see CLAUDE.md). Only page-owned
    (non-shared) streams are edited, and every edit is verified against the
    page's extracted text and reverted if it did anything unexpected.

    `targets` is [(rect, line_text), ...] for this page. Returns the list of
    targets that could NOT be removed this way, for the caller to redact instead.
    """
    remaining = {(text, tuple(round(v, 1) for v in rect)) for rect, text in targets}
    if not remaining:
        return []

    before = page_lines(page)

    for xref in page.get_contents():
        if usage[xref] > 1:
            continue  # shared by other pages -- editing it would affect them unverified
        if not remaining:
            break

        original = doc.xref_stream(xref) or b""
        current = original
        changed = False

        for match in TEXT_OBJECT.finditer(original):
            candidate = current[: match.start()] + current[match.end() :]
            doc.update_stream(xref, candidate)
            after = page_lines(page)

            removed = Counter(before) - Counter(after)
            added = Counter(after) - Counter(before)
            if removed and not added and set(removed) <= remaining:
                current = candidate
                before = after
                remaining -= set(removed)
                changed = True
                if not remaining:
                    break
            else:
                doc.update_stream(xref, current)  # revert this attempt

        if not changed:
            doc.update_stream(xref, original)  # no-op, but keep this explicit

    leftover = [(rect, text) for rect, text in targets if (text, tuple(round(v, 1) for v in rect)) in remaining]
    return leftover


def choose_candidates(candidates):
    """Prompt the user to pick one or more auto-detected candidates.

    Returns the set of chosen texts, or None if the user aborted.
    """
    print("Auto-detected repeating lines in the lower-left corner:")
    for i, (text, count) in enumerate(candidates, start=1):
        print(f'  {i}. "{text}"  on {count} page(s)')

    while True:
        choice = input("Which line(s) to remove? (e.g. 2 or 1,2; blank to abort): ").strip()
        if not choice:
            return None

        parts = [p.strip() for p in choice.split(",") if p.strip()]
        indexes = []
        valid = True
        for part in parts:
            # isascii() guards against non-ASCII digits (e.g. superscript "²")
            # that pass str.isdigit() but raise in int()
            if not (part.isascii() and part.isdigit()) or not (1 <= int(part) <= len(candidates)):
                valid = False
                break
            indexes.append(int(part))

        if valid and indexes:
            return {candidates[i - 1][0] for i in indexes}

        print("Please enter numbers from the list, e.g. 1 or 1,2.")


def xobject_signature(doc, xref):
    """A Form XObject's resource fingerprint: the object numbers it references.

    Used by repair_page() to test whether a rewritten form (produced by
    apply_redactions on old-method output) could have come from a given
    orphaned original -- see the subset comparison there. The rewrite drops
    unused fonts, reorders entries, and drops /ProcSet, so this compares sets
    of references rather than the resource dict text.

    Image entries in Resources/XObject are included (with an "img" prefix,
    to keep them out of the same namespace as other object numbers) because
    a nested Form XObject gets a new xref on rewrite but a nested Image keeps
    its own -- without this, two textless art pages with the same fonts (i.e.
    none) would be indistinguishable.
    """
    refs = set()
    for sub in XOBJECT_RESOURCE_SUBDICTS:
        typ, val = doc.xref_get_key(xref, f"Resources/{sub}")
        if typ != "null":
            refs.update(re.findall(r"(\d+) 0 R", val))
    typ, val = doc.xref_get_key(xref, "Resources/XObject")
    if typ != "null":
        for ref in re.findall(r"(\d+) 0 R", val):
            if doc.xref_get_key(int(ref), "Subtype")[1] == "/Image":
                refs.add("img" + ref)
    return frozenset(refs)


def find_orphan_forms(doc):
    """Every Form XObject not referenced by any page's content.

    These are the pre-redaction page originals that a plain (non-garbage-
    collecting) save() left behind in the file when the old version of this
    script redacted every page -- orphaned but intact. Indexed by BBox so
    repair_page() can quickly find same-size candidates for a given page.
    """
    referenced = set(xref for page in doc for xref, *_ in page.get_xobjects())
    orphans = defaultdict(list)
    for xref in range(1, doc.xref_length()):
        if xref in referenced or not doc.xref_is_stream(xref):
            continue
        if doc.xref_get_key(xref, "Subtype")[1] != "/Form":
            continue
        bbox = doc.xref_get_key(xref, "BBox")[1].replace(" ", "")
        orphans[bbox].append((xref, xobject_signature(doc, xref)))
    return orphans


def old_output_pages(doc, usage):
    """Page indexes whose content has the shape old-method output has: a
    single, page-owned content stream that just draws one rewritten Form
    XObject. Cheap -- no rendering or text extraction, just structure checks.
    """
    pages = []
    for page_index, page in enumerate(doc):
        cx = page.get_contents()
        if len(cx) != 1 or usage[cx[0]] > 1:
            continue
        if doc.xref_get_key(page.xref, "Resources")[0] != "dict":
            continue  # indirect/shared Resources -- could affect other pages, skip
        stream = doc.xref_stream(cx[0]) or b""
        match = OLD_OUTPUT_RE.match(stream)
        if not match:
            continue
        name = match.group(1).decode("latin-1")
        if doc.xref_get_key(page.xref, f"Resources/XObject/{name}")[0] != "xref":
            continue
        pages.append(page_index)
    return pages


def old_output_form_info(doc, page):
    """For a page verified by old_output_pages(): (content_xref, stream_bytes,
    offset_where_the_form_reference_ends, old_xobject_name, form_xref, form_bbox).

    Cheap -- no rendering, no text extraction. Shared by run_repair() (to check
    up front whether any orphan could possibly match this page, before trying
    any real repairs) and repair_page() (to do the actual repair).
    """
    cx = page.get_contents()[0]
    stream = doc.xref_stream(cx) or b""
    match = OLD_OUTPUT_RE.match(stream)
    old_name = match.group(1).decode("latin-1")
    fm_xref = int(doc.xref_get_key(page.xref, f"Resources/XObject/{old_name}")[1].split()[0])
    bbox = doc.xref_get_key(fm_xref, "BBox")[1].replace(" ", "")
    return cx, stream, match.end(), old_name, fm_xref, bbox


def repair_page(doc, page, orphans, used):
    """Try to point `page` back at its pre-redaction original Form XObject.

    `page` must already qualify per old_output_pages(). `orphans` is the dict
    from find_orphan_forms(); `used` is the set of orphan xrefs already
    claimed by an earlier page in this run (each original belongs to exactly
    one page, so once claimed it's removed from consideration).

    On success, mutates the page's content stream and Resources/XObject to
    draw the original instead of the rewritten form (preserving any drawing
    that followed the old form reference), and returns True. On failure,
    restores the page exactly as it was and returns False.
    """
    cx, stream, rest_start, old_name, fm_xref, bbox = old_output_form_info(doc, page)
    fm_sig = xobject_signature(doc, fm_xref)

    want_text = re.sub(r"\s+", "", page.get_text())
    before = page.get_pixmap(dpi=REPAIR_RENDER_DPI, alpha=False).samples

    xobject_dict = doc.xref_get_key(page.xref, "Resources/XObject")[1]
    existing_names = set(re.findall(r"/(\S+)\s+\d+\s+0\s+R", xobject_dict))
    new_name = "Orig0"
    n = 0
    while new_name in existing_names:
        n += 1
        new_name = f"Orig{n}"

    rest = stream[rest_start:]
    trial = b"q /" + new_name.encode("latin-1") + b" Do Q" + rest

    candidates = sorted(xref for xref, sig in orphans.get(bbox, []) if xref not in used and fm_sig <= sig)

    passing = []
    for cand in candidates:
        doc.xref_set_key(page.xref, f"Resources/XObject/{new_name}", f"{cand} 0 R")
        doc.update_stream(cx, trial)
        if re.sub(r"\s+", "", page.get_text()) == want_text:
            after = page.get_pixmap(dpi=REPAIR_RENDER_DPI, alpha=False).samples
            diff = sum(abs(a - b) for a, b in zip(before, after)) / len(before)
            passing.append((diff, cand))

    passing.sort()
    if passing and passing[0][0] < REPAIR_MATCH_THRESHOLD:
        _, best_xref = passing[0]
        doc.xref_set_key(page.xref, f"Resources/XObject/{new_name}", f"{best_xref} 0 R")
        doc.update_stream(cx, trial)
        doc.xref_set_key(page.xref, f"Resources/XObject/{old_name}", "null")
        used.add(best_xref)
        return True

    if candidates:
        doc.xref_set_key(page.xref, f"Resources/XObject/{new_name}", "null")
    doc.update_stream(cx, stream)
    return False


def hint_if_old_output(doc, usage):
    old_pages = old_output_pages(doc, usage)
    if len(old_pages) >= OLD_OUTPUT_MIN_FRACTION * doc.page_count:
        print("This file looks like output from the old redaction method; run with --repair to restore its original page content.")


def run_repair(input_path, output_path):
    doc = fitz.open(input_path)
    usage = Counter(xref for p in doc for xref in p.get_contents())
    total = doc.page_count
    old_pages = old_output_pages(doc, usage)

    if len(old_pages) < OLD_OUTPUT_MIN_FRACTION * total:
        print(
            f"This doesn't look like output from the old redaction method "
            f"(only {len(old_pages)} of {total} pages match), so there's nothing to repair."
        )
        doc.close()
        sys.exit(1)

    # bboxes of the rewritten form each old-method page actually needs a replacement
    # for -- checking orphans against these (rather than "are there any orphan Form
    # XObjects at all") avoids being fooled by unrelated orphaned forms elsewhere in
    # the file (e.g. small transparency-group forms kept alive by something other
    # than page content, which survive garbage collection for their own reasons)
    needed_bboxes = {old_output_form_info(doc, doc[pi])[5] for pi in old_pages}
    orphans = find_orphan_forms(doc)
    if not any(bbox in orphans for bbox in needed_bboxes):
        print(
            "This looks like old-method output, but the original page content has already "
            "been cleaned out of the file, so it can't be repaired. Re-download the book from "
            "your DriveThruRPG library and run this script on the fresh copy."
        )
        doc.close()
        sys.exit(1)

    confirm = (
        input(f"This looks like output from the old redaction method ({len(old_pages)} of {total} pages). Repair it? [y/N]: ")
        .strip()
        .casefold()
    )
    if confirm not in ("y", "yes"):
        print("Aborted, nothing saved.")
        doc.close()
        sys.exit(0)

    used = set()
    repaired = 0
    failed_pages = []
    for page_index in old_pages:
        page = doc[page_index]
        if repair_page(doc, page, orphans, used):
            repaired += 1
        else:
            failed_pages.append(page_index)

    doc.save(output_path, garbage=3, deflate=True)

    print(f"Repaired {repaired} of {len(old_pages)} page(s).")
    if failed_pages:
        shown = sorted(p + 1 for p in failed_pages)[:10]
        pages_str = ", ".join(str(p) for p in shown)
        if len(failed_pages) > len(shown):
            pages_str += ", ..."
        print(
            f"{len(failed_pages)} page(s) couldn't be matched to their original content "
            f"and were left as they were (pages: {pages_str})"
        )
    print(f"Saved to: {output_path}")

    doc.close()


def main():
    args = sys.argv[1:]
    repair_mode = "--repair" in args
    args = [a for a in args if a != "--repair"]

    if not args:
        print(f"Usage: {sys.argv[0]} [--repair] <input.pdf> [output.pdf]")
        sys.exit(1)

    input_path = args[0]
    default_suffix = "_repaired.pdf" if repair_mode else "_redacted.pdf"
    output_path = args[1] if len(args) > 1 else input_path.rsplit(".", 1)[0] + default_suffix

    if os.path.abspath(input_path) == os.path.abspath(output_path):
        print("Output path must be different from the input path, aborting.")
        sys.exit(1)

    if repair_mode:
        run_repair(input_path, output_path)
        return

    search_text = input("Partial text of the corner line to remove (leave blank to auto-detect): ").strip()

    doc = fitz.open(input_path)
    usage = Counter(xref for p in doc for xref in p.get_contents())
    lines_by_page = collect_lines(doc)

    if search_text:
        needle_cf = search_text.casefold()
        match = lambda text: needle_cf in text.casefold()
        label = f'"{search_text}"'
    else:
        candidates = detect(lines_by_page)
        if not candidates:
            print(
                "Couldn't auto-detect a watermark: no lower-left line repeats on at least "
                f"{int(AUTO_MIN_PAGE_FRACTION * 100)}% of pages. Run again and type part of its text."
            )
            hint_if_old_output(doc, usage)
            doc.close()
            sys.exit(1)

        if len(lines_by_page) < 5:
            print(f"Note: only {len(lines_by_page)} page(s), so repetition is weak evidence; check the line(s) below carefully.")

        if len(candidates) == 1:
            chosen = {candidates[0][0]}
        else:
            chosen = choose_candidates(candidates)
            if chosen is None:
                print("Aborted, nothing saved.")
                doc.close()
                sys.exit(0)

        match = lambda text: text.strip() in chosen
        label = "the detected line"

    matches, outside_pages = scan(lines_by_page, match)

    if not matches:
        print(f"No line containing {label} was found in the lower-left corner.")
        if outside_pages:
            print(f"(It does appear outside the corner on {len(outside_pages)} page(s); those were left alone.)")
        hint_if_old_output(doc, usage)
        doc.close()
        sys.exit(1)

    line_counts = Counter(text for (_, _, text) in matches)
    print("Found in the lower-left corner:")
    for text, count in line_counts.most_common():
        print(f'  "{text}"  on {count} page(s)')

    if outside_pages:
        shown = sorted(p + 1 for p in outside_pages)[:10]
        pages_str = ", ".join(str(p) for p in shown)
        if len(outside_pages) > len(shown):
            pages_str += ", ..."
        print(
            f"Note: {label} also appears outside the corner on {len(outside_pages)} page(s); "
            f"those were left alone (pages: {pages_str})"
        )

    confirm = input("Remove these line(s)? [y/N]: ").strip().casefold()
    if confirm not in ("y", "yes"):
        print("Aborted, nothing saved.")
        doc.close()
        sys.exit(0)

    matches_by_page = {}
    for page_index, rect, text in matches:
        matches_by_page.setdefault(page_index, []).append((rect, text))

    fallback_pages = []
    for page_index, targets in matches_by_page.items():
        page = doc[page_index]
        leftover = strip_from_streams(doc, page, targets, usage)
        if leftover:
            # watermark wasn't in its own page-owned top-level text object here --
            # fall back to redaction, which can slightly shift nearby text spacing
            for rect, _ in leftover:
                page.add_redact_annot(rect, fill=None)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )
            fallback_pages.append(page_index)

    doc.save(output_path, garbage=3, deflate=True)

    summary = f"Removed {len(matches)} line(s) from {len(matches_by_page)} page(s)."
    if fallback_pages:
        shown = sorted(p + 1 for p in fallback_pages)[:10]
        pages_str = ", ".join(str(p) for p in shown)
        if len(fallback_pages) > len(shown):
            pages_str += ", ..."
        summary += f" ({len(fallback_pages)} page(s) needed redaction fallback: {pages_str})"
    print(summary)
    if fallback_pages:
        print("Note: on fallback pages, nearby text spacing may shift slightly.")
    print(f"Saved to: {output_path}")

    doc.close()


if __name__ == "__main__":
    main()
