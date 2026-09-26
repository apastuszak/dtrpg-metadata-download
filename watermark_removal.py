"""Wraps a DriveThruRPG-watermark-removal tool (`remove_dtrpg_watermarks.py`),
vendored as a plain file in this project's own directory -- same
vendoring reasoning as `rpg_hyperlink.py`'s own module docstring (read
that first; this repeats only what's different). Named
`watermark_removal.py`, not `rpg_watermark.py`, since it originally came
from a different sibling repo (`text-removal`, not
`gurps_4e_revised_hyperlink`) than the `rpg_*.py` wrappers.

Unlike those wrappers, the sibling script's own `main()` has no separable
non-interactive entry point at all -- every step (auto-detect vs.
free-text search, which candidate to remove, confirm-before-removing) is
an `input()` prompt baked directly into `main()`'s body. Its detection/
removal *logic* is already factored into real top-level functions,
though (`collect_lines`/`detect`/`scan`/`strip_from_streams`), loaded the
same way as every other wrapper here (`importlib.util.spec_from_file_location`)
and called directly -- this project's own interactive UI (a TUI screen /
a Qt dialog) stands in for the sibling script's `input()` prompts.

Only auto-detect mode is used here, never the sibling script's free-text
search -- `tag`'s own flow calls this unconditionally on every file
before anything else happens to it (see `tag_tui.py`'s `_process_one()`/
`gui_tag_flow.py`'s `TagFlowController._process_one()`), so there's no
prompt this project's UI could sensibly forward a search string through;
"check for a watermark" only makes sense as automatic detection.

`detect_watermark()` and `remove_watermark()` each read the whole PDF's
text once (`collect_lines()`, ~5s/book per the sibling script's own
comment) rather than sharing one read across both -- `detect_watermark()`
runs first, and only if the user then confirms removal does
`remove_watermark()` run at all, so caching a read that's thrown away
most of the time (nothing detected, or the user declines) isn't worth
the extra state; this matches the sibling script's own single-pass
structure, and this project's general preference for directness over
micro-optimization.

`remove_watermark()`'s actual removal sequence (`strip_from_streams()`
per page, with a redaction fallback for any leftover lines, then save)
mirrors the sibling script's own `main()` body verbatim, after its
"Remove these line(s)?" confirmation -- that logic was never factored
into a function of its own, so it's reproduced here against the real
imported functions rather than re-derived independently.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pymupdf as fitz

WATERMARK_SCRIPT = Path(__file__).parent / "remove_dtrpg_watermarks.py"

# Mirrors the vendored script's own main(), which warns below this many
# pages that "repetition is weak evidence" -- on a 1-4 page PDF almost any
# lower-left line clears the 90%-of-pages bar.
WEAK_EVIDENCE_PAGE_COUNT = 5


@dataclass
class WatermarkDetection:
    text: str
    page_count: int
    total_pages: int


def describe_detection(book_title: str, detection: WatermarkDetection) -> list[str]:
    """The prompt lines both UIs show before asking to remove -- shared so
    the TUI modal and GUI dialog can't drift apart. Worded as a *possible*
    watermark: detection only means a lower-left line repeats on most
    pages, which a publisher's own copyright footer can do too."""
    lines = [
        f"Possible watermark in {book_title}:",
        f'"{detection.text}" repeats in the lower-left corner on {detection.page_count} of {detection.total_pages} page(s).',
        "This could also be a normal footer (e.g. a copyright line) -- check before removing.",
    ]
    if detection.total_pages < WEAK_EVIDENCE_PAGE_COUNT:
        lines.append(f"Only {detection.total_pages} page(s), so the repetition is weak evidence.")
    return lines


@dataclass
class WatermarkRemovalResult:
    success: bool
    removed: int = 0
    message: str = ""


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _load_functions(script_path: Path):
    """Imports the sibling script by absolute file path and returns its
    (collect_lines, detect, scan, strip_from_streams) functions -- the
    same ones its own main() calls, minus the input()-driven flow around
    them."""
    spec = importlib.util.spec_from_file_location(f"watermark_removal_{script_path.stem}", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load a module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.collect_lines, module.detect, module.scan, module.strip_from_streams


def detect_watermark(path: Path) -> WatermarkDetection | None:
    """Read-only check for a DriveThruRPG-style corner watermark (a line
    repeating in the lower-left corner of most pages) -- never raises;
    a missing/unopenable PDF or nothing detected both return None the
    same way, since this check runs unconditionally on every file `tag`
    processes and a bad PDF here must not block the rest of that file's
    tagging. Returns only the single highest-confidence candidate (the
    sibling script's own `detect()` already sorts by page count, most
    pages first) -- if more than one repeating corner line exists, this
    project's own flow only ever offers the most likely one, not the
    sibling script's own multi-candidate picker (there's no interactive
    step here to pick from a list; if the top candidate is declined,
    nothing is removed).
    """
    if not path.exists() or not WATERMARK_SCRIPT.exists():
        return None
    try:
        collect_lines, detect, _scan, _strip = _load_functions(WATERMARK_SCRIPT)
        doc = fitz.open(path)
        try:
            lines_by_page = collect_lines(doc)
            candidates = detect(lines_by_page)
        finally:
            doc.close()
    except Exception:
        return None
    if not candidates:
        return None
    text, count = candidates[0]
    return WatermarkDetection(text=text, page_count=count, total_pages=len(lines_by_page))


def remove_watermark(
    path: Path,
    text: str,
    log: Callable[[str], None] | None = None,
) -> WatermarkRemovalResult:
    """Removes every lower-left-corner line whose text exactly matches
    `text` (the candidate detect_watermark() found and the user
    confirmed), re-saving over the same path. Never raises -- returns
    success=False with a message on any failure, matching this whole
    family's report-don't-crash convention.

    Saves to a temp file and atomically replaces `path` only once
    everything has succeeded (`os.replace()`, same filesystem via
    `dir=path.parent`, hidden `.dtrpg-tmp-` prefix so a force-killed run
    can't leave a stray file `scan_pdfs()` would later try to match) --
    same reasoning as the rest of this family: a failure partway through
    must leave the original untouched.
    """
    if not path.exists():
        return WatermarkRemovalResult(success=False, message="file not found")
    if not WATERMARK_SCRIPT.exists():
        # Should be unreachable -- vendored, committed file -- but a
        # broken/partial working tree is a real enough possibility to
        # degrade instead of crashing.
        return WatermarkRemovalResult(success=False, message=f"expected vendored script missing: {WATERMARK_SCRIPT}")

    try:
        collect_lines, _detect, scan, strip_from_streams = _load_functions(WATERMARK_SCRIPT)
    except Exception as exc:
        return WatermarkRemovalResult(success=False, message=f"could not load watermark-removal script: {exc}")

    tmp_fd, tmp_out_str = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
    os.close(tmp_fd)
    tmp_out_path: Path | None = Path(tmp_out_str)

    try:
        doc = fitz.open(path)
        try:
            usage = Counter(xref for p in doc for xref in p.get_contents())
            lines_by_page = collect_lines(doc)
            matches, _outside_pages = scan(lines_by_page, lambda t: t.strip() == text)
            if not matches:
                return WatermarkRemovalResult(success=False, message="watermark text no longer found in the corner")

            matches_by_page: dict[int, list] = {}
            for page_index, rect, line_text in matches:
                matches_by_page.setdefault(page_index, []).append((rect, line_text))

            fallback_pages = []
            for page_index, targets in matches_by_page.items():
                page = doc[page_index]
                leftover = strip_from_streams(doc, page, targets, usage)
                if leftover:
                    # Not in a page-owned top-level text object -- fall
                    # back to redaction, exactly as the sibling script's
                    # own main() does for the same case.
                    for rect, _ in leftover:
                        page.add_redact_annot(rect, fill=None)
                    page.apply_redactions(
                        images=fitz.PDF_REDACT_IMAGE_NONE,
                        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                    )
                    fallback_pages.append(page_index)

            doc.save(tmp_out_path, garbage=3, deflate=True)
        finally:
            doc.close()

        # Keep the book's original permissions (best-effort) -- see
        # rpg_background_layer.py. (PyMuPDF's full save happens to recreate
        # the file today, so this isn't currently needed here, but it
        # shouldn't depend on that.)
        try:
            shutil.copymode(path, tmp_out_path)
        except OSError:
            pass
        os.replace(tmp_out_path, path)
        tmp_out_path = None

        if log is not None:
            log(f"Removed {len(matches)} watermark line(s) from {len(matches_by_page)} page(s)")
            if fallback_pages:
                log(f"{len(fallback_pages)} page(s) needed a redaction fallback; nearby text spacing may shift slightly")

        return WatermarkRemovalResult(success=True, removed=len(matches))
    except Exception as exc:
        return WatermarkRemovalResult(success=False, message=f"watermark removal failed: {exc}")
    finally:
        if tmp_out_path is not None:
            _unlink_quietly(tmp_out_path)
