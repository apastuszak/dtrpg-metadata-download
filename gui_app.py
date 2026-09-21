"""PyQt6 desktop GUI for dtrpg-metadata-download, covering every
subcommand in one window (Tag/Scan/Review/Write PDFs/Rename/All).

Presentation layer only, exactly like tag_tui.py is for `tag` alone: every
tab here calls straight into matcher.py/review.py/pdf_writer.py/renamer.py
-- the same functions dtrpg-metadata-download.py's own cmd_* functions
call -- none of those modules know or care that a GUI exists. The Tag
tab's own flow/dialog code lives in gui_tag_flow.py (mirroring tag_tui.py's
size and scope), imported here as one more tab.

This replaced an earlier Tkinter version of this GUI. The switch was
prompted by an unresolved widget-rendering bug (a custom-bordered,
scrollable Description field that misaligned and never showed a focus
ring on one real machine, despite being verified correct by every means
short of running it there) that turned out to be the latest in a run of
subtle Tk/Tcl platform-rendering quirks this project kept hitting -- all
traceable to Tk delegating widget painting to native OS APIs. Qt paints
its own widgets, so this whole class of bug doesn't have room to exist;
see CLAUDE.md's "Desktop GUI" section for the fuller history.

Threading: every tab that can block (network calls, pikepdf I/O) runs its
work on a background QThread via UiTaskRunner, whose log lines cross back
to the GUI thread through a queued Qt signal connection -- event-driven,
no polling loop needed (unlike the Tkinter version's queue.Queue +
root.after() polling). Only one job per tab at a time (the Run button is
disabled while busy). The Tag tab needs a richer version of this same
idea (a blocking request/response round trip for its interactive
dialogs, not just fire-and-forget log lines) -- see gui_tag_flow.py.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QObject, QRectF, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dtrpg_client import DtrpgClient
from matcher import load_known_urls, load_manual_overrides, run_scan_batch, scan_pdfs
from pdf_writer import write_approved
from preferences import DEFAULT_PREFERENCES_PATH, Preferences, load_preferences, resolve_api_key, save_preferences
from provenance import Status
from renamer import apply_rename, plan_rename
from review import ReviewRow, load_review, save_review
from rpg_grayscale import DEFAULT_GRAYSCALE_SCRIPT
from rpg_hyperlink import DEFAULT_GURPS_HYPERLINK_SCRIPT, DEFAULT_MONGOOSE_HYPERLINK_SCRIPT

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
CONVERT_GRAYSCALE_HINT = (
    "Converts the whole PDF to grayscale using a separate script on this machine (requires "
    "Ghostscript -- 'gs' -- on PATH, and grayscale_script set in config.yaml). Mutually exclusive "
    "with converting to RGB above -- checking one unchecks the other."
)
RENAME_HINT = "Renames the file after the metadata update, using the format: Series Name - Book Name.pdf"
HYPERLINK_GURPS_HINT = (
    "Auto-hyperlinks in-text page and chapter references (e.g. \"see p. 208\") using a separate "
    "GURPS-specific script on this machine. Only useful for GURPS PDFs; requires "
    "gurps_hyperlink_script to be set in config.yaml."
)
HYPERLINK_MONGOOSE_HINT = (
    "Auto-hyperlinks in-text page and chapter references using a separate script specific to "
    "Mongoose Publishing's Traveller line. Only useful for Mongoose Traveller PDFs; requires "
    "mongoose_hyperlink_script to be set in config.yaml."
)

# QLineEdit gets a native-looking grey border (blue on focus) for free from
# Qt's macOS style, since qmacstyle draws it the way a real NSTextField
# would. QPlainTextEdit/QTextEdit don't get that same native treatment --
# their frame is just a generic QFrame panel (a plain black/dark sunken
# rectangle on this style, with no focus-color change at all) since Qt has
# no equivalent native-control mapping for a multi-line text view the way
# it does for a single-line field. This is a real, verified Qt/macOS
# styling gap, not a leftover from the Tkinter version this GUI replaced
# (which had the identical visual symptom for the identical underlying
# reason -- Tk's plain Text widget doesn't get single-line Entry's native
# treatment either -- but had to be worked around by hand there, since Tk
# has no declarative focus-state styling; Qt's QSS supports a `:focus`
# pseudo-state natively, so this is one applied stylesheet rule instead
# of manual FocusIn/FocusOut bindings and a forced repaint).
#
# Applied per-widget via _style_text_edit(), not app-wide via
# QApplication.setStyleSheet() -- that was tried first and was a real
# regression, not just an unnecessary broad brush: setting *any*
# stylesheet on the QApplication (even one whose only selector is
# QPlainTextEdit) makes Qt route *all* widget painting through its
# QStyleSheetStyle proxy instead of the native macOS style, which is
# exactly what was suppressing QLineEdit's own native blue focus glow
# app-wide (confirmed by a real screenshot: every field's blue focus ring
# disappeared, not just the ones this stylesheet targets). A stylesheet
# set directly on one widget only affects that widget (and its children),
# leaving sibling QLineEdits on native rendering untouched.
_TEXT_BORDER_COLOR = "#ebebeb"
_TEXT_BORDER_FOCUS_COLOR = "#0a84ff"
_TEXT_EDIT_STYLE = f"""
QPlainTextEdit {{
    border: 1px solid {_TEXT_BORDER_COLOR};
    border-radius: 5px;
}}
QPlainTextEdit:focus {{
    border: 1px solid {_TEXT_BORDER_FOCUS_COLOR};
}}
"""


class _FocusRingEffect(QGraphicsEffect):
    """Paints a soft rounded-rectangle ring around a widget when enabled
    -- Qt's own QGraphicsDropShadowEffect was tried first and rejected
    after a direct side-by-side screenshot comparison: a real macOS
    NSTextField focus ring (sampled from a real screenshot) is a fairly
    uniform-width, pale, mostly-flat band, while QGraphicsDropShadowEffect's
    Gaussian blur inherently produces a darker/denser edge that fades out
    over a distance no matter how its blur radius/opacity were tuned --
    visibly thinner and more "shadow-like", less "ring-like", than the
    real thing. A QGraphicsEffect subclass with its own draw() gives full
    control over the band's shape instead of fighting a Gaussian falloff.
    QGraphicsEffect (unlike a plain paintEvent override) can paint outside
    a widget's own geometry -- boundingRectFor() below is what makes Qt
    reserve that extra space instead of clipping the ring away."""

    RING_WIDTH = 3.0  # logical px, close to the ~3.5px measured from a real focused QLineEdit
    RING_MARGIN = 0.0  # flush against the widget's own border -- a real screenshot showed a
    # visible gap here at 2.0px, which native doesn't have: the ring sits right up against the
    # control's own border with no white space between them.
    RING_RADIUS = 6.0  # logical px -- 8 looked distinctly rounder than native's own corners

    def boundingRectFor(self, rect: QRectF) -> QRectF:
        pad = self.RING_WIDTH + self.RING_MARGIN
        return rect.adjusted(-pad, -pad, pad, pad)

    def draw(self, painter: QPainter) -> None:
        source_rect = self.sourceBoundingRect()
        pen = QPen(QColor(0x0A, 0x84, 0xFF, 130))
        pen.setWidthF(self.RING_WIDTH)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        offset = self.RING_MARGIN + self.RING_WIDTH / 2
        ring_rect = source_rect.adjusted(-offset, -offset, offset, offset)
        painter.drawRoundedRect(ring_rect, self.RING_RADIUS, self.RING_RADIUS)
        painter.restore()
        self.drawSource(painter)


class _StyledTextEdit(QPlainTextEdit):
    """A QPlainTextEdit that actually looks and behaves like the other
    fields around it -- three real, independently-verified rendering gaps
    a bare QPlainTextEdit + a stylesheet still didn't fix, each confirmed
    with an actual screenshot before being called done (this project's
    established verify-against-the-real-thing habit, not just reasoning
    about what Qt "should" do):

    1. QPlainTextEdit is a QAbstractScrollArea, which paints its own
       native QFrame panel independently of a QSS `border` property --
       setting the QSS border alone left that native frame still visible
       along the four straight edges, with only the QSS's rounded corners
       showing through where the square native frame didn't cover.
       Fixed by disabling the native frame (setFrameShape(NoFrame)) so
       only the QSS-drawn border remains.
    2. `_TEXT_BORDER_COLOR` originally guessed a plausible-looking grey
       (#a3a3a3) -- it rendered clearly darker than a real QLineEdit's
       native border once both were on screen together, closer to black
       than grey by comparison. Fixed by sampling the actual rendered
       pixel color of a real QLineEdit's border from a screenshot
       (#ebebeb) and matching it exactly, rather than eyeballing a value.
    3. The `:focus` QSS pseudo-state was never repainting on a real focus
       change at all, even with the border/frame fixes above and a
       confirmed-correct hasFocus()==True: QAbstractScrollArea delivers
       keyboard focus through its internal viewport() child, not the
       outer widget the stylesheet targets, so the outer widget never got
       its own focusInEvent()-triggered repaint the way a plain QWidget
       (e.g. QLineEdit) would. Confirmed by forcing
       style().unpolish()/.polish()/.update() by hand after setFocus()
       and seeing the blue border finally appear in a screenshot that was
       otherwise identical. Fixed below by doing exactly that
       automatically on every focus change, which is what Qt would
       normally do on its own for a plain QWidget.
    4. Even with 1-3 fixed, a real focused QLineEdit doesn't just get a
       thicker/bluer 1px border -- pixel-sampling a real screenshot showed
       macOS actually draws a separate soft ~3px ring *outside* the
       control's own (still-thin) border, which a flat QSS `border` can
       never reproduce. A `2px solid` QSS border, then a
       QGraphicsDropShadowEffect, were each tried and each compared
       directly against a real screenshot side by side with the real
       thing -- both were visibly wrong in a different way (too flat;
       too dark/thin and shadow-like instead of an even pale band).
       Replaced with `_FocusRingEffect` above (a custom QGraphicsEffect,
       not a stock one), which paints the ring shape directly instead of
       approximating it through a border property or a blur algorithm --
       see that class's own docstring for why the stock effect wasn't
       enough.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(_TEXT_EDIT_STYLE)
        self._focus_glow = _FocusRingEffect(self)
        self._focus_glow.setEnabled(False)
        self.setGraphicsEffect(self._focus_glow)

    def _repolish(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self._focus_glow.setEnabled(True)
        self._repolish()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self._focus_glow.setEnabled(False)
        self._repolish()


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


class _FnWorker(QObject):
    """Runs one plain function on whatever thread it's moved to, then
    emits `finished`. `log` is a bound UiTaskRunner.log -- already safe to
    call from this (non-GUI) thread, since it only emits a Qt signal
    (thread-safe by construction) rather than touching a widget directly."""

    finished = pyqtSignal()

    def __init__(self, fn: Callable, args: tuple, kwargs: dict, log: Callable[[str], None]):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._log = log

    def run(self) -> None:
        try:
            self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            self._log(f"ERROR: {exc}")
        self.finished.emit()


class UiTaskRunner(QObject):
    """Runs one target function on a background QThread and forwards its
    plain-string progress calls to a log widget. Replaces the Tkinter
    version's queue.Queue()+root.after() polling with genuine event-driven
    delivery -- Qt automatically marshals a signal emitted from a worker
    thread onto the thread its connected slot's receiver lives on (here,
    the GUI thread), so `log()` is safe to call from the worker thread
    without any polling loop on this side. Only one job at a time (the Run
    button disables itself while busy); no job queue/executor needed."""

    log_line = pyqtSignal(str)

    def __init__(self, log_widget: QPlainTextEdit, on_done: Callable[[], None] | None = None):
        super().__init__()
        self.log_widget = log_widget
        self.on_done = on_done
        self.log_line.connect(self._append)
        self._thread: QThread | None = None
        self._worker: _FnWorker | None = None

    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, fn: Callable, *args, **kwargs) -> None:
        if self.busy():
            return
        self._thread = QThread()
        self._worker = _FnWorker(fn, args, kwargs, self.log)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def log(self, line: str) -> None:
        self.log_line.emit(line)

    def _append(self, line: str) -> None:
        self.log_widget.appendPlainText(line)

    def _on_finished(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None
        if self.on_done is not None:
            self.on_done()


def _make_description_label(text: str) -> QLabel:
    """Short explanatory text at the top of a tab, saying what that tab
    does -- default (not muted) text color, since it's the primary
    orientation for the whole tab rather than a secondary caveat like
    _make_hint_label() below."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _make_hint_label(text: str) -> QLabel:
    """Small muted explanatory text under a checkbox."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #666666;")
    return label


def _make_link_label(url: str) -> QLabel:
    """A real clickable hyperlink label -- unlike the Tkinter version,
    which had to fake this with a colored/underlined font and a manual
    webbrowser.open() click binding (ttk has no hyperlink widget), Qt's
    QLabel supports rich text and opening links itself."""
    label = QLabel(f'<a href="{url}">{url}</a>')
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setOpenExternalLinks(True)
    return label


def _make_log_widget() -> QPlainTextEdit:
    widget = _StyledTextEdit()
    widget.setReadOnly(True)
    return widget


def _make_mutually_exclusive(a: QCheckBox, b: QCheckBox) -> None:
    """Checking either box unchecks the other -- used for --convert-images
    vs --convert-grayscale (opposite operations on the same images).
    `toggled` only fires on an actual state change, so `setChecked(False)`
    on an already-unchecked box is a no-op, not an infinite signal loop."""
    a.toggled.connect(lambda checked: b.setChecked(False) if checked else None)
    b.toggled.connect(lambda checked: a.setChecked(False) if checked else None)


# ---------------------------------------------------------------------------
# Shared scan/write-pdfs/rename logic -- factored out once so ScanTab/
# WritePdfsTab/RenameTab/AllTab don't each keep their own copy (the same
# reasoning that moved cmd_scan's loop into matcher.run_scan_batch()).
# None of these functions touch Qt (or touched tkinter before them) --
# they take a plain `log` callable, so the Tk->Qt port needed no changes
# to their bodies at all, only to their callers.
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


def _do_write_pdfs(
    log, rows: list[ReviewRow], root: str, bookorbit_mode: bool, convert_images: bool = False,
    convert_grayscale: bool = False, grayscale_script: str = str(DEFAULT_GRAYSCALE_SCRIPT),
    hyperlink_gurps: bool = False, gurps_hyperlink_script: str = str(DEFAULT_GURPS_HYPERLINK_SCRIPT),
    hyperlink_mongoose: bool = False, mongoose_hyperlink_script: str = str(DEFAULT_MONGOOSE_HYPERLINK_SCRIPT),
) -> None:
    if not any(r.is_approved() for r in rows):
        log("No approved/auto-accepted rows to write.")
        return
    results = write_approved(
        rows, root, bookorbit_mode=bookorbit_mode, convert_images=convert_images,
        convert_grayscale=convert_grayscale, grayscale_script=grayscale_script,
        hyperlink_gurps=hyperlink_gurps, gurps_hyperlink_script=gurps_hyperlink_script,
        hyperlink_mongoose=hyperlink_mongoose, mongoose_hyperlink_script=mongoose_hyperlink_script, log=log,
    )
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


class ScanTab(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.config_ = config

        layout = QVBoxLayout(self)
        layout.addWidget(_make_description_label(
            "Matches every PDF under a folder against DriveThruRPG and writes the results to "
            "review.csv -- never touches your PDFs directly. Approve matches here or in the "
            "Review tab, then use Write PDFs (or All) to apply them."
        ))

        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Root folder:"))
        self.root_edit = QLineEdit(config.get("root", ""))
        root_row.addWidget(self.root_edit, 1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse)
        root_row.addWidget(browse_button)
        layout.addLayout(root_row)

        self.refresh_check = QCheckBox("Refresh library (--refresh-library)")
        layout.addWidget(self.refresh_check)
        self.apply_review_check = QCheckBox("Only match new files (--apply-review)")
        layout.addWidget(self.apply_review_check)

        self.run_button = QPushButton("Run Scan")
        self.run_button.clicked.connect(self._run)
        layout.addWidget(self.run_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.log = _make_log_widget()
        layout.addWidget(self.log, 1)
        self.runner = UiTaskRunner(self.log, on_done=self._on_done)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Root folder", self.root_edit.text() or ".")
        if d:
            self.root_edit.setText(d)

    def _run(self) -> None:
        root = self.root_edit.text().strip()
        if not root:
            QMessageBox.critical(self, "Scan", "No root folder given.")
            return
        self.run_button.setEnabled(False)
        review_csv = Path(self.config_.get("review_csv", "data/review.csv"))
        manual_overrides_path = Path(self.config_.get("manual_overrides", "data/manual_overrides.yaml"))
        thresholds = self.config_.get("matching", {})
        self.runner.start(
            _do_scan, self.config_, self.runner.log, root, review_csv, manual_overrides_path,
            thresholds, self.refresh_check.isChecked(), self.apply_review_check.isChecked(),
        )

    def _on_done(self) -> None:
        self.run_button.setEnabled(True)


class WritePdfsTab(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.config_ = config

        layout = QVBoxLayout(self)
        layout.addWidget(_make_description_label(
            "Writes metadata into every PDF whose review.csv row is approved or auto-accepted -- "
            "this step never matches files itself. Run Scan (and approve rows in Review) first."
        ))

        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Root folder:"))
        self.root_edit = QLineEdit(config.get("root", ""))
        root_row.addWidget(self.root_edit, 1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse)
        root_row.addWidget(browse_button)
        layout.addLayout(root_row)

        self.bookorbit_check = QCheckBox("BookOrbit mode (--bookorbit-mode)")
        layout.addWidget(self.bookorbit_check)
        layout.addWidget(_make_hint_label(BOOKORBIT_HINT))
        layout.addWidget(_make_link_label(BOOKORBIT_URL))

        self.convert_images_check = QCheckBox("Convert all images to RGB JPEG (--convert-images)")
        layout.addWidget(self.convert_images_check)
        layout.addWidget(_make_hint_label(CONVERT_IMAGES_HINT))

        self.convert_grayscale_check = QCheckBox("Convert PDF to grayscale (--convert-grayscale)")
        layout.addWidget(self.convert_grayscale_check)
        layout.addWidget(_make_hint_label(CONVERT_GRAYSCALE_HINT))
        _make_mutually_exclusive(self.convert_images_check, self.convert_grayscale_check)

        self.hyperlink_gurps_check = QCheckBox("Hyperlink GURPS page/chapter references (--hyperlink-gurps)")
        layout.addWidget(self.hyperlink_gurps_check)
        layout.addWidget(_make_hint_label(HYPERLINK_GURPS_HINT))

        self.hyperlink_mongoose_check = QCheckBox(
            "Hyperlink Mongoose Traveller page/chapter references (--hyperlink-mongoose)"
        )
        layout.addWidget(self.hyperlink_mongoose_check)
        layout.addWidget(_make_hint_label(HYPERLINK_MONGOOSE_HINT))

        self.run_button = QPushButton("Write Approved PDFs")
        self.run_button.clicked.connect(self._run)
        layout.addWidget(self.run_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.log = _make_log_widget()
        layout.addWidget(self.log, 1)
        self.runner = UiTaskRunner(self.log, on_done=self._on_done)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Root folder", self.root_edit.text() or ".")
        if d:
            self.root_edit.setText(d)

    def _run(self) -> None:
        root = self.root_edit.text().strip()
        if not root:
            QMessageBox.critical(self, "Write PDFs", "No root folder given.")
            return
        self.run_button.setEnabled(False)
        review_csv = Path(self.config_.get("review_csv", "data/review.csv"))
        grayscale_script = self.config_.get("grayscale_script", str(DEFAULT_GRAYSCALE_SCRIPT))
        gurps_hyperlink_script = self.config_.get("gurps_hyperlink_script", str(DEFAULT_GURPS_HYPERLINK_SCRIPT))
        mongoose_hyperlink_script = self.config_.get(
            "mongoose_hyperlink_script", str(DEFAULT_MONGOOSE_HYPERLINK_SCRIPT)
        )
        self.runner.start(
            self._worker, review_csv, root, self.bookorbit_check.isChecked(), self.convert_images_check.isChecked(),
            self.convert_grayscale_check.isChecked(), grayscale_script,
            self.hyperlink_gurps_check.isChecked(), gurps_hyperlink_script,
            self.hyperlink_mongoose_check.isChecked(), mongoose_hyperlink_script,
        )

    def _worker(
        self, review_csv: Path, root: str, bookorbit_mode: bool, convert_images: bool,
        convert_grayscale: bool, grayscale_script: str,
        hyperlink_gurps: bool, gurps_hyperlink_script: str,
        hyperlink_mongoose: bool, mongoose_hyperlink_script: str,
    ) -> None:
        rows = load_review(review_csv)
        _do_write_pdfs(
            self.runner.log, rows, root, bookorbit_mode, convert_images,
            convert_grayscale, grayscale_script,
            hyperlink_gurps, gurps_hyperlink_script, hyperlink_mongoose, mongoose_hyperlink_script,
        )

    def _on_done(self) -> None:
        self.run_button.setEnabled(True)


class RenameTab(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.config_ = config

        layout = QVBoxLayout(self)
        layout.addWidget(_make_description_label(
            "Renames an already-tagged PDF (and its .bak/.opf/.metadata.json sidecars) to "
            '"Series Name - Book Name.pdf", using the title/series recorded in its '
            ".metadata.json sidecar. Untagged or already-correctly-named files are skipped."
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

        # False by default, matching the CLI's own --dry-run flag (which
        # is action="store_true" -- off unless passed, so `rename` really
        # renames by default). Defaulting this checkbox to True would
        # invert that: clicking "Run Rename" without noticing/unchecking
        # it first would silently only preview, never actually renaming --
        # a real bug this GUI shipped with once already (see CLAUDE.md).
        self.dry_run_check = QCheckBox("Dry run (preview only)")
        layout.addWidget(self.dry_run_check)

        self.run_button = QPushButton("Run Rename")
        self.run_button.clicked.connect(self._run)
        layout.addWidget(self.run_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.log = _make_log_widget()
        layout.addWidget(self.log, 1)
        self.runner = UiTaskRunner(self.log, on_done=self._on_done)

    def _browse(self) -> None:
        if self.file_radio.isChecked():
            p, _ = QFileDialog.getOpenFileName(self, "Select PDF", "", "PDF files (*.pdf)")
        else:
            p = QFileDialog.getExistingDirectory(self, "Root folder", self.path_edit.text() or ".")
        if p:
            self.path_edit.setText(p)

    def _run(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.critical(self, "Rename", "No file/folder given.")
            return
        self.run_button.setEnabled(False)
        mode = "file" if self.file_radio.isChecked() else "root"
        self.runner.start(self._worker, mode, path, self.dry_run_check.isChecked())

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
        self.run_button.setEnabled(True)


class AllTab(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.config_ = config

        layout = QVBoxLayout(self)
        layout.addWidget(_make_description_label(
            "Runs Scan, then Write PDFs, in one pass -- still gated by review.csv status, so "
            "anything left unapproved (needs-review/no-match) isn't written."
        ))

        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Root folder:"))
        self.root_edit = QLineEdit(config.get("root", ""))
        root_row.addWidget(self.root_edit, 1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse)
        root_row.addWidget(browse_button)
        layout.addLayout(root_row)

        self.refresh_check = QCheckBox("Refresh library (--refresh-library)")
        layout.addWidget(self.refresh_check)
        self.apply_review_check = QCheckBox("Only match new files (--apply-review)")
        layout.addWidget(self.apply_review_check)

        self.bookorbit_check = QCheckBox("BookOrbit mode (--bookorbit-mode)")
        layout.addWidget(self.bookorbit_check)
        layout.addWidget(_make_hint_label(BOOKORBIT_HINT))
        layout.addWidget(_make_link_label(BOOKORBIT_URL))

        self.convert_images_check = QCheckBox("Convert all images to RGB JPEG (--convert-images)")
        layout.addWidget(self.convert_images_check)
        layout.addWidget(_make_hint_label(CONVERT_IMAGES_HINT))

        self.convert_grayscale_check = QCheckBox("Convert PDF to grayscale (--convert-grayscale)")
        layout.addWidget(self.convert_grayscale_check)
        layout.addWidget(_make_hint_label(CONVERT_GRAYSCALE_HINT))
        _make_mutually_exclusive(self.convert_images_check, self.convert_grayscale_check)

        self.run_button = QPushButton("Run Scan + Write PDFs")
        self.run_button.clicked.connect(self._run)
        layout.addWidget(self.run_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.log = _make_log_widget()
        layout.addWidget(self.log, 1)
        self.runner = UiTaskRunner(self.log, on_done=self._on_done)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Root folder", self.root_edit.text() or ".")
        if d:
            self.root_edit.setText(d)

    def _run(self) -> None:
        root = self.root_edit.text().strip()
        if not root:
            QMessageBox.critical(self, "All", "No root folder given.")
            return
        self.run_button.setEnabled(False)
        review_csv = Path(self.config_.get("review_csv", "data/review.csv"))
        manual_overrides_path = Path(self.config_.get("manual_overrides", "data/manual_overrides.yaml"))
        thresholds = self.config_.get("matching", {})
        grayscale_script = self.config_.get("grayscale_script", str(DEFAULT_GRAYSCALE_SCRIPT))
        self.runner.start(
            self._worker, root, review_csv, manual_overrides_path, thresholds,
            self.refresh_check.isChecked(), self.apply_review_check.isChecked(), self.bookorbit_check.isChecked(),
            self.convert_images_check.isChecked(), self.convert_grayscale_check.isChecked(), grayscale_script,
        )

    def _worker(
        self, root: str, review_csv: Path, manual_overrides_path: Path, thresholds: dict,
        refresh_library: bool, apply_review: bool, bookorbit_mode: bool, convert_images: bool,
        convert_grayscale: bool, grayscale_script: str,
    ) -> None:
        log = self.runner.log
        merged = _do_scan(self.config_, log, root, review_csv, manual_overrides_path, thresholds, refresh_library, apply_review)
        _do_write_pdfs(log, merged, root, bookorbit_mode, convert_images, convert_grayscale, grayscale_script)

    def _on_done(self) -> None:
        self.run_button.setEnabled(True)


class StatusDelegate(QStyledItemDelegate):
    """Editor for the Review grid's `status` column: a closed-choice
    QComboBox locked to Status's four values -- free text here would
    silently break ReviewRow.is_approved()'s exact string match.
    Structurally impossible to enter anything else, replacing the Tkinter
    version's hand-rolled .bbox()-hit-tested ttk.Combobox overlay (which
    had its own real bug: Treeview.identify_column() and .bbox() disagreed
    about column boundaries on one Tk build -- see CLAUDE.md). Qt's item
    delegates place/size the editor internally, so there's no manual hit-
    testing left to get wrong."""

    def createEditor(self, parent, option, index):  # noqa: N802 (Qt override naming)
        combo = QComboBox(parent)
        combo.addItems([s.value for s in Status])
        combo.activated.connect(self._commit_and_close)
        return combo

    def _commit_and_close(self, _index: int) -> None:
        editor = self.sender()
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor, index):  # noqa: N802
        current = index.data(Qt.ItemDataRole.DisplayRole) or ""
        pos = editor.findText(current)
        if pos >= 0:
            editor.setCurrentIndex(pos)

    def setModelData(self, editor, model, index):  # noqa: N802
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)


class _DescriptionEdit(_StyledTextEdit):
    """QPlainTextEdit has no built-in "editing finished" signal the way
    QLineEdit does -- this adds one on focus-out, so the Review tab's
    detail form can commit a description edit the same way it commits
    every other field (on losing focus), without needing a separate
    explicit "apply" button."""

    focus_lost = pyqtSignal()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.focus_lost.emit()


class ReviewTab(QWidget):
    """The scan -> approve -> write-pdfs workflow's approval step, done
    entirely in-GUI instead of handing off to a spreadsheet app. Reads/
    writes review.csv unchanged (review.load_review/save_review).
    `status`/`series`/`series_index` are editable directly in the grid
    (status via StatusDelegate above; series/series_index via Qt's own
    default line-edit cell editor -- no custom delegate needed for those,
    see StatusDelegate's docstring for why a delegate is only needed where
    free text would be wrong). Everything else (publisher/authors/tags/
    product_url/isbn, and a multi-line field for description) is edited
    via the detail form below the grid, populated on row selection. Both
    surfaces write into the same in-memory rows_by_filename dict (the
    single source of truth) so the grid and the detail form can't diverge.
    """

    GRID_COLUMNS = [
        "filename", "matched_title", "series", "series_index",
        "publisher", "confidence_score", "source", "status", "isbn",
    ]
    EDITABLE_COLUMNS = {"series", "series_index", "status"}
    DETAIL_FIELDS = ["publisher", "authors", "tags", "product_url", "isbn"]

    def __init__(self, config: dict):
        super().__init__()
        self.review_csv = Path(config.get("review_csv", "data/review.csv"))
        self.rows_by_filename: dict[str, ReviewRow] = {}
        self._row_of_filename: dict[str, int] = {}
        self._selected_filename: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(_make_description_label(
            "Approve or edit matches from review.csv directly -- change a row's status, fix its "
            "series, or edit its description -- then Save. Reload picks up a fresh Scan or an "
            "external hand-edit."
        ))

        toolbar = QHBoxLayout()
        reload_button = QPushButton("Reload")
        reload_button.clicked.connect(self.reload)
        toolbar.addWidget(reload_button)
        save_button = QPushButton("Save")
        save_button.clicked.connect(self.save)
        toolbar.addWidget(save_button)
        self.counts_label = QLabel("")
        toolbar.addWidget(self.counts_label)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, len(self.GRID_COLUMNS))
        self.table.setHorizontalHeaderLabels(self.GRID_COLUMNS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setItemDelegateForColumn(self.GRID_COLUMNS.index("status"), StatusDelegate(self.table))
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.currentCellChanged.connect(self._on_current_cell_changed)
        layout.addWidget(self.table, 1)

        detail_box = QGroupBox("Details for selected row")
        detail_layout = QFormLayout(detail_box)
        self.detail_edits: dict[str, QLineEdit] = {}
        for field in self.DETAIL_FIELDS:
            edit = QLineEdit()
            edit.editingFinished.connect(lambda f=field: self._commit_detail_field(f))
            detail_layout.addRow(f"{field}:", edit)
            self.detail_edits[field] = edit

        self.description_edit = _DescriptionEdit()
        self.description_edit.setFixedHeight(100)
        self.description_edit.focus_lost.connect(self._commit_description)
        detail_layout.addRow("description:", self.description_edit)
        layout.addWidget(detail_box)

        self.reload()

    def reload(self) -> None:
        rows = load_review(self.review_csv)
        self.rows_by_filename = {row.filename: row for row in rows}
        self._selected_filename = None
        self._populate_table()

    def save(self) -> None:
        save_review(self.review_csv, list(self.rows_by_filename.values()))
        QMessageBox.information(self, "Review", f"Saved {len(self.rows_by_filename)} rows to {self.review_csv}")

    def _populate_table(self) -> None:
        # Must block signals here -- QTableWidget.itemChanged fires for
        # setItem() just as it does for a real user edit, and without this
        # guard, populating N rows would fire N spurious commits into
        # rows_by_filename (each one populating from data that's still
        # only partially loaded) before the table is even done being
        # built. No Tkinter analog: ttk.Treeview has no equivalent signal
        # that fires during bulk insert.
        self.table.blockSignals(True)
        rows = list(self.rows_by_filename.values())
        self.table.setRowCount(len(rows))
        self._row_of_filename = {}
        for r, row in enumerate(rows):
            self._row_of_filename[row.filename] = r
            for c, col in enumerate(self.GRID_COLUMNS):
                item = QTableWidgetItem(getattr(row, col))
                if col not in self.EDITABLE_COLUMNS:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(r, c, item)
        self.table.blockSignals(False)
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        counts = Counter(row.status for row in self.rows_by_filename.values())
        summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
        self.counts_label.setText(f"{len(self.rows_by_filename)} rows: {summary}")

    def _row_filename(self, row_index: int) -> str | None:
        item = self.table.item(row_index, self.GRID_COLUMNS.index("filename"))
        return item.text() if item is not None else None

    def _on_current_cell_changed(self, current_row: int, _current_col: int, _prev_row: int, _prev_col: int) -> None:
        if current_row < 0:
            self._selected_filename = None
            return
        filename = self._row_filename(current_row)
        self._selected_filename = filename
        if filename is None:
            return
        row = self.rows_by_filename[filename]
        for field in self.DETAIL_FIELDS:
            self.detail_edits[field].setText(getattr(row, field))
        self.description_edit.setPlainText(row.description)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        filename = self._row_filename(item.row())
        if filename is None:
            return
        col = self.GRID_COLUMNS[item.column()]
        setattr(self.rows_by_filename[filename], col, item.text())
        if col == "status":
            self._refresh_counts()

    def _commit_detail_field(self, field: str) -> None:
        if self._selected_filename is None:
            return
        row = self.rows_by_filename[self._selected_filename]
        setattr(row, field, self.detail_edits[field].text())
        if field in self.GRID_COLUMNS:
            self._set_table_cell(self._selected_filename, field, getattr(row, field))

    def _commit_description(self) -> None:
        if self._selected_filename is None:
            return
        self.rows_by_filename[self._selected_filename].description = self.description_edit.toPlainText()

    def _set_table_cell(self, filename: str, col_name: str, value: str) -> None:
        row_index = self._row_of_filename.get(filename)
        if row_index is None:
            return
        self.table.blockSignals(True)
        self.table.item(row_index, self.GRID_COLUMNS.index(col_name)).setText(value)
        self.table.blockSignals(False)


class PreferencesTab(QWidget):
    """Saved API key + DriveThruRPG name -- see preferences.py's module
    docstring for why these live in their own gitignored, owner-only-
    permissioned file rather than config.yaml. The API key field is
    masked by default (a real secret), with a checkbox to reveal it,
    matching preferences_tui.py's equivalent in the TUI."""

    def __init__(self, config: dict):
        super().__init__()
        self.preferences_path = Path(config.get("preferences", str(DEFAULT_PREFERENCES_PATH)))
        prefs = load_preferences(self.preferences_path)

        layout = QVBoxLayout(self)
        layout.addWidget(_make_description_label(
            f"Your DriveThruRPG API key and account name, saved to {self.preferences_path} -- "
            "not config.yaml, which is meant to be safe to share/commit. An existing "
            "DTRPG_API_KEY environment variable always takes priority over what's saved here."
        ))

        form = QFormLayout()
        self.api_key_edit = QLineEdit(prefs.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API Key:", self.api_key_edit)

        self.show_key_check = QCheckBox("Show API key")
        self.show_key_check.toggled.connect(self._toggle_show)
        form.addRow("", self.show_key_check)

        self.name_edit = QLineEdit(prefs.dtrpg_name)
        form.addRow("DriveThruRPG Name:", self.name_edit)
        layout.addLayout(form)

        save_row = QHBoxLayout()
        self.save_button = QPushButton("Save Preferences")
        self.save_button.clicked.connect(self._save)
        save_row.addWidget(self.save_button)
        self.status_label = QLabel("")
        save_row.addWidget(self.status_label)
        save_row.addStretch(1)
        layout.addLayout(save_row)
        layout.addStretch(1)

    def _toggle_show(self, checked: bool) -> None:
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)

    def _save(self) -> None:
        prefs = Preferences(api_key=self.api_key_edit.text().strip(), dtrpg_name=self.name_edit.text().strip())
        save_preferences(prefs, self.preferences_path)
        self.status_label.setText(f"Saved to {self.preferences_path}")


class GuiApp(QWidget):
    def __init__(self, config: dict):
        super().__init__()
        self.setWindowTitle("dtrpg-metadata-download")
        self.resize(950, 700)

        from gui_tag_flow import TagTab

        tabs = QTabWidget(self)
        self._tag_tab = TagTab(config)
        tabs.addTab(self._tag_tab, "Tag")
        tabs.addTab(ScanTab(config), "Scan")
        tabs.addTab(ReviewTab(config), "Review")
        tabs.addTab(WritePdfsTab(config), "Write PDFs")
        tabs.addTab(RenameTab(config), "Rename")
        tabs.addTab(AllTab(config), "All")
        tabs.addTab(PreferencesTab(config), "Preferences")

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Unblocks the Tag tab's worker thread if it's mid-run waiting on
        # a dialog answer that will now never come -- see TagTab.stop()'s
        # own docstring for why this is a best-effort courtesy, not a
        # correctness requirement.
        self._tag_tab.stop()
        super().closeEvent(event)


def run_gui(config: dict) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    window = GuiApp(config)
    window.show()
    app.exec()
