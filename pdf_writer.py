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

``bookorbit_mode`` (write_metadata's second knob, off by default) takes
the opposite approach instead of fighting BookOrbit's precedence: rather
than duplicating series into the embedded PDF, it wipes *all* PDF-level
metadata — the full XMP packet plus the classic Info dictionary, not
just the fields this tool would otherwise write — so BookOrbit's
embedded-metadata source finds nothing at all and is forced to fall
through to the .opf sidecar, which is written in this mode instead of
skipped. A surgical removal of only this tool's own fields wouldn't be
enough: a PDF can already carry a publisher-set /Title or /Author before
this tool ever touches it, and that alone is sufficient for BookOrbit's
"first source that returns anything" check to short-circuit past the
sidecar. When this mode is off (the default), the .opf sidecar is *not*
written at all — with normal embedded metadata present, BookOrbit would
never open it anyway (see above), so writing it unconditionally, as
earlier versions of this module did, was dead weight.

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
from typing import Callable

import pikepdf
from lxml import etree
from lxml.etree import QName

from image_converter import convert_images_to_rgb
from matcher import scan_pdfs
from review import ReviewRow
from rpg_background_layer import apply_background_layer
from rpg_grayscale import convert_pdf_grayscale
from rpg_hyperlink import GURPS_HYPERLINK_SCRIPT, MONGOOSE_HYPERLINK_SCRIPT, run_hyperlink_script
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


def backup(path: Path) -> Path:
    """One-time backup -- only creates a `.bak` if one doesn't already
    exist, so it always represents the pre-tagging original even across
    repeated re-tagging runs (must not be refreshed on every call). Public
    (not `_backup`) since `tag_tui.py`/`gui_tag_flow.py` also need to call
    this directly, before removing a detected watermark -- that step runs
    before write_metadata() ever gets to make its own backup, and without
    this the `.bak` would end up capturing the *watermark-removed* file
    instead of the true original."""
    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
    else:
        logger.debug("Backup already exists for %s, leaving it as-is", path.name)
    return backup_path


def _run_hyperlink_step(
    path: Path, system_label: str, script_path: str | Path, log: Callable[[str], None] | None
) -> None:
    """Shared by both --hyperlink-gurps and --hyperlink-mongoose below --
    same script interface (see rpg_hyperlink.py), same logging shape,
    only the script path and a human-readable label differ."""
    logger.info("Hyperlinking %s (%s)...", path.name, system_label)
    if log is not None:
        log(f"Hyperlinking {path.name} ({system_label})...")
    result = run_hyperlink_script(path, script_path, log=log)
    if not result.success:
        logger.warning("Hyperlinking failed for %s (%s): %s", path.name, system_label, result.message)
        if log is not None:
            log(f"Hyperlinking failed for {path.name} ({system_label}): {result.message}")
    else:
        logger.info(
            "Added %d page-reference/%d chapter/%d TOC link(s) in %s (%s)",
            result.added, result.chapter_added, result.toc_added, path.name, system_label,
        )
        if log is not None:
            log(
                f"Added {result.added} page-reference, {result.chapter_added} chapter, "
                f"{result.toc_added} TOC link(s) in {path.name} ({system_label})"
            )


def write_metadata(
    path: Path,
    row: ReviewRow,
    bookorbit_mode: bool = False,
    convert_images: bool = False,
    convert_grayscale: bool = False,
    background_layer: bool = False,
    remove_background: bool = False,
    hyperlink_gurps: bool = False,
    hyperlink_mongoose: bool = False,
    log: Callable[[str], None] | None = None,
) -> WriteResult:
    """`log`, if given, is called with human-readable progress lines for
    the image-conversion/hyperlinking steps specifically -- separate from
    this function's own `logger.info()`/`logger.warning()` calls, which
    cover the plain CLI (visible by default; TextualHandler routes them to
    stderr there since no Textual app is active) but never reach the
    TUI's or GUI's own visible log/notification surfaces, which have no
    connection to Python's logging module at all. Without an explicit
    "starting" message here, --convert-images/--convert-grayscale/
    --background-layer/--remove-background/--hyperlink-gurps/
    --hyperlink-mongoose on a PDF with many images/pages can run for a
    while with nothing visible happening in either UI, which looks
    indistinguishable from a hang.

    `convert_images`/`convert_grayscale` are opposite operations on the
    same images, and `background_layer`/`remove_background` are opposite
    operations on the same background -- both pairs' mutual exclusivity
    is enforced up at the CLI (argparse mutually-exclusive group) and GUI
    (checking one un-checks the other) layers, not here; this function
    just runs whichever step(s) it's told to, in the order given below."""
    if not path.exists():
        return WriteResult(path.name, False, "file not found")

    try:
        backup(path)
    except OSError as exc:
        return WriteResult(path.name, False, f"backup failed: {exc}")

    if background_layer or remove_background:
        # Runs first among the optional pre-pikepdf steps -- if the
        # background is about to be removed entirely, there's no reason
        # for a later --convert-images/--convert-grayscale pass to waste
        # time re-encoding it first. Otherwise safe regardless of order:
        # OCG tagging only ever touches an XObject's /OC key, completely
        # disjoint from the image-encoding keys --convert-images/
        # --convert-grayscale touch, or the /Annots links hyperlinking
        # adds. Verified directly (not just reasoned through) that both
        # modes survive a subsequent pikepdf metadata write intact -- see
        # rpg_background_layer.py's module docstring.
        action = "Removing background from" if remove_background else "Tagging background layer in"
        logger.info("%s %s...", action, path.name)
        if log is not None:
            log(f"{action} {path.name}...")
        bg_result = apply_background_layer(path, remove=remove_background, log=log)
        if not bg_result.success:
            logger.warning("Background-layer step failed for %s: %s", path.name, bg_result.message)
            if log is not None:
                log(f"Background-layer step failed for {path.name}: {bg_result.message}")
        else:
            logger.info("Background-layer step succeeded for %s", path.name)

    if convert_images:
        # Deliberately runs before pikepdf ever opens the file below --
        # see image_converter.py's module docstring for why this must not
        # happen *after* pikepdf's metadata write. Best-effort: a failure
        # here is logged and the metadata write still proceeds, matching
        # the sidecar-failure convention elsewhere in this function
        # (losing one piece must not fail a write that could otherwise
        # succeed).
        logger.info("Converting images in %s...", path.name)
        if log is not None:
            log(f"Converting images in {path.name}...")
        result = convert_images_to_rgb(path)
        if not result.success:
            logger.warning("Image conversion failed for %s: %s", path.name, result.message)
            if log is not None:
                log(f"Image conversion failed for {path.name}: {result.message}")
        else:
            logger.info(
                "Converted %d image(s) to RGB JPEG in %s (%d already fine/skipped)",
                result.converted, path.name, result.skipped,
            )
            if log is not None:
                log(f"Converted {result.converted} image(s) in {path.name} ({result.skipped} already fine/skipped)")

    if convert_grayscale:
        # Also runs before pikepdf opens the file below, same reasoning as
        # --convert-images above -- verified directly (not just reasoned
        # through) that a link annotation survives Ghostscript's
        # grayscale pass (see rpg_grayscale.py's module docstring), so
        # running this before the hyperlink steps below is safe. A file
        # never gets both --convert-images and --convert-grayscale (see
        # the mutual-exclusivity note above), so there's no meaningful
        # ordering question between the two of them.
        logger.info("Converting %s to grayscale...", path.name)
        gray_result = convert_pdf_grayscale(path, log=log)
        if not gray_result.success:
            logger.warning("Grayscale conversion failed for %s: %s", path.name, gray_result.message)
            if log is not None:
                log(f"Grayscale conversion failed for {path.name}: {gray_result.message}")
        else:
            logger.info("Converted %s to grayscale", path.name)

    # Both also run before pikepdf opens the file below, same reasoning as
    # --convert-images above -- verified directly (not just reasoned
    # through) that a link annotation added via PyMuPDF's saveIncr()
    # survives a subsequent pikepdf metadata rewrite, so this ordering is
    # safe; see rpg_hyperlink.py's module docstring. Best-effort, same as
    # --convert-images: a missing/misconfigured script or a book the
    # detector can't make sense of must not fail a write that could
    # otherwise succeed. Mutually independent -- a file could in
    # principle have both flags set, though in practice a given book is
    # only ever one publisher's, so at most one will actually find
    # anything to link.
    if hyperlink_gurps:
        _run_hyperlink_step(path, "GURPS", GURPS_HYPERLINK_SCRIPT, log)
    if hyperlink_mongoose:
        _run_hyperlink_step(path, "Mongoose", MONGOOSE_HYPERLINK_SCRIPT, log)

    try:
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            if bookorbit_mode:
                # See module docstring: wipe everything, not just this
                # tool's own fields, so BookOrbit's embedded-metadata
                # source is guaranteed to return nothing and fall
                # through to the .opf sidecar written below.
                with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                    meta.clear()
                for key in list(pdf.docinfo.keys()):
                    del pdf.docinfo[key]
                pdf.save(path)
            else:
                _write_embedded_metadata(pdf, path, row)
    except (pikepdf.PdfError, OSError) as exc:
        return WriteResult(path.name, False, f"write failed: {exc}")

    # Sidecar generation is always best-effort: the embedded PDF write above
    # already succeeded, so a sidecar failure (of any kind, not just OSError
    # — e.g. an encoding issue) must not blow up the whole write.
    try:
        write_grimmory_sidecar(path, row)
    except Exception:
        logger.exception("Failed to write Grimmory .metadata.json sidecar for %s", path.name)

    if bookorbit_mode:
        # Off by default -- see module docstring for why writing this
        # unconditionally (as earlier versions did) was dead weight.
        try:
            write_bookorbit_opf(path, row)
        except Exception:
            logger.exception("Failed to write BookOrbit .opf sidecar for %s", path.name)
    else:
        # A prior write may have been --bookorbit-mode and left an .opf
        # behind with that match's data -- must not silently survive a
        # later non-bookorbit-mode re-tag to a different match, same
        # principle as the series-clearing logic above.
        opf_path = path.with_suffix(".opf")
        if opf_path.exists():
            try:
                opf_path.unlink()
            except OSError:
                logger.exception("Failed to remove stale BookOrbit .opf sidecar for %s", path.name)

    return WriteResult(path.name, True, "ok")


def _write_embedded_metadata(pdf: pikepdf.Pdf, path: Path, row: ReviewRow) -> None:
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
            try:
                rdfdesc = _get_or_create_rdfdesc(meta)
                for existing in rdfdesc.findall(str(QName(CALIBRE_NS, "series"))):
                    rdfdesc.remove(existing)
            except AttributeError:
                # Same private-pikepdf-internals risk _set_calibre_series()
                # above already guards against -- this call site was
                # missing the guard (an earlier oversight, caught in a
                # later pass), so a future pikepdf version changing these
                # internals would crash the whole write here specifically
                # when a match has *no* series, instead of degrading the
                # same way every other Calibre-specific write already does.
                logger.warning(
                    "Could not clear stale Calibre-style series for %s "
                    "(pikepdf internals may have changed); leaving it as-is",
                    path.name,
                )
            for key in (bookorbit_series_name_key, bookorbit_series_index_key):
                if key in meta:
                    del meta[key]

    pdf.save(path)


def write_approved(
    rows: list[ReviewRow],
    root: str | Path,
    bookorbit_mode: bool = False,
    convert_images: bool = False,
    convert_grayscale: bool = False,
    background_layer: bool = False,
    remove_background: bool = False,
    hyperlink_gurps: bool = False,
    hyperlink_mongoose: bool = False,
    log: Callable[[str], None] | None = None,
) -> list[WriteResult]:
    root = Path(root)
    # Built once as a plain name lookup rather than calling
    # root.rglob(row.filename) per row -- that treated the filename as a
    # glob *pattern*, not a literal name: a real filename containing
    # glob-special characters (verified: "Sword Worlds [OCR].pdf" matched
    # nothing at all, since "[OCR]" is a character class to rglob) was
    # silently reported as "file not found under root" even though the
    # file was right there. `*`/`?` in a filename could also silently
    # match the *wrong* file instead of erroring. Duplicate filenames
    # under different subfolders are warned about once here (the same
    # "first match wins" behavior rglob() already had, just now
    # unambiguous about when it's happening) rather than silently picking
    # whichever one os.walk() happens to see first.
    pdfs_by_name: dict[str, Path] = {}
    duplicate_names: set[str] = set()
    for p in scan_pdfs(root):
        if p.name in pdfs_by_name:
            duplicate_names.add(p.name)
        else:
            pdfs_by_name[p.name] = p
    for name in sorted(duplicate_names):
        logger.warning("Multiple files named %r found under %s; using %s", name, root, pdfs_by_name[name])

    results: list[WriteResult] = []
    for row in rows:
        if not row.is_approved():
            continue
        match = pdfs_by_name.get(row.filename)
        if match is None:
            results.append(WriteResult(row.filename, False, "file not found under root"))
            continue
        try:
            results.append(
                write_metadata(
                    match, row, bookorbit_mode=bookorbit_mode, convert_images=convert_images,
                    convert_grayscale=convert_grayscale,
                    background_layer=background_layer, remove_background=remove_background,
                    hyperlink_gurps=hyperlink_gurps, hyperlink_mongoose=hyperlink_mongoose,
                    log=log,
                )
            )
        except Exception as exc:
            # write_metadata() already catches (pikepdf.PdfError, OSError)
            # around its own pikepdf.open() block, and every optional
            # pre-write step (image conversion, hyperlinking, grayscale,
            # background-layer) is documented never to raise at all -- but
            # anything else escaping from in between (a malformed
            # ReviewRow field hitting an edge case in a library this
            # doesn't specifically guard, etc.) used to propagate straight
            # out of this function, aborting every remaining approved file
            # in the batch over one bad one. One bad file must not be able
            # to do that to the rest of a `write-pdfs`/`all` run.
            logger.exception("Unexpected error writing %s", match.name)
            results.append(WriteResult(row.filename, False, f"unexpected error: {exc}"))

    succeeded = sum(1 for r in results if r.success)
    logger.info("Wrote metadata to %d/%d approved files", succeeded, len(results))
    for r in results:
        if not r.success:
            logger.warning("Failed: %s (%s)", r.filename, r.message)
    return results
