"""Writes Calibre-compatible XMP metadata into PDFs via pikepdf, plus
BookOrbit and Grimmory sidecar files alongside each one.

Embedded PDF metadata targets Calibre's own conventions (not a specific
reader's schema) — this is a deliberate, project-specific policy since
DriveThruRPG's per-book author credits are inconsistent for this library:

    dc:title       <- matched title
    dc:creator     <- publisher (not the actual author list; DTRPG's author
                      credits are too inconsistent to trust for this library)
    dc:publisher   <- publisher
    dc:description <- description text (shown as "Comments" by ebook-meta)
    dc:subject     <- tags/categories (shown as "Tags" by ebook-meta — there
                      is no separate "Subject" field in this scheme)
    pdf:Keywords   <- the same tags/categories, joined with "; " — pikepdf's
                      docinfo sync maps the classic /Keywords Info-dict field
                      from pdf:Keywords specifically, not from dc:subject, so
                      a plain reader that only looks at the Info dictionary
                      would otherwise see an empty Keywords field
    xmp:Identifier <- dtrpg:<product_id> plus isbn:<isbn> when DriveThruRPG
                      has one on file, in Calibre's own qualified identifier
                      structure (scheme + value per entry, not a plain
                      dc:identifier string) — that's the only form
                      ebook-meta's "Identifiers" field actually recognizes;
                      verified against a real `ebook-meta` run, not just
                      written on spec.
    calibre:series, calibre:series_index <- as matched, in Calibre's own
                      qualified structure (calibre:series wraps its value
                      in a nested rdf:value; series_index lives in a
                      *different* namespace, calibreSI:series_index,
                      nested inside the series element) — confirmed from
                      Calibre's own source (xmp.py: create_series/
                      read_series) after a plain scalar assignment (what
                      pikepdf's public dict API produces) turned out to
                      round-trip through pikepdf fine but silently never
                      show up as a series in real Calibre; verified fixed
                      against a real `ebook-meta --to-opf` run on a
                      pristine file, not just written on spec.

One deliberate exception to "Calibre format only": bookorbit:seriesName /
bookorbit:seriesIndex also get written, in BookOrbit's own simple scalar
form (no nested-value structure needed — confirmed from BookOrbit's real
pdf-xmp-reader.ts). This isn't redundant with the .opf sidecar below:
BookOrbit's scanner defaults to trying embedded PDF metadata *first*, and
only falls back to the .opf sidecar if the embedded extraction returns
*nothing at all* — not per-field, the whole source either wins or doesn't
(see scanner.service.ts: extractFirstAvailableMetadataSource). Since a
Calibre-tagged PDF always has *some* embedded metadata (title, authors,
etc.), that first source always "wins," and the .opf sidecar's series
data becomes unreachable in practice. Writing BookOrbit's own series
fields directly into the embedded PDF is the only way around that.

Re-tagging a file (a supported, expected workflow) explicitly clears all
four series-related properties before writing whatever the *current*
match has — unlike identifiers/tags/etc., where an empty value from the
matcher just means "leave this field alone," an empty series here means
"this match doesn't have one," and any series data from a *previous*
match must not silently survive. `series_index` on the bookorbit side is
only written when it parses as a number, matching the validation
`_set_calibre_series()` already does for the Calibre side.

pikepdf's docinfo sync (on by default) also mirrors title/author into the
classic PDF Info dictionary for readers that only look there.

Sidecar files (see sidecar_writer.py) are written alongside every PDF this
writes to, targeting the two actual reader apps in use — BookOrbit's own
OPF format and Grimmory's own JSON format, both determined by reading each
app's real open-source parser/writer, not guessed from a generic schema.
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
from sidecar_writer import _split_isbn, write_bookorbit_opf, write_grimmory_sidecar

logger = logging.getLogger("pdf_writer")

CALIBRE_NS = "http://calibre-ebook.com/xmp-namespace"
CALIBRE_PREFIX = "calibre"

BOOKORBIT_NS = "https://bookorbit.app/metadata/1.0/"
BOOKORBIT_PREFIX = "bookorbit"

XMP_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_NS_XMP = "http://ns.adobe.com/xap/1.0/"
XMP_NS_XMPIDQ = "http://ns.adobe.com/xmp/Identifier/qual/1.0/"
XMP_NS_CALIBRE_SI = "http://calibre-ebook.com/xmp-namespace-series-index"


@dataclass
class WriteResult:
    filename: str
    success: bool
    message: str


def _get_or_create_rdfdesc(meta: pikepdf.models.metadata.PdfMetadata):
    """Shared by the Calibre-specific writers below, all of which need to
    reach into pikepdf's private XMP internals (_xmp_doc, _get_rdf_root)
    since Calibre's own reader expects qualified/structured properties
    (rdf:parseType="Resource") that pikepdf's public dict-style API has
    no way to write. Reuses the same rdf:Description pikepdf's own public
    API writes simple properties into, rather than creating a second one
    — harmless either way for RDF (multiple Description blocks with the
    same rdf:about are equivalent to one), but keeps the output tidier.
    """
    xmp_doc = meta._xmp_doc
    rdf = xmp_doc._get_rdf_root()
    rdfdesc = rdf.find('rdf:Description[@rdf:about=""]', xmp_doc.NS)
    if rdfdesc is None:
        rdfdesc = etree.SubElement(
            rdf,
            str(QName(XMP_NS_RDF, "Description")),
            attrib={str(QName(XMP_NS_RDF, "about")): ""},
        )
    return rdfdesc


def _set_calibre_identifiers(meta: pikepdf.models.metadata.PdfMetadata, identifiers: dict[str, str]) -> None:
    """Write one or more identifiers in Calibre's own recognized structure.

    See calibre/ebooks/metadata/xmp.py: create_identifiers — this mirrors
    that function's dict-of-schemes signature so multiple identifiers land
    in a single Bag, not overwriting each other. If a future pikepdf
    version changes the internals _get_or_create_rdfdesc relies on, this
    will raise AttributeError, which callers should catch and treat as
    non-fatal — losing the identifier tags is much better than failing
    the whole write.
    """
    meta.register_xml_namespace(XMP_NS_XMPIDQ, "xmpidq")
    rdfdesc = _get_or_create_rdfdesc(meta)

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


def _set_calibre_series(meta: pikepdf.models.metadata.PdfMetadata, series: str, series_index: str | None) -> None:
    """Write series in Calibre's own recognized structure.

    This isn't a plain scalar property either: Calibre's own reader
    (calibre/ebooks/metadata/xmp.py: read_series) looks for the series
    name in a *nested* rdf:value inside calibre:series, and series_index
    as a child element in a completely different namespace
    (calibreSI:series_index, not calibre:series_index) — confirmed
    directly from Calibre's own source (create_series in xmp.py) after a
    plain `<calibre:series>Name</calibre:series>` (what pikepdf's public
    dict API produces for a scalar assignment) turned out to round-trip
    through pikepdf fine but never actually show up as a series in real
    Calibre: read_series's XPath only looks for rdf:value descendants,
    which a plain scalar element doesn't have.
    """
    meta.register_xml_namespace(XMP_NS_CALIBRE_SI, "calibreSI")
    rdfdesc = _get_or_create_rdfdesc(meta)

    for existing in rdfdesc.findall(str(QName(CALIBRE_NS, "series"))):
        rdfdesc.remove(existing)

    s = etree.SubElement(rdfdesc, str(QName(CALIBRE_NS, "series")), attrib={str(QName(XMP_NS_RDF, "parseType")): "Resource"})
    val = etree.SubElement(s, str(QName(XMP_NS_RDF, "value")))
    val.text = series
    # Only write the index when we actually have one -- Calibre's own
    # read_series() defaults a *missing* calibreSI:series_index to 1.0
    # itself, so omitting it changes nothing about what Calibre displays,
    # but avoids fabricating a false "confirmed #1" in the raw data when
    # we genuinely don't know the index (matches how the OPF/JSON sidecars
    # already handle this — see sidecar_writer.py).
    if series_index:
        try:
            idx = float(series_index)
        except ValueError:
            idx = None
        if idx is not None:
            si = etree.SubElement(s, str(QName(XMP_NS_CALIBRE_SI, "series_index")))
            si.text = f"{idx:.2f}"


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
                    meta["pdf:Keywords"] = "; ".join(tags)

                identifiers = {}
                if row.product_id:
                    identifiers["dtrpg"] = row.product_id
                if row.isbn:
                    if any(_split_isbn(row.isbn)):
                        identifiers["isbn"] = row.isbn
                    else:
                        logger.warning("ISBN %r for %s doesn't look like 10 or 13 digits; skipping", row.isbn, path.name)
                if identifiers:
                    try:
                        _set_calibre_identifiers(meta, identifiers)
                    except AttributeError:
                        logger.warning(
                            "Could not write Calibre-style identifiers for %s "
                            "(pikepdf internals may have changed); skipping them",
                            path.name,
                        )

                bookorbit_series_name_key = f"{BOOKORBIT_PREFIX}:seriesName"
                bookorbit_series_index_key = f"{BOOKORBIT_PREFIX}:seriesIndex"

                if row.series:
                    try:
                        _set_calibre_series(meta, row.series, row.series_index)
                    except AttributeError:
                        logger.warning(
                            "Could not write Calibre-style series for %s "
                            "(pikepdf internals may have changed); skipping it",
                            path.name,
                        )
                    # Also in BookOrbit's own (simpler) form -- see the
                    # module docstring for why this isn't redundant with
                    # the .opf sidecar: BookOrbit's default scan precedence
                    # makes the sidecar's series data unreachable whenever
                    # the embedded PDF has any metadata at all, which a
                    # Calibre-tagged PDF always does.
                    meta[bookorbit_series_name_key] = row.series
                    series_index_valid = False
                    if row.series_index:
                        try:
                            float(row.series_index)
                        except ValueError:
                            logger.warning(
                                "Series index %r for %s isn't numeric; omitting bookorbit:seriesIndex",
                                row.series_index, path.name,
                            )
                        else:
                            meta[bookorbit_series_index_key] = row.series_index
                            series_index_valid = True
                    if not series_index_valid and bookorbit_series_index_key in meta:
                        # A prior write may have set this for a different
                        # match that did have a known index -- must not
                        # silently survive a re-tag that doesn't.
                        del meta[bookorbit_series_index_key]
                else:
                    # No series in the current match at all -- clear any
                    # stale series data a previous write left behind,
                    # rather than letting it silently survive a re-tag.
                    # _set_calibre_series() only clears calibre:series when
                    # it's actually *called*, which doesn't happen here.
                    rdfdesc = _get_or_create_rdfdesc(meta)
                    for existing in rdfdesc.findall(str(QName(CALIBRE_NS, "series"))):
                        rdfdesc.remove(existing)
                    for key in (bookorbit_series_name_key, bookorbit_series_index_key):
                        if key in meta:
                            del meta[key]

            pdf.save(path)
    except (pikepdf.PdfError, OSError) as exc:
        return WriteResult(path.name, False, f"write failed: {exc}")

    # Sidecar generation is always best-effort: the embedded PDF write above
    # already succeeded, so a sidecar failure (of any kind, not just OSError
    # — e.g. an encoding issue) must not blow up the whole write.
    try:
        write_bookorbit_opf(path, row)
    except Exception:
        logger.exception("Failed to write BookOrbit .opf sidecar for %s", path.name)
    try:
        write_grimmory_sidecar(path, row)
    except Exception:
        logger.exception("Failed to write Grimmory .metadata.json sidecar for %s", path.name)

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
