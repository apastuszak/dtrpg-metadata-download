"""Renames a tagged PDF (and its sidecars) to "<series> - <title>.pdf".

Reads title/series back from whatever the file was tagged with, in this
order (see pdf_writer.METADATA_MODES for which mode writes what):

1. the Grimmory `.metadata.json` sidecar -- flat JSON, fully regenerated
   on every write, carrying exactly `title` and `series.name`;
2. the BookOrbit `.opf` sidecar -- `dc:title` and
   `<meta name="calibre:series">`;
3. the embedded Calibre XMP -- `dc:title`, and `calibre:series`'s nested
   `rdf:value`, read from the raw XMP with lxml. pikepdf's public dict
   API can't read that structured property back any more reliably than it
   can write it (see pdf_writer.py's module docstring), so it isn't used
   for the series. A file with a title but no series there is renamed
   without one -- reading the raw structure is exact, so "no series"
   means the file has none.

`tag --rename` passes the confirmed title and series in directly instead,
so it works even with metadata tagging off.

Renaming a PDF means renaming everything else that shares its stem too
(sidecar_writer.py, pdf_writer.py's own backup logic, and rpg_hyperlink.py
all derive their filenames from the PDF's stem) — `.pdf.bak`, `.opf`,
`.metadata.json`, and `_link_report.csv` all move together with the PDF,
or none of them do.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("renamer")

_DC_NS = "http://purl.org/dc/elements/1.1/"
_OPF_NS = "http://www.idpf.org/2007/opf"
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_CALIBRE_NS = "http://calibre-ebook.com/xmp-namespace"  # pdf_writer.CALIBRE_NS

_UNSAFE_CHARS_RE = re.compile(r'[/\\:*?"<>|\x00]')
# Matches a colon with at least one trailing space, plus any surrounding
# whitespace (so "Title : Subtitle" and "Title:  Subtitle" both collapse
# to a single " - " instead of leaving extra spaces behind) -- but not a
# colon with no trailing space at all ("Title:Subtitle", or one at the
# very end of a string), which still falls through to the general
# substitution below and becomes a bare hyphen, same as any other
# unsafe character.
_COLON_SUBTITLE_RE = re.compile(r"\s*:\s+")


def _sanitize_component(text: str) -> str:
    """Replace characters that are illegal in a filename -- '/' and NUL
    are the only ones illegal on a native Unix filesystem, but this also
    replaces the Windows/SMB-reserved set (\\:*?"<>|) so a title/series
    doesn't silently fail to write, or get mangled by the OS, if this
    library is ever accessed over SMB/exFAT/a Windows-formatted drive --
    not this project's primary target today, but cheap protection against
    a real failure mode if that ever changes. See CLAUDE.md's note on this
    function's path-traversal defense if this character set changes again;
    it depends on '/' always being one of the replaced characters.

    "Title: Subtitle" is a very common RPG title shape, so a colon
    followed by whitespace (_COLON_SUBTITLE_RE) is special-cased to
    " - " first, before the general substitution below turns any *other*
    colon into a bare hyphen -- a plain colon-to-hyphen swap butts the
    hyphen right up against the preceding word ("Title- Subtitle"), which
    reads badly for the single most common case this character-set
    change actually affects in practice."""
    text = _COLON_SUBTITLE_RE.sub(" - ", text)
    return _UNSAFE_CHARS_RE.sub("-", text).strip()


def _read_sidecar_metadata(pdf_path: Path) -> tuple[str | None, str | None]:
    sidecar_path = pdf_path.parent / f"{pdf_path.stem}.metadata.json"
    if not sidecar_path.exists():
        return None, None
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    # Tolerate a shape that doesn't match what this tool itself writes --
    # e.g. a hand-edited sidecar with `series` simplified to a plain
    # string instead of {name, number} -- rather than crashing the whole
    # batch on one malformed file. Same "skip this file, not the run"
    # guarantee _read_sidecar_metadata's JSON-decode-failure case above
    # already has.
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if not isinstance(metadata, dict):
        return None, None
    title = metadata.get("title")
    title = title if isinstance(title, str) and title else None
    series_obj = metadata.get("series")
    series = series_obj.get("name") if isinstance(series_obj, dict) else None
    series = series if isinstance(series, str) and series else None
    return title, series


def _read_opf_metadata(pdf_path: Path) -> tuple[str | None, str | None]:
    """Title and series from the BookOrbit `.opf` sidecar, as
    sidecar_writer.write_bookorbit_opf() writes them."""
    from lxml import etree

    opf_path = pdf_path.with_suffix(".opf")
    if not opf_path.exists():
        return None, None
    try:
        root = etree.parse(str(opf_path), etree.XMLParser(resolve_entities=False, no_network=True)).getroot()
    except (OSError, etree.XMLSyntaxError):
        return None, None
    title = root.findtext(f".//{{{_DC_NS}}}title")
    series = next(
        (m.get("content") for m in root.iter(f"{{{_OPF_NS}}}meta") if m.get("name") == "calibre:series"), None)
    return (title or "").strip() or None, (series or "").strip() or None


def _read_embedded_metadata(pdf_path: Path) -> tuple[str | None, str | None]:
    """Title and series from the PDF's embedded Calibre XMP (see the
    module docstring for why the series is read from the raw XMP)."""
    import pikepdf
    from lxml import etree

    try:
        with pikepdf.open(pdf_path) as pdf:
            stream = pdf.Root.get("/Metadata")
            if stream is None:
                return None, None
            xmp = stream.read_bytes()
    except (pikepdf.PdfError, OSError):
        return None, None
    try:
        root = etree.fromstring(xmp, etree.XMLParser(resolve_entities=False, no_network=True, recover=True))
    except etree.XMLSyntaxError:
        return None, None
    if root is None:
        return None, None
    title_el = root.find(f".//{{{_DC_NS}}}title")
    title = None
    if title_el is not None:
        # dc:title is an rdf:Alt of language alternatives; take the first
        title = next((li.text for li in title_el.iter(f"{{{_RDF_NS}}}li") if li.text), title_el.text)
    series = None
    series_el = root.find(f".//{{{_CALIBRE_NS}}}series")
    if series_el is not None:
        series = series_el.findtext(f".//{{{_RDF_NS}}}value") or series_el.text
    return (title or "").strip() or None, (series or "").strip() or None


def read_title_and_series(pdf_path: Path) -> tuple[str | None, str | None, str | None]:
    """(title, series, where it came from) from the first source that has
    a title -- see the module docstring for the order."""
    for source, read in (
        (".metadata.json sidecar", _read_sidecar_metadata),
        (".opf sidecar", _read_opf_metadata),
        ("embedded metadata", _read_embedded_metadata),
    ):
        title, series = read(pdf_path)
        if title:
            return title, series, source
    return None, None, None


def _target_stem(title: str, series: str | None) -> str:
    title = _sanitize_component(title)
    if series:
        return f"{_sanitize_component(series)} - {title}"
    return title


@dataclass
class RenamePlan:
    pdf_path: Path
    new_stem: str
    reason: str | None = None  # set means this file will be skipped, not renamed


def plan_rename(pdf_path: Path, title: str | None = None, series: str | None = None) -> RenamePlan:
    """Plans renaming `pdf_path` from `title`/`series` if given (tag's
    --rename passes what was just confirmed), or else from what the file
    was tagged with (`read_title_and_series()`)."""
    if not title:
        title, series, _source = read_title_and_series(pdf_path)
    if not title:
        return RenamePlan(
            pdf_path, pdf_path.stem,
            reason="no title in a .metadata.json or .opf sidecar or the embedded metadata (untagged file?)")
    new_stem = _target_stem(title, series)
    if new_stem == pdf_path.stem:
        return RenamePlan(pdf_path, new_stem, reason="already named correctly")
    return RenamePlan(pdf_path, new_stem)


def _companion_pairs(pdf_path: Path, new_pdf_path: Path, existing_only: bool = True) -> list[tuple[Path, Path]]:
    """(old, new) path pairs for the PDF plus every sidecar/backup that
    shares its stem — mirrors the exact naming logic pdf_writer.py and
    sidecar_writer.py use to derive those paths in the first place — for
    whichever of those actually exist on disk (or all of them, with
    `existing_only=False`)."""
    pairs = [
        (pdf_path, new_pdf_path),
        (pdf_path.with_suffix(pdf_path.suffix + ".bak"), new_pdf_path.with_suffix(new_pdf_path.suffix + ".bak")),
        (pdf_path.with_suffix(".opf"), new_pdf_path.with_suffix(".opf")),
        (pdf_path.parent / f"{pdf_path.stem}.metadata.json", new_pdf_path.parent / f"{new_pdf_path.stem}.metadata.json"),
        # Written by --hyperlink-gurps/--hyperlink-mongoose (rpg_hyperlink.py).
        (pdf_path.parent / f"{pdf_path.stem}_link_report.csv", new_pdf_path.parent / f"{new_pdf_path.stem}_link_report.csv"),
    ]
    return [(old, new) for old, new in pairs if old.exists() or not existing_only]


@dataclass
class RenameResult:
    old_pdf: Path
    new_pdf: Path | None
    success: bool
    message: str


def apply_rename(plan: RenamePlan, dry_run: bool = False) -> RenameResult:
    if plan.reason is not None:
        return RenameResult(plan.pdf_path, None, False, plan.reason)

    new_pdf_path = plan.pdf_path.with_name(plan.new_stem + plan.pdf_path.suffix)
    pairs = _companion_pairs(plan.pdf_path, new_pdf_path)

    # Every companion name is checked, not just the ones this book has: an
    # unrelated "<new name>.metadata.json" left at the target would
    # otherwise become this book's sidecar after the rename, and the next
    # read_title_and_series() would return that other book's title.
    for old, new in _companion_pairs(plan.pdf_path, new_pdf_path, existing_only=False):
        # On a case-insensitive filesystem (the default on macOS/Windows),
        # a case-only rename like "sword worlds.pdf" -> "Sword Worlds.pdf"
        # has new.exists() true for the very file being renamed -- verified
        # directly: this used to reject every such rename as "target
        # already exists" even though nothing would actually collide.
        # samefile() distinguishes that from a real different file already
        # sitting at the target name.
        if new.exists() and not (old.exists() and new.samefile(old)):
            return RenameResult(plan.pdf_path, new_pdf_path, False, f"target already exists: {new.name}")

    if dry_run:
        return RenameResult(plan.pdf_path, new_pdf_path, True, "dry-run")

    done: list[tuple[Path, Path]] = []
    for old, new in pairs:
        try:
            old.rename(new)
        except OSError as exc:
            # A mid-group failure (permission denied, file locked by
            # another app, etc.) must not leave this file's PDF/sidecars
            # split across old and new names -- best-effort rollback of
            # whatever already succeeded before reporting the failure.
            for renamed_old, renamed_new in reversed(done):
                try:
                    renamed_new.rename(renamed_old)
                except OSError:
                    logger.exception("Failed to roll back %s -> %s after a partial rename", renamed_new, renamed_old)
            return RenameResult(plan.pdf_path, new_pdf_path, False, f"rename failed on {old.name}: {exc}")
        done.append((old, new))

    return RenameResult(plan.pdf_path, new_pdf_path, True, "ok")
