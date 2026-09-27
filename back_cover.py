"""Moves a book's back cover between PDF page 2 and the last page
(--back-cover-to-page-2 / --back-cover-to-end).

Publishers disagree on where the back cover goes: Steve Jackson Games,
Delta Green and Evil Hat put it right after the front cover; most others
put it last. Both options find the back cover the same way and only move
it when there's positive evidence of where it is, since moving the wrong
page is much worse than moving nothing:

- A landscape PDF page 1 in a portrait book is a wraparound cover (front
  and back art on one page, e.g. Shadowrun), so there is no separate back
  cover to move.
- Otherwise the back cover is whichever of PDF page 2 and the last page
  carries a retail barcode (page_render.py). Many books
  have no back cover at all (they end on an index, a blank page or a
  divider), and a barcode is what tells a back cover apart from those.
- A barcode on both, or on neither, means nothing is moved automatically.
  PDF-only releases usually have no barcode at all, so when there isn't
  one, locate_back_cover() also names the page that *would* be the back
  cover for the requested move (the last page for "page-2", page 2 for
  "end"), and tag -- which can ask -- shows it to the user and passes
  their answer back as move_back_cover(..., index=...). write-pdfs/all
  can't ask, so they only move on a barcode.

The move keeps every page object, so bookmarks and in-document links
still point at the same pages (verified on a hyperlinked book: 24
bookmarks and 81 links, both directions). Existing /PageLabels are
position-based, so they are rewritten to follow their pages. The save
leaves the XMP packet untouched (fix_metadata_version=False) and is
atomic, same as the other pre-write steps.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pikepdf
import pymupdf as fitz

from page_labels import is_wraparound, read_existing_labels, write_existing_labels
from page_render import page_has_barcode, page_is_blank, render_page_png


@dataclass
class BackCoverLocation:
    """`index`: the back cover's 0-based page, found by its barcode.
    `candidate`: with no barcode, the page to ask the user about (None when
    there's nothing sensible to ask -- a wraparound, a blank page, etc.)."""

    index: int | None
    reason: str
    page_count: int = 0
    candidate: int | None = None


@dataclass
class BackCoverMoveResult:
    moved: bool
    message: str


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def locate_back_cover(path: Path, to: str | None = None) -> BackCoverLocation:
    """Where the back cover is, by barcode. With no barcode, `to` picks the
    candidate page to ask about (see the module docstring)."""
    try:
        with fitz.open(path) as doc:
            page_count = doc.page_count
            if page_count < 3:
                return BackCoverLocation(None, "too few pages to have a separate back cover", page_count)
            if is_wraparound(doc):
                return BackCoverLocation(
                    None, "PDF page 1 is a wraparound cover (front and back on one page), so there's no separate back cover",
                    page_count,
                )
    except Exception as exc:
        return BackCoverLocation(None, f"couldn't read the PDF: {exc}")
    second = page_has_barcode(path, 1)
    last = page_has_barcode(path, page_count - 1)
    if second and last:
        return BackCoverLocation(
            None, "PDF page 2 and the last page both have a barcode, so it's unclear which is the back cover", page_count,
        )
    if second:
        return BackCoverLocation(1, "the back cover (barcode) is PDF page 2", page_count)
    if last:
        return BackCoverLocation(page_count - 1, "the back cover (barcode) is the last page", page_count)
    candidate = {"page-2": page_count - 1, "end": 1}.get(to or "")
    if candidate is not None and page_is_blank(path, candidate):
        candidate = None
    return BackCoverLocation(
        None, "no barcode on PDF page 2 or the last page, so the back cover can't be confirmed automatically",
        page_count, candidate,
    )


@dataclass
class BackCoverQuestion:
    """What tag shows when there's no barcode: the candidate page, a text
    snippet from it, and thumbnails of it and the front cover."""

    page_index: int
    page_count: int
    to: str
    snippet: str
    front_png: Path | None
    candidate_png: Path | None


def prepare_question(path: Path, location: BackCoverLocation, to: str, tmpdir: Path) -> BackCoverQuestion:
    """Thumbnails go in `tmpdir`, which the caller owns and removes."""
    index = location.candidate
    assert index is not None
    try:
        with fitz.open(path) as doc:
            snippet = " ".join(doc[index].get_text("text").split())
    except Exception:
        snippet = ""
    front, candidate = tmpdir / "front.png", tmpdir / "candidate.png"
    return BackCoverQuestion(
        index, location.page_count, to, snippet[:200],
        front if render_page_png(path, 0, front) else None,
        candidate if render_page_png(path, index, candidate) else None,
    )


def describe_question(book_title: str, question: BackCoverQuestion) -> list[str]:
    """Prompt wording shared by the terminal UI and the GUI."""
    where = "to PDF page 2" if question.to == "page-2" else "to the end"
    lines = [
        f"No barcode found in {book_title}, so its back cover can't be confirmed automatically.",
        f"Is PDF page {question.page_index + 1} of {question.page_count} the back cover? Yes moves it {where}.",
    ]
    if question.snippet:
        lines.append(f'Text on that page: "{question.snippet}"')
    return lines


def move_back_cover(
    path: Path, to: str, index: int | None = None, reason: str = "confirmed by you",
) -> BackCoverMoveResult:
    """Moves the back cover to PDF page 2 (to="page-2") or to the end
    (to="end"). `index` is a back cover the caller already found (tag's
    prompt, or its own barcode lookup, with `reason` saying which); without
    it, only a barcode-located back cover is moved. Never raises."""
    if to not in ("page-2", "end"):
        raise ValueError(f"unknown destination {to!r}")
    if index is not None:
        where = index
    else:
        location = locate_back_cover(path)
        where, reason = location.index, location.reason
        if where is None:
            return BackCoverMoveResult(False, reason)
    if (to == "page-2" and where == 1) or (to == "end" and where != 1):
        return BackCoverMoveResult(False, f"already in place: {reason}")

    tmp_fd, tmp_str = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
    os.close(tmp_fd)
    tmp: Path | None = Path(tmp_str)
    try:
        with pikepdf.open(path) as pdf:
            count = len(pdf.pages)
            if where not in (1, count - 1):
                return BackCoverMoveResult(False, "page count changed while reading")
            labels = read_existing_labels(pdf)
            if to == "page-2":
                page = pdf.pages[count - 1]
                del pdf.pages[count - 1]
                pdf.pages.insert(1, page)
                order = [0, count - 1] + list(range(1, count - 1))
            else:
                page = pdf.pages[1]
                del pdf.pages[1]
                pdf.pages.append(page)
                order = [0] + list(range(2, count)) + [1]
            if labels is not None:
                write_existing_labels(pdf, [labels[i] for i in order])
            pdf.save(tmp, fix_metadata_version=False)
        try:
            shutil.copymode(path, tmp)  # mkstemp's 0600 would otherwise stick (see CLAUDE.md)
        except OSError:
            pass
        os.replace(tmp, path)
        tmp = None
    except Exception as exc:
        return BackCoverMoveResult(False, f"moving the back cover failed: {exc}")
    finally:
        if tmp is not None:
            _unlink_quietly(tmp)
    destination = "PDF page 2" if to == "page-2" else "the end"
    return BackCoverMoveResult(True, f"moved the back cover to {destination} ({reason})")
