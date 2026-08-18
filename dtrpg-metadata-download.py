#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pikepdf>=8.0",
#     "rapidfuzz>=3.0",
#     "PyYAML>=6.0",
#     "requests>=2.31",
#     "lxml>=4.9",
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
        Match PDF(s) by filename, show ranked candidates per file, and
        write the one you pick straight into it — interactively, no
        review.csv. With --root, loops over every PDF under that
        directory one at a time (type 'q' at any prompt to stop early).
        --root also asks once up front whether every book in the batch
        shares one series — if so, that name is applied to all of them
        with no further prompting; otherwise series is asked per book
        for candidate-pick/manual-override matches (dtrpg_urls.csv
        matches never get a series prompt either way, batch answer or
        not — that path is deliberately non-interactive end to end).
        At any candidate prompt (or when no candidates are found at
        all), type 'm' (or answer yes when offered) to type in title/
        publisher/series/description/tags/isbn/product_url by hand
        instead — for titles DriveThruRPG doesn't have at all, without
        needing to pre-edit data/manual_overrides.yaml. Leaving Title
        blank cancels back out.
        --rename immediately renames a file (and its sidecars) to
        "<series> - <title>.pdf" right after a successful write, same
        as running the rename subcommand on it afterward — for every
        match path (candidate-pick, manual override, known URL, or
        manual entry). Skipped for a write that failed, and for any
        file skipped/cancelled/left unwritten.

    --bookorbit-mode (on write-pdfs/all/tag): instead of writing Calibre
        metadata into the PDF, strips ALL PDF-level metadata (full XMP
        packet + classic Info dictionary, not just this tool's own
        fields) and writes the BookOrbit .opf sidecar. Off by default,
        in which case .opf is not written at all (BookOrbit's scanner
        never opens it when the embedded PDF has any metadata, which it
        normally does — see pdf_writer.py). The Grimmory .metadata.json
        sidecar is unaffected either way.

    rename PDF_PATH | rename --root PATH [--dry-run]
        Rename a single already-tagged PDF, or every one under --root
        (plus its .bak/.opf/.metadata.json sidecars), to
        "<series> - <title>.pdf" (or just "<title>.pdf" with no series),
        read from each file's .metadata.json sidecar. Untagged files (no
        sidecar/no title) and files already named correctly are skipped;
        a computed name that collides with an existing file is skipped
        with a warning rather than overwritten. --dry-run previews
        without renaming anything.

Config defaults (root, thresholds, cache locations) come from
config.yaml; CLI flags override them. DTRPG_API_KEY must be set in the
environment (an Application Key from the DriveThruRPG account page).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

from dtrpg_client import DtrpgClient
from matcher import (
    extract_product_id,
    find_candidates,
    load_known_urls,
    load_manual_overrides,
    match_file,
    row_from_match,
    scan_pdfs,
)
from pdf_writer import write_approved, write_metadata
from provenance import ProductMetadata, Source, Status
from renamer import apply_rename, plan_rename
from review import ReviewRow, load_review, merge_by_filename, save_review

logger = logging.getLogger("dtrpg-metadata-download")

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_client(config: dict) -> DtrpgClient:
    api_key = os.environ.get("DTRPG_API_KEY")
    if not api_key:
        sys.exit("DTRPG_API_KEY is not set. Generate an Application Key on your "
                 "DriveThruRPG account page and export it, e.g.:\n"
                 "  export DTRPG_API_KEY=...")
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
    existing_filenames = {row.filename for row in existing_rows}

    pdfs = scan_pdfs(root)
    if args.apply_review:
        pdfs = [p for p in pdfs if p.name not in existing_filenames]
        logger.info("--apply-review: matching only %d new file(s) not already in review.csv", len(pdfs))

    fresh_rows = []
    for i, pdf_path in enumerate(pdfs, 1):
        logger.info("[%d/%d] Matching %s", i, len(pdfs), pdf_path.name)
        fresh_rows.append(
            match_file(
                pdf_path,
                client,
                manual_overrides,
                known_urls,
                high_confidence=thresholds.get("high_confidence_threshold", 90.0),
                review_floor=thresholds.get("review_floor_threshold", 70.0),
            )
        )

    merged = merge_by_filename(existing_rows, fresh_rows)
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

    results = write_approved(rows, root, bookorbit_mode=args.bookorbit_mode)
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


def _print_candidate(index: int | None, meta, score: float | None) -> None:
    label = f"[{index}] " if index is not None else ""
    score_part = f"({score:.1f}) " if score is not None else ""
    print(f"  {label}{score_part}{meta.title}")
    if meta.publisher:
        print(f"        publisher: {meta.publisher}")
    if meta.authors:
        print(f"        authors:   {meta.authors_str()}")
    if meta.series:
        print(f"        series:    {meta.series} #{meta.series_index}")
    print(f"        source:    {meta.source.value if hasattr(meta.source, 'value') else meta.source}")


def _apply_default_series(row: ReviewRow, default_series: str | None) -> None:
    """Apply a batch-wide series default, if any, without ever prompting.

    `default_series` being not-None means the user already answered the
    batch-wide "same series?" question in `cmd_tag`'s --root path — that
    holds even if they left the series name itself blank, which must
    still count as "don't ask me again", not "fall back to per-book
    prompting". Shared by `_prompt_series` and the known_urls branch of
    `_tag_one`, which must stay non-interactive regardless of batch state.
    """
    if not row.series and default_series:
        row.series = default_series


def _prompt_series(row: ReviewRow, default_series: str | None = None) -> None:
    """DriveThruRPG has no structured series field, so matched rows never
    come with one pre-filled — ask once, here, rather than requiring a
    manual_overrides.yaml edit for every book that's part of a series.

    `default_series` not being None means the batch-wide question was
    already answered "yes" — apply it (even if blank) and never prompt,
    since that's the whole point of answering that question once.
    """
    if row.series:
        return
    if default_series is not None:
        _apply_default_series(row, default_series)
        return
    series = input("Series (press Enter to leave blank): ").strip()
    if series:
        row.series = series


def _prompt_manual_metadata(path: Path, default_series: str | None) -> ProductMetadata | None:
    """Collect metadata by hand for a file with no usable DriveThruRPG
    match at all -- the same fields data/manual_overrides.yaml supports,
    just typed in now instead of hand-edited into that file ahead of
    time. Returns None if the user backs out by leaving Title blank.

    Honors a batch-wide default_series exactly like _prompt_series does
    (not-None means already answered, even if blank -- don't ask again).
    """
    print(f"Enter metadata for {path.name} (blank Title cancels):")
    title = input("  Title: ").strip()
    if not title:
        print("Cancelled.")
        return None
    publisher = input("  Publisher: ").strip()
    series = default_series if default_series is not None else input("  Series (Enter to leave blank): ").strip()
    series_index = input("  Series index (Enter to leave blank): ").strip() if series else ""
    description = input("  Description (Enter to leave blank): ").strip()
    tags_raw = input("  Tags, semicolon-separated (Enter to leave blank): ").strip()
    tags = [t.strip() for t in tags_raw.split(";") if t.strip()]
    isbn = input("  ISBN (Enter to leave blank): ").strip()
    product_url = input("  Product URL, for your own reference (Enter to leave blank): ").strip()

    return ProductMetadata(
        title=title,
        series=series,
        series_index=series_index,
        publisher=publisher,
        tags=tags,
        description=description,
        product_url=product_url,
        source=Source.MANUAL,
        isbn=isbn,
    )


def _write_and_maybe_rename(path: Path, row: ReviewRow, bookorbit_mode: bool, rename: bool) -> None:
    """Write metadata, print the outcome, and -- if `rename` is set --
    immediately rename the file (and its sidecars) to match. Reuses
    rename's own plan_rename()/apply_rename() logic wholesale (same
    collision handling, same mid-group rollback on failure) rather than
    duplicating any of it here. Skipped entirely if the write itself
    failed -- nothing to rename yet, and reading back a .metadata.json
    sidecar that write_metadata() never actually got to write would
    just report a spurious "no title" skip.
    """
    result = write_metadata(path, row, bookorbit_mode=bookorbit_mode)
    print(f"Wrote metadata to {path.name}" if result.success else f"FAILED: {result.message}")
    if result.success and rename:
        _rename_one(path, dry_run=False)


def _manual_entry_flow(path: Path, default_series: str | None, bookorbit_mode: bool, rename: bool) -> bool:
    """Prompt for metadata by hand and write it, mirroring the same
    confirm-then-write pattern the candidate-pick path uses. Returns
    True if the user typed 'q' (or backed out of the manual prompt
    itself), so a batch run can bail out early -- consistent with
    every other branch of _tag_one."""
    meta = _prompt_manual_metadata(path, default_series)
    if meta is None:
        return False

    print(f"About to write: {meta.title}")
    answer = input("Proceed? [y/N/q] ").strip().lower()
    if answer == "q":
        return True
    if answer not in ("y", "yes"):
        print("Skipped.")
        return False

    row = row_from_match(path.name, meta, 100.0, Status.APPROVED)
    _write_and_maybe_rename(path, row, bookorbit_mode, rename)
    return False


def _tag_one(
    path: Path,
    client: DtrpgClient,
    manual_overrides: dict,
    known_urls: dict[str, str],
    thresholds: dict,
    default_series: str | None = None,
    bookorbit_mode: bool = False,
    rename: bool = False,
) -> bool:
    """Match+tag a single PDF interactively. Returns True if the user
    asked to stop (typed 'q'), so a batch run can bail out early."""
    if path.name in manual_overrides:
        meta = manual_overrides[path.name]
        print(f"Manual override found for {path.name}:")
        _print_candidate(None, meta, None)
        answer = input("Write this metadata into the PDF? [y/N/q] ").strip().lower()
        if answer == "q":
            return True
        if answer not in ("y", "yes"):
            print("Skipped.")
            return False
        row = row_from_match(path.name, meta, 100.0, Status.APPROVED)
        _prompt_series(row, default_series)
        _write_and_maybe_rename(path, row, bookorbit_mode, rename)
        return False

    if path.name in known_urls:
        product_id = known_urls[path.name]
        meta = client.get_product(product_id)
        if meta is None:
            print(f"Known URL for {path.name} (product {product_id}) could not be fetched; falling back to search.")
        else:
            row = row_from_match(path.name, meta, 100.0, Status.APPROVED)
            _apply_default_series(row, default_series)
            print(f"Known URL matched: {meta.title}")
            _write_and_maybe_rename(path, row, bookorbit_mode, rename)
            return False

    candidates = find_candidates(path, client)
    if not candidates:
        print(f"No candidates found for {path.name}.")
        answer = input("Enter metadata manually? [y/N/q] ").strip().lower()
        if answer == "q":
            return True
        if answer in ("y", "yes"):
            return _manual_entry_flow(path, default_series, bookorbit_mode, rename)
        print("Skipped.")
        return False

    print(f"Candidates for {path.name}:")
    for i, (meta, score) in enumerate(candidates, 1):
        _print_candidate(i, meta, score)

    choice = input(
        "Pick a number to write, paste a DriveThruRPG product URL (or 'id:PRODUCT_ID') "
        "for a direct lookup, 'm' to enter metadata manually, Enter to skip, or 'q' to stop: "
    ).strip()
    choice_lower = choice.lower()
    if choice_lower == "q":
        return True
    if choice_lower == "m":
        return _manual_entry_flow(path, default_series, bookorbit_mode, rename)
    if not choice:
        print("Skipped.")
        return False

    try:
        idx = int(choice_lower)
    except ValueError:
        idx = None

    if idx is not None and 1 <= idx <= len(candidates):
        # A number that's a valid list index always means "pick this
        # candidate" — checked before extract_product_id() so a short
        # index like "1" can never be misread as a literal DriveThruRPG
        # product ID (extract_product_id() accepts bare digit strings too,
        # for dtrpg_urls.csv parsing, where that ambiguity doesn't exist).
        meta, score = candidates[idx - 1]
        if meta.source == Source.DTRPG_LIBRARY and not meta.description:
            try:
                meta = client.enrich(meta)
            except Exception as exc:
                print(f"(couldn't fetch full details: {exc}; proceeding with what we have)")
    else:
        product_id = extract_product_id(choice_lower)
        if product_id is None:
            print("Invalid selection, skipping.")
            return False
        meta = client.get_product(product_id)
        if meta is None:
            print(f"Could not fetch product {product_id} from DriveThruRPG — skipping.")
            return False
        score = 100.0

    status = Status.AUTO_ACCEPTED if score >= thresholds.get("high_confidence_threshold", 90.0) else Status.APPROVED
    row = row_from_match(path.name, meta, score, status)

    print(f"About to write: {meta.title}")
    answer = input("Proceed? [y/N/q] ").strip().lower()
    if answer == "q":
        return True
    if answer not in ("y", "yes"):
        print("Skipped.")
        return False

    _prompt_series(row, default_series)
    _write_and_maybe_rename(path, row, bookorbit_mode, rename)
    return False


def cmd_tag(args: argparse.Namespace, config: dict) -> None:
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
        print(f"Found {len(pdfs)} PDF(s) under {root}\n")

        default_series = None
        same_series = input("Are all books in this batch part of the same series? [y/N] ").strip().lower() in ("y", "yes")
        if same_series:
            # Deliberately not "or None" here -- a blank answer still means
            # "don't ask me again per book", just with no series to apply.
            default_series = input("Series name: ").strip()
        print()

        for i, path in enumerate(pdfs, 1):
            print(f"[{i}/{len(pdfs)}] {path}")
            if _tag_one(path, client, manual_overrides, known_urls, thresholds, default_series, args.bookorbit_mode, args.rename):
                print("Stopped.")
                break
            print()
        return

    path = Path(args.pdf)
    if not path.exists():
        sys.exit(f"File not found: {path}")
    if path.suffix.lower() != ".pdf":
        sys.exit(f"Not a PDF: {path}")

    client = build_client(config)
    known_urls = load_known_urls(path.parent)
    _tag_one(path, client, manual_overrides, known_urls, thresholds, bookorbit_mode=args.bookorbit_mode, rename=args.rename)


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

    write_parser = subparsers.add_parser("write-pdfs", help="Write approved metadata into PDFs")
    write_parser.add_argument("--root", help="Root folder of RPG PDFs (overrides config.yaml)")
    write_parser.add_argument("--review-csv", help="Path to review.csv (overrides config.yaml)")
    write_parser.add_argument("--bookorbit-mode", action="store_true", help=bookorbit_mode_help)
    write_parser.set_defaults(func=cmd_write_pdfs)

    all_parser = subparsers.add_parser("all", help="Run scan, then write-pdfs")
    all_parser.add_argument("--root", help="Root folder of RPG PDFs (overrides config.yaml)")
    all_parser.add_argument("--review-csv", help="Path to review.csv (overrides config.yaml)")
    all_parser.add_argument("--refresh-library", action="store_true")
    all_parser.add_argument("--apply-review", action="store_true")
    all_parser.add_argument("--bookorbit-mode", action="store_true", help=bookorbit_mode_help)
    all_parser.set_defaults(func=cmd_all)

    tag_parser = subparsers.add_parser("tag", help="Match and tag PDF(s) interactively, no review.csv")
    tag_group = tag_parser.add_mutually_exclusive_group(required=True)
    tag_group.add_argument("pdf", nargs="?", help="Path to a single PDF file to match and tag")
    tag_group.add_argument("--root", help="Process every PDF under this directory instead of a single file")
    tag_parser.add_argument("--bookorbit-mode", action="store_true", help=bookorbit_mode_help)
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

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(Path(args.config))
    args.func(args, config)


if __name__ == "__main__":
    main()
