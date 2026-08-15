"""Writes Calibre-compatible XMP metadata into PDFs via pikepdf.

Field mapping (intentionally not a 1:1 copy of Calibre's own output —
this is a project-specific policy since DriveThruRPG's per-book author
credits and descriptions are frequently missing or unreliable):

    dc:title       <- matched title
    dc:creator     <- publisher (not the actual author list; RPG PDFs in
                      this library are browsed/grouped by publisher, and
                      DTRPG's author credits are too inconsistent to trust)
    dc:publisher   <- publisher
    dc:description <- description text (shown as "Comments" by ebook-meta)
    dc:subject     <- tags/categories (shown as "Tags" by ebook-meta —
                      note this is the *only* field ebook-meta calls
                      "Tags"; there is no separate "Subject" field)
    xmp:Identifier <- dtrpg:<product_id> (the DriveThruRPG item number, so
                      the exact listing a file was matched against can be
                      traced back later — their search isn't reliable
                      enough to just re-derive it from the title) and
                      isbn:<isbn> when DriveThruRPG has one on file (many
                      PDF-only products don't). Both go in the same
                      Calibre-style qualified identifier structure (scheme
                      + value per entry, not a plain dc:identifier string)
                      — that's the only form ebook-meta's "Identifiers"
                      field actually recognizes; verified against a real
                      `ebook-meta` run, not just written on spec.
    calibre:series, calibre:series_index <- as matched

pikepdf's docinfo sync (on by default) also mirrors title/author into
the classic PDF Info dictionary for readers that only look there.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from lxml import etree
from lxml.etree import QName

from review import ReviewRow

logger = logging.getLogger("pdf_writer")

CALIBRE_NS = "http://calibre-ebook.com/xmp-namespace"
CALIBRE_PREFIX = "calibre"

XMP_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_NS_XMP = "http://ns.adobe.com/xap/1.0/"
XMP_NS_XMPIDQ = "http://ns.adobe.com/xmp/Identifier/qual/1.0/"


@dataclass
class WriteResult:
    filename: str
    success: bool
    message: str


def _set_calibre_identifiers(meta: pikepdf.models.metadata.PdfMetadata, identifiers: dict[str, str]) -> None:
    """Write one or more identifiers in Calibre's own recognized structure.

    pikepdf's public dict-style metadata API only supports simple
    scalar/array XMP properties — there's no supported way to write a
    qualified/structured property (rdf:parseType="Resource") through it,
    so this reaches into pikepdf's private XMP internals (_xmp_doc,
    _get_rdf_root) to build the exact shape Calibre's own reader expects
    (see calibre/ebooks/metadata/xmp.py: create_identifiers — this mirrors
    that function's dict-of-schemes signature so multiple identifiers land
    in a single Bag, not overwriting each other). If a future pikepdf
    version changes that internal structure this will raise AttributeError,
    which callers should catch and treat as non-fatal — losing the
    identifier tags is much better than failing the whole write.
    """
    meta.register_xml_namespace(XMP_NS_XMPIDQ, "xmpidq")
    xmp_doc = meta._xmp_doc
    rdf = xmp_doc._get_rdf_root()
    rdfdesc = rdf.find('rdf:Description[@rdf:about=""]', xmp_doc.NS)
    if rdfdesc is None:
        rdfdesc = etree.SubElement(
            rdf,
            str(QName(XMP_NS_RDF, "Description")),
            attrib={str(QName(XMP_NS_RDF, "about")): ""},
        )

    for existing in rdfdesc.findall(str(QName(XMP_NS_XMP, "Identifier"))):
        rdfdesc.remove(existing)

    xmpid = etree.SubElement(rdfdesc, str(QName(XMP_NS_XMP, "Identifier")))
    bag = etree.SubElement(xmpid, str(QName(XMP_NS_RDF, "Bag")))
    for scheme, value in identifiers.items():
        li = etree.SubElement(bag, str(QName(XMP_NS_RDF, "li")), attrib={str(QName(XMP_NS_RDF, "parseType")): "Resource"})
        scheme_el = etree.SubElement(li, str(QName(XMP_NS_XMPIDQ, "Scheme")))
        scheme_el.text = scheme
        value_el = etree.SubElement(li, str(QName(XMP_NS_RDF, "value")))
        value_el.text = value


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
                meta.register_xml_namespace(CALIBRE_NS, CALIBRE_PREFIX)

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
                identifiers = {}
                if row.product_id:
                    identifiers["dtrpg"] = row.product_id
                if row.isbn:
                    identifiers["isbn"] = row.isbn
                if identifiers:
                    try:
                        _set_calibre_identifiers(meta, identifiers)
                    except AttributeError:
                        logger.warning(
                            "Could not write Calibre-style identifiers for %s "
                            "(pikepdf internals may have changed); skipping them",
                            path.name,
                        )
                if row.series:
                    meta[f"{CALIBRE_PREFIX}:series"] = row.series
                if row.series_index:
                    meta[f"{CALIBRE_PREFIX}:series_index"] = str(row.series_index)

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
