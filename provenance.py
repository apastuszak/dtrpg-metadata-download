"""Shared metadata model and match-provenance tracking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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


# XMP namespaces this project writes (pdf_writer.py) and reads back
# (renamer.py) -- one copy, so the reader can't drift from the writer and
# silently take every tagged book for an untagged one.
CALIBRE_NS = "http://calibre-ebook.com/xmp-namespace"
BOOKORBIT_NS = "https://bookorbit.app/metadata/1.0/"
XMP_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XMP_NS_XMP = "http://ns.adobe.com/xap/1.0/"
XMP_NS_XMPIDQ = "http://ns.adobe.com/xmp/Identifier/qual/1.0/"


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


# Tokens that are noisy for matching purposes (scanner/release artifacts,
# not part of the actual title) but that we keep around after stripping,
# since an edition/version token that also shows up in a candidate's
# title is a useful tie-breaker rather than pure noise.
NOISE_TOKEN_PATTERN = re.compile(
    r"""(?ix)
    \b(
        ocr | scan | remaster(?:ed)? | v\d+(?:\.\d+)* |
        \d+(?:st|nd|rd|th)\s*edition | \d+(?:\.\d+)?e | revised | reprint |
        gmt | pdf
    )\b
    """
)
BRACKETED_PATTERN = re.compile(r"[\(\[\{][^)\]\}]*[\)\]\}]")

# Publisher SKU/product codes (e.g. "MGP3800", "MGP40000"). DriveThruRPG's
# catalog search endpoint isn't a fuzzy multi-token search — extra words
# that don't appear in the actual product title (SKUs, edition shorthand
# above) drive it to zero results rather than just ranking lower, so these
# need to come out of the query entirely, not just get down-weighted.
SKU_PATTERN = re.compile(r"\b[A-Z]{2,6}\d{3,}[A-Za-z0-9]*\b")


def derive_query(filename: str) -> tuple[str, list[str]]:
    """Turn a filename into a search query, plus a list of stripped
    noise/edition tokens that can still inform scoring."""
    stem = Path(filename).stem
    # Noise comes out both before and after periods and hyphens become
    # spaces: before, or "v1.23" loses only its "v1" and leaves "23";
    # after, or "2nd-Edition" isn't seen as "2nd Edition".
    stem = stem.replace("_", " ")
    noise_tokens = [m.group(1).strip() for m in NOISE_TOKEN_PATTERN.finditer(stem)]
    stem = NOISE_TOKEN_PATTERN.sub(" ", stem)
    stem = stem.replace(".", " ").replace("-", " ")
    noise_tokens += [m.group(1).strip() for m in NOISE_TOKEN_PATTERN.finditer(stem)]
    stem = NOISE_TOKEN_PATTERN.sub(" ", stem)

    noise_tokens += [m.group(0) for m in SKU_PATTERN.finditer(stem)]
    stem = SKU_PATTERN.sub(" ", stem)
    stem = BRACKETED_PATTERN.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem, noise_tokens
