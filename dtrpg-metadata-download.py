#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pikepdf>=8.0",
#     "rapidfuzz>=3.0",
#     "PyYAML>=6.0",
#     "requests>=2.31",
# ]
# ///
"""CLI entry point for the RPG PDF metadata pipeline.

    scan --root PATH [--refresh-library] [--apply-review]
        Build/update data/review.csv by matching PDFs under --root
        against manual_overrides.yaml, then <root>/dtrpg_urls.csv, then
        DriveThruRPG's library, then its public catalog.

    review-status
        Print match counts by status from data/review.csv.

    write-pdfs --root PATH
        Write metadata for every "approved"/"auto-accepted" row in
        data/review.csv into the corresponding PDF (in place, with a
        one-time .bak backup per file).

    all --root PATH [--refresh-library] [--apply-review]
        Run scan, then write-pdfs. Still gated by review.csv status —
        rows left at needs-review/no-match are not written.

    tag PDF_PATH | tag --root PATH
        Match PDF(s) by filename, show ranked candidates per file, and
        write the one you pick straight into it — interactively, no
        review.csv. With --root, loops over every PDF under that
        directory one at a time (type 'q' at any prompt to stop early).

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
from provenance import Source, Status
from review import load_review, merge_by_filename, save_review

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

    results = write_approved(rows, root)
    succeeded = sum(1 for r in results if r.success)
    print(f"Wrote metadata to {succeeded}/{len(results)} approved files")
    for r in results:
        if not r.success:
            print(f"  FAILED: {r.filename}: {r.message}")


def cmd_all(args: argparse.Namespace, config: dict) -> None:
    cmd_scan(args, config)
    cmd_write_pdfs(args, config)


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


def _tag_one(
    path: Path,
    client: DtrpgClient,
    manual_overrides: dict,
    known_urls: dict[str, str],
    thresholds: dict,
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
        result = write_metadata(path, row)
        print(f"Wrote metadata to {path.name}" if result.success else f"FAILED: {result.message}")
        return False

    if path.name in known_urls:
        product_id = known_urls[path.name]
        meta = client.get_product(product_id)
        if meta is None:
            print(f"Known URL for {path.name} (product {product_id}) could not be fetched; falling back to search.")
        else:
            row = row_from_match(path.name, meta, 100.0, Status.APPROVED)
            result = write_metadata(path, row)
            print(f"Known URL matched: {meta.title}")
            print(f"Wrote metadata to {path.name}" if result.success else f"FAILED: {result.message}")
            return False

    candidates = find_candidates(path, client)
    if not candidates:
        print(f"No candidates found for {path.name}.")
        return False

    print(f"Candidates for {path.name}:")
    for i, (meta, score) in enumerate(candidates, 1):
        _print_candidate(i, meta, score)

    choice = input(
        "Pick a number to write, paste a DriveThruRPG product URL (or 'id:PRODUCT_ID') "
        "for a direct lookup, Enter to skip, or 'q' to stop: "
    ).strip()
    choice_lower = choice.lower()
    if choice_lower == "q":
        return True
    if not choice:
        print("Skipped.")
        return False

    product_id = extract_product_id(choice_lower)
    if product_id is not None:
        meta = client.get_product(product_id)
        if meta is None:
            print(f"Could not fetch product {product_id} from DriveThruRPG — skipping.")
            return False
        score = 100.0
    else:
        try:
            idx = int(choice_lower)
            if not (1 <= idx <= len(candidates)):
                raise ValueError
        except ValueError:
            print("Invalid selection, skipping.")
            return False

        meta, score = candidates[idx - 1]
        if meta.source == Source.DTRPG_LIBRARY and not meta.description:
            try:
                meta = client.enrich(meta)
            except Exception as exc:
                print(f"(couldn't fetch full details: {exc}; proceeding with what we have)")

    status = Status.AUTO_ACCEPTED if score >= thresholds.get("high_confidence_threshold", 90.0) else Status.APPROVED
    row = row_from_match(path.name, meta, score, status)

    print(f"About to write: {meta.title}")
    answer = input("Proceed? [y/N/q] ").strip().lower()
    if answer == "q":
        return True
    if answer not in ("y", "yes"):
        print("Skipped.")
        return False

    result = write_metadata(path, row)
    print(f"Wrote metadata to {path.name}" if result.success else f"FAILED: {result.message}")
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
        for i, path in enumerate(pdfs, 1):
            print(f"[{i}/{len(pdfs)}] {path}")
            if _tag_one(path, client, manual_overrides, known_urls, thresholds):
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
    _tag_one(path, client, manual_overrides, known_urls, thresholds)


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

    write_parser = subparsers.add_parser("write-pdfs", help="Write approved metadata into PDFs")
    write_parser.add_argument("--root", help="Root folder of RPG PDFs (overrides config.yaml)")
    write_parser.add_argument("--review-csv", help="Path to review.csv (overrides config.yaml)")
    write_parser.set_defaults(func=cmd_write_pdfs)

    all_parser = subparsers.add_parser("all", help="Run scan, then write-pdfs")
    all_parser.add_argument("--root", help="Root folder of RPG PDFs (overrides config.yaml)")
    all_parser.add_argument("--review-csv", help="Path to review.csv (overrides config.yaml)")
    all_parser.add_argument("--refresh-library", action="store_true")
    all_parser.add_argument("--apply-review", action="store_true")
    all_parser.set_defaults(func=cmd_all)

    tag_parser = subparsers.add_parser("tag", help="Match and tag PDF(s) interactively, no review.csv")
    tag_group = tag_parser.add_mutually_exclusive_group(required=True)
    tag_group.add_argument("pdf", nargs="?", help="Path to a single PDF file to match and tag")
    tag_group.add_argument("--root", help="Process every PDF under this directory instead of a single file")
    tag_parser.set_defaults(func=cmd_tag)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(Path(args.config))
    args.func(args, config)


if __name__ == "__main__":
    main()
