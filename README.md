# dtrpg-metadata-download

Tag your RPG PDF library with proper book information from DriveThruRPG: title, publisher, series and number, description, tags, ISBN and product link. Your ebook app (Calibre, Grimmory, BookOrbit, Kavita, …) can then sort, search and group your books instead of showing a pile of filenames.

It looks each PDF up by its filename, in your purchased DriveThruRPG library first and then the public catalog, and shows you the likely matches. Pick one, check the details, and it writes them into the file. Along the way it can also:

- set the page numbers your PDF reader shows to match the printed ones ("Cover", i, ii, 1, 2, …)
- turn "see p. 208" references, the table of contents and the index into clickable links (GURPS, Mongoose Traveller and 2300AD, Delta Green, Shadowrun Sixth World)
- move the back cover to right after the front cover, or to the end
- convert images for Apple devices that show them missing or in the wrong colours, or convert the whole book to grayscale
- rename files to `Series - Title.pdf`

It runs on your own computer, on your own files. There's a desktop app and a terminal version, and they do the same things.

## Contents

- [Installation](#installation)
- [Your DriveThruRPG API key](#your-drivethrurpg-api-key)
- [Quick start](#quick-start)
- [Tagging books](#tagging-books)
- [What gets written: choosing your reader app](#what-gets-written-choosing-your-reader-app)
- [Extra options](#extra-options)
- [Working with books you've already tagged](#working-with-books-youve-already-tagged)
- [Books DriveThruRPG can't find](#books-drivethrurpg-cant-find)
- [Clickable page references (hyperlinking)](#clickable-page-references-hyperlinking)
- [Files the tool creates](#files-the-tool-creates)
- [Settings file (config.yaml)](#settings-file-configyaml)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)

## Installation

You need **Python 3.10 or newer** and, ideally, **[uv](https://docs.astral.sh/uv/)**, which installs everything else for you.

```bash
git clone https://github.com/apastuszak/dtrpg-metadata-download.git
cd dtrpg-metadata-download
chmod +x dtrpg-metadata-download.py
./dtrpg-metadata-download.py --help
```

The first run takes a few seconds while uv downloads the libraries it needs. After that it starts straight away, and there's nothing to activate or install separately, the desktop app included.

**Without uv**, use a virtual environment instead:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python dtrpg-metadata-download.py --help
```

(Then run `./.venv/bin/python dtrpg-metadata-download.py` wherever this guide says `./dtrpg-metadata-download.py`.)

**Optional: Ghostscript.** Only needed for the grayscale conversion. On macOS: `brew install ghostscript`. On Linux, install your distribution's `ghostscript` package.

## Your DriveThruRPG API key

The tool reads your library and the catalog through DriveThruRPG's API, using an **Application Key**. This is not your account password. You can find or generate one on your DriveThruRPG account page.

Give it to the tool in either of two ways:

- **Save it once**, in the desktop app's **Preferences** tab, or with:
  ```bash
  ./dtrpg-metadata-download.py preferences
  ```
  It's stored in `data/preferences.yaml`, readable only by your user account, and never committed to git. It is still a plain text file on your disk.
- **Or set it in your shell** for the session:
  ```bash
  export DTRPG_API_KEY=your-application-key
  ```
  If both are set, the environment variable wins.

You don't need a key at all when you only use the options that don't look books up, such as renaming, page labels or hyperlinking books you've already tagged ([see below](#working-with-books-youve-already-tagged)).

## Quick start

**Desktop app:**

```bash
./dtrpg-metadata-download.py gui
```

On the **Tag** tab, choose **Whole folder** (or **Single file**), pick your folder with **Browse…**, and press **Start**. For each book, pick the right match from the list, check the details, and press **Confirm**. Hover over the blue **?** next to any option to see what it does.

**Terminal:**

```bash
./dtrpg-metadata-download.py tag --root "/path/to/your/rpg/pdfs"
./dtrpg-metadata-download.py tag "Sword Worlds.pdf"            # just one book
```

Before you tag your whole collection, try it on a copy of a few books. A backup of every file is made before it's first changed ([`.pdf.bak`](#files-the-tool-creates)), but it's still worth seeing the results in your reader app first.

## Tagging books

`tag` works through your books one at a time. With a folder, it includes every PDF in its subfolders too.

### How a book is matched

For each PDF it tries, in this order:

1. **Your manual overrides**, for books DriveThruRPG doesn't sell ([details](#manual-overrides)).
2. **A known product link** from a `dtrpg_urls.csv` file in the same folder ([details](#known-product-links)). These are used directly, with no questions.
3. **Your purchased library, then the DriveThruRPG catalog.** It searches both using the filename, cleaned of things like edition codes and product numbers, and shows you the five best matches.

### Picking a match

The match screen lists the candidates, best first, each with a match score.

| To… | Desktop app | Terminal |
|---|---|---|
| use a candidate | select it, then **Pick** | arrow keys, then Enter |
| look up a specific product | paste a DriveThruRPG link (or `id:12345`) into the box, then **Use URL** | same, then Enter |
| type the details yourself | **Manual entry** | `m` |
| skip this book | **Skip (Esc)** | Escape |
| stop the whole run | **Quit** | `q` |

Pasting a link is the way out when the right book isn't in the list. DriveThruRPG's search is very literal ([see Troubleshooting](#a-book-isnt-found-or-the-wrong-one-is)), so this happens. The box is there even when no candidates were found at all.

**Manual entry** is for books DriveThruRPG doesn't sell. You fill in the title, publisher, series, number, tags, ISBN, product link and a multi-line description.

### Checking the details

Whatever you pick, a **confirm screen** shows every value that's about to be written: title, publisher, series, series number, description, tags, ISBN and product link. Correct anything DriveThruRPG got wrong or left out, then **Confirm** (`ctrl+s` in the terminal), skip, or quit.

If the series is still empty after you confirm, you're asked for it once more. Leave it blank for a book that isn't part of a series.

### Series for a whole folder

When you tag a folder, you're first asked for a series name, with the note *"leave blank if all books are not in the same series"*. Give one, and every book's series starts out filled in with it (you can still change it per book). This applies to books matched from the catalog, from your library, and from manual overrides alike, but a match that already has its own series keeps it. Leave it blank and each book starts with whatever series its match has.

### When the run ends

The terminal version closes itself when it's done and prints one line per book, for example "Wrote metadata to …", "Skipped." or "Renamed: … -> …". The desktop app shows the same lines in its log.

If you close the desktop app in the middle of a run, it finishes the book it's working on, then closes itself. That way a book is never left half-written.

## What gets written: choosing your reader app

The **Metadata tagging** setting decides where the book information goes. Pick the one that matches the app you read your library in. On the terminal, it's `--metadata MODE`.

| Choice | Writes | For |
|---|---|---|
| **Grimmory** (the default) | the information inside the PDF, in Calibre's format, plus a `Book.metadata.json` file next to it | Grimmory, plus every app listed for Calibre |
| **Calibre** | only the information inside the PDF | Calibre; Kavita (everything except the ISBN); BookOrbit with its default settings |
| **BookOrbit** | only a `Book.opf` file next to the PDF, and removes all information from the PDF itself | BookOrbit, **only after** the library-setting change below |
| **Off** | nothing; no lookups, no API key needed | running other options on books that are already tagged |

**About BookOrbit:** BookOrbit normally reads a PDF's own information first and never looks at an `.opf` file next to it. If you use BookOrbit with its default settings, choose **Calibre** or **Grimmory**: BookOrbit reads the information inside the PDF, series included. Only if you want BookOrbit to use the `.opf` file, choose **BookOrbit** here *and* change the library's metadata precedence in BookOrbit's library settings so **OPF file** comes before **Embedded**.

**Switching between choices is safe.** Each choice except Off removes the other choices' sidecar files left over from an earlier run, so an old file can't contradict the new one.

**What's in the information:**

| Field in your reader app | Comes from |
|---|---|
| Title | the matched title |
| Author(s) | the publisher, on purpose: DriveThruRPG's per-book author credits are too inconsistent to sort by |
| Publisher | the publisher |
| Description / Comments | the product description |
| Tags | DriveThruRPG's categories, without noise like "PDF" or "English" |
| Series and number | your series and series number (the number only when you gave one) |
| Identifiers | `dtrpg:` plus the product number, and `isbn:` when DriveThruRPG has an ISBN |

If a match has no description, tags or ISBN, whatever the PDF already had in that field is left alone. For example, a publisher's own description is kept when DriveThruRPG has none.

## Extra options

These run while tagging, in the desktop app's Tag tab or as options to `tag`. Each is off unless you turn it on, and you can combine them.

### Page labels

**Desktop app:** **Set page labels** · **Terminal:** `--page-labels`

This sets the page numbers your PDF reader shows in its page box and thumbnails, so "go to page 47" lands on the page printed 47:

- The front cover and the back cover are **Cover**.
- Pages before printed page 1 are **i, ii, iii, …**
- Every other page gets the number printed on it, read from the headers and footers. Pages with no visible number, such as full-page art, ads and title pages, are numbered from their neighbours.

The back cover is normally the last page. Some publishers (Steve Jackson Games, Delta Green, Evil Hat) put it second, which is spotted from the retail barcode on PDF page 2. A wraparound cover (front and back as one wide page 1, like Shadowrun Sixth World) or a blank last page means there's no back cover, so the last page gets a number.

Books whose numbering can't be read confidently keep their existing labels, with the reason in the log. That covers scans whose page numbers aren't text, boxed sets where only one booklet is numbered, and several books merged into one PDF. The rest of the tagging still happens.

### Rename

**Desktop app:** **Rename** · **Terminal:** `--rename`

This renames the file to `Series - Title.pdf`, or `Title.pdf` with no series, right after it's tagged. Its backup, sidecar files and link report are renamed with it.

Characters that aren't allowed in file names are replaced: "Delta Green: Handler's Guide" becomes `Delta Green - Handler's Guide.pdf`. If another file already has the new name, the book is left as it is and reported, never overwritten.

### Convert images (RGB or grayscale)

**Desktop app:** **Convert images** dropdown · **Terminal:** `--convert-images` or `--convert-grayscale`

- **RGB** converts CMYK, grayscale and JPEG2000 images to standard RGB JPEG. Use it for books whose images show up missing, black or in the wrong colours on a Mac, iPhone or iPad, which is common with print-ready PDFs. Diagrams and pixel art are kept lossless so they stay sharp. The file usually gets bigger.
- **Grayscale** converts the whole book to grayscale, for example for an e-ink reader or to save printer ink. It needs [Ghostscript](#installation).

You can only pick one, since they do opposite things to the same images. A book's existing information, page labels and layers are kept either way.

### Move the back cover

**Desktop app:** **Move back cover** dropdown · **Terminal:** `--back-cover-to-page-2` or `--back-cover-to-end`

This moves the back cover so your whole library follows one convention: either right after the front cover (handy for seeing both covers together), or at the end.

**This doesn't search the book for the back cover.** Pick the move that matches where the back cover is now:

- **To page 2** is for a back cover that's currently the **last page**.
- **To the end** is for a back cover that's currently **PDF page 2**.

Only that page is ever considered. If it has a retail barcode, that confirms it's the back cover and it's moved straight away. Without one, which is usual for PDF-only releases, you're shown the page and asked first:
- **Desktop app:** you see it next to the front cover.
- **Terminal:** you see a snippet of its text, plus an **Open page** button that opens it in your PDF viewer.

Blank pages are never offered. Books with a wraparound cover are left alone. Bookmarks, links and page labels all follow their pages.

### Clickable page references

**Desktop app:** **Hyperlink references** dropdown · **Terminal:** `--hyperlink RULES`

This turns "see p. 208", chapter references, the table of contents and the index into clickable links. Pick your publisher's rule set. See [Clickable page references](#clickable-page-references-hyperlinking) for the details.

### Page backgrounds (Troll Lord Games only)

**Desktop app:** **Page background** dropdown · **Terminal:** `--background-layer` or `--remove-background`

This is for Castles & Crusades and other Troll Lord Games PDFs, whose pages print on a decorative parchment background.
- **Layer** puts the background on its own layer, which you can hide in a PDF reader that supports layers.
- **Remove** deletes it outright.

It isn't meant for other publishers' books. Any layers the book already had are kept.

### Refresh library

**Desktop app:** **Refresh library** · **Terminal:** `--refresh-library`

Your purchased library is downloaded once and kept in `data/`. It isn't updated automatically, so **after buying new books, tick this once** (or add `--refresh-library`), or they won't show up as matches. If the download fails (you're offline, say), the run carries on with the copy you already have.

## Working with books you've already tagged

Set **Metadata tagging** to **Off** (`--metadata off` in the terminal), and the tool doesn't look anything up. No API key is needed and nothing asks you any questions. It just runs the options you tick on every book. **Rename** then takes the name from the information already in each file.

For example, to add page labels and clickable references to a folder of GURPS books you tagged earlier, and tidy their names:

```bash
./dtrpg-metadata-download.py tag --root "/path/to/GURPS" --metadata off --page-labels --hyperlink gurps --rename
```

In the desktop app, tick **Dry run** to see what **Set page labels** and **Rename** *would* do, without changing anything. Dry run ignores the other options.

Renaming and page labels are also available as commands of their own, with a preview:

```bash
./dtrpg-metadata-download.py rename --root "/path/to/pdfs" --dry-run     # see what would change
./dtrpg-metadata-download.py rename --root "/path/to/pdfs"
./dtrpg-metadata-download.py rename "Some Book.pdf"

./dtrpg-metadata-download.py page-labels --root "/path/to/pdfs" --dry-run
./dtrpg-metadata-download.py page-labels "Some Book.pdf"
```

`rename` reads each book's title and series from its `.metadata.json` file, its `.opf` file, or the information inside the PDF, whichever it finds first. Untagged books and books already named correctly are skipped.

## Books DriveThruRPG can't find

### Manual overrides

For books DriveThruRPG doesn't sell (publisher-direct purchases, Bits and Mortar extras, Kickstarter PDFs), add their details to `data/manual_overrides.yaml`, keyed by the exact filename:

```yaml
"GURPS Dungeon Fantasy 1 - Adventurers.pdf":
  title: "GURPS Dungeon Fantasy 1: Adventurers"
  series: "GURPS Dungeon Fantasy"
  series_index: 1
  publisher: "Steve Jackson Games"
  tags:
    - GURPS
    - Fantasy
  description: "The first book in the line…"
  isbn: "9781556347290"
  product_url: ""
  source: bits-and-mortar    # or manual (the default)
```

Any field can be left out. A book listed here skips the search and goes straight to the confirm screen, where you can still edit it. For a one-off book you don't want to keep a record of, use **Manual entry** on the match screen instead.

### Known product links

For books that *are* on DriveThruRPG but that the search doesn't find, put a `dtrpg_urls.csv` file in the folder with the PDFs:

```csv
filename,drivethrurpg_url
Mongoose Traveller 1E - MGP3800 - Core Rulebook.pdf,https://www.drivethrurpg.com/en/product/56586/traveller-main-rulebook
Some Other Book.pdf,id:12345
```

The second column takes a product link, `id:` plus the product number, or just the number. These books are looked up directly and written without a confirm screen. If a link doesn't work (a typo, or the product was removed), that book falls back to the normal search.

## Clickable page references (hyperlinking)

| Rule set | For |
|---|---|
| `gurps` | GURPS (Steve Jackson Games) |
| `mongoose` | Mongoose Traveller (1st and 2nd edition) and 2300AD |
| `deltagreen` | Delta Green (Arc Dream Publishing) |
| `shadowrun_sixth_world` | Shadowrun Sixth World (Catalyst Game Labs) |

Each rule set follows its publisher's own way of writing references, so use it only on that publisher's books.

**What gets linked:**
- page references in the text, such as "p. 208", "pp. 12–14", or Delta Green's "see COMBAT on page 48"
- every page number in the index
- chapter references (GURPS and Mongoose), such as "Chapter 3"
- the table of contents, unless the publisher already linked it

**What's deliberately left alone:**
- **References to other books.** For example "*GURPS Magic*, p. 23", GURPS's "p. B123" (the *Basic Set*), Delta Green's "page 85 of the *Agent's Handbook*", or Shadowrun's "(p. 94, SR6)". Linking those would jump to the wrong page of *this* book.
- **Numbers outside the book's page range.**
- **Anything that's already a link.** Running it twice adds nothing.

It finds the right page from the numbers printed on the pages, so "p. 208" goes to the page that says 208, whatever the covers and front matter do to the count. A book with no readable page numbers, such as a scan, is skipped, with a note in the log.

Next to each book you'll find **`Book_link_report.csv`**, with one row per reference. It gives the page, the text, where it points, and whether it was linked or skipped (and why). It's the quickest way to check a book's links.

### Adding another publisher

`hyperlink_survey.py` drafts a rule set from a folder of one publisher's PDFs. It only reads them:

```bash
python3 hyperlink_survey.py --name pelgrane --label "Pelgrane Press" "/path/to/Pelgrane PDFs"
```

It writes a draft rule set, `pelgrane.py`, and a report, `pelgrane_survey.md`, into `hyperlink-survey-pelgrane/`. The report has a table per book, sample sentences for everything that might be a reference to another book, and a trial run showing what the draft would link.

Review the draft's list of known book titles and the samples before using it. Some calls need a person: "page 177 in THE SENECA WHIRLWIND REPORT" is a section of the same book, but "page 85 of the *Agent's Handbook*" is another book. When you're happy with it, copy it into `hyperlink_rules/`, or rerun the survey with `--install`. It then appears in `--hyperlink` and in the desktop app's dropdown.

You can also link a single book without tagging it:

```bash
python3 hyperlink_engine.py --rules gurps Book.pdf Book-linked.pdf
python3 hyperlink_engine.py --list        # the rule sets available
```

## Files the tool creates

Next to each PDF:

| File | When |
|---|---|
| `Book.pdf.bak` | a copy of the original, made the first time the book is changed. Later runs never overwrite it, so it always holds the untouched original. Rename the `.bak` back to `.pdf` to undo everything. |
| `Book.metadata.json` | with **Grimmory** metadata tagging |
| `Book.opf` | with **BookOrbit** metadata tagging |
| `Book_link_report.csv` | with hyperlinking |

In the tool's own `data/` folder:

| File | What |
|---|---|
| `preferences.yaml` | your saved API key and DriveThruRPG name |
| `manual_overrides.yaml` | your [manual overrides](#manual-overrides) |
| `library_cache.json` | your purchased library ([refresh it](#refresh-library) after buying books) |
| `catalog_cache.json` | earlier catalog search results, so repeated searches don't hit DriveThruRPG again |
| `debug/` | raw DriveThruRPG responses the tool couldn't understand ([see Troubleshooting](#something-else-went-wrong)) |

## Settings file (config.yaml)

`config.yaml` holds a few defaults. Paths are relative to where `config.yaml` itself lives.

```yaml
root: /path/to/rpg/pdfs        # the folder the desktop app starts in, and the default for rename / page-labels
data_dir: data                 # where the caches and debug files go
manual_overrides: data/manual_overrides.yaml
preferences: data/preferences.yaml
dtrpg:
  catalog_rate_limit_seconds: 1.0   # pause between catalog requests, to be polite to DriveThruRPG
```

Your API key never goes in this file. To use a different settings file: `./dtrpg-metadata-download.py --config other.yaml …`

## Command reference

```
./dtrpg-metadata-download.py [--config FILE] [-v] COMMAND …
```

| Command | Does |
|---|---|
| `gui` | open the desktop app |
| `tag FILE` / `tag --root FOLDER` | tag one book, or every book in a folder (and its subfolders) |
| `rename FILE` / `rename --root FOLDER` | rename already-tagged books to `Series - Title.pdf`; `--dry-run` to preview |
| `page-labels FILE` / `page-labels --root FOLDER` | set page labels on their own; `--dry-run` to preview |
| `preferences` | save your API key and DriveThruRPG name |

Options for `tag`:

| Option | Does |
|---|---|
| `--metadata off\|calibre\|grimmory\|bookorbit` | [what gets written](#what-gets-written-choosing-your-reader-app) (default `grimmory`) |
| `--page-labels` | [page labels](#page-labels) |
| `--rename` | [rename](#rename) after tagging |
| `--convert-images` / `--convert-grayscale` | [convert images](#convert-images-rgb-or-grayscale) to RGB, or the whole book to grayscale |
| `--back-cover-to-page-2` / `--back-cover-to-end` | [move the back cover](#move-the-back-cover) |
| `--hyperlink RULES` | [clickable page references](#clickable-page-references-hyperlinking) |
| `--background-layer` / `--remove-background` | [page backgrounds](#page-backgrounds-troll-lord-games-only) (Troll Lord Games) |
| `--refresh-library` | [re-download your library](#refresh-library) first |

`-v` shows more detail in the log.

## Troubleshooting

### A book isn't found, or the wrong one is

DriveThruRPG's search isn't fuzzy. Every word searched for must appear in the product's real title, so a filename with extra words (the publisher's name, say) can find nothing, or the wrong book. The tool strips edition codes and product numbers from filenames and also tries again without the first word, but some books still won't turn up. Then:
- paste the product's link into the match screen, or
- add it to [`dtrpg_urls.csv`](#known-product-links) so it's always found, or
- for a book DriveThruRPG doesn't sell, use [Manual entry](#picking-a-match) or a [manual override](#manual-overrides).

### A book I just bought doesn't show up

Tick **Refresh library** (or add `--refresh-library`) once.

### "No DriveThruRPG API key found"

Save one with `preferences` or the Preferences tab, or set `DTRPG_API_KEY` ([see above](#your-drivethrurpg-api-key)). If DriveThruRPG rejects it, generate a new Application Key on your account page. It isn't your password.

### BookOrbit doesn't show the series or description

With BookOrbit's default settings, use **Calibre** or **Grimmory** metadata tagging, not **BookOrbit**. See [About BookOrbit](#what-gets-written-choosing-your-reader-app).

### Page labels weren't set

The log says why. Usually it's a scan whose page numbers aren't text, a boxed set, or several books merged into one PDF. The book's existing labels are left alone.

### Grayscale conversion fails

Install [Ghostscript](#installation) and make sure `gs` works in your terminal.

### Undoing changes to a book

Delete the changed PDF and rename `Book.pdf.bak` back to `Book.pdf`. Delete its `.metadata.json`, `.opf` and link report too, if you don't want them.

### Something else went wrong

DriveThruRPG's API isn't documented, and it occasionally changes. When the tool gets a response it can't understand, it saves the raw response in `data/debug/` (with your API key removed). That's the first thing to look at, or to attach to a bug report.

## For developers

How the code fits together, and why things are done the way they are, is in [CLAUDE.md](CLAUDE.md) and [docs/HISTORY.md](docs/HISTORY.md). There are two test scripts, `tests/test_tag_tui.py` and `tests/test_gui_app.py`, which run directly.

## License

[GNU General Public License v3.0](LICENSE).
