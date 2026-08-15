# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A pipeline that matches local RPG PDF filenames against DriveThruRPG metadata (purchased library, then public catalog, then manual overrides) and writes the result into each PDF's embedded XMP metadata, so [BookOrbit](https://bookorbit.app) picks it up on its own library scan. There is no database and no long-running service — it's a CLI run manually against a folder of PDFs. BookOrbit is the target reader; Kavita was used earlier in this project's history but is no longer in use — don't reintroduce Kavita/Calibre-specific field mapping without checking first (`kavita_client.py` was deleted as dead code once the switch was made).

## Setup and running

No `pyproject.toml`/`setup.py` — this is a flat script directory, not a packaged/installable project. `dtrpg-metadata-download.py` carries its own dependency list as [PEP 723](https://peps.python.org/pep-0723/) inline script metadata (the `# /// script` block at the top) and is executable directly — no manual venv:

```bash
export DTRPG_API_KEY=...   # Application Key from the DriveThruRPG account page, NOT the account password
./dtrpg-metadata-download.py <subcommand>
```

`uv` resolves and caches the dependencies (pikepdf/rapidfuzz/PyYAML/requests) transparently on first run via the shebang (`#!/usr/bin/env -S uv run --script`); every run after that is instant, no `.venv` involved. `requirements.txt` + a classic `python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt` still works too, for anyone without `uv` — keep both in sync if dependencies change. Subcommands:

- `tag PDF_PATH` / `tag --root DIR` — interactive, no review.csv: shows ranked candidates for one file (or loops over every PDF under a directory, `q` to stop early), writes the picked match immediately on confirm. This is the primary/actively-used path. At the candidate prompt you can also paste a DriveThruRPG product URL or type `id:PRODUCT_ID` to fetch that exact listing directly, bypassing search (see `DtrpgClient.get_product()` / `matcher.extract_product_id()`) — needed because catalog search can miss or misrank the right book (see below).
- `scan --root DIR [--refresh-library] [--apply-review]` → `write-pdfs --root DIR` — batch path gated by `data/review.csv`: `scan` matches everything and writes rows to the CSV (never auto-writes PDFs), you hand-edit `status` to `approved`, then `write-pdfs` applies only approved/auto-accepted rows. `all` runs both in sequence.
- `review-status` — prints CSV status counts.

`config.yaml` supplies defaults (root, thresholds, cache paths); CLI flags override it. Copy/edit it per-machine — it's meant to stay free of secrets (the API key only ever comes from `DTRPG_API_KEY`).

**Match precedence** (checked in this order, in both `tag` and `scan`, via `matcher.match_file()`/`_tag_one()`): `data/manual_overrides.yaml` (full hand-typed metadata, for titles not on DriveThruRPG at all) → `<root>/dtrpg_urls.csv` (just `filename,drivethrurpg_url` — lives alongside the PDFs it describes, not in `data/`; full metadata is fetched live via `DtrpgClient.get_product()`; auto-approved with no confirmation prompt even in `tag`, falls back to search if the fetch fails) → purchased library → public catalog.

## No test suite

There's no pytest/unittest scaffolding and no lint config in this repo. Verification during development has been done with ad hoc inline Python scripts: build a throwaway PDF with `pikepdf.new()`, run it through `matcher`/`pdf_writer` with a `unittest.mock.MagicMock()` standing in for `DtrpgClient` (to avoid live API calls), then reopen the PDF with `pikepdf.open(...).open_metadata()` and assert on the actual XMP fields written — not just that the code ran without raising. When the real API's response shape needs checking, `data/debug/*.txt` dumps (raw response bodies, written automatically on any parse failure — see below) are the source of truth; prefer replaying a real captured dump over guessing at shape from third-party code.

## Architecture

**Module chain:** `dtrpg_client.py` (network + caching) → `matcher.py` (filename→query, fuzzy scoring, decides `ReviewRow`/candidate list) → `review.py` (CSV I/O, only used by the batch path) → `pdf_writer.py` (writes XMP via pikepdf) → `dtrpg-metadata-download.py` (CLI, wires it all together). `provenance.py` holds the two shared types (`ProductMetadata`, and the `Source`/`Status` enums) that every other module imports.

**DriveThruRPG API is undocumented — treat field-path assumptions as provisional.** `dtrpg_client.py`'s docstring records what's actually confirmed vs. inherited from third-party reverse-engineering:
- `order_products` (purchased library, via `search_library`) and the `auth_key`/token auth flow were reverse-engineered from `glujan/drpg`'s source and are trusted.
- `products/{id}` (product detail — authors, publisher, description, tags) was *initially* modeled on `quickwick/drivethrurpg-calibre-plugin`, which assumes a JSON:API envelope (`{"data": {"attributes": ...}}`). That assumption was wrong against a real authenticated account — the live response is a **flat** object. `_parse_product_detail` is now written against the confirmed flat shape (verified from an actual `data/debug/` dump), not the plugin's code. If matching breaks again with a `KeyError` during product-detail parsing, check `data/debug/` for the raw dump before changing the parser blind.
- There's no structured "series"/series-index field anywhere in the API; series info lives inside the title text itself, so `ProductMetadata.series` is expected to come from matching/manual overrides, not the client.

**Library matches are cheap but thin; catalog matches are rich but rate-limited.** `search_library()` only has title+publisher (that's all `order_products` carries) and does its matching *locally* against a cached full-library pull — no server-side query. `search_catalog()` hits the real search endpoint and enriches every result via a `products/{id}` call immediately (already rate-limited, only ~6 results). Because of this asymmetry, a library-sourced match that's missing a description (or isbn/authors/tags) doesn't mean DriveThruRPG has none — it means nobody's called `DtrpgClient.enrich()` on it yet. `enrich()` is deliberately *not* called on every candidate `search_library()`/`find_candidates()` returns (that would burn the rate limit on options nobody picks); it's called once, lazily, on whichever candidate is actually kept — see the call sites in `matcher.match_file()`/`_maybe_enrich()` and `dtrpg-metadata-download.py`'s `_tag_one()`.

**XMP field mapping targets BookOrbit's actual schema, verified against its real source — not guessed.** `pdf_writer.py`'s docstring cites the exact files read in `github.com/bookorbit/bookorbit` (reader: `pdf-xmp-reader.ts`/`pdf-parser.ts`; writer: `pdf-xmp-builder.ts`; namespace: `bookorbit-ns.ts`). BookOrbit's schema is its own — not Calibre's — and was confirmed end-to-end by actually running BookOrbit's real TypeScript reader (via `tsx`, with `pdf-lib`/`fast-xml-parser` installed) against a PDF this project wrote, not just by reading their source and assuming:
- `dc:creator` (Authors) and `dc:publisher` both get the **publisher** name — not the actual author list DriveThruRPG returns. This is a deliberate policy carried over from the Kavita era (DriveThruRPG's per-book author credits are inconsistent for this library) and was explicitly re-confirmed to keep, not revisit, when the target app changed to BookOrbit despite BookOrbit having a separate Authors field.
- `dc:description` maps directly to BookOrbit's Description field (no "Comments"-style naming confusion like the old Calibre/`ebook-meta` mapping had).
- `dc:subject` gets DriveThruRPG's tags/categories and is read by BookOrbit as **Genres**. The same tag list is *also* written to `bookorbit:tags` (BookOrbit's own separate, dedicated Tags field) — the two aren't the same concept in BookOrbit's schema, so both get populated from the one source list. Tags are filtered through `TAG_BLOCKLIST` in `dtrpg_client.py` first, same as before.
- `bookorbit:seriesName` / `bookorbit:seriesIndex` — as matched.
- `bookorbit:isbn13` / `bookorbit:isbn10` — `pdf_writer._isbn_field()` routes `ProductMetadata.isbn` to whichever field matches its digit count (13 → isbn13, 10 → isbn10) after stripping punctuation; anything else is skipped with a warning rather than guessed at.
- `dc:identifier` gets a plain `dtrpg:<product_id>` string, for manual traceability only — BookOrbit's reader doesn't look for this field at all (confirmed from its source: unlike Kavita/Calibre, there's no qualified `xmp:Identifier` structure needed anymore). Unrecognized XMP properties are simply ignored by BookOrbit, so this costs nothing to keep. The old Calibre-specific qualified-identifier code (which reached into pikepdf's private XMP internals to satisfy `ebook-meta`'s stricter parsing) was removed along with the rest of the Kavita-era mapping — no longer needed.
- The `bookorbit:` namespace prefix must be written as literally `bookorbit` — BookOrbit's XMP reader (`fast-xml-parser`) matches on literal tag-prefix text, not resolved namespace URIs, so any other prefix string for the same namespace URI would silently not be recognized.

Don't reshuffle this mapping without checking `pdf_writer.py`'s docstring and BookOrbit's real source first — it reflects explicit, verified choices, not defaults.

**Backups are one-time, not per-run.** `pdf_writer._backup()` only creates a `.bak` if one doesn't already exist, so the backup always represents the pre-tagging original even across repeated re-tagging runs — it must not be refreshed on every write.

**`review.py`'s CSV merge preserves user edits.** `merge_by_filename()` skips overwriting any row already marked `approved` when a fresh `scan` runs again — re-scanning must not clobber manual corrections.
