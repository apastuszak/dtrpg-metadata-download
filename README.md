# dtrpg-metadata-download

Matches local RPG PDF files against DriveThruRPG metadata — checking manual overrides, then a pre-supplied product URL, then your purchased library, then DriveThruRPG's public catalog, in that order — and writes the result into each PDF's embedded XMP metadata, so tools like Kavita or Calibre pick it up on their own library scan. No database, no server — a CLI you run against a folder of PDFs.

## Setup

No manual venv needed — the script carries its own dependency list ([PEP 723](https://peps.python.org/pep-0723/) inline metadata) and is directly executable. With [uv](https://docs.astral.sh/uv/) installed:

```bash
chmod +x dtrpg-metadata-download.py   # first time only
export DTRPG_API_KEY=...   # an Application Key from your DriveThruRPG account page — not your account password
./dtrpg-metadata-download.py --help
```

The first run resolves and caches pikepdf/rapidfuzz/PyYAML/requests automatically (a few seconds); every run after that is instant. No `.venv` directory, no `pip install` step, nothing to activate.

Don't have `uv`? The classic path still works:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
export DTRPG_API_KEY=...
./.venv/bin/python dtrpg-metadata-download.py --help
```

Copy `config.yaml` and adjust `root` (and anything else) for your machine. It's meant to stay free of secrets — the API key only ever comes from the `DTRPG_API_KEY` environment variable.

## Usage

The primary workflow is `tag` — interactive, no review step, writes immediately on confirm:

```bash
# One file
./dtrpg-metadata-download.py tag "Sword Worlds.pdf"

# Every PDF under a directory, one at a time
./dtrpg-metadata-download.py tag --root "/path/to/rpg/pdfs"
```

For each file it searches your library, then DriveThruRPG's public catalog, and shows ranked candidates:

```
Candidates for Sword Worlds.pdf:
  [1] (98.6) Sword Worlds
        publisher: Mongoose
        source:    dtrpg-library
  [2] (61.3) Traveller: Explorer's Edition
        publisher: Mongoose
        source:    dtrpg-library
Pick a number to write, paste a DriveThruRPG product URL (or 'id:PRODUCT_ID') for a direct lookup, Enter to skip, or 'q' to stop:
```

- Type a number to write that candidate's metadata into the file (asks for a final y/N confirm first).
- Paste a DriveThruRPG product URL, or type `id:PRODUCT_ID`, to fetch an exact listing directly — useful when the right book doesn't show up in the candidates at all (DriveThruRPG's search requires query words to literally match the title text, so it can miss real matches; see Known quirks below).
- Press Enter to skip the file, or `q` to stop the whole batch early without touching the rest.

### Batch mode with a review step

If you'd rather review matches in bulk before anything gets written, use the CSV-gated path instead:

```bash
./dtrpg-metadata-download.py scan --root "/path/to/rpg/pdfs"   # writes data/review.csv, never touches PDFs
./dtrpg-metadata-download.py review-status                      # counts by status
# edit data/review.csv by hand — fix bad matches, flip status to "approved"
./dtrpg-metadata-download.py write-pdfs --root "/path/to/rpg/pdfs"
./dtrpg-metadata-download.py all --root "/path/to/rpg/pdfs"     # scan + write-pdfs in one go
```

`scan --refresh-library` re-pulls your purchased library instead of using the cache. `scan --apply-review` only matches files not already present in `review.csv`, so a re-run doesn't re-query files you've already resolved.

### Manual overrides

For titles DriveThruRPG doesn't sell at all (Bits and Mortar exclusives, publisher-direct purchases, etc.) — where there's no DriveThruRPG listing to fetch — fill in `data/manual_overrides.yaml` keyed by exact filename with the full metadata by hand; see the template in that file for the format. Both `tag` and `scan` check this first, before anything else.

### Known URLs — pre-populating known matches

For titles that *are* on DriveThruRPG but aren't reliably findable through search (see Known quirks below), drop a `dtrpg_urls.csv` file directly in the folder of PDFs you're processing (not in `data/` — it lives alongside the files it describes, so each folder can carry its own list):

```csv
filename,drivethrurpg_url
Mongoose Traveller 1E - MGP3800 - Core Rulebook.pdf,https://www.drivethrurpg.com/en/product/56586/traveller-main-rulebook
Some Other Book.pdf,id:12345
```

The `drivethrurpg_url` column accepts a full product URL, `id:PRODUCT_ID`, or a bare product ID. Both `tag` and `scan` check it next (after manual overrides, before search) — full metadata is fetched live from DriveThruRPG, so you only ever need to supply the filename and the URL, nothing else. Matches from this file are auto-approved with no confirmation prompt, even in `tag`; if a listed product ID can't be fetched (typo, delisted, etc.), that file falls back to normal search instead of failing outright.

## What gets written

Field mapping is deliberately not a straight copy of Calibre's own conventions — DriveThruRPG's per-book author credits are inconsistent enough that this library is tagged by publisher instead:

| PDF field (`ebook-meta` label) | Source |
|---|---|
| Title | Matched title |
| Author(s) | Publisher |
| Publisher | Publisher |
| Comments | Description |
| Tags | Categories/tags (filtered — format/language/AI-policy noise like "PDF"/"English" is dropped) |
| Identifiers | `dtrpg:<item number>`, plus `isbn:<isbn>` when DriveThruRPG has one on file (many PDF-only products don't) — so the exact listing a file was matched against can be traced back later. Written in Calibre's own qualified identifier structure, not a plain string, so it actually shows up under this label |
| Series / Series Index | As matched (DriveThruRPG doesn't expose these as structured fields — they're inferred from the title text) |

The original file is copied to `<name>.pdf.bak` before the first write; re-tagging a file later won't overwrite that backup, so it always holds the pre-tagging original.

## Known quirks of DriveThruRPG's API

It's undocumented, and a few real surprises came up building this:

- The **catalog search** (`products?name=...`) isn't fuzzy — it behaves like every query word must be literally present in the product's real title. A perfectly reasonable query (e.g. including the publisher name) can come back empty, or worse, silently match the wrong product if a word isn't part of the actual title. The tool automatically strips filename noise (edition shorthand like `1E`/`2E`, SKU codes) and retries with the leading word dropped, but some titles genuinely aren't findable by any reasonable query — that's what the direct URL/`id:` lookup in `tag` is for.
- The **product detail** endpoint (`products/{id}`) returns different response shapes depending on request headers (a flat object vs. an older JSON:API-style envelope) — content negotiation, not randomness. The client's fixed headers get the flat shape reliably.
- Your **purchased library** listing only has title + publisher, not authors/tags/description — those get fetched lazily (one extra API call) only for whichever match actually gets used, not for every candidate shown.

If matching breaks in a new way, check `data/debug/` — a raw response dump is written automatically whenever a response can't be parsed as expected.

## Not implemented

Pushing metadata to Kavita directly via its API is deliberately out of scope for now — `kavita_client.py` is an intentional placeholder. The pipeline relies on Kavita's own library scan picking up the embedded PDF metadata instead.
