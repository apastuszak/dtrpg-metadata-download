"""Tag tab: Tkinter port of tag_tui.py's interactive flow (candidate
list, manual entry, confirm-then-write, the batch/per-book series
dialogs). Reuses tag_tui.py's pure helpers and result dataclasses
directly -- resolve_series/build_manual_metadata/apply_edits/
format_candidate_lines/do_rename/CandidateResult/ManualEntryResult/
ConfirmResult -- rather than reimplementing any of that logic; only the
*presentation* differs.

The central problem this file solves: Textual's `push_screen_wait()`
(push a screen, `await` until it's dismissed) has no Tkinter equivalent
-- Tkinter has no async event loop, and only the main thread may touch
widgets. TagFlowController runs the same orchestration TagApp does
(run/_process_one/_confirm_and_write/_manual_entry/_maybe_ask_series/
_write_and_maybe_rename, ported near-verbatim from tag_tui.py) on a
background daemon thread. Every place the original does
`await self.push_screen_wait(SomeScreen(...))`, this does a blocking
`self._ask(kind, payload)` call instead:

  1. `_ask()` puts (kind, payload, a fresh single-slot response queue)
     onto one shared request_queue, then blocks the *worker* thread on
     that response queue's `.get()`.
  2. TagTab's `root.after(POLL_MS, ...)` poll loop (main thread) drains
     request_queue and calls the matching show_*_dialog() function,
     which builds a tk.Toplevel, waits on it via `.wait_window()`
     (Tkinter's own "block here until this window closes" primitive --
     it re-enters Tk's event loop, so the dialog's own widgets keep
     working while it's up), then puts the result on the response queue.
  3. The worker thread's `.get()` unblocks and the controller continues.

Verified concretely before relying on it (matching this project's
verify-don't-assume habit): unlike Textual, a Tkinter Toplevel-level key
binding (e.g. bind("m", ...)) *also* fires while a plain Entry has focus
and is receiving that same keystroke as typed text -- so letter mnemonics
("m" for manual entry, "q" for quit) are deliberately NOT bound globally
on any dialog that also has a free-text Entry, since that would corrupt
typing. Escape and Control-s were separately verified safe (neither
inserts a character into a focused Entry) and are used instead, matching
tag_tui.py's Escape-to-skip/cancel and ctrl+s-to-submit/confirm bindings.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from matcher import extract_product_id, find_candidates, load_known_urls, load_manual_overrides, row_from_match, scan_pdfs
from pdf_writer import write_metadata
from provenance import ProductMetadata, Source, Status
from review import ReviewRow
from tag_tui import (
    CandidateResult,
    ConfirmResult,
    ManualEntryResult,
    apply_edits,
    build_manual_metadata,
    do_rename,
    format_candidate_lines,
    resolve_series,
)

from gui_app import (
    BOOKORBIT_HINT,
    BOOKORBIT_URL,
    CONVERT_IMAGES_HINT,
    POLL_MS,
    RENAME_HINT,
    _make_description_label,
    _make_hint_label,
    _make_link_label,
    _make_log_widget,
    build_client_safe,
)

# ---------------------------------------------------------------------------
# Orchestration -- runs on a background thread
# ---------------------------------------------------------------------------


class TagFlowController:
    """Direct port of TagApp's orchestration methods (see tag_tui.py) --
    same control flow, same calls into matcher/pdf_writer/renamer, just
    synchronous (already off the main thread, so no asyncio.to_thread()
    wrapper is needed) and blocking on `_ask()` instead of
    `await push_screen_wait(...)`."""

    def __init__(
        self,
        pdfs: list[Path],
        client,
        manual_overrides: dict,
        known_urls: dict[str, str],
        thresholds: dict,
        bookorbit_mode: bool,
        rename: bool,
        root_mode: bool,
        request_queue: "queue.Queue",
        log,
        stop_event: threading.Event,
        convert_images: bool = False,
    ):
        self.pdfs = pdfs
        self.client = client
        self.manual_overrides = manual_overrides
        self.known_urls = known_urls
        self.thresholds = thresholds
        self.bookorbit_mode = bookorbit_mode
        self.convert_images = convert_images
        self.rename = rename
        self.root_mode = root_mode
        self.request_queue = request_queue
        self._log_cb = log
        self.stop_event = stop_event
        self.history: list[str] = []

    def _log(self, line: str) -> None:
        self.history.append(line)
        self._log_cb(line)

    def _progress(self, i: int) -> str:
        return f"[{i}/{len(self.pdfs)}]" if self.root_mode else ""

    def _ask(self, kind: str, payload: dict):
        if self.stop_event.is_set():
            return self.quit_result(kind)
        response_q: queue.Queue = queue.Queue(maxsize=1)
        self.request_queue.put((kind, payload, response_q))
        return response_q.get()

    @staticmethod
    def quit_result(kind: str):
        return {
            "batch_series": None,
            "series": "",
            "candidate": CandidateResult(action="quit"),
            "manual_entry": ManualEntryResult(action="cancel"),
            "confirm": ConfirmResult(action="quit"),
        }[kind]

    def run(self) -> None:
        default_series: str | None = None
        if self.root_mode and len(self.pdfs) > 1:
            default_series = self._ask("batch_series", {})

        for i, path in enumerate(self.pdfs, 1):
            stop = self._process_one(path, self._progress(i), default_series)
            if stop:
                self._log("Stopped.")
                break

        self._log("Run complete." if not self.stop_event.is_set() else "Stopped.")

    def _process_one(self, path: Path, progress: str, default_series: str | None) -> bool:
        if path.name in self.manual_overrides:
            return self._confirm_and_write(
                path, progress, self.manual_overrides[path.name],
                header=f"Manual override found for {path.name}:",
            )

        if path.name in self.known_urls:
            product_id = self.known_urls[path.name]
            meta = self.client.get_product(product_id)
            if meta is not None:
                row = row_from_match(path.name, meta, 100.0, Status.APPROVED)
                series, _ = resolve_series(row.series, default_series)
                row.series = series
                self._log(f"Known URL matched: {meta.title}")
                self._write_and_maybe_rename(path, row)
                return False
            self._log(f"Known URL for {path.name} could not be fetched; falling back to search.")

        candidates = find_candidates(path, self.client)
        result: CandidateResult = self._ask(
            "candidate", {"path": path, "progress": progress, "candidates": candidates}
        )

        if result.action == "quit":
            return True
        if result.action == "skip":
            self._log(f"Skipped: {path.name}")
            return False
        if result.action == "manual":
            return self._manual_entry(path, progress, default_series)

        if result.action == "pick":
            assert result.index is not None
            meta, score = candidates[result.index]
            if meta.source == Source.DTRPG_LIBRARY and not meta.description:
                try:
                    meta = self.client.enrich(meta)
                except Exception as exc:
                    self._log(f"(couldn't fetch full details: {exc}; proceeding with what we have)")
        else:
            assert result.product_id is not None
            meta = self.client.get_product(result.product_id)
            if meta is None:
                self._log(f"Could not fetch product {result.product_id} from DriveThruRPG -- skipping.")
                return False
            score = 100.0

        status = Status.AUTO_ACCEPTED if score >= self.thresholds.get("high_confidence_threshold", 90.0) else Status.APPROVED
        return self._confirm_and_write(
            path, progress, meta, header=f"About to write: {meta.title}", score=score, status=status,
            default_series=default_series,
        )

    def _manual_entry(self, path: Path, progress: str, default_series: str | None) -> bool:
        result: ManualEntryResult = self._ask(
            "manual_entry", {"path": path, "progress": progress, "default_series": default_series}
        )
        if result.action == "cancel":
            self._log(f"Cancelled: {path.name}")
            return False
        assert result.meta is not None
        confirm: ConfirmResult = self._ask(
            "confirm", {"header": f"About to write: {result.meta.title}", "meta": result.meta, "progress": progress}
        )
        if confirm.action == "quit":
            return True
        if confirm.action == "skip":
            self._log("Skipped.")
            return False
        assert confirm.meta is not None
        confirmed_meta = self._maybe_ask_series(confirm.meta)
        row = row_from_match(path.name, confirmed_meta, 100.0, Status.APPROVED)
        self._write_and_maybe_rename(path, row)
        return False

    def _confirm_and_write(
        self,
        path: Path,
        progress: str,
        meta: ProductMetadata,
        header: str,
        score: float = 100.0,
        status: Status = Status.APPROVED,
        default_series: str | None = None,
    ) -> bool:
        series, _ = resolve_series(meta.series, default_series)
        if series != meta.series:
            meta = replace(meta, series=series)
        result: ConfirmResult = self._ask("confirm", {"header": header, "meta": meta, "progress": progress})
        if result.action == "quit":
            return True
        if result.action == "skip":
            self._log("Skipped.")
            return False
        assert result.meta is not None
        confirmed_meta = self._maybe_ask_series(result.meta)
        row = row_from_match(path.name, confirmed_meta, score, status)
        self._write_and_maybe_rename(path, row)
        return False

    def _maybe_ask_series(self, meta: ProductMetadata) -> ProductMetadata:
        if meta.series:
            return meta
        series = self._ask("series", {"book_title": meta.title})
        return replace(meta, series=series) if series else meta

    def _write_and_maybe_rename(self, path: Path, row: ReviewRow) -> None:
        # Unlike tag_tui.py's TagApp, this controller already runs
        # entirely on its own single worker thread (no further threading
        # inside write_metadata()), and self._log() only appends to a
        # plain list and puts onto a thread-safe queue.Queue -- safe to
        # call directly here with no marshaling back to the main thread.
        result = write_metadata(
            path, row, bookorbit_mode=self.bookorbit_mode, convert_images=self.convert_images, log=self._log
        )
        self._log(f"Wrote metadata to {path.name}" if result.success else f"FAILED: {result.message}")
        if result.success and self.rename:
            outcome = do_rename(path)
            if outcome:
                self._log(outcome)


# ---------------------------------------------------------------------------
# Dialogs -- built on the main thread only, one per TagApp screen
# ---------------------------------------------------------------------------


def show_batch_series_dialog(parent: tk.Misc, payload: dict) -> str | None:
    dialog = tk.Toplevel(parent)
    dialog.title("Series for this batch")
    dialog.transient(parent)
    dialog.grab_set()
    result = {"value": None}

    ttk.Label(dialog, text="Series name for this batch:").pack(anchor="w", padx=10, pady=(10, 0))
    ttk.Label(dialog, text="Leave blank if all books are not in the same series.").pack(anchor="w", padx=10)
    var = tk.StringVar()
    entry = ttk.Entry(dialog, textvariable=var, width=50)
    entry.pack(fill="x", padx=10, pady=6)
    entry.focus_set()

    def submit(_event=None) -> None:
        result["value"] = var.get().strip() or None
        dialog.destroy()

    ttk.Button(dialog, text="Continue", command=submit).pack(pady=(0, 10))
    entry.bind("<Return>", submit)
    dialog.protocol("WM_DELETE_WINDOW", submit)
    dialog.wait_window()
    return result["value"]


def show_series_dialog(parent: tk.Misc, payload: dict) -> str:
    book_title = payload["book_title"]
    dialog = tk.Toplevel(parent)
    dialog.title("Series")
    dialog.transient(parent)
    dialog.grab_set()
    result = {"value": ""}

    ttk.Label(dialog, text=f"Series for {book_title}:").pack(anchor="w", padx=10, pady=(10, 0))
    ttk.Label(dialog, text="Leave blank for no series.").pack(anchor="w", padx=10)
    var = tk.StringVar()
    entry = ttk.Entry(dialog, textvariable=var, width=50)
    entry.pack(fill="x", padx=10, pady=6)
    entry.focus_set()

    def submit(_event=None) -> None:
        result["value"] = var.get().strip()
        dialog.destroy()

    ttk.Button(dialog, text="Continue", command=submit).pack(pady=(0, 10))
    entry.bind("<Return>", submit)
    dialog.protocol("WM_DELETE_WINDOW", submit)
    dialog.wait_window()
    return result["value"]


def show_candidate_dialog(parent: tk.Misc, payload: dict) -> CandidateResult:
    path: Path = payload["path"]
    progress: str = payload["progress"]
    candidates: list[tuple[ProductMetadata, float]] = payload["candidates"]

    dialog = tk.Toplevel(parent)
    dialog.title(f"{progress} Candidates for {path.name}".strip())
    dialog.transient(parent)
    dialog.grab_set()
    dialog.geometry("620x460")

    result = {"value": CandidateResult(action="skip")}

    listbox: tk.Listbox | None = None
    if candidates:
        ttk.Label(
            dialog, text=f"{progress} Choose a Book from the List Provided ({path.name}):".strip()
        ).pack(anchor="w", padx=10, pady=(10, 0))
        listbox = tk.Listbox(dialog, height=10)
        for meta, score in candidates:
            listbox.insert("end", format_candidate_lines(meta, score).splitlines()[0])
        listbox.pack(fill="both", expand=True, padx=10, pady=6)
        listbox.selection_set(0)
    else:
        ttk.Label(dialog, text=f"{progress} No candidates found for {path.name}.".strip()).pack(
            anchor="w", padx=10, pady=(10, 0)
        )

    ttk.Label(dialog, text="Paste a DriveThruRPG URL, or type id:PRODUCT_ID:").pack(
        anchor="w", padx=10, pady=(6, 0)
    )
    url_row = ttk.Frame(dialog)
    url_row.pack(fill="x", padx=10, pady=(0, 6))
    url_var = tk.StringVar()
    url_entry = ttk.Entry(url_row, textvariable=url_var, width=42)
    url_entry.pack(side="left", fill="x", expand=True)

    def pick(_event=None) -> None:
        if listbox is None:
            return
        selection = listbox.curselection()
        if not selection:
            return
        result["value"] = CandidateResult(action="pick", index=selection[0])
        dialog.destroy()

    def submit_url(_event=None) -> None:
        text = url_var.get().strip()
        if not text:
            return
        product_id = extract_product_id(text)
        if product_id:
            result["value"] = CandidateResult(action="url", product_id=product_id)
            dialog.destroy()
        else:
            messagebox.showerror("Candidates", "Could not parse a product ID/URL from that.", parent=dialog)

    def manual(_event=None) -> None:
        result["value"] = CandidateResult(action="manual")
        dialog.destroy()

    def skip(_event=None) -> None:
        result["value"] = CandidateResult(action="skip")
        dialog.destroy()

    def quit_batch() -> None:
        result["value"] = CandidateResult(action="quit")
        dialog.destroy()

    if listbox is not None:
        listbox.bind("<Double-1>", pick)
        listbox.bind("<Return>", pick)
        listbox.focus_set()
    else:
        url_entry.focus_set()
    url_entry.bind("<Return>", submit_url)
    ttk.Button(url_row, text="Use URL", command=submit_url).pack(side="left", padx=(4, 0))

    buttons = ttk.Frame(dialog)
    buttons.pack(pady=(0, 10))
    if listbox is not None:
        ttk.Button(buttons, text="Pick", command=pick).pack(side="left", padx=4)
    # No letter-mnemonic bindings here (m/q) -- verified that a Toplevel-
    # level key binding also fires while the URL Entry above has focus
    # and is receiving that same keystroke as typed text, which would
    # corrupt pasting a URL containing "m" or "q". Buttons only for
    # Manual entry/Quit; Escape (verified not to insert a character) is
    # still bound for Skip.
    ttk.Button(buttons, text="Manual entry", command=manual).pack(side="left", padx=4)
    ttk.Button(buttons, text="Skip (Esc)", command=skip).pack(side="left", padx=4)
    ttk.Button(buttons, text="Quit", command=quit_batch).pack(side="left", padx=4)

    dialog.bind("<Escape>", skip)
    dialog.protocol("WM_DELETE_WINDOW", quit_batch)

    dialog.wait_window()
    return result["value"]


def show_manual_entry_dialog(parent: tk.Misc, payload: dict) -> ManualEntryResult:
    path: Path = payload["path"]
    progress: str = payload["progress"]
    default_series: str | None = payload["default_series"]

    dialog = tk.Toplevel(parent)
    dialog.title(f"Manual entry -- {path.name}")
    dialog.transient(parent)
    dialog.grab_set()
    dialog.geometry("540x560")

    result = {"value": ManualEntryResult(action="cancel")}

    container = ttk.Frame(dialog, padding=10)
    container.pack(fill="both", expand=True)
    row = 0
    ttk.Label(container, text=f"{progress} Enter metadata for {path.name}".strip()).grid(
        row=row, column=0, columnspan=2, sticky="w"
    )
    row += 1

    fields: dict[str, tk.StringVar] = {}

    def add_field(label: str, key: str) -> ttk.Entry:
        nonlocal row
        ttk.Label(container, text=label).grid(row=row, column=0, sticky="w")
        var = tk.StringVar()
        entry = ttk.Entry(container, textvariable=var, width=40)
        entry.grid(row=row, column=1, sticky="we")
        fields[key] = var
        row += 1
        return entry

    title_entry = add_field("Title (required):", "title")
    add_field("Publisher:", "publisher")

    if default_series is not None:
        ttk.Label(container, text=f"Series (locked to this batch's answer): {default_series or '(blank)'}").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
    else:
        add_field("Series:", "series")

    add_field("Series index:", "series_index")

    ttk.Label(container, text="Description:").grid(row=row, column=0, sticky="nw")
    description_text = tk.Text(container, height=6, width=40, wrap="word")
    description_text.grid(row=row, column=1, sticky="we")
    row += 1

    add_field("Tags, semicolon-separated:", "tags")
    add_field("ISBN:", "isbn")
    add_field("Product URL (for your own reference):", "product_url")

    container.columnconfigure(1, weight=1)
    title_entry.focus_set()

    def submit(_event=None) -> None:
        series_value = default_series if default_series is not None else fields["series"].get()
        meta = build_manual_metadata(
            title=fields["title"].get(),
            publisher=fields["publisher"].get(),
            series=series_value,
            series_index=fields["series_index"].get(),
            description=description_text.get("1.0", "end-1c"),
            tags_raw=fields["tags"].get(),
            isbn=fields["isbn"].get(),
            product_url=fields["product_url"].get(),
        )
        if meta is None:
            messagebox.showerror("Manual entry", "Title is required.", parent=dialog)
            title_entry.focus_set()
            return
        result["value"] = ManualEntryResult(action="submit", meta=meta)
        dialog.destroy()

    def cancel(_event=None) -> None:
        result["value"] = ManualEntryResult(action="cancel")
        dialog.destroy()

    buttons = ttk.Frame(container)
    buttons.grid(row=row, column=0, columnspan=2, pady=(10, 0))
    ttk.Button(buttons, text="Submit (ctrl+s)", command=submit).pack(side="left", padx=4)
    ttk.Button(buttons, text="Cancel (Esc)", command=cancel).pack(side="left", padx=4)

    # Control-s / Escape verified safe to bind at the Toplevel level even
    # with free-text Entry/Text widgets focused (neither inserts a
    # character) -- unlike bare letter keys, see module docstring.
    dialog.bind("<Control-s>", submit)
    dialog.bind("<Escape>", cancel)
    dialog.protocol("WM_DELETE_WINDOW", cancel)

    dialog.wait_window()
    return result["value"]


def show_confirm_dialog(parent: tk.Misc, payload: dict) -> ConfirmResult:
    header: str = payload["header"]
    meta: ProductMetadata = payload["meta"]
    progress: str = payload["progress"]

    dialog = tk.Toplevel(parent)
    dialog.title("Confirm")
    dialog.transient(parent)
    dialog.grab_set()
    dialog.geometry("560x580")

    result = {"value": ConfirmResult(action="skip")}

    container = ttk.Frame(dialog, padding=10)
    container.pack(fill="both", expand=True)
    row = 0
    ttk.Label(container, text=f"{progress} {header}".strip()).grid(row=row, column=0, columnspan=2, sticky="w")
    row += 1
    if meta.authors:
        ttk.Label(container, text=f"authors: {meta.authors_str()}").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
    source_value = meta.source.value if hasattr(meta.source, "value") else meta.source
    ttk.Label(container, text=f"source: {source_value}").grid(row=row, column=0, columnspan=2, sticky="w")
    row += 1

    fields: dict[str, tk.StringVar] = {}

    def add_field(label: str, key: str, value: str) -> ttk.Entry:
        nonlocal row
        ttk.Label(container, text=label).grid(row=row, column=0, sticky="w")
        var = tk.StringVar(value=value)
        entry = ttk.Entry(container, textvariable=var, width=40)
        entry.grid(row=row, column=1, sticky="we")
        fields[key] = var
        row += 1
        return entry

    title_entry = add_field("Title (required):", "title", meta.title)
    add_field("Publisher:", "publisher", meta.publisher)
    add_field("Series:", "series", meta.series)
    add_field("Series index:", "series_index", meta.series_index)

    ttk.Label(container, text="Description:").grid(row=row, column=0, sticky="nw")
    description_text = tk.Text(container, height=6, width=40, wrap="word")
    description_text.insert("1.0", meta.description)
    description_text.grid(row=row, column=1, sticky="we")
    row += 1

    add_field("Tags, semicolon-separated:", "tags", meta.tags_str())
    add_field("ISBN:", "isbn", meta.isbn)
    add_field("Product URL:", "product_url", meta.product_url)

    container.columnconfigure(1, weight=1)

    def confirm(_event=None) -> None:
        edited = apply_edits(
            meta,
            title=fields["title"].get(),
            publisher=fields["publisher"].get(),
            series=fields["series"].get(),
            series_index=fields["series_index"].get(),
            description=description_text.get("1.0", "end-1c"),
            tags_raw=fields["tags"].get(),
            isbn=fields["isbn"].get(),
            product_url=fields["product_url"].get(),
        )
        if edited is None:
            messagebox.showerror("Confirm", "Title is required.", parent=dialog)
            title_entry.focus_set()
            return
        result["value"] = ConfirmResult(action="confirm", meta=edited)
        dialog.destroy()

    def skip(_event=None) -> None:
        result["value"] = ConfirmResult(action="skip")
        dialog.destroy()

    def quit_batch() -> None:
        result["value"] = ConfirmResult(action="quit")
        dialog.destroy()

    buttons = ttk.Frame(container)
    buttons.grid(row=row, column=0, columnspan=2, pady=(10, 0))
    confirm_button = ttk.Button(buttons, text="Confirm (ctrl+s)", command=confirm)
    confirm_button.pack(side="left", padx=4)
    ttk.Button(buttons, text="Skip (Esc)", command=skip).pack(side="left", padx=4)
    ttk.Button(buttons, text="Quit", command=quit_batch).pack(side="left", padx=4)

    dialog.bind("<Control-s>", confirm)
    dialog.bind("<Escape>", skip)
    dialog.protocol("WM_DELETE_WINDOW", quit_batch)
    confirm_button.focus_set()

    dialog.wait_window()
    return result["value"]


_DIALOG_BUILDERS = {
    "batch_series": show_batch_series_dialog,
    "candidate": show_candidate_dialog,
    "manual_entry": show_manual_entry_dialog,
    "confirm": show_confirm_dialog,
    "series": show_series_dialog,
}


# ---------------------------------------------------------------------------
# The Tag tab itself
# ---------------------------------------------------------------------------


class TagTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, config: dict):
        super().__init__(parent, padding=10)
        self.config_ = config

        self.mode_var = tk.StringVar(value="root")
        self.path_var = tk.StringVar(value=config.get("root", ""))
        self.bookorbit_var = tk.BooleanVar(value=False)
        self.convert_images_var = tk.BooleanVar(value=False)
        self.rename_var = tk.BooleanVar(value=False)

        _make_description_label(
            self,
            "Match and tag PDF(s) interactively -- choose a book from the list provided, enter "
            "metadata by hand, or paste a known URL, then confirm each one before it's written. "
            "Works on a single file or every PDF in a folder; no review.csv involved.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Radiobutton(self, text="Single file", value="file", variable=self.mode_var).grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(self, text="Whole folder", value="root", variable=self.mode_var).grid(row=1, column=1, sticky="w")
        ttk.Entry(self, textvariable=self.path_var, width=50).grid(row=2, column=0, columnspan=2, sticky="we")
        ttk.Button(self, text="Browse...", command=self._browse).grid(row=2, column=2)

        ttk.Checkbutton(self, text="BookOrbit mode (--bookorbit-mode)", variable=self.bookorbit_var).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        _make_hint_label(self, BOOKORBIT_HINT).grid(row=4, column=0, columnspan=3, sticky="w", padx=(20, 0))
        _make_link_label(self, BOOKORBIT_URL).grid(row=5, column=0, columnspan=3, sticky="w", padx=(20, 0))

        ttk.Checkbutton(
            self, text="Convert all images to RGB JPEG (--convert-images)", variable=self.convert_images_var
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 0))
        _make_hint_label(self, CONVERT_IMAGES_HINT).grid(row=7, column=0, columnspan=3, sticky="w", padx=(20, 0))

        ttk.Checkbutton(self, text="Rename after write (--rename)", variable=self.rename_var).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        _make_hint_label(self, RENAME_HINT).grid(row=9, column=0, columnspan=3, sticky="w", padx=(20, 0))

        self.start_button = ttk.Button(self, text="Start", command=self._start)
        self.start_button.grid(row=10, column=0, sticky="w", pady=(10, 0))

        self.log_widget = _make_log_widget(self, row=11, columnspan=3)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(11, weight=1)

        self.request_queue: queue.Queue = queue.Queue()
        self.log_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._polling = False

    def _browse(self) -> None:
        if self.mode_var.get() == "file":
            p = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
        else:
            p = filedialog.askdirectory(initialdir=self.path_var.get() or ".")
        if p:
            self.path_var.set(p)

    def _log_line(self, line: str) -> None:
        self.log_queue.put(line)

    def _start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        path_str = self.path_var.get().strip()
        if not path_str:
            messagebox.showerror("Tag", "No file/folder given.")
            return

        root_mode = self.mode_var.get() == "root"
        if root_mode:
            root = Path(path_str)
            pdfs = scan_pdfs(root)
            if not pdfs:
                messagebox.showerror("Tag", f"No PDFs found under {root}")
                return
            known_urls = load_known_urls(root)
        else:
            path = Path(path_str)
            if not path.exists():
                messagebox.showerror("Tag", f"File not found: {path}")
                return
            if path.suffix.lower() != ".pdf":
                messagebox.showerror("Tag", f"Not a PDF: {path}")
                return
            pdfs = [path]
            known_urls = load_known_urls(path.parent)

        try:
            client = build_client_safe(self.config_)
        except RuntimeError as exc:
            messagebox.showerror("Tag", str(exc))
            return

        manual_overrides_path = Path(self.config_.get("manual_overrides", "data/manual_overrides.yaml"))
        manual_overrides = load_manual_overrides(manual_overrides_path)
        thresholds = self.config_.get("matching", {})

        self.request_queue = queue.Queue()
        self.log_queue = queue.Queue()
        self.stop_event = threading.Event()
        controller = TagFlowController(
            pdfs=pdfs, client=client, manual_overrides=manual_overrides, known_urls=known_urls,
            thresholds=thresholds, bookorbit_mode=self.bookorbit_var.get(), rename=self.rename_var.get(),
            root_mode=root_mode, request_queue=self.request_queue, log=self._log_line, stop_event=self.stop_event,
            convert_images=self.convert_images_var.get(),
        )
        self.start_button.configure(state="disabled")
        self._thread = threading.Thread(target=controller.run, daemon=True)
        self._thread.start()
        if not self._polling:
            self._polling = True
            self.after(POLL_MS, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_widget.configure(state="normal")
                self.log_widget.insert("end", line + "\n")
                self.log_widget.see("end")
                self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass

        try:
            while True:
                kind, payload, response_q = self.request_queue.get_nowait()
                response_q.put(_DIALOG_BUILDERS[kind](self, payload))
        except queue.Empty:
            pass

        if self._thread is not None and self._thread.is_alive():
            self.after(POLL_MS, self._poll)
        else:
            self._polling = False
            self.start_button.configure(state="normal")

    def stop(self) -> None:
        """Called when the whole GUI window is closing -- unblocks a
        worker thread currently waiting on an unanswered dialog request
        so it can't hang forever on an answer that will never come. The
        worker thread is daemon=True regardless, so the process exits
        cleanly either way; this just avoids leaving it stuck mid-run."""
        self.stop_event.set()
        try:
            while True:
                kind, _payload, response_q = self.request_queue.get_nowait()
                response_q.put(TagFlowController.quit_result(kind))
        except queue.Empty:
            pass
