"""DriveThruRPG API client.

This wraps DriveThruRPG's undocumented REST API. There is no official
public documentation for it. The base URL, auth flow, and the
purchased-library listing endpoint (``order_products``) were
reverse-engineered from glujan/drpg (https://github.com/glujan/drpg).

The catalog search (``products``) and product detail (``products/{id}``)
endpoints were initially modeled on quickwick/drivethrurpg-calibre-plugin
(https://github.com/quickwick/drivethrurpg-calibre-plugin), which expects
a JSON:API-style envelope (``{"data": {"attributes": ...}, "included": [...]}``).
That turned out to be wrong against a real, authenticated account: the
live ``products/{id}`` response is a **flat** object (verified from an
actual raw response dump in ``data/debug/`` after a parse failure —
either DriveThruRPG's API changed since that plugin was last updated, or
an authenticated request gets a different shape than the plugin's
unauthenticated browser fetch). ``_parse_product_detail`` below is
written against that confirmed live shape, not the plugin's assumption.
Relevant top-level fields: ``authors`` (list), ``publisher.name``,
``description.name`` (title) / ``description.description`` (HTML body),
``categories[].descriptions[]`` / ``filters[].descriptions[]`` (per-
language tag names, mixed with non-genre noise like "PDF"/"English").

Notably, DriveThruRPG does not appear to expose structured "series" /
"series index" fields anywhere in this API — series info typically lives
inside the product title itself (e.g. "GURPS Dungeon Fantasy 1: ..."),
so ``ProductMetadata.series`` is left blank here and is expected to be
filled in during matching/review, not by this client.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from provenance import ProductMetadata, Source

logger = logging.getLogger("dtrpg_client")

API_BASE = "https://api.drivethrurpg.com/api/vBeta"

# category/filter names that describe format, language, or site policy
# rather than the book's actual subject matter — not useful as tags.
TAG_BLOCKLIST = {"PDF", "English", "Digital", "Creation Method", "Human-Created Without AI"}

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(raw: str) -> str:
    text = _HTML_TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _english_tag_names(entries: list[dict[str, Any]]) -> list[str]:
    names = []
    for entry in entries:
        for desc in entry.get("descriptions", []):
            if desc.get("languageId") == 1:
                name = desc.get("name")
                if name:
                    names.append(name)
                break
    return names


class DtrpgApiError(Exception):
    """Raised when the DriveThruRPG API returns something we can't use."""


@dataclass
class _RateLimiter:
    min_interval_seconds: float
    _last_call: float = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_call = time.monotonic()


class DtrpgClient:
    """Read-only client for DriveThruRPG's purchased library and public catalog.

    No purchasing or account-modifying calls are made — search only.
    """

    def __init__(
        self,
        api_key: str,
        cache_dir: str | Path = "data",
        catalog_rate_limit_seconds: float = 1.0,
        session: requests.Session | None = None,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.debug_dir = self.cache_dir / "debug"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "dtrpg-metadata-download/0.1 (personal library tagging tool)",
            }
        )
        self.max_retries = max_retries
        self._catalog_rate_limiter = _RateLimiter(catalog_rate_limit_seconds)
        self._token: str | None = None

        self._library_cache_path = self.cache_dir / "library_cache.json"
        self._catalog_cache_path = self.cache_dir / "catalog_cache.json"
        self._catalog_cache: dict[str, Any] = self._load_json(self._catalog_cache_path, default={})

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _authenticate(self) -> None:
        if self._token:
            return
        resp = self.session.post(
            f"{API_BASE}/auth_key",
            params={"applicationKey": self.api_key},
        )
        if resp.status_code == 401:
            raise DtrpgApiError(
                "DriveThruRPG rejected the application key (401). Generate a new "
                "Application Key from the DriveThruRPG account page — this is not "
                "your account password."
            )
        resp.raise_for_status()
        data = self._parse_json(resp, context="auth_key")
        token = data.get("token")
        if not token:
            self._dump_debug("auth_key", resp)
            raise DtrpgApiError("auth_key response did not contain a 'token' field")
        self._token = token
        self.session.headers["Authorization"] = token

    # ------------------------------------------------------------------
    # Purchased library
    # ------------------------------------------------------------------

    def pull_library(self, refresh: bool = False, page_size: int = 50) -> list[dict[str, Any]]:
        """Fetch (or load from cache) the full purchased-library listing.

        Each entry is the raw ``order_products`` record (productId, name,
        publisher, files, etc.) — not yet normalized to ProductMetadata,
        since library entries carry title/publisher only, not authors/tags/
        description. Call ``_enrich_with_product_detail`` for that.
        """
        if not refresh:
            cached = self._load_json(self._library_cache_path, default=None)
            if cached is not None:
                logger.debug("Loaded %d library entries from cache", len(cached))
                return cached

        self._authenticate()
        entries: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self.session.get(
                f"{API_BASE}/order_products",
                params={
                    "getChecksum": 0,
                    "getFilters": 0,
                    "page": page,
                    "pageSize": page_size,
                    "library": 1,
                    "archived": 0,
                },
            )
            resp.raise_for_status()
            page_items = self._parse_json(resp, context=f"order_products page {page}")
            if not isinstance(page_items, list):
                self._dump_debug(f"order_products_page_{page}", resp)
                raise DtrpgApiError(
                    f"Expected a list from order_products page {page}, got {type(page_items)}"
                )
            if not page_items:
                break
            entries.extend(page_items)
            logger.debug("Pulled library page %d (%d items)", page, len(page_items))
            page += 1

        self._save_json(self._library_cache_path, entries)
        logger.info("Pulled %d items from DriveThruRPG library", len(entries))
        return entries

    def search_library(self, query: str, limit: int = 5) -> list[ProductMetadata]:
        """Fuzzy-search the cached (or freshly pulled) library listing by title.

        This does a local match against ``pull_library()`` results rather
        than hitting a server-side search endpoint — the library listing
        endpoint doesn't appear to support a name/query filter, so we pull
        it once (cached) and match locally.
        """
        from rapidfuzz import fuzz, process

        entries = self.pull_library(refresh=False)
        names = [entry.get("name", "") for entry in entries]
        matches = process.extract(query, names, scorer=fuzz.token_sort_ratio, limit=limit)

        results: list[ProductMetadata] = []
        for _name, _score, idx in matches:
            entry = entries[idx]
            product_id = str(entry.get("productId", ""))
            results.append(
                ProductMetadata(
                    title=entry.get("name", ""),
                    publisher=(entry.get("publisher") or {}).get("name", ""),
                    product_url=f"https://www.drivethrurpg.com/product/{product_id}",
                    source=Source.DTRPG_LIBRARY,
                    product_id=product_id,
                )
            )
        return results

    def enrich(self, meta: ProductMetadata) -> ProductMetadata:
        """Fill in authors/tags/description/isbn for a library-sourced match.

        ``search_library`` only has title + publisher available (that's
        all the ``order_products`` listing carries) even though the same
        product's detail page — one ``products/{id}`` call away — has the
        richer data. Call this once on whichever candidate you're actually
        about to use, not on every candidate returned by ``search_library``,
        to avoid burning the rate limit on options that don't get picked.
        """
        if not meta.product_id or meta.description:
            return meta
        detail = self._fetch_product_detail(meta.product_id)
        if detail is None:
            return meta
        meta.authors = meta.authors or detail.authors
        meta.tags = meta.tags or detail.tags
        meta.description = detail.description
        meta.isbn = meta.isbn or detail.isbn
        if not meta.publisher:
            meta.publisher = detail.publisher
        return meta

    def get_product(self, product_id: int | str) -> ProductMetadata | None:
        """Fetch a single product directly by ID, bypassing search entirely.

        For when the right listing is known (e.g. from its DriveThruRPG
        product URL) but isn't reliably discoverable through catalog
        search — their ``name`` filter's relevance ranking can bury or
        outright miss products whose title doesn't literally contain the
        query words (see the "Traveller Main Rulebook" case: findable by
        ID, not by any reasonable title-based search).
        """
        return self._fetch_product_detail(product_id)

    # ------------------------------------------------------------------
    # Public catalog
    # ------------------------------------------------------------------

    def search_catalog(self, query: str, limit: int = 6, use_cache: bool = True) -> list[ProductMetadata]:
        """Search DriveThruRPG's full public catalog by title text.

        The ``name`` filter on this endpoint isn't a fuzzy/ranked search —
        it behaves like every query word must be literally present in the
        product's actual title. That cuts both ways: a word that isn't
        part of the real title (often the publisher/brand — DriveThruRPG
        product titles don't reliably include it) can make the query
        return nothing, *or* it can silently return the wrong product
        entirely (real example: "Mongoose Traveller Core Rulebook"
        returns exactly one hit, and it's a ships supplement, not the
        core rulebook — the actual core rulebook's title doesn't contain
        "Mongoose"). A result count alone can't tell those apart, so we
        can't just retry-on-empty. Instead we always also query with the
        leading word dropped and union both result sets (deduped, capped
        at `limit`), and let the caller's local fuzzy scoring — which
        *can* tell a near-exact title match from a loosely-related one —
        pick the winner from the wider pool.

        Results are cached to disk per the *original* query so repeated
        runs (e.g. `--apply-review` iterations) don't re-hit the API.
        """
        cache_key = query.strip().lower()
        if use_cache and cache_key in self._catalog_cache:
            return [ProductMetadata(**item) for item in self._catalog_cache[cache_key]]

        product_ids = self._catalog_search_ids(query, limit)

        parts = query.split(maxsplit=1)
        if len(parts) == 2:
            fallback_query = parts[1]
            fallback_ids = self._catalog_search_ids(fallback_query, limit)
            seen = set(product_ids)
            for pid in fallback_ids:
                if pid not in seen:
                    seen.add(pid)
                    product_ids.append(pid)
            product_ids = product_ids[:limit]

        results = [self._fetch_product_detail(pid) for pid in product_ids]
        results = [r for r in results if r is not None]

        if use_cache:
            self._catalog_cache[cache_key] = [
                {
                    "title": r.title,
                    "series": r.series,
                    "series_index": r.series_index,
                    "publisher": r.publisher,
                    "authors": r.authors,
                    "tags": r.tags,
                    "description": r.description,
                    "product_url": r.product_url,
                    "source": r.source.value if isinstance(r.source, Source) else r.source,
                    "product_id": r.product_id,
                    "isbn": r.isbn,
                }
                for r in results
            ]
            self._save_json(self._catalog_cache_path, self._catalog_cache)

        return results

    def _catalog_search_ids(self, query: str, limit: int) -> list[int | str]:
        """One rate-limited catalog search request; returns product IDs."""
        self._catalog_rate_limiter.wait()
        resp = self.session.get(
            f"{API_BASE}/products",
            params={
                "page": 1,
                "pageSize": limit,
                "groupId": 1,
                "name": query,
                "order[matchWeight]": "desc",
                "siteId": 10,
                "contentRating[lte]": 1,
                "status": 1,
                "partial": "false",
            },
        )
        resp.raise_for_status()
        data = self._parse_json(resp, context=f"products search '{query}'")
        product_ids: list[int | str] = []
        try:
            # Observed live behavior is inconsistent with the JSON:API
            # envelope ({"data": [...]}) the older calibre plugin assumed —
            # sometimes the response is a bare list of product dicts
            # instead (see the products/{id} flat-shape discovery above;
            # same pattern seems to apply here). Handle both.
            items = data if isinstance(data, list) else data.get("data", [])
            for product in items:
                pid = product["productId"] if "productId" in product else product["attributes"]["productId"]
                product_ids.append(pid)
        except (KeyError, TypeError, AttributeError):
            self._dump_debug(f"products_search_{query.strip().lower()}", resp)
            raise DtrpgApiError(f"Unexpected shape from catalog search for '{query}'")
        return product_ids

    def _fetch_product_detail(self, product_id: int | str) -> ProductMetadata | None:
        self._catalog_rate_limiter.wait()
        resp = self.session.get(f"{API_BASE}/products/{product_id}")
        if not resp.ok:
            logger.warning("Product detail lookup failed for id=%s (HTTP %d)", product_id, resp.status_code)
            self._dump_debug(f"product_detail_{product_id}", resp)
            return None
        data = self._parse_json(resp, context=f"product detail {product_id}")
        try:
            return self._parse_product_detail(data, product_id)
        except (KeyError, TypeError) as exc:
            logger.warning("Failed to parse product detail for id=%s: %s", product_id, exc)
            self._dump_debug(f"product_detail_{product_id}", resp)
            return None

    @staticmethod
    def _parse_product_detail(data: dict[str, Any], product_id: int | str) -> ProductMetadata:
        description_block = data["description"]
        title = description_block["name"]
        description = _clean_html(description_block.get("description") or "")

        authors = list(data.get("authors") or [])
        publisher = (data.get("publisher") or {}).get("name", "")

        tags = _english_tag_names(data.get("categories", [])) + _english_tag_names(data.get("filters", []))
        seen: set[str] = set()
        clean_tags: list[str] = []
        for tag in tags:
            if tag in TAG_BLOCKLIST or tag == publisher or tag in seen:
                continue
            seen.add(tag)
            clean_tags.append(tag)

        return ProductMetadata(
            title=title.replace(">", "").strip(),
            publisher=publisher,
            authors=authors,
            tags=clean_tags,
            description=description,
            product_url=f"https://www.drivethrurpg.com/product/{product_id}",
            source=Source.DTRPG_CATALOG,
            product_id=str(product_id),
            isbn=(data.get("isbn") or "").strip(),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_json(self, resp: requests.Response, context: str) -> Any:
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            self._dump_debug(context.replace(" ", "_"), resp)
            raise DtrpgApiError(f"Failed to parse JSON response for {context}; raw body dumped to debug log")

    def _dump_debug(self, label: str, resp: requests.Response) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        path = self.debug_dir / f"{int(time.time())}_{label}.txt"
        try:
            path.write_text(
                f"URL: {resp.url}\nStatus: {resp.status_code}\n\n{resp.text}",
                encoding="utf-8",
            )
            logger.error("Dumped raw response to %s for debugging", path)
        except OSError:
            logger.exception("Failed to write debug dump for %s", label)

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read cache file %s, ignoring", path)
            return default

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
