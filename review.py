"""The metadata to write for one PDF, as `tag` confirms it.

`ReviewRow` is what pdf_writer.write_metadata() and sidecar_writer.py
take (built by matcher.row_from_match()). It's named after the review.csv
file it used to be a row of: the scan -> review.csv -> write-pdfs batch
workflow was removed (see docs/HISTORY.md), but every writer still takes
this one flat, all-strings record, so it stays.
"""

from __future__ import annotations

from dataclasses import dataclass

from provenance import Status


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
