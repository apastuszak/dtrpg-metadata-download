"""Renames a tagged PDF (and its sidecars) to "<series> - <title>.pdf".

Reads title/series back from the Grimmory `.metadata.json` sidecar
(sidecar_writer.py) rather than the embedded PDF XMP. The embedded
Calibre `calibre:series` field is a qualified/structured XMP property
(nested rdf:value) that pikepdf's public dict API can't read back any
more reliably than it can write it (see pdf_writer.py's module
docstring) — and the plain-scalar `bookorbit:seriesName` fallback isn't
guaranteed present on every file (confirmed missing on a real,
previously-tagged file in Books/, most likely written before that field
existed). The JSON sidecar has no such ambiguity: it's flat JSON,
fully regenerated on every write (no staleness risk), and already
carries exactly `title` and `series.name`.

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


def plan_rename(pdf_path: Path) -> RenamePlan:
    title, series = _read_sidecar_metadata(pdf_path)
    if not title:
        return RenamePlan(pdf_path, pdf_path.stem, reason="no title in .metadata.json sidecar (untagged file?)")
    new_stem = _target_stem(title, series)
    if new_stem == pdf_path.stem:
        return RenamePlan(pdf_path, new_stem, reason="already named correctly")
    return RenamePlan(pdf_path, new_stem)


def _companion_pairs(pdf_path: Path, new_pdf_path: Path) -> list[tuple[Path, Path]]:
    """(old, new) path pairs for the PDF plus every sidecar/backup that
    shares its stem — mirrors the exact naming logic pdf_writer.py and
    sidecar_writer.py use to derive those paths in the first place — for
    whichever of those actually exist on disk."""
    pairs = [
        (pdf_path, new_pdf_path),
        (pdf_path.with_suffix(pdf_path.suffix + ".bak"), new_pdf_path.with_suffix(new_pdf_path.suffix + ".bak")),
        (pdf_path.with_suffix(".opf"), new_pdf_path.with_suffix(".opf")),
        (pdf_path.parent / f"{pdf_path.stem}.metadata.json", new_pdf_path.parent / f"{new_pdf_path.stem}.metadata.json"),
        # Written by --hyperlink-gurps/--hyperlink-mongoose (rpg_hyperlink.py).
        (pdf_path.parent / f"{pdf_path.stem}_link_report.csv", new_pdf_path.parent / f"{new_pdf_path.stem}_link_report.csv"),
    ]
    return [(old, new) for old, new in pairs if old.exists()]


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

    for old, new in pairs:
        # On a case-insensitive filesystem (the default on macOS/Windows),
        # a case-only rename like "sword worlds.pdf" -> "Sword Worlds.pdf"
        # has new.exists() true for the very file being renamed -- verified
        # directly: this used to reject every such rename as "target
        # already exists" even though nothing would actually collide.
        # samefile() distinguishes that from a real different file already
        # sitting at the target name.
        if new.exists() and not new.samefile(old):
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
