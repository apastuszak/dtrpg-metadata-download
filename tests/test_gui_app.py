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
"""Smoke tests for gui_app.py/gui_tag_flow.py -- a scoped exception to
this project's no-test-suite convention (see CLAUDE.md), same
justification as tests/test_tag_tui.py: the request/response protocol
between TagWorker's background QThread and TagTab's main-thread dialogs
is exactly the "wrong id / dropped handoff" class of bug that file was
created to catch for Textual's screen wiring, and it's easy to get subtly
wrong without actually driving it.

Uses PyQt6's own PyQt6.QtTest.QTest module for widget interaction rather
than pytest-qt -- this project has zero test-framework dependencies by
deliberate convention (plain runnable scripts, no pytest), and QtTest
ships inside PyQt6 itself, so it doesn't need one either. Run directly:
./tests/test_gui_app.py

Needs a usable Qt platform plugin. Unlike Tkinter's tk.Tk(), which raises
a catchable TclError with no display, QApplication() can hard-abort the
process in some genuinely display-less environments rather than raising a
catchable Python exception -- if this happens in your environment, set
QT_QPA_PLATFORM=offscreen before running (this repo has no CI today, so
that's a documented workaround, not something this script auto-detects).
"""

from __future__ import annotations

import queue
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

import pikepdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except ModuleNotFoundError as exc:
    print(f"SKIPPED: PyQt6 not available ({exc})")
    sys.exit(0)

from provenance import ProductMetadata, Source, Status  # noqa: E402
from review import ReviewRow, load_review, merge_by_filename, save_review  # noqa: E402
from tag_tui import CandidateResult, ConfirmResult, ManualEntryResult, build_manual_metadata  # noqa: E402
from gui_tag_flow import (  # noqa: E402
    CandidateDialog,
    ConfirmDialog,
    ManualEntryDialog,
    TagFlowController,
    WatermarkDialog,
)
from gui_app import ReviewTab  # noqa: E402
from watermark_removal import WatermarkDetection, WatermarkRemovalResult  # noqa: E402


def _client(**overrides) -> MagicMock:
    client = MagicMock()
    client.search_library.return_value = []
    client.search_catalog.return_value = []
    for name, value in overrides.items():
        getattr(client, name).return_value = value
    return client


def _drive_controller(pdfs, client, responses: dict, **controller_kwargs) -> TagFlowController:
    """Runs TagFlowController.run() on a background thread while this
    (the calling/main thread) answers each dialog request from a scripted
    `responses` mapping instead of building any real dialog -- tests the
    request/response contract itself without needing a display or Qt at
    all, since TagFlowController is framework-agnostic (it only needs a
    `notify(kind, payload, response_q)` callback, which this supplies
    directly instead of going through TagWorker/Qt signals)."""
    request_queue: queue.Queue = queue.Queue()
    kwargs = {
        "manual_overrides": {}, "known_urls": {}, "thresholds": {},
        "bookorbit_mode": False, "rename": False, "root_mode": False,
    }
    kwargs.update(controller_kwargs)
    controller = TagFlowController(
        pdfs=pdfs, client=client,
        notify=lambda kind, payload, response_q: request_queue.put((kind, payload, response_q)),
        log=lambda line: None, stop_event=threading.Event(),
        **kwargs,
    )
    thread = threading.Thread(target=controller.run, daemon=True)
    thread.start()
    while True:
        try:
            kind, payload, response_q = request_queue.get(timeout=0.05)
        except queue.Empty:
            if not thread.is_alive():
                break
            continue
        responder = responses[kind]
        response_q.put(responder(payload) if callable(responder) else responder)
    thread.join(timeout=2)
    return controller


# ---------------------------------------------------------------------------
# TagFlowController -- the request/response contract, no real dialogs, no Qt
# ---------------------------------------------------------------------------


def test_controller_candidate_pick_write():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        pikepdf.new().save(pdf)
        client = _client()
        meta = ProductMetadata(
            title="Picked Title", series="Picked Series", description="d",
            source=Source.DTRPG_LIBRARY, product_id="1",
        )
        with patch("gui_tag_flow.find_candidates", return_value=[(meta, 95.0)]):
            responses = {
                "candidate": lambda payload: CandidateResult(action="pick", index=0),
                "confirm": lambda payload: ConfirmResult(action="confirm", meta=payload["meta"]),
            }
            controller = _drive_controller([pdf], client, responses)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "Picked Title", m.get("dc:title")
        assert any("Wrote metadata" in line for line in controller.history), controller.history
    print("PASS: controller_candidate_pick_write")


def test_controller_watermark_detected_removed():
    # detect_watermark()/remove_watermark() are verified against the real
    # sibling script with ad hoc scripts (see watermark_removal.py's own
    # module docstring); this only guards TagFlowController's wiring --
    # the watermark check must run before matching even starts, and
    # confirming it must call remove_watermark() with the detected text.
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Watermarked.pdf"
        pikepdf.new().save(pdf)
        client = _client()
        meta = ProductMetadata(title="Picked", description="d", source=Source.DTRPG_LIBRARY, product_id="1")
        detection = WatermarkDetection(text="www.drivethrurpg.com", page_count=3, total_pages=3)
        with (
            patch("gui_tag_flow.find_candidates", return_value=[(meta, 95.0)]),
            patch("gui_tag_flow.detect_watermark", return_value=detection) as mock_detect,
            patch("gui_tag_flow.remove_watermark") as mock_remove,
        ):
            responses = {
                "watermark": lambda payload: True,
                "candidate": lambda payload: CandidateResult(action="pick", index=0),
                "confirm": lambda payload: ConfirmResult(action="confirm", meta=payload["meta"]),
                "series": lambda payload: "",
            }
            controller = _drive_controller([pdf], client, responses)

        mock_detect.assert_called_once()
        mock_remove.assert_called_once_with(pdf, "www.drivethrurpg.com")
        assert any("Removed watermark" in line for line in controller.history), controller.history
    print("PASS: controller_watermark_detected_removed")


def test_controller_watermark_removal_backs_up_original_first():
    # A real bug, caught in review: write_metadata() makes the one-time
    # .bak backup, but that runs well *after* watermark removal (which
    # happens before matching even starts) -- so without an explicit
    # backup() call in the watermark-removal path itself, the .bak ended
    # up capturing the *watermark-removed* file instead of the true
    # pre-tagging original. Guards the fix directly: remove_watermark()'s
    # mock records whether the backup already existed, with the
    # pre-removal content, at the moment it was called -- recorded into a
    # plain list rather than asserted inline, since _drive_controller()
    # runs the controller on a background thread and an AssertionError
    # raised there would just kill that thread silently (a real gap this
    # test's first draft actually had, caught by deliberately reverting
    # the fix and finding the "regression" test still passed).
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Watermarked.pdf"
        pikepdf.new().save(pdf)
        original_bytes = pdf.read_bytes()
        client = _client()
        detection = WatermarkDetection(text="www.drivethrurpg.com", page_count=1, total_pages=1)

        observed: list[tuple[bool, bool]] = []

        def fake_remove_watermark(path, text, log=None):
            bak = path.with_suffix(".pdf.bak")
            observed.append((bak.exists(), bak.exists() and bak.read_bytes() == original_bytes))
            return WatermarkRemovalResult(success=True, removed=1)

        with (
            patch("gui_tag_flow.detect_watermark", return_value=detection),
            patch("gui_tag_flow.remove_watermark", side_effect=fake_remove_watermark) as mock_remove,
        ):
            responses = {
                "watermark": lambda payload: True,
                "candidate": lambda payload: CandidateResult(action="skip"),
            }
            _drive_controller([pdf], client, responses)

        mock_remove.assert_called_once()
        assert observed == [(True, True)], (
            f"backup() must run (with the pre-removal content) before remove_watermark() is called; observed={observed}"
        )
    print("PASS: controller_watermark_removal_backs_up_original_first")


def test_controller_manual_entry_write():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Manual.pdf"
        pikepdf.new().save(pdf)
        client = _client()
        manual_meta = build_manual_metadata(
            title="Manually Typed", publisher="", series="", series_index="",
            description="First paragraph.\n\nSecond paragraph.", tags_raw="", isbn="", product_url="",
        )
        with patch("gui_tag_flow.find_candidates", return_value=[]):
            responses = {
                "candidate": lambda payload: CandidateResult(action="manual"),
                "manual_entry": lambda payload: ManualEntryResult(action="submit", meta=manual_meta),
                "confirm": lambda payload: ConfirmResult(action="confirm", meta=payload["meta"]),
                "series": lambda payload: "",
            }
            _drive_controller([pdf], client, responses)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "Manually Typed"
                assert m.get("dc:description") == "First paragraph.\n\nSecond paragraph.", repr(m.get("dc:description"))
    print("PASS: controller_manual_entry_write")


def test_controller_skip():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Skip.pdf"
        pikepdf.new().save(pdf)
        client = _client()
        meta = ProductMetadata(title="X", description="d", source=Source.DTRPG_LIBRARY, product_id="1")
        with patch("gui_tag_flow.find_candidates", return_value=[(meta, 95.0)]):
            responses = {"candidate": lambda payload: CandidateResult(action="skip")}
            controller = _drive_controller([pdf], client, responses)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") is None
        assert any("Skipped" in line for line in controller.history)
    print("PASS: controller_skip")


def test_controller_quit_mid_batch():
    with tempfile.TemporaryDirectory() as tmp:
        pdf1 = Path(tmp) / "A.pdf"
        pdf2 = Path(tmp) / "B.pdf"
        pikepdf.new().save(pdf1)
        pikepdf.new().save(pdf2)
        client = _client()
        meta = ProductMetadata(title="X", description="d", source=Source.DTRPG_LIBRARY, product_id="1")
        with patch("gui_tag_flow.find_candidates", return_value=[(meta, 95.0)]):
            responses = {
                "batch_series": lambda payload: None,
                "candidate": lambda payload: CandidateResult(action="quit"),
            }
            controller = _drive_controller([pdf1, pdf2], client, responses, root_mode=True)

        for pdf in (pdf1, pdf2):
            with pikepdf.open(pdf) as p:
                with p.open_metadata() as m:
                    assert m.get("dc:title") is None
        assert "Stopped." in controller.history
    print("PASS: controller_quit_mid_batch")


# ---------------------------------------------------------------------------
# Real dialogs -- built and driven directly (no .exec(), matching the
# original Tkinter suite's own "call the dialog function directly, no
# mainloop running" approach: build the widget tree, set values, invoke
# a button, assert on the result), needing the one shared QApplication.
# ---------------------------------------------------------------------------


def test_show_confirm_dialog_edit_before_confirm() -> None:
    meta = ProductMetadata(
        title="Original", publisher="Pub", series="Series", description="Desc",
        tags=["a", "b"], isbn="1234567890", source=Source.DTRPG_LIBRARY, product_id="1",
    )
    payload = {"header": "About to write: Original", "meta": meta, "progress": ""}
    dialog = ConfirmDialog(None, payload)
    dialog.title_edit.setText("Edited Title")
    dialog.description_edit.setPlainText("Edited description")
    dialog.confirm_button.click()

    assert dialog.value.action == "confirm"
    assert dialog.value.meta.title == "Edited Title", dialog.value.meta.title
    assert dialog.value.meta.description == "Edited description"
    assert dialog.value.meta.series == "Series"
    assert dialog.value.meta.product_id == "1"
    print("PASS: show_confirm_dialog_edit_before_confirm")


def test_watermark_dialog_remove() -> None:
    detection = WatermarkDetection(text="www.drivethrurpg.com", page_count=42, total_pages=45)
    payload = {"book_title": "Book.pdf", "detection": detection}
    dialog = WatermarkDialog(None, payload)
    assert dialog.value is False  # default before any button is clicked
    dialog.remove_button.click()
    assert dialog.value is True
    print("PASS: watermark_dialog_remove")


def test_watermark_dialog_leave() -> None:
    detection = WatermarkDetection(text="www.drivethrurpg.com", page_count=1, total_pages=1)
    payload = {"book_title": "Book.pdf", "detection": detection}
    dialog = WatermarkDialog(None, payload)
    dialog.leave_button.click()
    assert dialog.value is False
    print("PASS: watermark_dialog_leave")


def test_show_manual_entry_dialog_multiline_description() -> None:
    payload = {"path": Path("Book.pdf"), "progress": "", "default_series": None}
    dialog = ManualEntryDialog(None, payload)
    dialog.title_edit.setText("Typed Title")
    dialog.description_edit.setPlainText("Line one\n\nLine two")
    dialog.submit_button.click()

    assert dialog.value.action == "submit"
    assert dialog.value.meta.title == "Typed Title"
    assert dialog.value.meta.description == "Line one\n\nLine two"
    print("PASS: show_manual_entry_dialog_multiline_description")


def test_show_candidate_dialog_pick() -> None:
    meta = ProductMetadata(title="Candidate One", source=Source.DTRPG_LIBRARY, product_id="1")
    payload = {"path": Path("Book.pdf"), "progress": "", "candidates": [(meta, 90.0)]}
    dialog = CandidateDialog(None, payload)
    dialog.list_widget.setCurrentRow(0)
    dialog.pick_button.click()

    assert dialog.value.action == "pick"
    assert dialog.value.index == 0
    print("PASS: show_candidate_dialog_pick")


def test_show_candidate_dialog_use_url_button() -> None:
    # A real candidate is present AND highlighted -- pasting a URL and
    # clicking "Use URL" must use the pasted URL, not the highlighted
    # candidate (the bug this button was added to fix: clicking Pick out
    # of habit silently ignored a pasted URL).
    meta = ProductMetadata(title="Wrong Candidate", source=Source.DTRPG_LIBRARY, product_id="1")
    payload = {"path": Path("Book.pdf"), "progress": "", "candidates": [(meta, 90.0)]}
    dialog = CandidateDialog(None, payload)
    dialog.list_widget.setCurrentRow(0)
    dialog.url_edit.setText("id:42")
    dialog.url_edit.returnPressed.emit()

    assert dialog.value.action == "url"
    assert dialog.value.product_id == "42"
    print("PASS: show_candidate_dialog_use_url_button")


def test_candidate_dialog_escape_skips_not_quits() -> None:
    # Escape means Skip, window-close means Quit -- these must stay
    # distinct outcomes (see CandidateDialog.keyPressEvent/reject()).
    meta = ProductMetadata(title="X", source=Source.DTRPG_LIBRARY, product_id="1")
    payload = {"path": Path("Book.pdf"), "progress": "", "candidates": [(meta, 90.0)]}
    dialog = CandidateDialog(None, payload)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.value.action == "skip", dialog.value

    dialog2 = CandidateDialog(None, payload)
    dialog2.reject()  # simulates the window-manager close button
    assert dialog2.value.action == "quit", dialog2.value
    print("PASS: candidate_dialog_escape_skips_not_quits")


# ---------------------------------------------------------------------------
# Review tab's editable table
# ---------------------------------------------------------------------------


def test_review_tab_status_edit_persists() -> None:
    # The original Tkinter version of this test guarded a real bug
    # specific to that toolkit (Treeview.identify_column() disagreeing
    # with .bbox() about column boundaries on one build, misrouting
    # clicks to the wrong column). That hit-testing no longer exists
    # under Qt -- QStyledItemDelegate placement is entirely Qt-internal --
    # so this is now a plain smoke test that an edit actually persists
    # through the real commit path (_on_item_changed -> save_review()).
    with tempfile.TemporaryDirectory() as tmp:
        review_csv = Path(tmp) / "review.csv"
        row = ReviewRow(filename="Book.pdf", matched_title="Title", status=Status.NEEDS_REVIEW.value)
        save_review(review_csv, [row])

        tab = ReviewTab({"review_csv": str(review_csv)})
        try:
            status_col = tab.GRID_COLUMNS.index("status")
            tab.table.item(0, status_col).setText(Status.APPROVED.value)

            assert tab.rows_by_filename["Book.pdf"].status == Status.APPROVED.value
            assert tab.table.item(0, status_col).text() == Status.APPROVED.value

            # save_review() directly, not tab.save() -- the latter also
            # pops a QMessageBox.information() confirmation, which blocks
            # on .exec() waiting for a click that will never come
            # headlessly. That confirmation is Qt UI chrome, not the
            # persistence contract this test actually cares about.
            save_review(review_csv, list(tab.rows_by_filename.values()))
            reloaded = load_review(review_csv)
            assert reloaded[0].status == Status.APPROVED.value
        finally:
            tab.deleteLater()
    print("PASS: review_tab_status_edit_persists")


def test_review_tab_edit_survives_rescan() -> None:
    """merge_by_filename() used to only protect a row flipped to
    `approved` -- editing an auto-accepted row's series in the Review tab
    without also flipping status was silently overwritten by the next
    scan. Guards the fix: a real inline edit through the actual widget
    (not just calling ReviewRow.mark_edited() on a plain object) must set
    `edited`, and merge_by_filename() must then preserve that row over a
    fresh, differently-matched row for the same filename."""
    with tempfile.TemporaryDirectory() as tmp:
        review_csv = Path(tmp) / "review.csv"
        row = ReviewRow(filename="Book.pdf", matched_title="Title", series="", status=Status.AUTO_ACCEPTED.value)
        save_review(review_csv, [row])

        tab = ReviewTab({"review_csv": str(review_csv)})
        try:
            series_col = tab.GRID_COLUMNS.index("series")
            tab.table.item(0, series_col).setText("Hand-Typed Series")

            edited_row = tab.rows_by_filename["Book.pdf"]
            assert edited_row.series == "Hand-Typed Series"
            assert edited_row.edited, "a real inline edit did not set ReviewRow.edited"
            assert edited_row.status == Status.AUTO_ACCEPTED.value, "editing series should not touch status"

            # save_review() directly -- tab.save() also pops a blocking
            # QMessageBox.information() with nothing headless to click it.
            save_review(review_csv, list(tab.rows_by_filename.values()))
        finally:
            tab.deleteLater()

        # Simulate a later re-scan matching this same file to something
        # else entirely -- merge_by_filename() must keep the hand-typed
        # series, not the fresh match, even though status was never
        # flipped to approved.
        existing = load_review(review_csv)
        fresh = [ReviewRow(filename="Book.pdf", matched_title="Re-matched Title", series="", status=Status.AUTO_ACCEPTED.value)]
        merged = merge_by_filename(existing, fresh)
        merged_row = next(r for r in merged if r.filename == "Book.pdf")
        assert merged_row.series == "Hand-Typed Series", "a hand edit was clobbered by a re-scan"
    print("PASS: review_tab_edit_survives_rescan")


def test_review_tab_no_change_does_not_mark_edited() -> None:
    """`_commit_detail_field()`/`_commit_description()` are wired to
    `editingFinished`/`focus_lost`, which fire whenever focus leaves the
    widget -- whether or not its value actually changed (e.g. clicking
    into a field just to read it, then tabbing away). An earlier version
    called `row.mark_edited()` unconditionally on every such commit,
    which permanently exempted that row from ever being refreshed by a
    later re-scan for no real reason. Guards the fix: only a genuine
    value change should set `ReviewRow.edited`."""
    with tempfile.TemporaryDirectory() as tmp:
        review_csv = Path(tmp) / "review.csv"
        row = ReviewRow(filename="Book.pdf", matched_title="Title", publisher="Pub", status=Status.AUTO_ACCEPTED.value)
        save_review(review_csv, [row])

        tab = ReviewTab({"review_csv": str(review_csv)})
        try:
            tab.table.setCurrentCell(0, 0)  # selects the row, populates the detail form
            assert tab.rows_by_filename["Book.pdf"].edited == ""

            # Focus-out with no actual change to the text.
            tab.detail_edits["publisher"].editingFinished.emit()
            tab._commit_description()
            assert tab.rows_by_filename["Book.pdf"].edited == "", (
                "a no-op focus change incorrectly marked the row edited"
            )

            # A real change must still mark it.
            tab.detail_edits["publisher"].setText("New Publisher")
            tab.detail_edits["publisher"].editingFinished.emit()
            assert tab.rows_by_filename["Book.pdf"].edited == "1", "a real edit did not mark the row edited"
            assert tab.rows_by_filename["Book.pdf"].publisher == "New Publisher"
        finally:
            tab.deleteLater()
    print("PASS: review_tab_no_change_does_not_mark_edited")


def test_review_tab_no_save_during_population() -> None:
    # A real Qt-specific gotcha with no Tkinter analog: QTableWidget's
    # itemChanged fires for setItem() during bulk population just as it
    # does for a real user edit -- ReviewTab._populate_table() guards
    # this with table.blockSignals(True). This test would fail (the
    # commit count would be > 0 after reload()) if that guard were ever
    # removed.
    with tempfile.TemporaryDirectory() as tmp:
        review_csv = Path(tmp) / "review.csv"
        rows = [ReviewRow(filename=f"Book{i}.pdf", matched_title=f"Title {i}") for i in range(5)]
        save_review(review_csv, rows)

        tab = ReviewTab({"review_csv": str(review_csv)})
        try:
            commits = []
            tab.table.itemChanged.connect(lambda item: commits.append(item))
            tab.reload()
            assert commits == [], f"itemChanged fired {len(commits)} time(s) during population"

            status_col = tab.GRID_COLUMNS.index("status")
            tab.table.item(0, status_col).setText(Status.APPROVED.value)
            assert len(commits) == 1, "a real edit should still fire itemChanged exactly once"
        finally:
            tab.deleteLater()
    print("PASS: review_tab_no_save_during_population")


# ---------------------------------------------------------------------------
# TagWorker end-to-end: a real QThread, a real signal-based _ask() round
# trip, no fake-thread harness -- the single highest-novelty piece of the
# Tk->Qt migration (see gui_tag_flow.py's module docstring), so it gets
# its own direct coverage beyond the _drive_controller() group above.
# ---------------------------------------------------------------------------


def test_tag_worker_signal_round_trip(app: QApplication) -> None:
    from gui_tag_flow import TagWorker

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        pikepdf.new().save(pdf)
        client = _client()
        meta = ProductMetadata(title="X", series="", description="d", source=Source.DTRPG_LIBRARY, product_id="1")

        received: list[tuple[str, dict]] = []

        with patch("gui_tag_flow.find_candidates", return_value=[(meta, 95.0)]):
            worker = TagWorker(
                pdfs=[pdf], client=client, manual_overrides={}, known_urls={}, thresholds={},
                bookorbit_mode=False, rename=False, root_mode=False, stop_event=threading.Event(),
            )
            thread = __import__("PyQt6.QtCore", fromlist=["QThread"]).QThread()
            worker.moveToThread(thread)
            thread.started.connect(worker.run)

            def on_dialog_requested(kind: str, payload: dict, response_q: "queue.Queue") -> None:
                received.append((kind, payload))
                if kind == "candidate":
                    response_q.put(CandidateResult(action="pick", index=0))
                elif kind == "confirm":
                    response_q.put(ConfirmResult(action="confirm", meta=payload["meta"]))
                else:
                    response_q.put(None)

            worker.dialog_requested.connect(on_dialog_requested)
            finished = []
            worker.finished.connect(lambda: finished.append(True))

            thread.start()
            deadline = __import__("time").time() + 5
            while not finished and __import__("time").time() < deadline:
                app.processEvents()
            thread.quit()
            thread.wait(2000)

        assert finished, "worker never signaled finished -- the round trip hung"
        # "series" is asked too -- meta.series is blank, so
        # TagFlowController._maybe_ask_series() asks once more after
        # confirm, same as every other path through the controller.
        assert [kind for kind, _ in received] == ["candidate", "confirm", "series"], received
        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "X"
    print("PASS: tag_worker_signal_round_trip")


def test_tag_worker_run_catches_exception(app: QApplication) -> None:
    """A PyQt6 slot running on a QThread has no default exception handler
    -- an uncaught exception escaping TagWorker.run() used to abort the
    whole process (verified with a standalone repro: SIGABRT, not a
    catchable Python exception) instead of just failing the one run.
    Guards the fix: TagWorker.run() must catch, log, and still emit
    `finished` so the GUI stays usable and the Start button re-enables."""
    from gui_tag_flow import TagWorker
    from PyQt6.QtCore import QThread

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        pikepdf.new().save(pdf)
        client = _client()

        worker = TagWorker(
            pdfs=[pdf], client=client, manual_overrides={}, known_urls={}, thresholds={},
            bookorbit_mode=False, rename=False, root_mode=False, stop_event=threading.Event(),
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        log_lines: list[str] = []
        worker.log_line.connect(log_lines.append)
        finished = []
        worker.finished.connect(lambda: finished.append(True))

        with patch.object(worker.controller, "run", side_effect=ConnectionError("simulated network failure")):
            thread.start()
            deadline = __import__("time").time() + 5
            while not finished and __import__("time").time() < deadline:
                app.processEvents()
            thread.quit()
            thread.wait(2000)

        assert finished, "worker never emitted finished after an exception -- the app would hang or abort"
        assert any("ERROR" in line and "simulated network failure" in line for line in log_lines), log_lines
    print("PASS: tag_worker_run_catches_exception")


NO_APP_TESTS = [
    test_controller_candidate_pick_write,
    test_controller_watermark_detected_removed,
    test_controller_watermark_removal_backs_up_original_first,
    test_controller_manual_entry_write,
    test_controller_skip,
    test_controller_quit_mid_batch,
]

NO_ARG_APP_TESTS = [
    test_show_confirm_dialog_edit_before_confirm,
    test_watermark_dialog_remove,
    test_watermark_dialog_leave,
    test_show_manual_entry_dialog_multiline_description,
    test_show_candidate_dialog_pick,
    test_show_candidate_dialog_use_url_button,
    test_candidate_dialog_escape_skips_not_quits,
    test_review_tab_status_edit_persists,
    test_review_tab_edit_survives_rescan,
    test_review_tab_no_change_does_not_mark_edited,
    test_review_tab_no_save_during_population,
]

APP_ARG_TESTS = [
    test_tag_worker_signal_round_trip,
    test_tag_worker_run_catches_exception,
]


def main() -> None:
    failed = 0
    for test in NO_APP_TESTS:
        try:
            test()
        except Exception:
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()

    # Qt permits exactly one QApplication per process -- shared across
    # every remaining test, matching the original Tkinter suite's own
    # "one root window, never a second" finding (there, creating a second
    # tk.Tk() was found to be unstable; here, Qt disallows a second
    # QApplication outright).
    try:
        app = QApplication.instance() or QApplication(sys.argv)
    except Exception as exc:
        print(f"SKIPPED remaining {len(NO_ARG_APP_TESTS) + len(APP_ARG_TESTS)} test(s): no usable Qt platform ({exc})")
        total = len(NO_APP_TESTS)
        print(f"\n{total - failed}/{total} passed")
        sys.exit(1 if failed else 0)

    for test in NO_ARG_APP_TESTS:
        try:
            test()
        except Exception:
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()

    for test in APP_ARG_TESTS:
        try:
            test(app)
        except Exception:
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()

    total = len(NO_APP_TESTS) + len(NO_ARG_APP_TESTS) + len(APP_ARG_TESTS)
    print(f"\n{total - failed}/{total} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
