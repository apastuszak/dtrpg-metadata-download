# dtrpg-metadata-download

Matches local RPG PDF files against DriveThruRPG metadata — checking manual overrides, then a pre-supplied product URL, then your purchased library, then DriveThruRPG's public catalog, in that order — and writes the result into each PDF's embedded XMP metadata in Calibre's own format, plus a [BookOrbit](https://bookorbit.app) `.opf` sidecar and a [Grimmory](https://grimmory.org) `.metadata.json` sidecar alongside it. No database, no server — a CLI you run against a folder of PDFs.

## Setup

No manual venv needed — the script carries its own dependency list ([PEP 723](https://peps.python.org/pep-0723/) inline metadata) and is directly executable. With [uv](https://docs.astral.sh/uv/) installed:

```bash
chmod +x dtrpg-metadata-download.py   # first time only
export DTRPG_API_KEY=...   # an Application Key from your DriveThruRPG account page — not your account password
./dtrpg-metadata-download.py --help
```

The first run resolves and caches pikepdf/rapidfuzz/PyYAML/requests/lxml/textual/pymupdf/pillow automatically (a few seconds); every run after that is instant. No `.venv` directory, no `pip install` step, nothing to activate.

Don't have `uv`? The classic path still works:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
export DTRPG_API_KEY=...
./.venv/bin/python dtrpg-metadata-download.py --help
```

Copy `config.yaml` and adjust `root` (and anything else) for your machine. It's meant to stay free of secrets. The API key comes from `DTRPG_API_KEY` if it's set; otherwise, don't want to manage an environment variable? Run `./dtrpg-metadata-download.py preferences` (or use the Preferences tab in the GUI) to save it to `data/preferences.yaml` instead — gitignored and written with owner-only file permissions, but still a plaintext file on disk, a real (if smaller) step down from an environment variable that only ever lived in one shell session. `DTRPG_API_KEY` always wins over a saved preference if both are present.

## Usage

Prefer a window over the terminal? `./dtrpg-metadata-download.py gui` launches a desktop GUI covering everything below in one place — see "Desktop GUI" further down. Everything else in this section describes the terminal/CLI paths.

The primary workflow is `tag` — a full-screen terminal UI (built with [Textual](https://textual.textualize.io/)), no review step, writes immediately on confirm:

```bash
# One file
./dtrpg-metadata-download.py tag "Sword Worlds.pdf"

# Every PDF under a directory, one at a time
./dtrpg-metadata-download.py tag --root "/path/to/rpg/pdfs"
```

For each file it searches your library, then DriveThruRPG's public catalog, and shows a screen of ranked candidates:

- **Arrow keys + Enter** to pick a candidate — this opens a confirm screen pre-filled with every value about to be written (title, publisher, series, series index, description, tags, ISBN, product URL), so anything DriveThruRPG got wrong or left out can be corrected right there before it's saved; press `ctrl+s` or click Confirm to write, Escape to skip, `q` to quit the batch. If the series is still blank after confirming, a small dialog asks for one before writing — leave it blank for no series.
- A text field to **paste a DriveThruRPG URL, or type `id:PRODUCT_ID`**, for a direct lookup — useful when the right book doesn't show up in the candidates at all (DriveThruRPG's search requires query words to literally match the title text, so it can miss real matches; see Known quirks below). This field is shown even when *no* candidates were found at all, so pasting a known link is never unavailable. Press Enter or click **Use URL** to submit it — Use URL is a separate button from picking a candidate, so pasting a link and clicking it always uses that link, never whatever's highlighted in the candidate list above.
- **`m`** opens a manual-entry form — title/publisher/series/series index/tags/ISBN/product URL as single-line fields, plus a real **multi-line text area for Description** — for a title DriveThruRPG doesn't sell at all, without pre-editing `data/manual_overrides.yaml` first. Leaving Title blank and pressing Escape cancels back out. Submitting still goes through the same editable confirm screen as a candidate pick, as one last chance to fix a typo before writing.
- **Escape** skips the file, **`q`** stops the whole batch early without touching the rest.

With `--root`, before the per-file loop starts you're asked once for a series name, with a hint that you can *"leave blank if all books are not in the same series."* Give a name and every later confirm screen's Series field comes pre-filled with it (still editable per book, just not blank by default); leave it blank and each book's confirm screen instead starts with whatever series that match already carries. (Files matched via `dtrpg_urls.csv`, below, never get a confirm screen either way, since that path is deliberately non-interactive end to end.)

Once the run finishes (or you quit a batch early with `q`), the TUI closes itself automatically and prints a plain summary line for every file — no "press q to exit" screen to dismiss.

Add `--rename` to tag and rename in one pass — right after each successful write, the file (and its `.opf`/`.metadata.json`/`.bak` siblings) is immediately renamed to `<series> - <title>.pdf`, the same result you'd get running `rename` on it afterward. Applies to every match path (candidate-pick, manual override, known URL, or manual entry); skipped for anything not actually written (a declined confirm, a cancelled manual entry, a failed write).

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

### Renaming files after tagging

```bash
./dtrpg-metadata-download.py rename "some_book.pdf"                        # a single file
./dtrpg-metadata-download.py rename --root "/path/to/rpg/pdfs" --dry-run   # preview first
./dtrpg-metadata-download.py rename --root "/path/to/rpg/pdfs"
```

Renames each already-tagged PDF to `<series> - <title>.pdf` (or just `<title>.pdf` when there's no series), reading title/series back from that file's `.metadata.json` sidecar — the only one of the three outputs that's plain, flat JSON, since Calibre's/BookOrbit's embedded XMP series fields are qualified/structured properties that can't be read back reliably through pikepdf's public API (see `renamer.py`). The `.pdf.bak`, `.opf`, and `.metadata.json` siblings are renamed right along with the PDF, since they're all named from its stem. Files with no sidecar (never tagged) or already named correctly are skipped; if the computed name collides with an existing file, that one's left alone and reported as failed rather than overwritten. If renaming one file's companion group fails partway through (permissions, a file locked by another app), whatever in that group already succeeded is rolled back rather than left split across old and new names, and the batch continues with the next file instead of aborting.

### Manual overrides

For titles DriveThruRPG doesn't sell at all (Bits and Mortar exclusives, publisher-direct purchases, etc.) — where there's no DriveThruRPG listing to fetch — fill in `data/manual_overrides.yaml` keyed by exact filename with the full metadata by hand; see the template in that file for the format. Both `tag` and `scan` check this first, before anything else. For a one-off file rather than something you want recorded for every future run, `tag`'s `m` option (above) prompts for the same fields interactively instead of editing this file.

### Known URLs — pre-populating known matches

For titles that *are* on DriveThruRPG but aren't reliably findable through search (see Known quirks below), drop a `dtrpg_urls.csv` file directly in the folder of PDFs you're processing (not in `data/` — it lives alongside the files it describes, so each folder can carry its own list):

```csv
filename,drivethrurpg_url
Mongoose Traveller 1E - MGP3800 - Core Rulebook.pdf,https://www.drivethrurpg.com/en/product/56586/traveller-main-rulebook
Some Other Book.pdf,id:12345
```

The `drivethrurpg_url` column accepts a full product URL, `id:PRODUCT_ID`, or a bare product ID. Both `tag` and `scan` check it next (after manual overrides, before search) — full metadata is fetched live from DriveThruRPG, so you only ever need to supply the filename and the URL, nothing else. Matches from this file are auto-approved with no confirmation prompt, even in `tag`; if a listed product ID can't be fetched (typo, delisted, etc.), that file falls back to normal search instead of failing outright.

### Desktop GUI

```bash
./dtrpg-metadata-download.py gui
```

A PyQt6 desktop window with a tab for every subcommand above — Tag (the same candidate-pick/manual-entry/confirm/series flow as the terminal UI, in dialog windows instead), Scan, Review (an editable table for `review.csv` — approve a row, tweak its series, edit its description — instead of opening it in a spreadsheet app), Write PDFs, Rename, All, and Preferences (your API key and DriveThruRPG name — see Setup above). Long-running work happens in the background so the window stays responsive.

The Tag, Write PDFs, and All tabs each have a **"Convert all images to RGB JPEG"** checkbox (`--convert-images` on the matching CLI subcommands) — see below.

PyQt6 is a normal dependency, installed automatically the first time you run `gui` via `uv run --script` — no separate install step, unlike an earlier Tkinter-based version of this GUI, which needed a Python built with Tk bindings.

## What gets written

Up to three things happen on every write, all from the same matched data — embedded metadata and the Grimmory sidecar always, the BookOrbit `.opf` only with `--bookorbit-mode`:

### 1. Embedded PDF metadata, in Calibre's format

Field mapping targets Calibre's own conventions (verified against a real, installed Calibre 9.13 — both `ebook-meta`'s summary view and its `--to-opf` export — not just written on spec and assumed correct):

| PDF field (`ebook-meta` label) | Source |
|---|---|
| Title | Matched title |
| Author(s) | Publisher (not the actual author list DriveThruRPG returns — deliberate, since DriveThruRPG's per-book author credits are too inconsistent to trust for this library) |
| Publisher | Publisher |
| Comments | Description |
| Tags | Categories/tags (filtered — format/language/AI-policy noise like "PDF"/"English" is dropped) |
| Identifiers | `dtrpg:<item number>` plus `isbn:<isbn>` when DriveThruRPG has one on file, in Calibre's own qualified identifier structure |
| Series | As matched, in Calibre's own qualified structure — **not** a plain scalar field. This tripped us up for a while: a naive write round-trips through pikepdf fine and looks correct, but real Calibre silently never shows it as a series, because `calibre:series` needs its value wrapped in a nested `rdf:value`, and the index lives in a completely different namespace (`calibreSI:series_index`) nested inside the series element. Confirmed fixed against a real `ebook-meta --to-opf` run. The index is only written when actually known — left blank rather than assuming "book 1" (Calibre's own reader defaults a missing index to 1 anyway, so this changes nothing about the display, just avoids fabricating a claim in the raw data). |
| Series (again) | Also written as `bookorbit:seriesName`/`bookorbit:seriesIndex`, BookOrbit's own simpler (unstructured) series fields — see "BookOrbit sidecar" below for why this duplication is actually necessary. |

The same tags/categories are additionally written to the classic `pdf:Keywords` field, so a plain PDF reader that only looks at the Info dictionary (not XMP at all) still sees something — pikepdf's automatic sync maps the Info dictionary's `/Subject` from the XMP description, not from tags, so `/Keywords` is otherwise the only place they'd show up.

**This works with any reader that understands Calibre-style XMP, not just Calibre itself** — the field mapping above targets Calibre's actual convention, not something invented for this project. Confirmed for [Kavita](https://www.kavitareader.com): its real PDF metadata parser (`Kavita.Services/Helpers/PdfMetadataExtractor.cs` in `Kareadita/Kavita`) reads the exact same `calibre:series`/nested-`rdf:value` structure and `calibreSI:series_index` namespace this project writes, plus standard title/publisher/author/description/tags — and it's wired into Kavita's real library scan, not just a theoretical parser. **One gap: ISBN.** Kavita's PDF extractor looks for `pdfx:isbn`/`prism:isbn`, which this project doesn't write (ISBN here lives inside Calibre's own qualified `xmp:Identifier` structure instead) — so ISBN specifically won't show up in Kavita, even though everything else does.

### 2. `<name>.opf` — BookOrbit sidecar

Standard EPUB2/Calibre-style OPF, written next to the PDF. BookOrbit reads a same-stem (or `metadata.opf`) sidecar automatically — confirmed by running BookOrbit's own real OPF parser (from its open-source repo) against files this tool writes. Series comes from the same `<meta name="calibre:series">` convention Calibre itself uses; ISBN is auto-bucketed into ISBN-10/13 on BookOrbit's end from a single `<dc:identifier opf:scheme="ISBN">`.

**Why series is also written directly into the embedded PDF (`bookorbit:seriesName`), not just here:** BookOrbit's default scan order tries embedded PDF metadata *first*, and only opens the `.opf` sidecar if the embedded extraction returns *nothing at all* — it's a whole-source fallback, not a per-field merge. Since a Calibre-tagged PDF always has some embedded metadata (title, authors, etc.), the sidecar's series data was silently unreachable in practice. Confirmed against BookOrbit's real scanner source and its real XMP reader, not just theory. **By default the `.opf` isn't even written** — pointless when the embedded PDF always "wins" — unless `--bookorbit-mode` is passed (see below), which flips the whole strategy: strip the embedded PDF metadata entirely instead, forcing BookOrbit to actually fall through to this file.

#### `--bookorbit-mode`

Available on `tag`, `write-pdfs`, and `all`. Instead of writing Calibre metadata into the PDF, this strips **all** PDF-level metadata — the full XMP packet and the classic Info dictionary, not just the fields this tool would otherwise write, since a leftover publisher-set `/Title` alone is enough for BookOrbit's embedded source to "win" — and writes the `.opf` sidecar (which is otherwise skipped, see above). The Grimmory `.metadata.json` sidecar is written either way; this flag only changes the embedded-PDF/`.opf` tradeoff for BookOrbit. Off by default — re-tagging a file with the flag now on top of a previous non-`--bookorbit-mode` write correctly wipes whatever Calibre metadata that earlier write left behind, and conversely, re-tagging *without* the flag after a previous `--bookorbit-mode` write deletes the stale `.opf` rather than leaving it behind with the old match's data.

#### `--convert-images`

Available on `tag`, `write-pdfs`, and `all` (and as a checkbox in the GUI's Tag/Write PDFs/All tabs). Converts every CMYK, grayscale, and JPEG2000 (JPX) image in the PDF to standard RGB JPEG before metadata is written — useful for PDFs from print workflows, which often carry CMYK images and/or JPX compression that some readers (notably Apple's PDF renderer on macOS/iOS) handle poorly, showing images as missing or wrong-colored. Indexed/palette images (diagrams, pixel art) are kept lossless rather than run through JPEG, which would blur their sharp edges. CMYK images without their own embedded color profile are re-tagged with a real CMYK ICC profile first (macOS only — falls back to a plain conversion elsewhere), and converted images get an explicit sRGB profile rather than bare `/DeviceRGB`, both aimed at avoiding the color casts a naive conversion produces in some readers. Runs before metadata is written, never after, so it can't disturb the Calibre-specific XMP structure `--bookorbit-mode`'s sibling section above already went through the trouble of getting right. Off by default; ported from a separate tool ([mac-pdf-rgb-fix](https://github.com/apastuszak/mac-pdf-rgb-fix)) built specifically for this problem.

### 3. `<name>.metadata.json` — Grimmory sidecar

Grimmory's own flat JSON sidecar format (confirmed against its real Java DTO schema and writer source, `github.com/grimmory-tools/grimmory`) — `title`, `authors`, `publisher`, `description`, `isbn10`/`isbn13` (bucketed by digit count, since Grimmory's JSON has no auto-detection the way the OPF does), `categories`/`tags`, and `series: {name, number}`.

Both sidecar-format choices are best-effort: a field with no data is simply omitted, matching how both apps' own writers behave.

The original file is copied to `<name>.pdf.bak` before the first write; re-tagging a file later won't overwrite that backup, so it always holds the pre-tagging original.

## Known quirks of DriveThruRPG's API

It's undocumented, and a few real surprises came up building this:

- The **catalog search** (`products?name=...`) isn't fuzzy — it behaves like every query word must be literally present in the product's real title. A perfectly reasonable query (e.g. including the publisher name) can come back empty, or worse, silently match the wrong product if a word isn't part of the actual title. The tool automatically strips filename noise (edition shorthand like `1E`/`2E`, SKU codes) and retries with the leading word dropped, but some titles genuinely aren't findable by any reasonable query — that's what the direct URL/`id:` lookup in `tag` is for.
- The **product detail** endpoint (`products/{id}`) returns different response shapes depending on request headers (a flat object vs. an older JSON:API-style envelope) — content negotiation, not randomness. The client's fixed headers get the flat shape reliably.
- Your **purchased library** listing only has title + publisher, not authors/tags/description — those get fetched lazily (one extra API call) only for whichever match actually gets used, not for every candidate shown.
- A purchased title's **product ID can go stale** — DriveThruRPG can re-list a book under a new ID after purchase (confirmed against a real title: fetching the library-linked ID 403'd with `"You cannot access this content right now"`, while the current listing at a different ID worked fine). Handled automatically: if fetching the library-linked ID fails, the tool retries with a catalog search by title and, if found, uses the corrected ID — including for the identifier written into the file, not just the description/tags/ISBN.

If matching breaks in a new way, check `data/debug/` — a raw response dump is written automatically whenever a response can't be parsed as expected.

## Not implemented

Pushing metadata directly via an app's API (rather than writing to the PDF and generating sidecar files) isn't implemented. The pipeline relies on each app's own library scan picking up the embedded metadata and/or sidecar files on disk.
