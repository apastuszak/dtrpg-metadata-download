#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pikepdf>=8.0",
#     "rapidfuzz>=3.0",
#     "PyYAML>=6.0",
#     "requests>=2.31",
#     "lxml>=4.9",
#     "textual>=4.0",
#     "pymupdf>=1.24",
#     "pillow>=10.0",
#     "PyQt6>=6.6",
# ]
# ///
"""CLI entry point for the RPG PDF metadata pipeline.

    tag PDF_PATH | tag --root PATH [--metadata MODE] [--rename] [--refresh-library]
        Match PDF(s) by filename -- against manual_overrides.yaml, then
        <root>/dtrpg_urls.csv, then DriveThruRPG's library, then its
        public catalog -- and write the one you pick straight into it
        (in place, with a one-time .bak backup per file), via a
        full-screen terminal UI (see tag_tui.py). A single file and --root both use this UI; --root
        works through the directory one PDF at a time and asks once up
        front whether every book in the batch shares one series (if so,
        applied to all of them with no further per-book prompt).

        For each file: a candidate screen shows ranked matches (arrow
        keys + Enter to pick, or 'm' for manual entry, Escape to skip,
        'q' to quit the whole run) plus a field to paste a DriveThruRPG
        URL or type id:PRODUCT_ID for a direct lookup — shown even when
        no candidates were found at all, so that escape hatch is never
        unavailable. Manual entry is a form with a real multi-line text
        area for Description (title/publisher/series/series index/tags/
        isbn/product_url are single-line) — for titles DriveThruRPG
        doesn't have at all, without needing to pre-edit
        data/manual_overrides.yaml. Every match path (candidate pick,
        manual override, known URL, manual entry) shares one confirm
        screen before anything is actually written; known-URL matches
        skip both the candidate screen and confirm screen entirely,
        staying non-interactive end to end.

        --rename immediately renames a file (and its sidecars) to
        "<series> - <title>.pdf" right after a successful write, same
        as running the rename subcommand on it afterward. Skipped for a
        write that failed, and for any file skipped/cancelled/left
        unwritten.

        With --metadata off, tag matches nothing (no API key needed): it
        runs only the file options given, and --rename names each file
        from the metadata already in it -- e.g. `tag --root DIR
        --metadata off --page-labels --rename`.

        --refresh-library re-downloads your DriveThruRPG library listing
        first. It's cached in data/ and otherwise never refreshed, so use
        this after buying new books.

    --metadata MODE (on tag): what metadata to write.
        off       -- none; the other options (image conversion,
                     hyperlinking, page labels, ...) still run.
        calibre   -- Calibre metadata embedded in the PDF, no sidecars.
        grimmory  -- Calibre metadata plus the Grimmory .metadata.json
                     sidecar (the default).
        bookorbit -- strips ALL PDF-level metadata (full XMP packet +
                     classic Info dictionary, not just this tool's own
                     fields) and writes only the BookOrbit .opf sidecar.
                     BookOrbit reads that sidecar only if the library's
                     metadata precedence puts "OPF file" ahead of
                     "Embedded"; by default it never opens it for a PDF
                     (see pdf_writer.py).
        A sidecar the chosen mode doesn't write is removed if an earlier
        run left one. --bookorbit-mode still works and means
        --metadata bookorbit.

    --convert-images (on tag): converts every CMYK/
        grayscale/JPEG2000 image in the PDF to RGB JPEG before metadata
        is written (see image_converter.py). Off by default. Runs before
        the embedded-metadata write, never after, so PyMuPDF's save
        (a different library from pikepdf) can't disturb the Calibre XMP
        pikepdf writes.

    --convert-grayscale (on tag): converts the whole PDF
        to grayscale before metadata is written, via a vendored script
        (see rpg_grayscale.py/convert_pdf_to_grayscale.py). Requires the
        real Ghostscript binary ('gs') on PATH -- a system package, not
        something PEP 723/uv/pip can install. Off by default, and
        mutually exclusive with --convert-images (the opposite operation
        on the same images) -- the GUI offers the two as one dropdown.

    --hyperlink RULES (on tag): auto-hyperlinks in-text
        page/chapter references, the Table of Contents and the Index, via
        hyperlink_engine.py (see rpg_hyperlink.py). RULES is a publisher
        rule set -- a module name in hyperlink_rules/ (gurps, mongoose,
        deltagreen, or any added later); an unknown name lists the
        available ones. Off by default, and publisher-specific: each rule
        set is tuned for one publisher's own reference conventions. Runs
        before the embedded-metadata write, same reasoning as
        --convert-images. --hyperlink-gurps / --hyperlink-mongoose /
        --hyperlink-deltagreen still work, as hidden aliases.

    --background-layer / --remove-background (on tag):
        tags each page's full-page decorative background as a toggleable
        Optional Content Group layer, or deletes it outright, via a
        vendored script (see rpg_background_layer.py/
        castles_and_crusades_background_layer.py). Off by default,
        mutually exclusive with each other, and tuned for one specific
        publisher's PDFs (Troll Lord Games, e.g. Castles & Crusades) -- not
        a general feature. Runs first among the optional pre-write steps, before
        --convert-images/--convert-grayscale/hyperlinking, all of which
        still run before the final embedded-metadata write either way.

    --page-labels (on tag): sets the page labels a PDF
        viewer shows (see page_labels.py) -- "Cover" for the front and back
        cover, lowercase roman numerals before printed page 1, and the
        printed page number everywhere else, read automatically from page
        headers/footers. Books whose numbering can't be read confidently
        (scans, boxed sets, merged volumes) keep their existing labels.
        Written in the same final save as the metadata.

    --back-cover-to-page-2 / --back-cover-to-end (on tag):
        moves the back cover right after the front cover, or to the end
        of the book (see back_cover.py). Mutually exclusive. The back
        cover is found by its retail barcode on PDF page 2 or the last
        page; a book with a wraparound cover (Shadowrun) or no back cover
        at all is left alone, with the reason logged; with no barcode,
        tag shows you the likely page and asks. Runs first among the
        pre-write steps; bookmarks, links and existing page labels
        follow their pages.

    page-labels PDF_PATH | page-labels --root PATH [--dry-run]
        The same page labeling on its own, without tagging -- for books
        that are already tagged. Makes the usual one-time .bak backup.
        --dry-run shows the planned labels without writing anything.

    rename PDF_PATH | rename --root PATH [--dry-run]
        Rename a single already-tagged PDF, or every one under --root
        (plus its .bak/.opf/.metadata.json/_link_report.csv companions), to
        "<series> - <title>.pdf" (or just "<title>.pdf" with no series),
        read from each file's .metadata.json or .opf sidecar, or else its
        embedded metadata. Untagged files (no title anywhere) and files already named correctly are skipped;
        a computed name that collides with an existing file is skipped
        with a warning rather than overwritten. --dry-run previews
        without renaming anything.

    gui
        Launch a desktop GUI (PyQt6, see gui_app.py/gui_tag_flow.py)
        covering every subcommand above in one window. PyQt6 is a normal
        dependency (installed automatically via `uv run`, same as every
        other dependency here) — no system-level prerequisite involved.

    preferences
        View/edit your saved DriveThruRPG API key and account name (a
        small Textual app, see preferences_tui.py) — also reachable from
        the GUI's own Preferences tab. Saved to data/preferences.yaml,
        gitignored and file-permissioned to your account only, kept
        separate from config.yaml specifically because that file is
        meant to be safe to share/commit and this one isn't. An existing
        DTRPG_API_KEY environment variable always takes priority over
        what's saved here.

Config defaults (root, cache locations) come from
config.yaml; CLI flags override them. The DriveThruRPG API key comes
from DTRPG_API_KEY in the environment, or from `preferences` (above) if
that's not set.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml
from textual.logging import TextualHandler

from dtrpg_client import DtrpgClient
from matcher import load_known_urls, load_manual_overrides, scan_pdfs
from pdf_writer import DEFAULT_METADATA, METADATA_MODES, write_page_labels_only
from preferences import DEFAULT_PREFERENCES_PATH, load_preferences, resolve_api_key
from renamer import apply_rename, plan_rename
from rpg_hyperlink import rule_sets
from tag_tui import TagApp

logger = logging.getLogger("dtrpg-metadata-download")

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# config.yaml's own relative paths (its shipped "data/manual_overrides.yaml" etc.)
# are documented/intended relative to config.yaml's own location, not to
# whatever directory the tool happens to be invoked from -- deliberately
# excludes "root", which behaves like a CLI argument (relative to the
# caller's own cwd), not an internal data file.
_CONFIG_RELATIVE_PATH_KEYS = (
    "data_dir",
    "manual_overrides",
    "preferences",
)


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # Verified this was a real bug: running `tag` from inside a PDF
    # library folder (rather than this project's own directory) silently
    # created a stray data/ subfolder there and reported "No DriveThruRPG
    # API key found" even with one already saved in the real
    # data/preferences.yaml next to config.yaml -- every one of these
    # values was being resolved against the current working directory
    # instead of config.yaml's own location.
    config_dir = path.resolve().parent
    for key in _CONFIG_RELATIVE_PATH_KEYS:
        value = config.get(key)
        if value and not Path(value).is_absolute():
            config[key] = str(config_dir / value)
    return config


def build_client(config: dict) -> DtrpgClient:
    prefs = load_preferences(config.get("preferences", DEFAULT_PREFERENCES_PATH))
    api_key = resolve_api_key(prefs)
    if not api_key:
        sys.exit("No DriveThruRPG API key found. Either export DTRPG_API_KEY "
                 "(an Application Key from your DriveThruRPG account page), e.g.:\n"
                 "  export DTRPG_API_KEY=...\n"
                 "or save one with:\n"
                 "  ./dtrpg-metadata-download.py preferences")
    return DtrpgClient(
        api_key=api_key,
        cache_dir=config.get("data_dir", "data"),
        catalog_rate_limit_seconds=config.get("dtrpg", {}).get("catalog_rate_limit_seconds", 1.0),
    )


def _rename_one(pdf_path: Path, dry_run: bool) -> str:
    """Rename a single PDF (plus its .bak/.opf/.metadata.json/_link_report.csv companions),
    print the outcome, and return 'renamed'/'skipped'/'failed' for the
    caller's tally."""
    plan = plan_rename(pdf_path)
    result = apply_rename(plan, dry_run=dry_run)
    if not result.success:
        if plan.reason is not None:
            print(f"SKIP: {pdf_path.name} ({plan.reason})")
            return "skipped"
        print(f"FAILED: {pdf_path.name}: {result.message}")
        return "failed"
    verb = "Would rename" if dry_run else "Renamed"
    print(f"{verb}: {pdf_path.name} -> {result.new_pdf.name}")
    return "renamed"


def cmd_rename(args: argparse.Namespace, config: dict) -> None:
    if args.pdf:
        path = Path(args.pdf)
        if not path.exists():
            sys.exit(f"File not found: {path}")
        if path.suffix.lower() != ".pdf":
            sys.exit(f"Not a PDF: {path}")
        _rename_one(path, args.dry_run)
        return

    root = args.root or config.get("root")
    if not root:
        sys.exit("No --root given and no 'root' set in config.yaml")

    pdfs = scan_pdfs(root)
    if not pdfs:
        print(f"No PDFs found under {root}")
        return

    counts = {"renamed": 0, "skipped": 0, "failed": 0}
    for pdf_path in pdfs:
        counts[_rename_one(pdf_path, args.dry_run)] += 1

    summary = f"{counts['renamed']} renamed, {counts['skipped']} skipped, {counts['failed']} failed"
    if args.dry_run:
        summary += " (dry run, nothing changed)"
    print(f"\n{summary}")


def _label_one(pdf_path: Path, dry_run: bool, show_ranges: bool) -> str:
    """Label one PDF, print the outcome, and return 'labeled'/'skipped'/
    'failed' for the caller's tally."""
    result = write_page_labels_only(pdf_path, dry_run=dry_run)
    if result.status == "skipped":
        print(f"SKIP: {pdf_path.name} ({result.message})")
    elif result.status == "failed":
        print(f"FAILED: {pdf_path.name}: {result.message}")
    else:
        verb = "Would label" if dry_run else "Labeled"
        print(f"{verb}: {pdf_path.name} ({result.message})")
        if show_ranges:
            for line in result.plan.describe():
                print(f"    {line}")
    return result.status


def cmd_page_labels(args: argparse.Namespace, config: dict) -> None:
    if args.pdf:
        path = Path(args.pdf)
        if not path.exists():
            sys.exit(f"File not found: {path}")
        if path.suffix.lower() != ".pdf":
            sys.exit(f"Not a PDF: {path}")
        _label_one(path, args.dry_run, show_ranges=True)
        return

    root = args.root or config.get("root")
    if not root:
        sys.exit("No --root given and no 'root' set in config.yaml")

    pdfs = scan_pdfs(root)
    if not pdfs:
        print(f"No PDFs found under {root}")
        return

    counts = {"labeled": 0, "skipped": 0, "failed": 0}
    for pdf_path in pdfs:
        counts[_label_one(pdf_path, args.dry_run, show_ranges=args.dry_run)] += 1

    summary = f"{counts['labeled']} labeled, {counts['skipped']} skipped, {counts['failed']} failed"
    if args.dry_run:
        summary += " (dry run, nothing changed)"
    print(f"\n{summary}")


def cmd_tag(args: argparse.Namespace, config: dict) -> None:
    """Thin dispatch into tag_tui.TagApp -- the actual interactive flow
    (candidate list, manual entry, confirm-then-write, --rename) lives
    there now. Everything below (guard clauses, loading overrides/known
    URLs, resolving the PDF list, building the DtrpgClient) is
    unchanged from before the TUI rewrite -- a missing DTRPG_API_KEY
    still sys.exit()s here, on a normal terminal, before the TUI ever
    starts. With --metadata off nothing is matched, so no client (or API
    key) is needed."""
    needs_client = args.metadata != "off"
    manual_overrides_path = Path(config.get("manual_overrides", "data/manual_overrides.yaml"))
    manual_overrides = load_manual_overrides(manual_overrides_path)

    if args.root:
        root = Path(args.root)
        pdfs = scan_pdfs(root)
        if not pdfs:
            print(f"No PDFs found under {root}")
            return
        client = build_client(config) if needs_client else None
        known_urls = load_known_urls(root)
    else:
        path = Path(args.pdf)
        if not path.exists():
            sys.exit(f"File not found: {path}")
        if path.suffix.lower() != ".pdf":
            sys.exit(f"Not a PDF: {path}")
        pdfs = [path]
        client = build_client(config) if needs_client else None
        known_urls = load_known_urls(path.parent)

    TagApp(
        pdfs=pdfs,
        client=client,
        manual_overrides=manual_overrides,
        known_urls=known_urls,
        metadata=args.metadata,
        convert_images=args.convert_images,
        convert_grayscale=args.convert_grayscale,
        background_layer=args.background_layer,
        remove_background=args.remove_background,
        hyperlink=args.hyperlink,
        page_labels=args.page_labels,
        back_cover_to_page_2=args.back_cover_to_page_2,
        back_cover_to_end=args.back_cover_to_end,
        refresh_library=args.refresh_library,
        rename=args.rename,
        root_mode=bool(args.root),
    ).run()


def cmd_gui(args: argparse.Namespace, config: dict) -> None:
    """Launch the desktop GUI (gui_app.py), which covers every subcommand
    above in one window. PyQt6 is imported lazily, here and only here, so
    that a missing GUI dependency only affects `gui` and not every other
    subcommand -- but unlike the Tk-based GUI this replaced, PyQt6 is an
    ordinary PEP 723/uv/pip dependency, not a system-level Python build
    feature, so there's no OS-specific install step to walk anyone
    through: under `uv run --script` (the primary supported path), uv
    resolves and installs it automatically and reports its own failures
    clearly. This guard only exists for the requirements.txt+venv path,
    where forgetting `pip install -r requirements.txt` after this
    dependency was added is a plausible, easy mistake."""
    try:
        import PyQt6.QtWidgets  # noqa: F401
    except ModuleNotFoundError:
        sys.exit(
            "PyQt6 is not installed in this Python.\n"
            "Run this via 'uv run ./dtrpg-metadata-download.py gui' (recommended, "
            "installs it automatically) or 'pip install -r requirements.txt' in your virtualenv."
        )
    import gui_app

    gui_app.run_gui(config)


def cmd_preferences(args: argparse.Namespace, config: dict) -> None:
    """Launch a small Textual app (preferences_tui.py) to view/edit the
    saved API key and DriveThruRPG name -- see preferences.py's module
    docstring for why these live in their own gitignored file rather
    than config.yaml. No DTRPG_API_KEY/build_client() guard here, unlike
    every other subcommand -- this one exists specifically to let you set
    that up in the first place."""
    from preferences_tui import PreferencesApp

    PreferencesApp(Path(config.get("preferences", DEFAULT_PREFERENCES_PATH))).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="RPG PDF metadata pipeline")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    metadata_help = (
        "What metadata to write: off (none; other options still run), calibre (embedded in the PDF), "
        "grimmory (embedded plus a Grimmory .metadata.json sidecar), or bookorbit (strip all PDF "
        "metadata, write only a BookOrbit .opf sidecar; BookOrbit reads it only with 'OPF file' ahead of "
        f"'Embedded' in the library's metadata precedence). Default: {DEFAULT_METADATA}"
    )

    def add_metadata_options(subparser: argparse.ArgumentParser) -> None:
        group = subparser.add_mutually_exclusive_group()
        group.add_argument("--metadata", metavar="MODE", choices=METADATA_MODES, default=DEFAULT_METADATA,
                           help=metadata_help)
        # The flag from before there was a choice.
        group.add_argument("--bookorbit-mode", dest="metadata", action="store_const", const="bookorbit",
                           help=argparse.SUPPRESS)
    convert_images_help = (
        "Convert CMYK/grayscale/JPEG2000 images in the PDF to RGB JPEG before writing metadata"
    )
    convert_grayscale_help = (
        "Convert the whole PDF to grayscale before writing metadata, via a vendored script "
        "(see rpg_grayscale.py; requires Ghostscript ('gs') on PATH). Mutually exclusive with "
        "--convert-images."
    )
    hyperlink_help = (
        "Auto-hyperlink in-text page/chapter references, the Table of Contents and the Index, using "
        "publisher rule set RULES (a module in hyperlink_rules/: gurps, mongoose, deltagreen, ...). "
        "An unknown name lists the available ones."
    )

    def hyperlink_rule_set(name: str) -> str:
        names = [r.name for r in rule_sets()]
        if name not in names:
            raise argparse.ArgumentTypeError(
                f"unknown rule set {name!r} (available: {', '.join(names) or 'none'})")
        return name

    def add_hyperlink_options(subparser: argparse.ArgumentParser) -> None:
        group = subparser.add_mutually_exclusive_group()
        group.add_argument("--hyperlink", metavar="RULES", type=hyperlink_rule_set, help=hyperlink_help)
        # The per-publisher flags from before rule sets were discoverable.
        for alias, rules in (("--hyperlink-gurps", "gurps"), ("--hyperlink-mongoose", "mongoose"),
                             ("--hyperlink-deltagreen", "deltagreen")):
            group.add_argument(alias, dest="hyperlink", action="store_const", const=rules,
                               help=argparse.SUPPRESS)

    background_layer_help = (
        "Tag each page's full-page decorative background as a toggleable Optional Content Group layer, "
        "via a vendored script (see rpg_background_layer.py). For Troll Lord Games PDFs "
        "(Castles & Crusades) only. Mutually exclusive with --remove-background."
    )
    remove_background_help = (
        "Delete each page's full-page decorative background outright, via the same vendored script as "
        "--background-layer. For Troll Lord Games PDFs (Castles & Crusades) only. Mutually exclusive with "
        "--background-layer."
    )

    page_labels_help = (
        "Set the page labels a PDF viewer shows: 'Cover' for the covers, i, ii, ... before printed "
        "page 1, then the printed page numbers, read automatically from headers/footers. Books whose "
        "numbering can't be read confidently keep their existing labels."
    )

    back_cover_to_page_2_help = (
        "For a back cover that's currently the last page: move it to right after the front cover. "
        "Doesn't search the book -- only the last page is considered, confirmed by a barcode or by "
        "asking you. Wraparound covers are left alone. Mutually exclusive with --back-cover-to-end."
    )
    back_cover_to_end_help = (
        "For a back cover that's currently PDF page 2: move it to the end of the book. Doesn't search "
        "the book -- only page 2 is considered, confirmed by a barcode or by asking you. Mutually "
        "exclusive with --back-cover-to-page-2."
    )

    def add_back_cover_options(subparser: argparse.ArgumentParser) -> None:
        group = subparser.add_mutually_exclusive_group()
        group.add_argument("--back-cover-to-page-2", action="store_true", help=back_cover_to_page_2_help)
        group.add_argument("--back-cover-to-end", action="store_true", help=back_cover_to_end_help)

    tag_parser = subparsers.add_parser("tag", help="Match and tag PDF(s) interactively")
    tag_group = tag_parser.add_mutually_exclusive_group(required=True)
    tag_group.add_argument("pdf", nargs="?", help="Path to a single PDF file to match and tag")
    tag_group.add_argument("--root", help="Process every PDF under this directory instead of a single file")
    add_metadata_options(tag_parser)
    tag_color_group = tag_parser.add_mutually_exclusive_group()
    tag_color_group.add_argument("--convert-images", action="store_true", help=convert_images_help)
    tag_color_group.add_argument("--convert-grayscale", action="store_true", help=convert_grayscale_help)
    add_hyperlink_options(tag_parser)
    tag_bg_group = tag_parser.add_mutually_exclusive_group()
    tag_bg_group.add_argument("--background-layer", action="store_true", help=background_layer_help)
    tag_bg_group.add_argument("--remove-background", action="store_true", help=remove_background_help)
    tag_parser.add_argument("--page-labels", action="store_true", help=page_labels_help)
    add_back_cover_options(tag_parser)
    tag_parser.add_argument(
        "--rename", action="store_true",
        help="Also rename the file (and its sidecars) to 'Series - Title' immediately after a successful write",
    )
    tag_parser.add_argument(
        "--refresh-library", action="store_true",
        help="Re-download your DriveThruRPG library listing first (it's cached and otherwise never refreshed)",
    )
    tag_parser.set_defaults(func=cmd_tag)

    rename_parser = subparsers.add_parser("rename", help="Rename tagged PDF(s) (and sidecars) to 'Series - Title'")
    rename_group = rename_parser.add_mutually_exclusive_group()
    rename_group.add_argument("pdf", nargs="?", help="Path to a single already-tagged PDF file to rename")
    rename_group.add_argument("--root", help="Rename every already-tagged PDF under this directory instead of a single file")
    rename_parser.add_argument("--dry-run", action="store_true", help="Preview renames without changing anything")
    rename_parser.set_defaults(func=cmd_rename)

    labels_parser = subparsers.add_parser(
        "page-labels", help="Set page labels (Cover / i, ii / printed page numbers) without tagging"
    )
    labels_group = labels_parser.add_mutually_exclusive_group()
    labels_group.add_argument("pdf", nargs="?", help="Path to a single PDF file to label")
    labels_group.add_argument("--root", help="Label every PDF under this directory instead of a single file")
    labels_parser.add_argument("--dry-run", action="store_true", help="Show the planned labels without writing anything")
    labels_parser.set_defaults(func=cmd_page_labels)

    gui_parser = subparsers.add_parser(
        "gui", help="Launch the desktop GUI (PyQt6), covering every subcommand above"
    )
    gui_parser.set_defaults(func=cmd_gui)

    preferences_parser = subparsers.add_parser(
        "preferences", help="View/edit your saved DriveThruRPG API key and account name"
    )
    preferences_parser.set_defaults(func=cmd_preferences)

    args = parser.parse_args()
    # TextualHandler routes to the active Textual app's own log (instead of
    # stderr) whenever one is running -- tag's TUI takes over the terminal
    # via an alternate screen, and a plain StreamHandler writing to stderr
    # mid-run (e.g. a logger.warning() from matcher.py/dtrpg_client.py)
    # would corrupt the display. It falls back to plain stderr when no app
    # is active, so this is a safe universal replacement, not just a
    # tag-specific special case.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[TextualHandler()],
    )

    config = load_config(Path(args.config))
    args.func(args, config)


if __name__ == "__main__":
    main()
