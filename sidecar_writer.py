"""Writes BookOrbit (.opf) and Grimmory (.metadata.json) sidecar files
alongside a tagged PDF, in addition to the metadata written into the PDF
itself (see pdf_writer.py). Both formats were determined by reading each
app's actual open-source parser/writer, not guessed from a generic schema.

BookOrbit (https://bookorbit.app), github.com/bookorbit/bookorbit:
    reader: server/src/modules/metadata/lib/opf-parser.ts
    Standard EPUB2/Calibre-style OPF. Its XML parsing strips namespace
    prefixes from element names (so `<dc:title>` and `<title>` parse
    identically), but attribute names are checked with their `opf:`
    prefix first (`opf:scheme`, `opf:role`), so those are written exactly
    as Calibre itself would. Series comes from Calibre's own
    `<meta name="calibre:series">`/`calibre:series_index` convention;
    ISBN is auto-detected from a `<dc:identifier opf:scheme="ISBN">` by
    digit count (10 vs 13) on their end, not split here. Tags (a separate
    concept from Genres/`dc:subject` in BookOrbit's own schema) come from
    `<meta name="bookorbit:tags" content="a; b">`, read via
    `parseBookOrbitTags(namedMeta('bookorbit:tags'))` — confirmed by
    running BookOrbit's real opf-parser.ts against a generated file, same
    as everything else here, not assumed just because it was also the
    convention during the (now-removed) BookOrbit-only-XMP era.

Grimmory (https://grimmory.org), github.com/grimmory-tools/grimmory
(an independent fork of Booklore — Java package names still say
`org.booklore`), docs at grimmory.org/docs/metadata/sidecar-files/:
    schema: backend/src/main/java/org/booklore/model/dto/sidecar/*.java
    writer: backend/.../sidecar/SidecarMetadataWriter.java
    Flat JSON, `<stem>.metadata.json` next to the book file. Unlike the
    OPF, isbn10/isbn13 are separate top-level fields with no auto-detection
    on their end, so this file buckets by digit count itself.

Both are best-effort, silent-if-nothing-to-write operations — a missing
title/publisher/etc. just means that field is omitted, matching how both
apps' own writers only emit fields they actually have data for.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from review import ReviewRow

_ISBN_STRIP_RE = re.compile(r"[^0-9Xx]")


def _split_isbn(isbn: str) -> tuple[str | None, str | None]:
    """Return (isbn10, isbn13), routed by digit count — or (None, None)
    if it doesn't look like either shape."""
    cleaned = _ISBN_STRIP_RE.sub("", isbn)
    if len(cleaned) == 13 and cleaned.isdigit():
        return None, cleaned
    if len(cleaned) == 10:
        return cleaned, None
    return None, None


def write_bookorbit_opf(pdf_path: Path, row: ReviewRow) -> Path:
    opf_path = pdf_path.with_suffix(".opf")
    tags = [t.strip() for t in row.tags.split(";") if t.strip()]

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" '
        'xmlns:opf="http://www.idpf.org/2007/opf" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">',
        "  <metadata>",
    ]

    if row.matched_title:
        lines.append(f"    <dc:title>{escape(row.matched_title)}</dc:title>")
    if row.publisher:
        lines.append(f'    <dc:creator opf:role="aut">{escape(row.publisher)}</dc:creator>')
        lines.append(f"    <dc:publisher>{escape(row.publisher)}</dc:publisher>")
    if row.description:
        lines.append(f"    <dc:description>{escape(row.description)}</dc:description>")
    for tag in tags:
        lines.append(f"    <dc:subject>{escape(tag)}</dc:subject>")
    if row.isbn:
        lines.append(f'    <dc:identifier opf:scheme="ISBN">{escape(row.isbn)}</dc:identifier>')
    if row.product_id:
        lines.append(f"    <dc:identifier>dtrpg:{escape(row.product_id)}</dc:identifier>")
    if row.series:
        lines.append(f'    <meta name="calibre:series" content={quoteattr(row.series)}/>')
    if row.series_index:
        lines.append(f'    <meta name="calibre:series_index" content={quoteattr(str(row.series_index))}/>')
    if tags:
        lines.append(f'    <meta name="bookorbit:tags" content={quoteattr("; ".join(tags))}/>')

    lines += ["  </metadata>", "  <manifest/>", "  <spine/>", "</package>"]

    opf_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return opf_path


def write_grimmory_sidecar(pdf_path: Path, row: ReviewRow) -> Path:
    sidecar_path = pdf_path.parent / f"{pdf_path.stem}.metadata.json"
    tags = [t.strip() for t in row.tags.split(";") if t.strip()]

    book_metadata: dict = {}
    if row.matched_title:
        book_metadata["title"] = row.matched_title
    if row.publisher:
        book_metadata["authors"] = [row.publisher]
        book_metadata["publisher"] = row.publisher
    if row.description:
        book_metadata["description"] = row.description
    if row.isbn:
        isbn10, isbn13 = _split_isbn(row.isbn)
        if isbn10:
            book_metadata["isbn10"] = isbn10
        if isbn13:
            book_metadata["isbn13"] = isbn13
    if tags:
        book_metadata["categories"] = tags
        book_metadata["tags"] = tags
    if row.series:
        series: dict = {"name": row.series}
        if row.series_index:
            try:
                series["number"] = float(row.series_index)
            except ValueError:
                pass
        book_metadata["series"] = series

    sidecar = {
        "version": "1.0",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "generatedBy": "dtrpg-metadata-download",
        "metadata": book_metadata,
    }

    sidecar_path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return sidecar_path
