"""Shared metadata model and match-provenance tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Source(str, Enum):
    """Where a matched piece of metadata came from."""

    DTRPG_LIBRARY = "dtrpg-library"
    DTRPG_CATALOG = "dtrpg-catalog"
    MANUAL = "manual"
    BITS_AND_MORTAR = "bits-and-mortar"


class Status(str, Enum):
    """Review state of a single filename -> metadata match."""

    AUTO_ACCEPTED = "auto-accepted"
    NEEDS_REVIEW = "needs-review"
    NO_MATCH = "no-match"
    APPROVED = "approved"


@dataclass
class ProductMetadata:
    """Normalized metadata for a single product, regardless of which
    DriveThruRPG endpoint (or manual override) it came from."""

    title: str
    series: str = ""
    series_index: str = ""
    publisher: str = ""
    authors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    description: str = ""
    product_url: str = ""
    source: Source = Source.MANUAL
    product_id: str = ""
    isbn: str = ""

    def authors_str(self) -> str:
        return "; ".join(self.authors)

    def tags_str(self) -> str:
        return "; ".join(self.tags)
