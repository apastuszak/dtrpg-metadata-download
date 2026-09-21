#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pikepdf>=8.0",
#     "rapidfuzz>=3.0",
#     "PyYAML>=6.0",
#     "requests>=2.31",
#     "lxml>=4.9",
#     "textual>=0.60",
#     "pymupdf>=1.24",
#     "pillow>=10.0",
#     "PyQt6>=6.6",
# ]
# ///
"""CLI entry point for the RPG PDF metadata pipeline.

    scan --root PATH [--refresh-library] [--apply-review]
        Build/update data/review.csv by matching PDFs under --root
        against manual_overrides.yaml, then <root>/dtrpg_urls.csv, then
        DriveThruRPG's library, then its public catalog.

    review-status
        Print match counts by status from data/review.csv.

    write-pdfs --root PATH [--bookorbit-mode]
        Write metadata for every "approved"/"auto-accepted" row in
        data/review.csv into the corresponding PDF (in place, with a
        one-time .bak backup per file).

    all --root PATH [--refresh-library] [--apply-review] [--bookorbit-mode]
        Run scan, then write-pdfs. Still gated by review.csv status —
        rows left at needs-review/no-match are not written.

    tag PDF_PATH | tag --root PATH [--bookorbit-mode] [--rename]
        Match PDF(s) by filename and write the one you pick straight
        into it, via a full-screen terminal UI (see tag_tui.py) — no
        review.csv. A single file and --root both use this UI; --root
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

    --bookorbit-mode (on write-pdfs/all/tag): instead of writing Calibre
        metadata into the PDF, strips ALL PDF-level metadata (full XMP
        packet + classic Info dictionary, not just this tool's own
        fields) and writes the BookOrbit .opf sidecar. Off by default,
        in which case .opf is not written at all (BookOrbit's scanner
        never opens it when the embedded PDF has any metadata, which it
        normally does — see pdf_writer.py). The Grimmory .metadata.json
        sidecar is unaffected either way.

    --convert-images (on write-pdfs/all/tag): converts every CMYK/
        grayscale/JPEG2000 image in the PDF to RGB JPEG before metadata
        is written (see image_converter.py). Off by default. Runs before
        the embedded-metadata write, never after, so PyMuPDF's save
        (a different library from pikepdf) can't disturb the Calibre XMP
        pikepdf writes.

    --convert-grayscale (on write-pdfs/all/tag): converts the whole PDF
        to grayscale before metadata is written, via a separate sibling
        project's script (see rpg_grayscale.py) whose path comes from
        config.yaml's grayscale_script key. Requires the real Ghostscript
        binary ('gs') on PATH -- a system package, not something PEP
        723/uv/pip can install. Off by default, and mutually exclusive
        with --convert-images (the opposite operation on the same
        images) -- the GUI enforces this by unchecking one when the
        other is checked.

    --hyperlink-gurps / --hyperlink-mongoose (on write-pdfs/tag):
        auto-hyperlinks in-text page/chapter references (see
        rpg_hyperlink.py), via a separate sibling project's script whose
        path comes from config.yaml's gurps_hyperlink_script/
        mongoose_hyperlink_script keys respectively. Off by default, and
        a machine-specific integration -- tuned for one publisher's own
        reference conventions each (GURPS / Mongoose Publishing's
        Traveller line), not a general feature. Both run before the
        embedded-metadata write, same reasoning as --convert-images.

    rename PDF_PATH | rename --root PATH [--dry-run]
        Rename a single already-tagged PDF, or every one under --root
        (plus its .bak/.opf/.metadata.json sidecars), to
        "<series> - <title>.pdf" (or just "<title>.pdf" with no series),
        read from each file's .metadata.json sidecar. Untagged files (no
        sidecar/no title) and files already named correctly are skipped;
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

Config defaults (root, thresholds, cache locations) come from
config.yaml; CLI flags override them. The DriveThruRPG API key comes
from DTRPG_API_KEY in the environment, or from `preferences` (above) if
that's not set.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path

import yaml
from textual.logging import TextualHandler

from dtrpg_client import DtrpgClient
from matcher import load_known_urls, load_manual_overrides, run_scan_batch, scan_pdfs
from pdf_writer import write_approved
from preferences import DEFAULT_PREFERENCES_PATH, load_preferences, resolve_api_key
from renamer import apply_rename, plan_rename
from review import load_review, save_review
from rpg_grayscale import DEFAULT_GRAYSCALE_SCRIPT
from rpg_hyperlink import DEFAULT_GURPS_HYPERLINK_SCRIPT, DEFAULT_MONGOOSE_HYPERLINK_SCRIPT
from tag_tui import TagApp

logger = logging.getLogger("dtrpg-metadata-download")

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


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


def cmd_scan(args: argparse.Namespace, config: dict) -> None:
    root = args.root or config.get("root")
    if not root:
        sys.exit("No --root given and no 'root' set in config.yaml")

    review_csv = Path(args.review_csv or config.get("review_csv", "data/review.csv"))
    manual_overrides_path = Path(config.get("manual_overrides", "data/manual_overrides.yaml"))
    thresholds = config.get("matching", {})

    client = build_client(config)
    if args.refresh_library:
        client.pull_library(refresh=True)

    manual_overrides = load_manual_overrides(manual_overrides_path)
    known_urls = load_known_urls(root)
    existing_rows = load_review(review_csv)

    if args.apply_review:
        logger.info("--apply-review: matching only new file(s) not already in review.csv")

    merged = run_scan_batch(
        root, client, manual_overrides, known_urls, existing_rows, thresholds,
        apply_review=args.apply_review,
        progress=lambda i, total, name: logger.info("[%d/%d] Matching %s", i, total, name),
    )
    save_review(review_csv, merged)

    counts = Counter(row.status for row in merged)
    print(f"Wrote {review_csv} ({len(merged)} rows)")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")


def cmd_review_status(args: argparse.Namespace, config: dict) -> None:
    review_csv = Path(args.review_csv or config.get("review_csv", "data/review.csv"))
    rows = load_review(review_csv)
    if not rows:
        print(f"No rows in {review_csv} (run 'scan' first)")
        return
    counts = Counter(row.status for row in rows)
    print(f"{review_csv}: {len(rows)} rows")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")


def cmd_write_pdfs(args: argparse.Namespace, config: dict) -> None:
    root = args.root or config.get("root")
    if not root:
        sys.exit("No --root given and no 'root' set in config.yaml")

    review_csv = Path(args.review_csv or config.get("review_csv", "data/review.csv"))
    rows = load_review(review_csv)
    approved = [r for r in rows if r.is_approved()]
    if not approved:
        print("No approved/auto-accepted rows to write.")
        return

    results = write_approved(
        rows, root, bookorbit_mode=args.bookorbit_mode, convert_images=args.convert_images,
        convert_grayscale=args.convert_grayscale,
        grayscale_script=config.get("grayscale_script", DEFAULT_GRAYSCALE_SCRIPT),
        hyperlink_gurps=args.hyperlink_gurps,
        gurps_hyperlink_script=config.get("gurps_hyperlink_script", DEFAULT_GURPS_HYPERLINK_SCRIPT),
        hyperlink_mongoose=args.hyperlink_mongoose,
        mongoose_hyperlink_script=config.get("mongoose_hyperlink_script", DEFAULT_MONGOOSE_HYPERLINK_SCRIPT),
    )
    succeeded = sum(1 for r in results if r.success)
    print(f"Wrote metadata to {succeeded}/{len(results)} approved files")
    for r in results:
        if not r.success:
            print(f"  FAILED: {r.filename}: {r.message}")


def cmd_all(args: argparse.Namespace, config: dict) -> None:
    cmd_scan(args, config)
    cmd_write_pdfs(args, config)


def _rename_one(pdf_path: Path, dry_run: bool) -> str:
    """Rename a single PDF (plus its .bak/.opf/.metadata.json companions),
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


def cmd_tag(args: argparse.Namespace, config: dict) -> None:
    """Thin dispatch into tag_tui.TagApp -- the actual interactive flow
    (candidate list, manual entry, confirm-then-write, --rename) lives
    there now. Everything below (guard clauses, loading overrides/known
    URLs/thresholds, resolving the PDF list, building the DtrpgClient) is
    unchanged from before the TUI rewrite -- a missing DTRPG_API_KEY
    still sys.exit()s here, on a normal terminal, before the TUI ever
    starts."""
    manual_overrides_path = Path(config.get("manual_overrides", "data/manual_overrides.yaml"))
    manual_overrides = load_manual_overrides(manual_overrides_path)
    thresholds = config.get("matching", {})

    if args.root:
        root = Path(args.root)
        pdfs = scan_pdfs(root)
        if not pdfs:
            print(f"No PDFs found under {root}")
            return
        client = build_client(config)
        known_urls = load_known_urls(root)
    else:
        path = Path(args.pdf)
        if not path.exists():
            sys.exit(f"File not found: {path}")
        if path.suffix.lower() != ".pdf":
            sys.exit(f"Not a PDF: {path}")
        pdfs = [path]
        client = build_client(config)
        known_urls = load_known_urls(path.parent)

    TagApp(
        pdfs=pdfs,
        client=client,
        manual_overrides=manual_overrides,
        known_urls=known_urls,
        thresholds=thresholds,
        bookorbit_mode=args.bookorbit_mode,
        convert_images=args.convert_images,
        convert_grayscale=args.convert_grayscale,
        grayscale_script=config.get("grayscale_script", DEFAULT_GRAYSCALE_SCRIPT),
        hyperlink_gurps=args.hyperlink_gurps,
        gurps_hyperlink_script=config.get("gurps_hyperlink_script", DEFAULT_GURPS_HYPERLINK_SCRIPT),
        hyperlink_mongoose=args.hyperlink_mongoose,
        mongoose_hyperlink_script=config.get("mongoose_hyperlink_script", DEFAULT_MONGOOSE_HYPERLINK_SCRIPT),
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

    scan_parser = subparsers.add_parser("scan", help="Match PDFs and build/update review.csv")
    scan_parser.add_argument("--root", help="Root folder of RPG PDFs (overrides config.yaml)")
    scan_parser.add_argument("--review-csv", help="Path to review.csv (overrides config.yaml)")
    scan_parser.add_argument("--refresh-library", action="store_true", help="Re-pull the DriveThruRPG library instead of using the cache")
    scan_parser.add_argument("--apply-review", action="store_true", help="Only match files not already present in review.csv")
    scan_parser.set_defaults(func=cmd_scan)

    status_parser = subparsers.add_parser("review-status", help="Print review.csv counts by status")
    status_parser.add_argument("--review-csv", help="Path to review.csv (overrides config.yaml)")
    status_parser.set_defaults(func=cmd_review_status)

    bookorbit_mode_help = (
        "Strip all PDF-level metadata instead of writing Calibre metadata, "
        "and write the BookOrbit .opf sidecar (skipped otherwise)"
    )
    convert_images_help = (
        "Convert CMYK/grayscale/JPEG2000 images in the PDF to RGB JPEG before writing metadata"
    )
    convert_grayscale_help = (
        "Convert the whole PDF to grayscale before writing metadata, via a separate sibling script "
        "(see rpg_grayscale.py; path configured by grayscale_script in config.yaml; requires Ghostscript "
        "('gs') on PATH). Mutually exclusive with --convert-images."
    )
    hyperlink_gurps_help = (
        "Auto-hyperlink in-text page/chapter references via a separate sibling GURPS-specific script "
        "(see rpg_hyperlink.py; path configured by gurps_hyperlink_script in config.yaml)"
    )
    hyperlink_mongoose_help = (
        "Auto-hyperlink in-text page/chapter references via a separate sibling Mongoose-Traveller-"
        "specific script (see rpg_hyperlink.py; path configured by mongoose_hyperlink_script in config.yaml)"
    )

    write_parser = subparsers.add_parser("write-pdfs", help="Write approved metadata into PDFs")
    write_parser.add_argument("--root", help="Root folder of RPG PDFs (overrides config.yaml)")
    write_parser.add_argument("--review-csv", help="Path to review.csv (overrides config.yaml)")
    write_parser.add_argument("--bookorbit-mode", action="store_true", help=bookorbit_mode_help)
    write_color_group = write_parser.add_mutually_exclusive_group()
    write_color_group.add_argument("--convert-images", action="store_true", help=convert_images_help)
    write_color_group.add_argument("--convert-grayscale", action="store_true", help=convert_grayscale_help)
    write_parser.add_argument("--hyperlink-gurps", action="store_true", help=hyperlink_gurps_help)
    write_parser.add_argument("--hyperlink-mongoose", action="store_true", help=hyperlink_mongoose_help)
    write_parser.set_defaults(func=cmd_write_pdfs)

    all_parser = subparsers.add_parser("all", help="Run scan, then write-pdfs")
    all_parser.add_argument("--root", help="Root folder of RPG PDFs (overrides config.yaml)")
    all_parser.add_argument("--review-csv", help="Path to review.csv (overrides config.yaml)")
    all_parser.add_argument("--refresh-library", action="store_true")
    all_parser.add_argument("--apply-review", action="store_true")
    all_parser.add_argument("--bookorbit-mode", action="store_true", help=bookorbit_mode_help)
    all_parser.add_argument("--hyperlink-gurps", action="store_true", help=hyperlink_gurps_help)
    all_parser.add_argument("--hyperlink-mongoose", action="store_true", help=hyperlink_mongoose_help)
    all_color_group = all_parser.add_mutually_exclusive_group()
    all_color_group.add_argument("--convert-images", action="store_true", help=convert_images_help)
    all_color_group.add_argument("--convert-grayscale", action="store_true", help=convert_grayscale_help)
    all_parser.set_defaults(func=cmd_all)

    tag_parser = subparsers.add_parser("tag", help="Match and tag PDF(s) interactively, no review.csv")
    tag_group = tag_parser.add_mutually_exclusive_group(required=True)
    tag_group.add_argument("pdf", nargs="?", help="Path to a single PDF file to match and tag")
    tag_group.add_argument("--root", help="Process every PDF under this directory instead of a single file")
    tag_parser.add_argument("--bookorbit-mode", action="store_true", help=bookorbit_mode_help)
    tag_color_group = tag_parser.add_mutually_exclusive_group()
    tag_color_group.add_argument("--convert-images", action="store_true", help=convert_images_help)
    tag_color_group.add_argument("--convert-grayscale", action="store_true", help=convert_grayscale_help)
    tag_parser.add_argument("--hyperlink-gurps", action="store_true", help=hyperlink_gurps_help)
    tag_parser.add_argument("--hyperlink-mongoose", action="store_true", help=hyperlink_mongoose_help)
    tag_parser.add_argument(
        "--rename", action="store_true",
        help="Also rename the file (and its sidecars) to 'Series - Title' immediately after a successful write",
    )
    tag_parser.set_defaults(func=cmd_tag)

    rename_parser = subparsers.add_parser("rename", help="Rename tagged PDF(s) (and sidecars) to 'Series - Title'")
    rename_group = rename_parser.add_mutually_exclusive_group()
    rename_group.add_argument("pdf", nargs="?", help="Path to a single already-tagged PDF file to rename")
    rename_group.add_argument("--root", help="Rename every already-tagged PDF under this directory instead of a single file")
    rename_parser.add_argument("--dry-run", action="store_true", help="Preview renames without changing anything")
    rename_parser.set_defaults(func=cmd_rename)

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
