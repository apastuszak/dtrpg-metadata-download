"""Matches local PDF filenames against DriveThruRPG metadata candidates."""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from dtrpg_client import DtrpgClient
from provenance import ProductMetadata, Source, Status
from review import ReviewRow

logger = logging.getLogger("matcher")

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

# Lives alongside the PDFs it describes (in --root, or a single file's
# parent directory), not in this project's data/ dir — so each folder of
# PDFs can carry its own list of known matches without needing paths that
# reference other folders.
KNOWN_URLS_FILENAME = "dtrpg_urls.csv"

_PRODUCT_ID_IN_URL_RE = re.compile(r"/products?/(\d+)")


def extract_product_id(text: str) -> str | None:
    """Recognize a bare product ID, 'id:56586', or a pasted DriveThruRPG
    product URL as a direct reference to a specific listing."""
    text = text.strip()
    if text.lower().startswith("id:"):
        rest = text[3:].strip()
        return rest if rest.isdigit() else None
    if text.isdigit():
        return text
    match = _PRODUCT_ID_IN_URL_RE.search(text)
    return match.group(1) if match else None


def load_known_urls(root: str | Path) -> dict[str, str]:
    """Load <root>/dtrpg_urls.csv: filename -> DriveThruRPG product ID.

    A lighter alternative to manual_overrides.yaml for titles that ARE on
    DriveThruRPG but aren't reliably findable through catalog search (see
    DtrpgClient.get_product) — you supply just the URL/ID per filename,
    already-verified, and the tool fetches the rest live instead of you
    typing out full metadata by hand.
    """
    path = Path(root) / KNOWN_URLS_FILENAME
    if not path.exists():
        return {}
    mapping: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "filename" not in fieldnames:
            logger.warning(
                "%s has no 'filename' column (found: %r) — likely missing its header row. "
                "The first line must be exactly 'filename,drivethrurpg_url'; every row is being "
                "skipped until that's fixed.",
                path, fieldnames,
            )
            return mapping
        for row in reader:
            filename = (row.get("filename") or "").strip()
            reference = (row.get("drivethrurpg_url") or row.get("url") or row.get("id") or "").strip()
            if not filename or not reference:
                continue
            product_id = extract_product_id(reference)
            if product_id:
                mapping[filename] = product_id
            else:
                logger.warning("Could not parse a product ID from %r for %r in %s", reference, filename, path)
    return mapping


def scan_pdfs(root: str | Path) -> list[Path]:
    root = Path(root)
    # macOS writes a hidden "._<name>.pdf" AppleDouble sidecar (resource
    # fork/extended attributes) alongside every real file on a filesystem
    # that doesn't natively support them -- common on network shares
    # (SMB/exFAT/etc.) -- and it matches "*.pdf" just as well as the real
    # file. Verified directly: an untouched macOS folder synced to such a
    # share picks these up as if they were real, matchable books. Skip
    # any dotfile, not just this specific prefix, matching how hidden
    # files are conventionally excluded from a file listing generally.
    return sorted(p for p in root.rglob("*.pdf") if p.is_file() and not p.name.startswith("."))


def derive_query(filename: str) -> tuple[str, list[str]]:
    """Turn a filename into a search query, plus a list of stripped
    noise/edition tokens that can still inform scoring."""
    stem = Path(filename).stem
    # Version tokens come out before periods become spaces: "v1.23" would
    # otherwise lose only its "v1", leaving "23" in the query.
    stem = stem.replace("_", " ")
    noise_tokens = [m.group(1).strip() for m in NOISE_TOKEN_PATTERN.finditer(stem)]
    stem = NOISE_TOKEN_PATTERN.sub(" ", stem)
    stem = stem.replace(".", " ").replace("-", " ")

    noise_tokens += [m.group(0) for m in SKU_PATTERN.finditer(stem)]
    stem = SKU_PATTERN.sub(" ", stem)
    stem = BRACKETED_PATTERN.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem, noise_tokens


def _score(query: str, noise_tokens: list[str], candidate: ProductMetadata) -> float:
    base = fuzz.token_sort_ratio(query, candidate.title)
    haystack = f"{candidate.title} {candidate.description}".lower()
    bonus = sum(3 for token in noise_tokens if token.lower() in haystack)
    return min(100.0, base + bonus)


def _text(value) -> str:
    """A manual_overrides.yaml value as text. YAML reads an unquoted
    `isbn: 9781234567897`, `title: 1984` or `series: 1776` as a number,
    and every consumer (the confirm screen's fields, the writers) expects
    str -- an int ISBN made building the confirm dialog raise TypeError."""
    return "" if value is None else str(value).strip()


def _text_list(value) -> list[str]:
    """A list field (authors, tags) as text; a single value written without
    list syntax (`tags: Fantasy`) is one item, not split into letters."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return [_text(v) for v in items if v is not None and _text(v)]


def load_manual_overrides(path: str | Path) -> dict[str, ProductMetadata]:
    path = Path(path)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    overrides: dict[str, ProductMetadata] = {}
    for filename, meta in raw.items():
        # A bare "some_file.pdf:" line with nothing indented under it (a
        # plausible hand-edit mistake, e.g. while deleting a stale entry)
        # parses to `meta = None`, not an empty dict -- `.get()` on that
        # crashed with AttributeError, taking down `tag`/`scan` entirely
        # since this loader runs unconditionally at the start of both,
        # over one malformed entry in a file explicitly meant to be
        # hand-edited. Skip just this entry instead, the same "recover
        # what's valid, don't fail the whole file" convention
        # renamer.py's own malformed-sidecar handling already follows.
        if not isinstance(meta, dict):
            logger.warning(
                "manual_overrides.yaml entry for %r has no fields under it; skipping it", filename
            )
            continue
        # `.get(key, default)` only falls back when the key is *missing* --
        # an entry with a present-but-blank key (e.g. "source:" with
        # nothing after it, a plausible mistake since most other fields in
        # the documented template *are* meant to be left blank) parses to
        # an explicit YAML null, which .get() happily returns as-is. `or`
        # catches that too.
        source_value = meta.get("source") or Source.MANUAL.value
        try:
            source = Source(source_value)
        except ValueError:
            logger.warning(
                "manual_overrides.yaml entry for %r has an invalid source %r; treating it as manual",
                filename, source_value,
            )
            source = Source.MANUAL
        product_url = _text(meta.get("product_url"))
        # A manual override previously had no way to carry a product_id at
        # all -- an explicit `product_id:` key wins if given; otherwise
        # derive one from `product_url` the same way a pasted URL/id: is
        # already parsed everywhere else in this project (extract_product_id(),
        # reused rather than reimplemented). Without this, a manually
        # overridden book's dc:identifier is always empty, even when the
        # override entry links straight to the real DriveThruRPG listing.
        product_id = _text(meta.get("product_id"))
        if not product_id and product_url:
            product_id = extract_product_id(product_url) or ""
        overrides[filename] = ProductMetadata(
            title=_text(meta.get("title")),
            series=_text(meta.get("series")),
            series_index=_text(meta.get("series_index")),
            publisher=_text(meta.get("publisher")),
            authors=_text_list(meta.get("authors")),
            tags=_text_list(meta.get("tags")),
            description=_text(meta.get("description")),
            product_url=product_url,
            source=source,
            product_id=product_id,
            isbn=_text(meta.get("isbn")),
        )
    return overrides


def find_candidates(
    path: Path, client: DtrpgClient, limit: int = 5
) -> list[tuple[ProductMetadata, float]]:
    """Search both the library and the catalog for a single file and return
    up to `limit` scored candidates, best first. Always queries both
    sources, since the user picks from the list."""
    filename = path.name
    query, noise_tokens = derive_query(filename)

    candidates: list[ProductMetadata] = []
    try:
        candidates.extend(client.search_library(query))
    except Exception:
        logger.exception("Library search failed for %r", filename)
    try:
        candidates.extend(client.search_catalog(query))
    except Exception:
        logger.exception("Catalog search failed for %r", filename)

    # The same product often comes back from both the library and the
    # catalog; without merging, duplicates took up places in the top
    # `limit` and crowded distinct books out. One entry per product ID,
    # keeping the copy with more metadata (usually the catalog's) and the
    # better of the two scores.
    best: dict[str, tuple[ProductMetadata, float]] = {}
    unidentified = []
    for c in candidates:
        score = _score(query, noise_tokens, c)
        if not c.product_id:
            unidentified.append((c, score))
            continue
        kept = best.get(c.product_id)
        if kept is None:
            best[c.product_id] = (c, score)
        else:
            meta = c if _richness(c) > _richness(kept[0]) else kept[0]
            best[c.product_id] = (meta, max(score, kept[1]))
    scored = list(best.values()) + unidentified
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def _richness(meta: ProductMetadata) -> int:
    """How many of the optional fields a match has filled in."""
    return sum(bool(v) for v in (meta.description, meta.tags, meta.authors, meta.isbn, meta.publisher, meta.series))


def row_from_match(filename: str, meta: ProductMetadata, score: float, status: Status) -> ReviewRow:
    return ReviewRow(
        filename=filename,
        matched_title=meta.title,
        series=meta.series,
        series_index=meta.series_index,
        publisher=meta.publisher,
        confidence_score=f"{score:.1f}",
        source=meta.source.value if isinstance(meta.source, Source) else meta.source,
        status=status.value,
        authors=meta.authors_str(),
        tags=meta.tags_str(),
        description=meta.description,
        product_url=meta.product_url,
        product_id=meta.product_id,
        isbn=meta.isbn,
    )
