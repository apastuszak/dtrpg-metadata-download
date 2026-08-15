"""Read/write helpers for the human-review CSV (data/review.csv).

This file is the gate between automated matching and writing metadata
into PDFs: nothing gets tagged unless its ``status`` column says
``approved`` (or was auto-accepted above the high-confidence threshold
and left that way).
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from provenance import Source, Status

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

    def is_approved(self) -> bool:
        return self.status in (Status.APPROVED.value, Status.AUTO_ACCEPTED.value)


def load_review(path: str | Path) -> list[ReviewRow]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        known = {f.name for f in fields(ReviewRow)}
        return [ReviewRow(**{k: v for k, v in row.items() if k in known}) for row in reader]


def save_review(path: str | Path, rows: list[ReviewRow]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def merge_by_filename(existing: list[ReviewRow], fresh: list[ReviewRow]) -> list[ReviewRow]:
    """Merge freshly-matched rows into an existing review file.

    Rows the user has already touched (status != no-match/needs-review
    left untouched from a prior auto-run, i.e. anything the user flipped
    to ``approved`` or hand-edited) are preserved as-is rather than
    clobbered by a re-scan. New filenames are appended.
    """
    by_filename = {row.filename: row for row in existing}
    for row in fresh:
        prior = by_filename.get(row.filename)
        if prior is not None and prior.status == Status.APPROVED.value:
            continue
        by_filename[row.filename] = row
    return list(by_filename.values())
