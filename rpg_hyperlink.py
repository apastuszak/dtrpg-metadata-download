"""Runs `hyperlink_engine.py` over a book in place, for `--hyperlink RULES`
(and the GUI's hyperlinking dropdown). RULES is one of the engine's
publisher rule sets: a module name in hyperlink_rules/, listed by
`rule_sets()` below.

This used to load three separate scripts by file path -- two vendored from
a sibling repo (`hyperlink_pdf_universal.py` for GURPS,
`hyperlink_pdf_mongoose.py`, a fork of it) and one written here for Delta
Green. They were replaced by one engine with a rule set per publisher, so
a fix to the shared parts (page numbering, existing-link handling, safe
saving) reaches every publisher; see docs/HISTORY.md.

The engine copies its input to a new output path (it can't write over
what it reads), so this runs it into a temp file next to the book and
atomically replaces the book only once that succeeds.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import hyperlink_engine


@dataclass
class RuleSetInfo:
    name: str          # what --hyperlink takes: the module name
    label: str         # for the GUI dropdown
    description: str


def rule_sets() -> list[RuleSetInfo]:
    """Every rule set that loads, sorted by label. A module that fails to
    load is left out here (it's logged, and `hyperlink_engine.py --list`
    shows why)."""
    found = [
        RuleSetInfo(name, rules.label or name, rules.description)
        for name, (rules, _error) in hyperlink_engine.available_rule_sets().items()
        if rules is not None
    ]
    return sorted(found, key=lambda r: r.label.lower())


def rule_set_label(name: str) -> str:
    """The label for rule set `name`, or `name` itself if it doesn't load."""
    return next((r.label for r in rule_sets() if r.name == name), name)


@dataclass
class HyperlinkResult:
    success: bool
    added: int = 0
    chapter_added: int = 0
    toc_added: int = 0
    message: str = ""


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def run_hyperlink(
    path: Path,
    rules: str,
    log: Callable[[str], None] | None = None,
) -> HyperlinkResult:
    """Hyperlinks `path` in place with rule set `rules`. Never raises --
    returns success=False with a message on any failure, matching
    `image_converter.py`'s report-don't-crash convention (one unsupported
    PDF must not take down a batch).

    Saves to a temp file and atomically replaces `path` only once that
    succeeds (`os.replace()`, same filesystem via `dir=path.parent`), so a
    failure partway through leaves the original untouched. The engine's
    `<out_stem>_link_report.csv` (everything it linked or skipped, and why)
    is kept, renamed to match `path`'s own stem, alongside the final file.
    """
    if not path.exists():
        return HyperlinkResult(success=False, message="file not found")

    def quiet(line: str) -> None:
        pass

    # Created inside the try, so a full disk or an unwritable folder fails
    # this step like any other error instead of escaping write_metadata()
    # and stopping a whole batch.
    tmp_out_path: Path | None = None
    tmp_report_path: Path | None = None
    try:
        # A hidden prefix (matching scan_pdfs()'s own dotfile skip in
        # matcher.py) means a temp file left behind by a force-killed run
        # can't later get picked up and "matched" as if it were a real book.
        tmp_fd, tmp_out_str = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
        tmp_out_path = Path(tmp_out_str)
        tmp_report_path = tmp_out_path.with_name(tmp_out_path.stem + "_link_report.csv")
        os.close(tmp_fd)

        counts = hyperlink_engine.hyperlink_pdf(
            str(path), str(tmp_out_path), rules, log=(log if log is not None else quiet))

        # Keep the book's original permissions (best-effort) -- see rpg_background_layer.py.
        try:
            shutil.copymode(path, tmp_out_path)
        except OSError:
            pass
        os.replace(tmp_out_path, path)
        tmp_out_path = None  # already moved -- don't clean it up in finally

        # The book is linked from here on, so a failure to keep the report
        # is only a warning -- it must not report the whole step as failed.
        report_dest = path.with_name(f"{path.stem}_link_report.csv")
        message = ""
        if tmp_report_path.exists():
            try:
                os.replace(tmp_report_path, report_dest)
                tmp_report_path = None
            except OSError as exc:
                message = f"links added, but the link report couldn't be saved as {report_dest.name}: {exc}"

        return HyperlinkResult(
            success=True,
            added=counts.get("added", 0),
            chapter_added=counts.get("chapter_added", 0),
            toc_added=counts.get("toc_added", 0),
            message=message,
        )
    except Exception as exc:
        return HyperlinkResult(success=False, message=f"hyperlinking failed: {exc}")
    finally:
        if tmp_out_path is not None:
            _unlink_quietly(tmp_out_path)
        if tmp_report_path is not None:
            _unlink_quietly(tmp_report_path)
