"""Shared metadata model and match-provenance tracking."""

from __future__ import annotations

import re
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


# A publisher's export that left its file name in /Title ("High Guard
# Cover.indd", "Banestorm Book.qxp"). Only real layout/document
# extensions: "any short suffix after a dot" also took "Rules 3.5e" and
# "Traveller Rules v1.23" for file names. Used by renamer.py and hyperlink_engine.py.
_FILENAME_TITLE_RE = re.compile(
    r"\.(?:indd|indt|idml|qxp|qxd|qxt|pmd|p65|fm|book|doc|docx|rtf|odt|pages|txt|pdf|ai|eps)$",
    re.IGNORECASE)


def is_filename_title(title: str) -> bool:
    """True if `title` is a file name, not a real title."""
    return bool(_FILENAME_TITLE_RE.search(title.strip()))
