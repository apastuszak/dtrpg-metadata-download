"""Read/write helpers for the human-review CSV (data/review.csv).

This file is the gate between automated matching and writing metadata
into PDFs: nothing gets tagged unless its ``status`` column says
``approved`` (or was auto-accepted above the high-confidence threshold
and left that way).
"""

from __future__ import annotations

import csv
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from provenance import Source, Status

# Leading characters a spreadsheet app (Excel/LibreOffice/Numbers) reads as
# the start of a formula. matched_title/description/publisher/tags are
# sourced from DriveThruRPG's *public* catalog -- anyone can list a
# product there, so this is attacker-reachable content, not just the
# user's own data -- and review.csv is a file the documented workflow has
# the user open by hand in a spreadsheet app. Prefixing with a leading
# apostrophe (the standard CSV-injection mitigation) forces those apps to
# treat the value as literal text instead of evaluating it.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

FIELDNAMES = [
    "filename",
    "matched_title",
    "series",
    "series_index",
    "publisher",
    "confidence_score",
    "source",
    "status",
    "authors",
    "tags",
    "description",
    "product_url",
    "product_id",
    "isbn",
    "edited",
]


@dataclass
class ReviewRow:
    filename: str
    matched_title: str = ""
    series: str = ""
    series_index: str = ""
    publisher: str = ""
    confidence_score: str = ""
    source: str = ""
    status: str = Status.NO_MATCH.value
    authors: str = ""
    tags: str = ""
    description: str = ""
    product_url: str = ""
    product_id: str = ""
    isbn: str = ""
    # Set by mark_edited() whenever a field is changed through the GUI's
    # Review tab (see gui_app.py's ReviewTab), independently of `status`.
    # merge_by_filename() below preserves any row with this set, not just
    # ones flipped to `approved` -- a real gap this closes: editing an
    # auto-accepted row's series in the Review tab, without also flipping
    # its status, used to be silently lost on the next scan. Blank by
    # default, so an old review.csv with no "edited" column loads exactly
    # as before (nothing is retroactively treated as edited).
    edited: str = ""

    def is_approved(self) -> bool:
        return self.status in (Status.APPROVED.value, Status.AUTO_ACCEPTED.value)

    def mark_edited(self) -> None:
        self.edited = "1"


def _defang_formula(value: str | None) -> str | None:
    # csv.DictReader fills a short/hand-edited row's missing trailing
    # columns with None (restval), and that can round-trip back in here
    # via merge_by_filename -> save_review -- pass non-str values through
    # unchanged rather than crashing on .startswith().
    if not isinstance(value, str):
        return value
    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _refang_formula(value: str | None) -> str | None:
    """Inverse of _defang_formula, applied on read -- otherwise the safety
    prefix would leak into matched_title/etc. and end up written into the
    PDF itself as a stray leading apostrophe."""
    if not isinstance(value, str):
        return value
    if value.startswith("'") and value[1:].startswith(_FORMULA_PREFIXES):
        return value[1:]
    return value


def load_review(path: str | Path) -> list[ReviewRow]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        known = {f.name for f in fields(ReviewRow)}
        # csv.DictReader fills a short/hand-edited row's missing trailing
        # columns with None (its restval default) -- review.csv is
        # explicitly meant to be hand-edited, so this is an easy state to
        # reach. Every ReviewRow field is typed str = "", and downstream
        # code (e.g. pdf_writer's row.tags.split(";")) assumes that; a
        # bare None here crashed with AttributeError partway through a
        # write batch instead of being treated as "no data for this
        # field", the same class of failure _refang_formula's own
        # None-passthrough guard exists to prevent one step earlier.
        return [
            ReviewRow(**{k: (_refang_formula(v) or "") for k, v in row.items() if k in known})
            for row in reader
        ]


def save_review(path: str | Path, rows: list[ReviewRow]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a temp file in the same directory, then swapped in with
    # os.replace() (atomic on the same filesystem) rather than writing
    # path.open("w", ...) directly -- a crash or kill partway through the
    # old direct-write approach (e.g. mid-`scan --apply-review`, or the
    # process dying while the GUI's Review tab saves) truncated
    # review.csv, silently losing every approved/hand-edited row it held,
    # not just whatever this particular save was trying to add.
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        suffix=".csv", prefix=f".{path.name}.tmp-", dir=str(path.parent)
    )
    try:
        # mkstemp() always creates its file 0600 (owner-only), and
        # os.replace() preserves the temp file's own mode on POSIX -- a
        # real regression this fixed: every save used to silently
        # tighten review.csv's permissions from its normal 644 down to
        # 600, which would break anything else (a NAS share, another
        # user/process) that expected to read it. Preserve the existing
        # file's mode if there is one; otherwise fall back to whatever a
        # plain `open(path, "w")` would have produced under the current
        # umask, matching this function's pre-atomic-write behavior.
        if path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
        else:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp_path_str, mode)
        with os.fdopen(tmp_fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _defang_formula(v) for k, v in asdict(row).items()})
        os.replace(tmp_path_str, path)
    except BaseException:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


def merge_by_filename(existing: list[ReviewRow], fresh: list[ReviewRow]) -> list[ReviewRow]:
    """Merge freshly-matched rows into an existing review file.

    A prior row is preserved as-is (not clobbered by a re-scan) if it's
    been flipped to ``approved``, or if `edited` is set -- the latter
    closes a real gap `approved`-only checking used to have: editing an
    auto-accepted row's series (say) in the GUI's Review tab, without
    also flipping its status, used to be silently overwritten by the
    next scan. New filenames are appended.
    """
    by_filename = {row.filename: row for row in existing}
    for row in fresh:
        prior = by_filename.get(row.filename)
        if prior is not None and (prior.status == Status.APPROVED.value or prior.edited):
            continue
        by_filename[row.filename] = row
    return list(by_filename.values())
