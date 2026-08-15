"""Writes BookOrbit-compatible XMP metadata into PDFs via pikepdf.

BookOrbit (https://bookorbit.app) is the target reader/library app —
Kavita is no longer in use. Field mapping was determined by reading
BookOrbit's actual open-source parser/writer, not by guessing from a
generic schema (github.com/bookorbit/bookorbit, at the commit current
when this was written):
  - reader: server/src/modules/metadata/lib/pdf-xmp-reader.ts,
            server/src/modules/metadata/lib/pdf-parser.ts
  - writer: server/src/modules/file-write/formats/pdf/pdf-xmp-builder.ts
  - namespace: server/src/common/bookorbit-ns.ts

Mapping used here (intentionally not a 1:1 copy of BookOrbit's own writer
— this is a project-specific policy, same reasoning as before: DriveThruRPG's
per-book author credits are inconsistent for this library, so Author is
still deliberately set to the publisher rather than the real author list):

    dc:title            <- matched title
    dc:creator          <- publisher (not the actual author list — same
                           policy as before, kept on purpose; BookOrbit
                           reads this as the Authors list)
    dc:publisher        <- publisher
    dc:description      <- description text (BookOrbit's Description
                           field maps directly to dc:description — no
                           "Comments" naming confusion like ebook-meta had)
    dc:subject          <- tags/categories, read by BookOrbit as Genres
    bookorbit:tags       <- the same tags/categories, also written to
                           BookOrbit's own dedicated Tags field (a
                           separate concept from Genres in their schema)
    pdf:Keywords         <- the same tags/categories again, joined with
                           "; " — not read by BookOrbit at all (it prefers
                           bookorbit:tags whenever XMP is present), but
                           pikepdf's docinfo sync maps the classic /Keywords
                           Info-dict field from pdf:Keywords specifically,
                           not from dc:subject, so a plain PDF reader that
                           only looks at the Info dictionary (not XMP) would
                           otherwise see an empty Keywords field
    bookorbit:seriesName,
    bookorbit:seriesIndex <- as matched
    bookorbit:isbn13 or
    bookorbit:isbn10     <- DriveThruRPG's isbn, routed by digit count
                           (13 digits -> isbn13, 10 -> isbn10); skipped
                           if it's neither shape
    dc:identifier        <- "dtrpg:<product_id>" as a plain string, for
                           manual traceability only — BookOrbit has no
                           concept of a DriveThruRPG identifier so it
                           doesn't read this, but it's harmless (unknown
                           XMP properties are just ignored) and still
                           useful if the file is ever inspected with
                           exiftool or similar. Unlike Kavita/Calibre,
                           this no longer needs BookOrbit's qualified
                           xmp:Identifier structure, so it's written via
                           pikepdf's normal public API — no more reaching
                           into private internals for this field.

BookOrbit's own namespace (`bookorbit:`) must be registered with exactly
that prefix — its XMP reader (fast-xml-parser) matches on literal tag
prefix text, not on resolved namespace URIs, so any other prefix string
for the same URI would not be recognized.

pikepdf's docinfo sync (on by default) also mirrors title/author into
the classic PDF Info dictionary for readers that only look there.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from review import ReviewRow

logger = logging.getLogger("pdf_writer")

BOOKORBIT_NS = "https://bookorbit.app/metadata/1.0/"
BOOKORBIT_PREFIX = "bookorbit"

_ISBN_STRIP_RE = re.compile(r"[^0-9Xx]")


@dataclass
class WriteResult:
    filename: str
    success: bool
    message: str


def _isbn_field(isbn: str) -> tuple[str, str] | None:
    """Return (xmp_key, cleaned_value) for an ISBN, routed by digit count
    to BookOrbit's separate isbn13/isbn10 fields — or None if it doesn't
    look like either shape."""
    cleaned = _ISBN_STRIP_RE.sub("", isbn)
    if len(cleaned) == 13 and cleaned.isdigit():
        return f"{BOOKORBIT_PREFIX}:isbn13", cleaned
    if len(cleaned) == 10:
        return f"{BOOKORBIT_PREFIX}:isbn10", cleaned
    return None


def _backup(path: Path) -> Path:
    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
    else:
        logger.debug("Backup already exists for %s, leaving it as-is", path.name)
    return backup_path


def write_metadata(path: Path, row: ReviewRow) -> WriteResult:
    if not path.exists():
        return WriteResult(path.name, False, "file not found")

    try:
        _backup(path)
    except OSError as exc:
        return WriteResult(path.name, False, f"backup failed: {exc}")

    try:
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                meta.register_xml_namespace(BOOKORBIT_NS, BOOKORBIT_PREFIX)

                if row.matched_title:
                    meta["dc:title"] = row.matched_title
                if row.publisher:
                    meta["dc:creator"] = [row.publisher]
                    meta["dc:publisher"] = [row.publisher]
                if row.description:
                    meta["dc:description"] = row.description

                tags = [t.strip() for t in row.tags.split(";") if t.strip()]
                if tags:
                    meta["dc:subject"] = tags
                    meta[f"{BOOKORBIT_PREFIX}:tags"] = tags
                    # pikepdf's docinfo sync maps the classic /Keywords field
                    # from pdf:Keywords specifically — not dc:subject — so a
                    # plain PDF reader that only looks at the Info dictionary
                    # (not XMP) would otherwise see an empty Keywords field.
                    meta["pdf:Keywords"] = "; ".join(tags)

                if row.series:
                    meta[f"{BOOKORBIT_PREFIX}:seriesName"] = row.series
                if row.series_index:
                    meta[f"{BOOKORBIT_PREFIX}:seriesIndex"] = str(row.series_index)

                if row.isbn:
                    isbn_field = _isbn_field(row.isbn)
                    if isbn_field:
                        meta[isbn_field[0]] = isbn_field[1]
                    else:
                        logger.warning("ISBN %r for %s doesn't look like 10 or 13 digits; skipping", row.isbn, path.name)

                if row.product_id:
                    meta["dc:identifier"] = f"dtrpg:{row.product_id}"

            pdf.save(path)
    except (pikepdf.PdfError, OSError) as exc:
        return WriteResult(path.name, False, f"write failed: {exc}")

    return WriteResult(path.name, True, "ok")


def write_approved(rows: list[ReviewRow], root: str | Path) -> list[WriteResult]:
    root = Path(root)
    results: list[WriteResult] = []
    for row in rows:
        if not row.is_approved():
            continue
        matches = list(root.rglob(row.filename))
        if not matches:
            results.append(WriteResult(row.filename, False, "file not found under root"))
            continue
        results.append(write_metadata(matches[0], row))

    succeeded = sum(1 for r in results if r.success)
    logger.info("Wrote metadata to %d/%d approved files", succeeded, len(results))
    for r in results:
        if not r.success:
            logger.warning("Failed: %s (%s)", r.filename, r.message)
    return results
