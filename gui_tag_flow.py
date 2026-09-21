"""Tag tab: PyQt6 port of tag_tui.py's interactive flow (candidate list,
manual entry, confirm-then-write, the batch/per-book series dialogs).
Reuses tag_tui.py's pure helpers and result dataclasses directly --
resolve_series/build_manual_metadata/apply_edits/format_candidate_lines/
do_rename/CandidateResult/ManualEntryResult/ConfirmResult -- rather than
reimplementing any of that logic; only the *presentation* differs.

The central problem this file solves: Textual's `push_screen_wait()`
(push a screen, `await` until it's dismissed) has no direct PyQt6
equivalent, and the actual tagging work (network calls, pikepdf I/O) must
run off the GUI thread so the window doesn't freeze -- but only the GUI
thread may build/exec a QDialog. TagFlowController (a plain, framework-
agnostic class -- an almost line-for-line port of TagApp's own
orchestration methods from tag_tui.py) runs on a background QThread via
TagWorker. Every place the original does
`await self.push_screen_wait(SomeScreen(...))`, this does a blocking
`self._ask(kind, payload)` call instead:

  1. `_ask()` builds a fresh single-slot `queue.Queue`, calls the
     `notify` callback given to the controller (TagWorker._notify, which
     just emits `dialog_requested`), then blocks the *worker* thread on
     that queue's `.get()`. This part is functionally identical to
     `tag_tui.py`'s Textual version and to this file's own earlier
     Tkinter version -- queue.Queue is still the right primitive for a
     one-shot cross-thread mailbox regardless of which GUI toolkit is
     asking the question.
  2. TagTab's `_on_dialog_requested` slot (connected to `dialog_requested`
     with Qt's default auto/queued cross-thread connection, so it runs on
     the GUI thread no matter which thread emitted the signal) builds the
     matching QDialog subclass and calls `.exec()` -- Qt's own "block
     here (this thread only) until this modal window closes" primitive,
     playing the same role `Toplevel`+`wait_window()` played in the
     Tkinter version. Once the dialog closes, its `.value` attribute is
     read and put on the response queue.
  3. The worker thread's `.get()` unblocks and `_ask()` returns.

This is simpler than the Tkinter version needed to be: Qt's cross-thread
signal delivery replaces both the shared `request_queue` *and* the
`root.after(POLL_MS, ...)` polling loop that used to drain it -- there's
no polling here at all, and (unlike Tkinter, where the main thread's
`_poll()` and window-close handling both raced against an independent
queue) a dialog request can never sit "pending" while the GUI thread does
something else: `.exec()` blocks the entire GUI event loop until that one
dialog closes, so by construction there's nothing else for the GUI thread
to be doing at the same time.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

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
    HYPERLINK_GURPS_HINT,
    RENAME_HINT,
    _make_description_label,
    _make_hint_label,
    _make_link_label,
    _make_log_widget,
    _StyledTextEdit,
    build_client_safe,
)
from gurps_hyperlink import DEFAULT_HYPERLINK_SCRIPT

# ---------------------------------------------------------------------------
# Orchestration -- runs on a background thread. Framework-agnostic (no Qt
# import here at all): it only needs a `notify(kind, payload, response_q)`
# callback and a `log(line)` callback, both supplied by TagWorker below.
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
        notify,
        log,
        stop_event: threading.Event,
        convert_images: bool = False,
        hyperlink_gurps: bool = False,
        gurps_hyperlink_script: str | Path = DEFAULT_HYPERLINK_SCRIPT,
    ):
        self.pdfs = pdfs
        self.client = client
        self.manual_overrides = manual_overrides
        self.known_urls = known_urls
        self.thresholds = thresholds
        self.bookorbit_mode = bookorbit_mode
        self.convert_images = convert_images
        self.hyperlink_gurps = hyperlink_gurps
        self.gurps_hyperlink_script = gurps_hyperlink_script
        self.rename = rename
        self.root_mode = root_mode
        self._notify = notify
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
        self._notify(kind, payload, response_q)
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
        # This controller already runs entirely on its own single worker
        # thread (no further threading inside write_metadata()), and
        # self._log() only appends to a plain list and calls a plain
        # callback (TagWorker._log, which just emits a Qt signal -- safe
        # from any thread) -- safe to call directly here with no
        # marshaling back to the GUI thread.
        result = write_metadata(
            path, row, bookorbit_mode=self.bookorbit_mode, convert_images=self.convert_images,
            hyperlink_gurps=self.hyperlink_gurps, gurps_hyperlink_script=self.gurps_hyperlink_script,
            log=self._log,
        )
        self._log(f"Wrote metadata to {path.name}" if result.success else f"FAILED: {result.message}")
        if result.success and self.rename:
            outcome = do_rename(path)
            if outcome:
                self._log(outcome)


class TagWorker(QObject):
    """Lives on a background QThread; runs TagFlowController.run() there.
    `dialog_requested` carries a fresh single-slot queue.Queue per
    request -- TagFlowController._ask() blocks on that queue's .get() (a
    worker-thread concern, unaffected by which thread eventually answers
    it); this signal only handles the *notification* step, crossing from
    the worker thread to whichever thread the connected slot's receiver
    lives on (TagTab, on the GUI thread) via Qt's automatic queued
    cross-thread signal delivery -- emitting a signal is safe from any
    thread, unlike calling a GUI-thread object's methods directly."""

    dialog_requested = pyqtSignal(str, dict, object)
    log_line = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self, pdfs, client, manual_overrides, known_urls, thresholds,
        bookorbit_mode, rename, root_mode, stop_event, convert_images=False,
        hyperlink_gurps=False, gurps_hyperlink_script=DEFAULT_HYPERLINK_SCRIPT,
    ):
        super().__init__()
        self.controller = TagFlowController(
            pdfs=pdfs, client=client, manual_overrides=manual_overrides, known_urls=known_urls,
            thresholds=thresholds, bookorbit_mode=bookorbit_mode, rename=rename, root_mode=root_mode,
            notify=self._notify, log=self._log, stop_event=stop_event, convert_images=convert_images,
            hyperlink_gurps=hyperlink_gurps, gurps_hyperlink_script=gurps_hyperlink_script,
        )

    def _notify(self, kind: str, payload: dict, response_q: "queue.Queue") -> None:
        self.dialog_requested.emit(kind, payload, response_q)

    def _log(self, line: str) -> None:
        self.log_line.emit(line)

    def run(self) -> None:
        self.controller.run()
        self.finished.emit()


# ---------------------------------------------------------------------------
# Dialogs -- built and .exec()'d on the GUI thread only, one class per
# TagApp screen. Each exposes a `.value` attribute (deliberately not named
# `.result` -- QDialog already has a built-in `.result()` method returning
# its Accepted/Rejected exec() code, which this isn't) holding the typed
# result the caller reads once `.exec()` returns.
# ---------------------------------------------------------------------------


def _plain_label(text: str) -> QLabel:
    """A QLabel that can never render its text as rich text/HTML,
    regardless of content. A bare QLabel(text) defaults to
    Qt::AutoText, which auto-detects and renders anything that looks
    like markup -- and title/authors here can come straight from
    DriveThruRPG's *public* catalog (anyone can list a product there,
    same attacker-reachable-content reasoning as the CSV-injection fix
    in review.py), so a crafted title/author string could otherwise
    inject formatting, fake links, or altered-looking text into a
    screen whose whole purpose is showing the user exactly what's
    about to be written. Security-audit finding, not something a user
    reported."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class BatchSeriesDialog(QDialog):
    def __init__(self, parent, payload: dict):
        super().__init__(parent)
        self.setWindowTitle("Series for this batch")
        self.value: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Series name for this batch:"))
        layout.addWidget(QLabel("Leave blank if all books are not in the same series."))
        self.edit = QLineEdit()
        self.edit.returnPressed.connect(self._submit)
        layout.addWidget(self.edit)
        button = QPushButton("Continue")
        button.clicked.connect(self._submit)
        layout.addWidget(button)
        self.edit.setFocus()

    def _submit(self) -> None:
        self.value = self.edit.text().strip() or None
        self.accept()

    def reject(self) -> None:
        # No real "cancel" state for this dialog -- window-close/Escape
        # still submits whatever's currently typed, matching the original
        # Tkinter version's WM_DELETE_WINDOW-→submit behavior.
        self.value = self.edit.text().strip() or None
        super().reject()


class SeriesDialog(QDialog):
    def __init__(self, parent, payload: dict):
        super().__init__(parent)
        book_title = payload["book_title"]
        self.setWindowTitle("Series")
        self.value: str = ""

        layout = QVBoxLayout(self)
        layout.addWidget(_plain_label(f"Series for {book_title}:"))
        layout.addWidget(QLabel("Leave blank for no series."))
        self.edit = QLineEdit()
        self.edit.returnPressed.connect(self._submit)
        layout.addWidget(self.edit)
        button = QPushButton("Continue")
        button.clicked.connect(self._submit)
        layout.addWidget(button)
        self.edit.setFocus()

    def _submit(self) -> None:
        self.value = self.edit.text().strip()
        self.accept()

    def reject(self) -> None:
        self.value = self.edit.text().strip()
        super().reject()


class CandidateDialog(QDialog):
    def __init__(self, parent, payload: dict):
        super().__init__(parent)
        path: Path = payload["path"]
        progress: str = payload["progress"]
        self.candidates: list[tuple[ProductMetadata, float]] = payload["candidates"]
        self.setWindowTitle(f"{progress} Candidates for {path.name}".strip())
        self.resize(620, 460)
        self.value: CandidateResult = CandidateResult(action="skip")

        layout = QVBoxLayout(self)
        self.list_widget: QListWidget | None = None
        if self.candidates:
            layout.addWidget(QLabel(f"{progress} Choose a Book from the List Provided ({path.name}):".strip()))
            self.list_widget = QListWidget()
            for meta, score in self.candidates:
                self.list_widget.addItem(format_candidate_lines(meta, score).splitlines()[0])
            self.list_widget.setCurrentRow(0)
            self.list_widget.itemDoubleClicked.connect(lambda _item: self._pick())
            layout.addWidget(self.list_widget, 1)
        else:
            layout.addWidget(QLabel(f"{progress} No candidates found for {path.name}.".strip()))

        buttons = QHBoxLayout()
        self.pick_button: QPushButton | None = None
        if self.list_widget is not None:
            self.pick_button = QPushButton("Pick")
            self.pick_button.clicked.connect(self._pick)
            buttons.addWidget(self.pick_button)
        self.manual_button = QPushButton("Manual entry")
        self.manual_button.clicked.connect(self._manual)
        buttons.addWidget(self.manual_button)
        self.skip_button = QPushButton("Skip (Esc)")
        self.skip_button.clicked.connect(self._skip)
        buttons.addWidget(self.skip_button)
        self.quit_button = QPushButton("Quit")
        self.quit_button.clicked.connect(self._quit)
        buttons.addWidget(self.quit_button)
        layout.addLayout(buttons)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

        or_label = QLabel("–OR–")
        or_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        or_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(or_label)

        layout.addWidget(QLabel("Paste a DriveThruRPG URL, or type id:PRODUCT_ID:"))
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.returnPressed.connect(self._submit_url)
        url_row.addWidget(self.url_edit, 1)
        use_url_button = QPushButton("Use URL")
        use_url_button.clicked.connect(self._submit_url)
        url_row.addWidget(use_url_button)
        layout.addLayout(url_row)

        if self.list_widget is not None:
            self.list_widget.setFocus()
        else:
            self.url_edit.setFocus()

    def _pick(self) -> None:
        if self.list_widget is None:
            return
        row = self.list_widget.currentRow()
        if row < 0:
            return
        self.value = CandidateResult(action="pick", index=row)
        self.accept()

    def _submit_url(self) -> None:
        text = self.url_edit.text().strip()
        if not text:
            return
        product_id = extract_product_id(text)
        if product_id:
            self.value = CandidateResult(action="url", product_id=product_id)
            self.accept()
        else:
            QMessageBox.critical(self, "Candidates", "Could not parse a product ID/URL from that.")

    def _manual(self) -> None:
        self.value = CandidateResult(action="manual")
        self.accept()

    def _skip(self) -> None:
        self.value = CandidateResult(action="skip")
        self.accept()

    def _quit(self) -> None:
        self.value = CandidateResult(action="quit")
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Escape means Skip; window-close (the OS close button, handled
        # by reject() below since it's never intercepted here) means
        # Quit -- these are distinct outcomes today and must stay
        # distinct, matching the original Tkinter version's Escape-vs-
        # WM_DELETE_WINDOW split.
        if event.key() == Qt.Key.Key_Escape:
            self._skip()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        self.value = CandidateResult(action="quit")
        super().reject()


class ManualEntryDialog(QDialog):
    def __init__(self, parent, payload: dict):
        super().__init__(parent)
        path: Path = payload["path"]
        progress: str = payload["progress"]
        self.default_series: str | None = payload["default_series"]
        self.setWindowTitle(f"Manual entry -- {path.name}")
        self.resize(540, 560)
        self.value: ManualEntryResult = ManualEntryResult(action="cancel")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{progress} Enter metadata for {path.name}".strip()))

        form = QFormLayout()
        # QFormLayout's default field growth policy sizes each field to its
        # own size hint -- QLineEdit's is a fixed-ish default width, while
        # QPlainTextEdit's is Expanding, so Description ended up visibly
        # wider than every other field. This makes every field grow to
        # fill the same column width instead.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.title_edit = QLineEdit()
        form.addRow("Title (required):", self.title_edit)
        self.publisher_edit = QLineEdit()
        form.addRow("Publisher:", self.publisher_edit)

        self.series_edit: QLineEdit | None = None
        if self.default_series is not None:
            form.addRow("Series (locked to this batch's answer):", QLabel(self.default_series or "(blank)"))
        else:
            self.series_edit = QLineEdit()
            form.addRow("Series:", self.series_edit)

        self.series_index_edit = QLineEdit()
        form.addRow("Series index:", self.series_index_edit)

        self.description_edit = _StyledTextEdit()
        self.description_edit.setFixedHeight(120)
        form.addRow("Description:", self.description_edit)

        self.tags_edit = QLineEdit()
        form.addRow("Tags, semicolon-separated:", self.tags_edit)
        self.isbn_edit = QLineEdit()
        form.addRow("ISBN:", self.isbn_edit)
        self.product_url_edit = QLineEdit()
        form.addRow("Product URL (for your own reference):", self.product_url_edit)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.submit_button = QPushButton("Submit (ctrl+s)")
        self.submit_button.clicked.connect(self._submit)
        buttons.addWidget(self.submit_button)
        self.cancel_button = QPushButton("Cancel (Esc)")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        # Control-s bound directly (not a global letter mnemonic) -- Qt
        # shortcuts are scoped to the widget/window they're attached to,
        # unlike Tkinter's Toplevel-level key bindings, which the original
        # version had to avoid for single letters specifically because
        # they *also* fired while a plain Entry had focus and was
        # receiving that same keystroke as typed text. That hazard has no
        # Qt equivalent; kept Ctrl+S/Escape-only anyway, matching the
        # original UX rather than introducing new mnemonics just because
        # Qt would allow them safely.
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._submit)
        self.title_edit.setFocus()

    def _submit(self) -> None:
        series_value = self.default_series if self.default_series is not None else self.series_edit.text()
        meta = build_manual_metadata(
            title=self.title_edit.text(),
            publisher=self.publisher_edit.text(),
            series=series_value,
            series_index=self.series_index_edit.text(),
            description=self.description_edit.toPlainText(),
            tags_raw=self.tags_edit.text(),
            isbn=self.isbn_edit.text(),
            product_url=self.product_url_edit.text(),
        )
        if meta is None:
            QMessageBox.critical(self, "Manual entry", "Title is required.")
            self.title_edit.setFocus()
            return
        self.value = ManualEntryResult(action="submit", meta=meta)
        self.accept()

    def reject(self) -> None:
        self.value = ManualEntryResult(action="cancel")
        super().reject()


class ConfirmDialog(QDialog):
    def __init__(self, parent, payload: dict):
        super().__init__(parent)
        header: str = payload["header"]
        meta: ProductMetadata = payload["meta"]
        progress: str = payload["progress"]
        self.meta = meta
        self.setWindowTitle("Confirm")
        self.resize(560, 580)
        self.value: ConfirmResult = ConfirmResult(action="skip")

        layout = QVBoxLayout(self)
        layout.addWidget(_plain_label(f"{progress} {header}".strip()))
        if meta.authors:
            layout.addWidget(_plain_label(f"authors: {meta.authors_str()}"))
        source_value = meta.source.value if hasattr(meta.source, "value") else meta.source
        layout.addWidget(QLabel(f"source: {source_value}"))

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.title_edit = QLineEdit(meta.title)
        form.addRow("Title (required):", self.title_edit)
        self.publisher_edit = QLineEdit(meta.publisher)
        form.addRow("Publisher:", self.publisher_edit)
        self.series_edit = QLineEdit(meta.series)
        form.addRow("Series:", self.series_edit)
        self.series_index_edit = QLineEdit(meta.series_index)
        form.addRow("Series index:", self.series_index_edit)

        self.description_edit = _StyledTextEdit()
        self.description_edit.setPlainText(meta.description)
        self.description_edit.setFixedHeight(120)
        form.addRow("Description:", self.description_edit)

        self.tags_edit = QLineEdit(meta.tags_str())
        form.addRow("Tags, semicolon-separated:", self.tags_edit)
        self.isbn_edit = QLineEdit(meta.isbn)
        form.addRow("ISBN:", self.isbn_edit)
        self.product_url_edit = QLineEdit(meta.product_url)
        form.addRow("Product URL:", self.product_url_edit)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.confirm_button = QPushButton("Confirm (ctrl+s)")
        self.confirm_button.setDefault(True)
        self.confirm_button.clicked.connect(self._confirm)
        buttons.addWidget(self.confirm_button)
        skip_button = QPushButton("Skip (Esc)")
        skip_button.clicked.connect(self._skip)
        buttons.addWidget(skip_button)
        quit_button = QPushButton("Quit")
        quit_button.clicked.connect(self._quit)
        buttons.addWidget(quit_button)
        layout.addLayout(buttons)

        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._confirm)
        self.confirm_button.setFocus()

    def _confirm(self) -> None:
        edited = apply_edits(
            self.meta,
            title=self.title_edit.text(),
            publisher=self.publisher_edit.text(),
            series=self.series_edit.text(),
            series_index=self.series_index_edit.text(),
            description=self.description_edit.toPlainText(),
            tags_raw=self.tags_edit.text(),
            isbn=self.isbn_edit.text(),
            product_url=self.product_url_edit.text(),
        )
        if edited is None:
            QMessageBox.critical(self, "Confirm", "Title is required.")
            self.title_edit.setFocus()
            return
        self.value = ConfirmResult(action="confirm", meta=edited)
        self.accept()

    def _skip(self) -> None:
        self.value = ConfirmResult(action="skip")
        self.accept()

    def _quit(self) -> None:
        self.value = ConfirmResult(action="quit")
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Same Escape-means-Skip / window-close-means-Quit split as
        # CandidateDialog -- see its keyPressEvent for the full rationale.
        if event.key() == Qt.Key.Key_Escape:
            self._skip()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        self.value = ConfirmResult(action="quit")
        super().reject()


_DIALOG_CLASSES = {
    "batch_series": BatchSeriesDialog,
    "candidate": CandidateDialog,
    "manual_entry": ManualEntryDialog,
    "confirm": ConfirmDialog,
    "series": SeriesDialog,
}


# ---------------------------------------------------------------------------
# The Tag tab itself
# ---------------------------------------------------------------------------


class TagTab(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.config_ = config

        layout = QVBoxLayout(self)
        layout.addWidget(_make_description_label(
            "Match and tag PDF(s) interactively -- choose a book from the list provided, enter "
            "metadata by hand, or paste a known URL, then confirm each one before it's written. "
            "Works on a single file or every PDF in a folder; no review.csv involved."
        ))

        mode_row = QHBoxLayout()
        self.file_radio = QRadioButton("Single file")
        self.root_radio = QRadioButton("Whole folder")
        self.root_radio.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.file_radio)
        self.mode_group.addButton(self.root_radio)
        mode_row.addWidget(self.file_radio)
        mode_row.addWidget(self.root_radio)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(config.get("root", ""))
        path_row.addWidget(self.path_edit, 1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse)
        path_row.addWidget(browse_button)
        layout.addLayout(path_row)

        self.bookorbit_check = QCheckBox("BookOrbit mode (--bookorbit-mode)")
        layout.addWidget(self.bookorbit_check)
        layout.addWidget(_make_hint_label(BOOKORBIT_HINT))
        layout.addWidget(_make_link_label(BOOKORBIT_URL))

        self.convert_images_check = QCheckBox("Convert all images to RGB JPEG (--convert-images)")
        layout.addWidget(self.convert_images_check)
        layout.addWidget(_make_hint_label(CONVERT_IMAGES_HINT))

        self.hyperlink_gurps_check = QCheckBox("Hyperlink GURPS page/chapter references (--hyperlink-gurps)")
        layout.addWidget(self.hyperlink_gurps_check)
        layout.addWidget(_make_hint_label(HYPERLINK_GURPS_HINT))

        self.rename_check = QCheckBox("Rename after write (--rename)")
        layout.addWidget(self.rename_check)
        layout.addWidget(_make_hint_label(RENAME_HINT))

        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self._start)
        layout.addWidget(self.start_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.log_widget = _make_log_widget()
        layout.addWidget(self.log_widget, 1)

        self.stop_event = threading.Event()
        self._thread: QThread | None = None
        self._worker: TagWorker | None = None

    def _browse(self) -> None:
        if self.file_radio.isChecked():
            p, _ = QFileDialog.getOpenFileName(self, "Select PDF", "", "PDF files (*.pdf)")
        else:
            p = QFileDialog.getExistingDirectory(self, "Root folder", self.path_edit.text() or ".")
        if p:
            self.path_edit.setText(p)

    def _log_line(self, line: str) -> None:
        self.log_widget.appendPlainText(line)

    def _start(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        path_str = self.path_edit.text().strip()
        if not path_str:
            QMessageBox.critical(self, "Tag", "No file/folder given.")
            return

        root_mode = self.root_radio.isChecked()
        if root_mode:
            root = Path(path_str)
            pdfs = scan_pdfs(root)
            if not pdfs:
                QMessageBox.critical(self, "Tag", f"No PDFs found under {root}")
                return
            known_urls = load_known_urls(root)
        else:
            path = Path(path_str)
            if not path.exists():
                QMessageBox.critical(self, "Tag", f"File not found: {path}")
                return
            if path.suffix.lower() != ".pdf":
                QMessageBox.critical(self, "Tag", f"Not a PDF: {path}")
                return
            pdfs = [path]
            known_urls = load_known_urls(path.parent)

        try:
            client = build_client_safe(self.config_)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Tag", str(exc))
            return

        manual_overrides_path = Path(self.config_.get("manual_overrides", "data/manual_overrides.yaml"))
        manual_overrides = load_manual_overrides(manual_overrides_path)
        thresholds = self.config_.get("matching", {})

        self.stop_event = threading.Event()
        self._thread = QThread()
        self._worker = TagWorker(
            pdfs=pdfs, client=client, manual_overrides=manual_overrides, known_urls=known_urls,
            thresholds=thresholds, bookorbit_mode=self.bookorbit_check.isChecked(),
            rename=self.rename_check.isChecked(), root_mode=root_mode, stop_event=self.stop_event,
            convert_images=self.convert_images_check.isChecked(),
            hyperlink_gurps=self.hyperlink_gurps_check.isChecked(),
            gurps_hyperlink_script=self.config_.get("gurps_hyperlink_script", str(DEFAULT_HYPERLINK_SCRIPT)),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log_line.connect(self._log_line)
        self._worker.dialog_requested.connect(self._on_dialog_requested)
        self._worker.finished.connect(self._on_run_finished)

        self.start_button.setEnabled(False)
        self._thread.start()

    @pyqtSlot(str, dict, object)
    def _on_dialog_requested(self, kind: str, payload: dict, response_q) -> None:
        dialog = _DIALOG_CLASSES[kind](self, payload)
        dialog.exec()
        response_q.put(dialog.value)

    def _on_run_finished(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None
        self.start_button.setEnabled(True)

    def stop(self) -> None:
        """Called when the whole GUI window is closing. Setting
        stop_event ensures the worker's *next* TagFlowController._ask()
        call returns a quit result immediately rather than emitting a
        fresh dialog request. Unlike the Tkinter version, there's no
        separate pending-request backlog to drain here:
        `_on_dialog_requested` runs `.exec()` synchronously on the GUI
        thread, so by the time this method can even run, no dialog
        request is left unanswered -- a modal dialog blocks the entire
        GUI event loop while it's up, so the window can't be in the
        process of closing at the same time one is open. The remaining
        case (the worker still blocked on in-flight network/pikepdf I/O,
        with no `_ask()` call pending yet) is simply abandoned, the same
        as the Tkinter version's daemon thread was."""
        self.stop_event.set()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(200)
