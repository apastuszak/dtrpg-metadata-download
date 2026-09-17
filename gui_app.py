"""Tkinter desktop GUI for dtrpg-metadata-download, covering every
subcommand in one window (Tag/Scan/Review/Write PDFs/Rename/All).

Presentation layer only, exactly like tag_tui.py is for `tag` alone: every
tab here calls straight into matcher.py/review.py/pdf_writer.py/renamer.py
-- the same functions dtrpg-metadata-download.py's own cmd_* functions
call -- none of those modules know or care that a GUI exists. The Tag
tab's own flow/dialog code lives in gui_tag_flow.py (mirroring tag_tui.py's
size and scope), imported here as one more tab.

tkinter itself is never imported at this module's top level from
dtrpg-metadata-download.py -- see cmd_gui()'s docstring there for why (Tk
bindings are a *system*-level prerequisite uv's managed Python builds
don't have, unlike every pip dependency this project declares via PEP
723). This module is only ever imported after that check has already
passed, so it's free to import tkinter normally itself.

Threading: every tab that can block (network calls, pikepdf I/O) runs its
work on a background daemon thread via UiTaskRunner, which drains a plain
string queue into that tab's log Text widget through root.after() polling
-- Tkinter widgets may only be touched from the main thread, so a worker
never does that directly. Only one job per tab at a time (the Run button
is disabled while busy); no job queue/executor needed for that reason.
The Tag tab needs a richer version of this same idea (a blocking
request/response round trip, not just fire-and-forget log lines) -- see
gui_tag_flow.py for why and how.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import webbrowser
from collections import Counter
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from dtrpg_client import DtrpgClient
from matcher import load_known_urls, load_manual_overrides, run_scan_batch, scan_pdfs
from pdf_writer import write_approved
from preferences import DEFAULT_PREFERENCES_PATH, Preferences, load_preferences, resolve_api_key, save_preferences
from provenance import Status
from renamer import apply_rename, plan_rename
from review import ReviewRow, load_review, save_review

POLL_MS = 50

# Shared checkbox explanatory text -- defined once so WritePdfsTab/AllTab
# (here) and TagTab (gui_tag_flow.py) can't drift apart on wording for the
# same flag.
BOOKORBIT_HINT = (
    "Writes no metadata into the PDF itself -- only a BookOrbit sidecar (.opf) file next to it. "
    "Use only if you actually use BookOrbit:"
)
BOOKORBIT_URL = "https://bookorbit.app/"
CONVERT_IMAGES_HINT = (
    "Converts all images in the PDF from CMYK to RGB, and all JPEG2000 images to JPEG. This can "
    "speed up file rendering on slower hardware, and can fix an issue on macOS with missing images "
    "in the PDF. This WILL increase the file size."
)
RENAME_HINT = "Renames the file after the metadata update, using the format: Series Name - Book Name.pdf"


def build_client_safe(config: dict) -> DtrpgClient:
    """GUI-safe equivalent of dtrpg-metadata-download.py's build_client():
    identical logic, but raises instead of sys.exit()ing on a missing API
    key -- sys.exit() would kill the whole GUI process, not just the tab
    that needed a client. build_client() itself is untouched; every other
    subcommand still needs its sys.exit() behavior on a bare terminal."""
    prefs = load_preferences(config.get("preferences", DEFAULT_PREFERENCES_PATH))
    api_key = resolve_api_key(prefs)
    if not api_key:
        raise RuntimeError(
            "No DriveThruRPG API key found. Set it in the Preferences tab, or export "
            "DTRPG_API_KEY in the environment before launching the GUI."
        )
    return DtrpgClient(
        api_key=api_key,
        cache_dir=config.get("data_dir", "data"),
        catalog_rate_limit_seconds=config.get("dtrpg", {}).get("catalog_rate_limit_seconds", 1.0),
    )


class UiTaskRunner:
    """Runs one target function on a background daemon thread and drains
    its plain-string progress queue into a log Text widget via
    root.after() polling. The worker function receives `self.log` as a
    plain callable progress hook -- calling it just queues a string, so
    it's safe to call from the worker thread; only _poll() (main thread)
    ever touches the Text widget itself."""

    def __init__(self, root: tk.Misc, log_widget: tk.Text, on_done=None):
        self.root = root
        self.log_widget = log_widget
        self.on_done = on_done
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._polling = False

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, fn, *args, **kwargs) -> None:
        if self.busy():
            return

        def _run() -> None:
            try:
                fn(*args, **kwargs)
            except Exception as exc:
                self._queue.put(f"ERROR: {exc}")

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        if not self._polling:
            self._polling = True
            self.root.after(POLL_MS, self._poll)

    def log(self, line: str) -> None:
        self._queue.put(line)

    def _poll(self) -> None:
        try:
            while True:
                line = self._queue.get_nowait()
                self.log_widget.configure(state="normal")
                self.log_widget.insert("end", line + "\n")
                self.log_widget.see("end")
                self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        if self.busy():
            self.root.after(POLL_MS, self._poll)
        else:
            self._polling = False
            if self.on_done is not None:
                self.on_done()


def _make_description_label(parent: tk.Widget, text: str) -> ttk.Label:
    """Short explanatory text at the top of a tab, saying what that tab
    does -- default (not muted) text color, since it's the primary
    orientation for the whole tab rather than a secondary caveat like
    _make_hint_label() below."""
    return ttk.Label(parent, text=text, wraplength=620, justify="left")


def _make_hint_label(parent: tk.Widget, text: str) -> ttk.Label:
    """Small muted explanatory text under a checkbox -- Tkinter Labels
    don't auto-wrap, so wraplength is set explicitly (matches this
    window's ~950px width minus padding)."""
    return ttk.Label(parent, text=text, foreground="#666666", wraplength=620, justify="left")


def _make_link_label(parent: tk.Widget, url: str) -> ttk.Label:
    """A clickable link-styled Label that opens `url` in the system
    browser -- ttk has no built-in hyperlink widget, so this fakes one
    with a colored/underlined font and a click binding."""
    label = ttk.Label(parent, text=url, foreground="#1a73e8", cursor="hand2", font=("TkDefaultFont", 9, "underline"))
    label.bind("<Button-1>", lambda _e: webbrowser.open(url))
    return label


def _make_log_widget(parent: tk.Widget, row: int, columnspan: int) -> tk.Text:
    frame = ttk.Frame(parent)
    frame.grid(row=row, column=0, columnspan=columnspan, sticky="nsew", pady=(8, 0))
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    text = tk.Text(frame, height=12, state="disabled", wrap="word")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    return text


# ---------------------------------------------------------------------------
# Shared scan/write-pdfs logic -- factored out once so ScanTab/WritePdfsTab/
# AllTab don't each keep their own copy (the same reasoning that moved
# cmd_scan's loop into matcher.run_scan_batch()).
# ---------------------------------------------------------------------------


def _do_scan(
    config: dict, log, root: str, review_csv: Path, manual_overrides_path: Path,
    thresholds: dict, refresh_library: bool, apply_review: bool,
) -> list[ReviewRow]:
    client = build_client_safe(config)
    if refresh_library:
        client.pull_library(refresh=True)
    manual_overrides = load_manual_overrides(manual_overrides_path)
    known_urls = load_known_urls(root)
    existing_rows = load_review(review_csv)
    merged = run_scan_batch(
        root, client, manual_overrides, known_urls, existing_rows, thresholds,
        apply_review=apply_review,
        progress=lambda i, total, name: log(f"[{i}/{total}] Matching {name}"),
    )
    save_review(review_csv, merged)
    log(f"Wrote {review_csv} ({len(merged)} rows)")
    for status, count in sorted(Counter(row.status for row in merged).items()):
        log(f"  {status}: {count}")
    return merged


def _do_write_pdfs(log, rows: list[ReviewRow], root: str, bookorbit_mode: bool, convert_images: bool = False) -> None:
    if not any(r.is_approved() for r in rows):
        log("No approved/auto-accepted rows to write.")
        return
    results = write_approved(rows, root, bookorbit_mode=bookorbit_mode, convert_images=convert_images, log=log)
    succeeded = sum(1 for r in results if r.success)
    log(f"Wrote metadata to {succeeded}/{len(results)} approved files")
    for r in results:
        if not r.success:
            log(f"  FAILED: {r.filename}: {r.message}")


def _rename_one(pdf_path: Path, dry_run: bool, log) -> str:
    """Same per-file logic as dtrpg-metadata-download.py's own
    _rename_one() -- reimplemented here (rather than imported) for the
    same reason tag_tui.py's do_rename() is: that file's hyphenated name
    can't be imported by name, and print()-based reporting doesn't fit a
    log-widget callback."""
    plan = plan_rename(pdf_path)
    result = apply_rename(plan, dry_run=dry_run)
    if not result.success:
        if plan.reason is not None:
            log(f"SKIP: {pdf_path.name} ({plan.reason})")
            return "skipped"
        log(f"FAILED: {pdf_path.name}: {result.message}")
        return "failed"
    verb = "Would rename" if dry_run else "Renamed"
    log(f"{verb}: {pdf_path.name} -> {result.new_pdf.name}")
    return "renamed"


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


class ScanTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, config: dict):
        super().__init__(parent, padding=10)
        self.config_ = config
        self.root_var = tk.StringVar(value=config.get("root", ""))
        self.refresh_var = tk.BooleanVar(value=False)
        self.apply_review_var = tk.BooleanVar(value=False)

        _make_description_label(
            self,
            "Matches every PDF under a folder against DriveThruRPG and writes the results to "
            "review.csv -- never touches your PDFs directly. Approve matches here or in the "
            "Review tab, then use Write PDFs (or All) to apply them.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(self, text="Root folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.root_var, width=50).grid(row=1, column=1, sticky="we")
        ttk.Button(self, text="Browse...", command=self._browse).grid(row=1, column=2)
        ttk.Checkbutton(
            self, text="Refresh library (--refresh-library)", variable=self.refresh_var
        ).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(
            self, text="Only match new files (--apply-review)", variable=self.apply_review_var
        ).grid(row=3, column=0, columnspan=3, sticky="w")
        self.run_button = ttk.Button(self, text="Run Scan", command=self._run)
        self.run_button.grid(row=4, column=0, sticky="w", pady=(6, 0))

        self.log = _make_log_widget(self, row=5, columnspan=3)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(5, weight=1)
        self.runner = UiTaskRunner(self, self.log, on_done=self._on_done)

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.root_var.get() or ".")
        if d:
            self.root_var.set(d)

    def _run(self) -> None:
        root = self.root_var.get().strip()
        if not root:
            messagebox.showerror("Scan", "No root folder given.")
            return
        self.run_button.configure(state="disabled")
        review_csv = Path(self.config_.get("review_csv", "data/review.csv"))
        manual_overrides_path = Path(self.config_.get("manual_overrides", "data/manual_overrides.yaml"))
        thresholds = self.config_.get("matching", {})
        self.runner.start(
            _do_scan, self.config_, self.runner.log, root, review_csv, manual_overrides_path,
            thresholds, self.refresh_var.get(), self.apply_review_var.get(),
        )

    def _on_done(self) -> None:
        self.run_button.configure(state="normal")


class WritePdfsTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, config: dict):
        super().__init__(parent, padding=10)
        self.config_ = config
        self.root_var = tk.StringVar(value=config.get("root", ""))
        self.bookorbit_var = tk.BooleanVar(value=False)
        self.convert_images_var = tk.BooleanVar(value=False)

        _make_description_label(
            self,
            "Writes metadata into every PDF whose review.csv row is approved or auto-accepted -- "
            "this step never matches files itself. Run Scan (and approve rows in Review) first.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(self, text="Root folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.root_var, width=50).grid(row=1, column=1, sticky="we")
        ttk.Button(self, text="Browse...", command=self._browse).grid(row=1, column=2)

        ttk.Checkbutton(
            self, text="BookOrbit mode (--bookorbit-mode)", variable=self.bookorbit_var
        ).grid(row=2, column=0, columnspan=3, sticky="w")
        _make_hint_label(self, BOOKORBIT_HINT).grid(row=3, column=0, columnspan=3, sticky="w", padx=(20, 0))
        _make_link_label(self, BOOKORBIT_URL).grid(row=4, column=0, columnspan=3, sticky="w", padx=(20, 0))

        ttk.Checkbutton(
            self, text="Convert all images to RGB JPEG (--convert-images)", variable=self.convert_images_var
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        _make_hint_label(self, CONVERT_IMAGES_HINT).grid(row=6, column=0, columnspan=3, sticky="w", padx=(20, 0))

        self.run_button = ttk.Button(self, text="Write Approved PDFs", command=self._run)
        self.run_button.grid(row=7, column=0, sticky="w", pady=(10, 0))

        self.log = _make_log_widget(self, row=8, columnspan=3)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(8, weight=1)
        self.runner = UiTaskRunner(self, self.log, on_done=self._on_done)

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.root_var.get() or ".")
        if d:
            self.root_var.set(d)

    def _run(self) -> None:
        root = self.root_var.get().strip()
        if not root:
            messagebox.showerror("Write PDFs", "No root folder given.")
            return
        self.run_button.configure(state="disabled")
        review_csv = Path(self.config_.get("review_csv", "data/review.csv"))
        self.runner.start(self._worker, review_csv, root, self.bookorbit_var.get(), self.convert_images_var.get())

    def _worker(self, review_csv: Path, root: str, bookorbit_mode: bool, convert_images: bool) -> None:
        rows = load_review(review_csv)
        _do_write_pdfs(self.runner.log, rows, root, bookorbit_mode, convert_images)

    def _on_done(self) -> None:
        self.run_button.configure(state="normal")


class RenameTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, config: dict):
        super().__init__(parent, padding=10)
        self.config_ = config
        self.mode_var = tk.StringVar(value="root")
        self.path_var = tk.StringVar(value=config.get("root", ""))
        # False by default, matching the CLI's own --dry-run flag (which
        # is action="store_true" -- off unless passed, so `rename` really
        # renames by default). Defaulting this checkbox to True would
        # invert that: clicking "Run Rename" without noticing/unchecking
        # it first would silently only preview, never actually renaming.
        self.dry_run_var = tk.BooleanVar(value=False)

        _make_description_label(
            self,
            "Renames an already-tagged PDF (and its .bak/.opf/.metadata.json sidecars) to "
            '"Series Name - Book Name.pdf", using the title/series recorded in its '
            ".metadata.json sidecar. Untagged or already-correctly-named files are skipped.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Radiobutton(self, text="Single file", value="file", variable=self.mode_var).grid(
            row=1, column=0, sticky="w"
        )
        ttk.Radiobutton(self, text="Whole folder", value="root", variable=self.mode_var).grid(
            row=1, column=1, sticky="w"
        )
        ttk.Entry(self, textvariable=self.path_var, width=50).grid(row=2, column=0, columnspan=2, sticky="we")
        ttk.Button(self, text="Browse...", command=self._browse).grid(row=2, column=2)
        ttk.Checkbutton(self, text="Dry run (preview only)", variable=self.dry_run_var).grid(
            row=3, column=0, columnspan=3, sticky="w"
        )
        self.run_button = ttk.Button(self, text="Run Rename", command=self._run)
        self.run_button.grid(row=4, column=0, sticky="w", pady=(6, 0))

        self.log = _make_log_widget(self, row=5, columnspan=3)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(5, weight=1)
        self.runner = UiTaskRunner(self, self.log, on_done=self._on_done)

    def _browse(self) -> None:
        if self.mode_var.get() == "file":
            p = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        else:
            p = filedialog.askdirectory(initialdir=self.path_var.get() or ".")
        if p:
            self.path_var.set(p)

    def _run(self) -> None:
        path = self.path_var.get().strip()
        if not path:
            messagebox.showerror("Rename", "No file/folder given.")
            return
        self.run_button.configure(state="disabled")
        self.runner.start(self._worker, self.mode_var.get(), path, self.dry_run_var.get())

    def _worker(self, mode: str, path: str, dry_run: bool) -> None:
        log = self.runner.log
        if mode == "file":
            pdf_path = Path(path)
            if not pdf_path.exists():
                log(f"ERROR: file not found: {pdf_path}")
                return
            if pdf_path.suffix.lower() != ".pdf":
                log(f"ERROR: not a PDF: {pdf_path}")
                return
            _rename_one(pdf_path, dry_run, log)
            return

        pdfs = scan_pdfs(path)
        if not pdfs:
            log(f"No PDFs found under {path}")
            return
        counts = {"renamed": 0, "skipped": 0, "failed": 0}
        for pdf_path in pdfs:
            counts[_rename_one(pdf_path, dry_run, log)] += 1
        summary = f"{counts['renamed']} renamed, {counts['skipped']} skipped, {counts['failed']} failed"
        if dry_run:
            summary += " (dry run, nothing changed)"
        log(summary)

    def _on_done(self) -> None:
        self.run_button.configure(state="normal")


class AllTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, config: dict):
        super().__init__(parent, padding=10)
        self.config_ = config
        self.root_var = tk.StringVar(value=config.get("root", ""))
        self.refresh_var = tk.BooleanVar(value=False)
        self.apply_review_var = tk.BooleanVar(value=False)
        self.bookorbit_var = tk.BooleanVar(value=False)
        self.convert_images_var = tk.BooleanVar(value=False)

        _make_description_label(
            self,
            "Runs Scan, then Write PDFs, in one pass -- still gated by review.csv status, so "
            "anything left unapproved (needs-review/no-match) isn't written.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(self, text="Root folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.root_var, width=50).grid(row=1, column=1, sticky="we")
        ttk.Button(self, text="Browse...", command=self._browse).grid(row=1, column=2)
        ttk.Checkbutton(
            self, text="Refresh library (--refresh-library)", variable=self.refresh_var
        ).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(
            self, text="Only match new files (--apply-review)", variable=self.apply_review_var
        ).grid(row=3, column=0, columnspan=3, sticky="w")

        ttk.Checkbutton(
            self, text="BookOrbit mode (--bookorbit-mode)", variable=self.bookorbit_var
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        _make_hint_label(self, BOOKORBIT_HINT).grid(row=5, column=0, columnspan=3, sticky="w", padx=(20, 0))
        _make_link_label(self, BOOKORBIT_URL).grid(row=6, column=0, columnspan=3, sticky="w", padx=(20, 0))

        ttk.Checkbutton(
            self, text="Convert all images to RGB JPEG (--convert-images)", variable=self.convert_images_var
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        _make_hint_label(self, CONVERT_IMAGES_HINT).grid(row=8, column=0, columnspan=3, sticky="w", padx=(20, 0))

        self.run_button = ttk.Button(self, text="Run Scan + Write PDFs", command=self._run)
        self.run_button.grid(row=9, column=0, sticky="w", pady=(10, 0))

        self.log = _make_log_widget(self, row=10, columnspan=3)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(10, weight=1)
        self.runner = UiTaskRunner(self, self.log, on_done=self._on_done)

    def _browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.root_var.get() or ".")
        if d:
            self.root_var.set(d)

    def _run(self) -> None:
        root = self.root_var.get().strip()
        if not root:
            messagebox.showerror("All", "No root folder given.")
            return
        self.run_button.configure(state="disabled")
        review_csv = Path(self.config_.get("review_csv", "data/review.csv"))
        manual_overrides_path = Path(self.config_.get("manual_overrides", "data/manual_overrides.yaml"))
        thresholds = self.config_.get("matching", {})
        self.runner.start(
            self._worker, root, review_csv, manual_overrides_path, thresholds,
            self.refresh_var.get(), self.apply_review_var.get(), self.bookorbit_var.get(),
            self.convert_images_var.get(),
        )

    def _worker(
        self, root: str, review_csv: Path, manual_overrides_path: Path, thresholds: dict,
        refresh_library: bool, apply_review: bool, bookorbit_mode: bool, convert_images: bool,
    ) -> None:
        log = self.runner.log
        merged = _do_scan(self.config_, log, root, review_csv, manual_overrides_path, thresholds, refresh_library, apply_review)
        _do_write_pdfs(log, merged, root, bookorbit_mode, convert_images)

    def _on_done(self) -> None:
        self.run_button.configure(state="normal")


class ReviewTab(ttk.Frame):
    """The scan -> approve -> write-pdfs workflow's approval step, done
    entirely in-GUI instead of handing off to a spreadsheet app. Reads/
    writes review.csv unchanged (review.load_review/save_review); a
    ttk.Treeview has no built-in cell editing, so:
      - `status` gets a ttk.Combobox overlay locked to Status's four
        values -- free text here would silently break
        ReviewRow.is_approved()'s exact string match.
      - `series`/`series_index` get a plain ttk.Entry overlay -- short,
        common one-off corrections.
      - everything else (publisher/authors/tags/product_url/isbn, and a
        multi-line Text for description) is edited via the detail form
        below the grid, populated on row selection.
    Both surfaces write into the same in-memory rows_by_filename dict (the
    single source of truth) so the grid and the detail form can't diverge.
    """

    GRID_COLUMNS = [
        "filename", "matched_title", "series", "series_index",
        "publisher", "confidence_score", "source", "status", "isbn",
    ]
    OVERLAY_COLUMNS = {"status", "series", "series_index"}
    DETAIL_FIELDS = ["publisher", "authors", "tags", "product_url", "isbn"]

    def __init__(self, parent: tk.Widget, config: dict):
        super().__init__(parent, padding=10)
        self.review_csv = Path(config.get("review_csv", "data/review.csv"))
        self.rows_by_filename: dict[str, ReviewRow] = {}
        self._selected_filename: str | None = None
        self._editor: tk.Widget | None = None

        _make_description_label(
            self,
            "Approve or edit matches from review.csv directly -- change a row's status, fix its "
            "series, or edit its description -- then Save. Reload picks up a fresh Scan or an "
            "external hand-edit.",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        toolbar = ttk.Frame(self)
        toolbar.grid(row=1, column=0, columnspan=2, sticky="we")
        ttk.Button(toolbar, text="Reload", command=self.reload).pack(side="left")
        ttk.Button(toolbar, text="Save", command=self.save).pack(side="left", padx=(6, 0))
        self.counts_label = ttk.Label(toolbar, text="")
        self.counts_label.pack(side="left", padx=(16, 0))

        self.tree = ttk.Treeview(self, columns=self.GRID_COLUMNS, show="headings", height=14)
        for col in self.GRID_COLUMNS:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=100, stretch=True)
        self.tree.grid(row=2, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=2, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._begin_edit)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail = ttk.LabelFrame(self, text="Details for selected row")
        detail.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        detail.columnconfigure(1, weight=1)

        self.detail_vars: dict[str, tk.StringVar] = {}
        r = 0
        for field in self.DETAIL_FIELDS:
            ttk.Label(detail, text=f"{field}:").grid(row=r, column=0, sticky="w")
            var = tk.StringVar()
            entry = ttk.Entry(detail, textvariable=var, width=60)
            entry.grid(row=r, column=1, sticky="we")
            entry.bind("<FocusOut>", lambda _e, f=field: self._commit_detail_field(f))
            self.detail_vars[field] = var
            r += 1

        ttk.Label(detail, text="description:").grid(row=r, column=0, sticky="nw")
        self.description_text = tk.Text(detail, height=5, width=60, wrap="word")
        self.description_text.grid(row=r, column=1, sticky="we")
        self.description_text.bind("<FocusOut>", lambda _e: self._commit_description())

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self.reload()

    def reload(self) -> None:
        rows = load_review(self.review_csv)
        self.rows_by_filename = {row.filename: row for row in rows}
        self._selected_filename = None
        self._populate_tree()

    def save(self) -> None:
        save_review(self.review_csv, list(self.rows_by_filename.values()))
        messagebox.showinfo("Review", f"Saved {len(self.rows_by_filename)} rows to {self.review_csv}")

    def _populate_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for filename, row in self.rows_by_filename.items():
            values = [getattr(row, col) for col in self.GRID_COLUMNS]
            self.tree.insert("", "end", iid=filename, values=values)
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        counts = Counter(row.status for row in self.rows_by_filename.values())
        summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
        self.counts_label.configure(text=f"{len(self.rows_by_filename)} rows: {summary}")

    def _on_select(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            self._selected_filename = None
            return
        filename = selected[0]
        self._selected_filename = filename
        row = self.rows_by_filename[filename]
        for field in self.DETAIL_FIELDS:
            self.detail_vars[field].set(getattr(row, field))
        self.description_text.delete("1.0", "end")
        self.description_text.insert("1.0", row.description)

    def _commit_detail_field(self, field: str) -> None:
        if self._selected_filename is None:
            return
        row = self.rows_by_filename[self._selected_filename]
        setattr(row, field, self.detail_vars[field].get())
        if field in self.GRID_COLUMNS:
            self.tree.set(self._selected_filename, field, getattr(row, field))

    def _commit_description(self) -> None:
        if self._selected_filename is None:
            return
        self.rows_by_filename[self._selected_filename].description = self.description_text.get("1.0", "end-1c")

    def _begin_edit(self, event) -> None:
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        # Deliberately not using identify_column()'s "#N" result here --
        # verified empirically (a throwaway script comparing bbox(item,
        # name) for every column against identify_column() at that exact
        # point) that the two disagreed on this Tk build: bbox said
        # "status" started at x=700, but identify_column(702) reported
        # the column one to its left ("source"). Hit-testing directly
        # against each column's own bbox sidesteps whatever's causing
        # that mismatch, rather than trusting either API's numbering.
        col_name = None
        for candidate in self.GRID_COLUMNS:
            bbox = self.tree.bbox(row_id, candidate)
            if bbox and bbox[0] <= event.x < bbox[0] + bbox[2]:
                col_name = candidate
                break
        if col_name is None or col_name not in self.OVERLAY_COLUMNS:
            return
        x, y, width, height = bbox
        current = self.tree.set(row_id, col_name)

        if col_name == "status":
            editor: tk.Widget = ttk.Combobox(self.tree, values=[s.value for s in Status], state="readonly")
            editor.set(current)
        else:
            editor = ttk.Entry(self.tree)
            editor.insert(0, current)
            editor.select_range(0, "end")
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()
        self._editor = editor

        def commit(_event=None) -> None:
            new_value = editor.get()
            self.tree.set(row_id, col_name, new_value)
            setattr(self.rows_by_filename[row_id], col_name, new_value)
            if col_name == "status":
                self._refresh_counts()
            editor.destroy()
            self._editor = None

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)
        if col_name == "status":
            editor.bind("<<ComboboxSelected>>", commit)


class PreferencesTab(ttk.Frame):
    """Saved API key + DriveThruRPG name -- see preferences.py's module
    docstring for why these live in their own gitignored, owner-only-
    permissioned file rather than config.yaml. The API key field is
    masked by default (a real secret), with a checkbox to reveal it,
    matching preferences_tui.py's equivalent in the TUI."""

    def __init__(self, parent: tk.Widget, config: dict):
        super().__init__(parent, padding=10)
        self.preferences_path = Path(config.get("preferences", str(DEFAULT_PREFERENCES_PATH)))
        prefs = load_preferences(self.preferences_path)

        _make_description_label(
            self,
            f"Your DriveThruRPG API key and account name, saved to {self.preferences_path} -- "
            "not config.yaml, which is meant to be safe to share/commit. An existing "
            "DTRPG_API_KEY environment variable always takes priority over what's saved here.",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.api_key_var = tk.StringVar(value=prefs.api_key)
        self.show_key_var = tk.BooleanVar(value=False)
        self.name_var = tk.StringVar(value=prefs.dtrpg_name)

        ttk.Label(self, text="API Key:").grid(row=1, column=0, sticky="w")
        self.api_key_entry = ttk.Entry(self, textvariable=self.api_key_var, width=50, show="*")
        self.api_key_entry.grid(row=1, column=1, sticky="we")
        ttk.Checkbutton(
            self, text="Show API key", variable=self.show_key_var, command=self._toggle_show
        ).grid(row=2, column=1, sticky="w")

        ttk.Label(self, text="DriveThruRPG Name:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(self, textvariable=self.name_var, width=50).grid(row=3, column=1, sticky="we", pady=(8, 0))

        self.save_button = ttk.Button(self, text="Save Preferences", command=self._save)
        self.save_button.grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.status_label = ttk.Label(self, text="")
        self.status_label.grid(row=4, column=1, sticky="w", pady=(10, 0))

        self.columnconfigure(1, weight=1)

    def _toggle_show(self) -> None:
        self.api_key_entry.configure(show="" if self.show_key_var.get() else "*")

    def _save(self) -> None:
        prefs = Preferences(api_key=self.api_key_var.get().strip(), dtrpg_name=self.name_var.get().strip())
        save_preferences(prefs, self.preferences_path)
        self.status_label.configure(text=f"Saved to {self.preferences_path}")


class GuiApp(tk.Tk):
    def __init__(self, config: dict):
        super().__init__()
        self.title("dtrpg-metadata-download")
        self.geometry("950x700")

        from gui_tag_flow import TagTab

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        self._tag_tab = TagTab(notebook, config)
        notebook.add(self._tag_tab, text="Tag")
        notebook.add(ScanTab(notebook, config), text="Scan")
        notebook.add(ReviewTab(notebook, config), text="Review")
        notebook.add(WritePdfsTab(notebook, config), text="Write PDFs")
        notebook.add(RenameTab(notebook, config), text="Rename")
        notebook.add(AllTab(notebook, config), text="All")
        notebook.add(PreferencesTab(notebook, config), text="Preferences")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        # Unblocks the Tag tab's worker thread if it's mid-run waiting on
        # a dialog answer that will now never come -- see TagTab.stop()'s
        # own docstring for why this is a courtesy, not a correctness
        # requirement (the worker is daemon=True either way).
        self._tag_tab.stop()
        self.destroy()


def run_gui(config: dict) -> None:
    GuiApp(config).mainloop()
