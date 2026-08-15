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
        ocr | scan | remaster(?:ed)? | v\d+(?:\.\d+)? |
        \d+(?:st|nd|rd|th)\s*edition | \d+e | revised | reprint |
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
    return sorted(p for p in root.rglob("*.pdf") if p.is_file())


def derive_query(filename: str) -> tuple[str, list[str]]:
    """Turn a filename into a search query, plus a list of stripped
    noise/edition tokens that can still inform scoring."""
    stem = Path(filename).stem
    stem = stem.replace("_", " ").replace(".", " ").replace("-", " ")

    noise_tokens = [m.group(1).strip() for m in NOISE_TOKEN_PATTERN.finditer(stem)]
    noise_tokens += [m.group(0) for m in SKU_PATTERN.finditer(stem)]
    stem = NOISE_TOKEN_PATTERN.sub(" ", stem)
    stem = SKU_PATTERN.sub(" ", stem)
    stem = BRACKETED_PATTERN.sub(" ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem, noise_tokens


def _score(query: str, noise_tokens: list[str], candidate: ProductMetadata) -> float:
    base = fuzz.token_sort_ratio(query, candidate.title)
    haystack = f"{candidate.title} {candidate.description}".lower()
    bonus = sum(3 for token in noise_tokens if token.lower() in haystack)
    return min(100.0, base + bonus)


def load_manual_overrides(path: str | Path) -> dict[str, ProductMetadata]:
    path = Path(path)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    overrides: dict[str, ProductMetadata] = {}
    for filename, meta in raw.items():
        overrides[filename] = ProductMetadata(
            title=meta.get("title", ""),
            series=meta.get("series", ""),
            series_index=str(meta.get("series_index", "")),
            publisher=meta.get("publisher", ""),
            authors=list(meta.get("authors") or []),
            tags=list(meta.get("tags") or []),
            description=meta.get("description", ""),
            product_url=meta.get("product_url", ""),
            source=Source(meta.get("source", Source.MANUAL.value)),
            isbn=meta.get("isbn", ""),
        )
    return overrides


def match_file(
    path: Path,
    client: DtrpgClient,
    manual_overrides: dict[str, ProductMetadata],
    known_urls: dict[str, str] | None = None,
    high_confidence: float = 90.0,
    review_floor: float = 70.0,
) -> ReviewRow:
    filename = path.name

    if filename in manual_overrides:
        meta = manual_overrides[filename]
        return row_from_match(filename, meta, 100.0, Status.APPROVED)

    if known_urls and filename in known_urls:
        product_id = known_urls[filename]
        meta = client.get_product(product_id)
        if meta is not None:
            return row_from_match(filename, meta, 100.0, Status.APPROVED)
        logger.warning(
            "Known URL for %r (product_id=%s) could not be fetched; falling back to search",
            filename, product_id,
        )

    query, noise_tokens = derive_query(filename)

    try:
        library_candidates = client.search_library(query)
    except Exception:
        logger.exception("Library search failed for %r", filename)
        library_candidates = []

    best = _best_candidate(query, noise_tokens, library_candidates)
    if best is not None and best[1] >= high_confidence:
        meta = _maybe_enrich(client, best[0])
        return row_from_match(filename, meta, best[1], Status.AUTO_ACCEPTED)

    try:
        catalog_candidates = client.search_catalog(query)
    except Exception:
        logger.exception("Catalog search failed for %r", filename)
        catalog_candidates = []

    catalog_best = _best_candidate(query, noise_tokens, catalog_candidates)
    if catalog_best is not None and (best is None or catalog_best[1] > best[1]):
        best = catalog_best

    if best is None:
        return ReviewRow(filename=filename, status=Status.NO_MATCH.value)

    meta, score = best
    if score >= high_confidence:
        meta = _maybe_enrich(client, meta)
        return row_from_match(filename, meta, score, Status.AUTO_ACCEPTED)
    if score >= review_floor:
        meta = _maybe_enrich(client, meta)
        return row_from_match(filename, meta, score, Status.NEEDS_REVIEW)
    return ReviewRow(filename=filename, status=Status.NO_MATCH.value)


def _maybe_enrich(client: DtrpgClient, meta: ProductMetadata) -> ProductMetadata:
    """Backfill description/authors/tags for a library-sourced match (see
    DtrpgClient.enrich) before it's kept — a no-op for catalog matches,
    which already carry this data, and cheap to call redundantly."""
    if meta.source != Source.DTRPG_LIBRARY or meta.description:
        return meta
    try:
        return client.enrich(meta)
    except Exception:
        logger.exception("Enrichment failed for %r (product_id=%s)", meta.title, meta.product_id)
        return meta


def find_candidates(
    path: Path, client: DtrpgClient, limit: int = 5
) -> list[tuple[ProductMetadata, float]]:
    """Search both the library and the catalog for a single file and return
    up to `limit` scored candidates, best first. Unlike `match_file`, this
    always queries both sources (no early-exit on a high-confidence library
    hit) since it's meant for one-off interactive lookups, not a bulk scan
    that needs to conserve rate-limited catalog calls."""
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

    scored = [(c, _score(query, noise_tokens, c)) for c in candidates]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def _best_candidate(
    query: str, noise_tokens: list[str], candidates: list[ProductMetadata]
) -> tuple[ProductMetadata, float] | None:
    if not candidates:
        return None
    scored = [(c, _score(query, noise_tokens, c)) for c in candidates]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[0]


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
