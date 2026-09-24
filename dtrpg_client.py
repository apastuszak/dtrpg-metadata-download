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
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from provenance import ProductMetadata, Source

logger = logging.getLogger("dtrpg_client")

API_BASE = "https://api.drivethrurpg.com/api/vBeta"

# No request here ever had a timeout, so a stalled connection (a dead
# network, a hung proxy) previously hung the whole run forever -- the
# TUI/GUI just sat there indistinguishable from a genuine freeze, with no
# way out short of killing the process. 30s is generous for a single
# request/response, not a total-run budget.
DEFAULT_TIMEOUT_SECONDS = 30

# category/filter names that describe format, language, or site policy
# rather than the book's actual subject matter — not useful as tags.
TAG_BLOCKLIST = {"PDF", "English", "Digital", "Creation Method", "Human-Created Without AI"}

# How close a catalog-search title has to be (rapidfuzz token_sort_ratio,
# 0-100) before DtrpgClient._enrich_fallback_by_title() will trust it as
# the same product as a stale library entry -- see that method's own
# docstring for why this fallback needs a real bar, not "closest of
# whatever came back". An exact (case/whitespace-insensitive) title match
# is handled separately, above this fallback -- that already covers the
# actual documented real-world case (a re-listing under a new ID with the
# *same* title). This threshold is only for minor title variants of that
# same case (e.g. a "(2nd Printing)"-style suffix DriveThruRPG re-listings
# commonly add) -- verified empirically that matcher.py's own
# high_confidence_threshold (90.0) is too strict for that real pattern
# (scored ~79), while a clearly unrelated title scores far lower (~36) --
# so this uses matcher.py's review_floor_threshold instead, the next tier
# down: still a real bar (nowhere near unrelated-title scores), but one
# that doesn't reject the actual case this fallback exists for.
_FALLBACK_TITLE_MATCH_THRESHOLD = 70.0

# Read once, here at import time, rather than via the read-with-a-
# sentinel trick (os.umask(0) then os.umask(saved)) inside _save_json()
# itself -- see review.py's matching _DEFAULT_UMASK for why that trick
# isn't safe to do at save-time: _save_json() runs on background
# QThreads (pull_library()/search_catalog(), called from the GUI's
# worker threads), and briefly zeroing the process-wide umask there
# could let another thread's file come out world-writable.
_DEFAULT_UMASK = os.umask(0)
os.umask(_DEFAULT_UMASK)

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
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
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
                "User-Agent": "dtrpg-metadata-download/1.3.0 (personal library tagging tool)",
            }
        )
        self.timeout = timeout
        # max_retries was previously stored and never used -- every
        # request below now actually goes through this. A transient
        # network blip or a 5xx/429 used to end the whole run immediately
        # (an uncaught requests exception, or an HTTPError on a retryable
        # status); now it's retried with backoff before giving up.
        # allowed_methods includes POST for auth_key specifically because
        # it's a read-only token fetch here, not something with a side
        # effect that would make retrying unsafe.
        retry = Retry(
            total=max_retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            # raise_on_status=False -- once retries are exhausted, hand
            # back the last (failed) response instead of raising
            # urllib3's own RetryError. A real regression this fixed:
            # every existing caller here was written against "get a
            # Response back, then decide what to do" (resp.status_code
            # == 401, resp.ok, resp.raise_for_status()) -- verified
            # against a real server that always 503s that get_product()
            # used to raise RetryError uncaught instead of returning None,
            # which crashed a whole scan/tag run over one bad lookup
            # instead of degrading the way every one of those call sites
            # already expected.
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
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
        # applicationKey has to go in the query string -- that's how this
        # (undocumented) endpoint is reverse-engineered to work -- but that
        # means resp.url now contains the raw key. Every error path below
        # is deliberately kept from ever surfacing that URL (dump or
        # exception message); see _dump_debug's redaction for the same
        # reasoning applied generically.
        resp = self.session.post(
            f"{API_BASE}/auth_key",
            params={"applicationKey": self.api_key},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            raise DtrpgApiError(
                "DriveThruRPG rejected the application key (401). Generate a new "
                "Application Key from the DriveThruRPG account page — this is not "
                "your account password."
            )
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            # Not `raise ... from exc` -- the original HTTPError's message
            # embeds resp.url, which contains the API key; chaining would
            # keep that reachable via the traceback's "direct cause" text.
            raise DtrpgApiError(f"auth_key request failed (HTTP {resp.status_code})") from None
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
                timeout=self.timeout,
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

        The ``order_products`` listing's ``productId`` can go stale — a
        purchased title can get re-listed under a new ID after purchase
        (e.g. following a content update), orphaning the one your library
        entry still points at. DriveThruRPG returns a 403 for the old ID
        in that case rather than data (confirmed against a real purchase:
        "Delta Green: Handler's Guide" 403'd on its library-linked ID,
        while the current listing at a different ID fetched cleanly). If
        the direct fetch 403s (or fails for any reason), fall back to a
        catalog search by title to find the current listing.
        """
        if not meta.product_id or meta.description:
            return meta
        detail = self._fetch_product_detail(meta.product_id)
        if detail is None:
            detail = self._enrich_fallback_by_title(meta)
        if detail is None:
            return meta
        meta.authors = meta.authors or detail.authors
        meta.tags = meta.tags or detail.tags
        meta.description = detail.description
        meta.isbn = meta.isbn or detail.isbn
        if not meta.publisher:
            meta.publisher = detail.publisher
        # Keep in sync with whichever listing the data actually came from —
        # matters most for the fallback path, where this corrects a stale
        # product_id that would otherwise get written into the file as an
        # unusable dc:identifier reference.
        meta.product_id = detail.product_id
        return meta

    def _enrich_fallback_by_title(self, meta: ProductMetadata) -> ProductMetadata | None:
        """Only accept a fuzzy title match above _FALLBACK_TITLE_MATCH_THRESHOLD
        -- this fallback runs fully automatically, deep inside enrich(),
        with no human ever reviewing its result (unlike matcher.py's own
        candidate scoring, which either auto-accepts above a high bar or
        surfaces lower-confidence matches for review). Blindly taking
        candidates[0] used to risk merging a *different* product's
        description/ISBN into a purchased library entry, and overwriting
        its product_id with the wrong listing's -- corrupting dc:identifier
        for a book that was never actually mismatched in the first place,
        just unlucky enough to need this fallback at all.
        """
        logger.info("product_id=%s for %r failed; retrying via catalog search by title", meta.product_id, meta.title)
        try:
            candidates = self.search_catalog(meta.title)
        except Exception:
            logger.exception("Catalog fallback search failed for %r", meta.title)
            return None
        if not candidates:
            return None
        for candidate in candidates:
            if candidate.title.strip().lower() == meta.title.strip().lower():
                return candidate

        from rapidfuzz import fuzz

        best = max(candidates, key=lambda c: fuzz.token_sort_ratio(meta.title, c.title))
        score = fuzz.token_sort_ratio(meta.title, best.title)
        if score < _FALLBACK_TITLE_MATCH_THRESHOLD:
            logger.warning(
                "Catalog fallback for %r found no confidently-matching title (closest was %r, score %.1f); "
                "leaving it unenriched rather than risk attaching the wrong product's data",
                meta.title, best.title, score,
            )
            return None
        return best

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
            timeout=self.timeout,
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
        try:
            resp = self.session.get(f"{API_BASE}/products/{product_id}", timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            # get_product()/enrich() call this directly with no try/except
            # of their own -- both are written against this function's
            # "return None on failure, never raise" contract (the same
            # contract resp.ok's own check below already honors), so a
            # connection error/timeout that survives retrying must degrade
            # the same way a bad HTTP status already does, not propagate
            # up and abort whatever scan/tag run was in progress.
            logger.warning("Product detail request failed for id=%s: %s", product_id, exc)
            return None
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
        # auth_key is the only endpoint that puts a secret in the URL (see
        # _authenticate), but redact generically here rather than only at
        # that one call site -- this is the one place these dumps get
        # written to disk, so it's the one place that has to hold no
        # matter which caller reaches it.
        # The empty-string guard matters: an empty api_key is "in" every
        # string, so without it .replace("", ...) would mangle the URL by
        # inserting the redaction marker between every character. Not
        # reachable via the CLI today (build_client() already refuses an
        # empty key before constructing this client), but cheap to guard.
        url = resp.url.replace(self.api_key, "***REDACTED***") if self.api_key and self.api_key in resp.url else resp.url
        try:
            path.write_text(
                f"URL: {url}\nStatus: {resp.status_code}\n\n{resp.text}",
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
        # Same atomic temp-file-then-os.replace() reasoning as
        # review.save_review() -- a crash/kill partway through a direct
        # write here would leave a truncated, unparseable cache file.
        # _load_json() above already tolerates that (falls back to
        # "no cache", not a crash), but that means silently re-paying for
        # a full library re-pull or burning rate-limited catalog calls
        # again, for no reason beyond bad timing on a previous run.
        tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".json", prefix=f".{path.name}.tmp-", dir=str(path.parent))
        try:
            # mkstemp() always creates its file 0600, and os.replace()
            # preserves that mode on POSIX -- without this, every save
            # silently tightened the cache file's permissions from its
            # normal 644 down to 600. See review.save_review()'s matching
            # fix for the same regression.
            if path.exists():
                mode = stat.S_IMODE(path.stat().st_mode)
            else:
                mode = 0o666 & ~_DEFAULT_UMASK
            os.chmod(tmp_path_str, mode)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path_str, path)
        except BaseException:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise
