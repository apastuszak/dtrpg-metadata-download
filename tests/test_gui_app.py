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

import html
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

from provenance import ProductMetadata, Source  # noqa: E402
from review import ReviewRow  # noqa: E402
from tag_tui import CandidateResult, ConfirmResult, ManualEntryResult, build_manual_metadata  # noqa: E402
from gui_tag_flow import (  # noqa: E402
    CandidateDialog,
    ConfirmDialog,
    ManualEntryDialog,
    TagFlowController,
    WatermarkDialog,
)
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
        "manual_overrides": {}, "known_urls": {},
        "metadata": "grimmory", "rename": False, "root_mode": False,
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


def _numbered_book(path: Path) -> list[str]:
    """Same synthetic book as tests/test_tag_tui.py's helper of this name --
    see its docstring. Returns the labels --page-labels should produce."""
    import pymupdf as fitz

    doc = fitz.open()
    for printed in (None, None, None, 2, 3, 4, 5, None):
        page = doc.new_page()
        page.insert_text((72, 100), "Body text")
        if printed is not None:
            page.insert_text((page.rect.width / 2, page.rect.height - 30), str(printed))
    doc.save(path)
    doc.close()
    return ["Cover", "i", "1", "2", "3", "4", "5", "Cover"]


def _labels(path: Path) -> list[str]:
    import pymupdf as fitz

    doc = fitz.open(path)
    try:
        return [page.get_label() for page in doc]
    finally:
        doc.close()


def test_controller_back_cover_to_page_2():
    import random

    import pymupdf as fitz

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        doc = fitz.open()
        for text in ("FRONT", "BODY 1", "BODY 2", "BODY 3", "BACK"):
            page = doc.new_page()
            page.insert_text((72, 100), text)
            if text == "BACK":
                rnd, x = random.Random(1), 400.0
                for _ in range(30):
                    w = rnd.choice([1.0, 1.5, 2.0, 3.0])
                    page.draw_rect(fitz.Rect(x, 650, x + w, 700), color=None, fill=(0, 0, 0))
                    x += w + rnd.choice([1.0, 1.5, 2.0, 3.0])
        doc.save(pdf)
        doc.close()
        meta = ProductMetadata(title="T", series="S", description="d", source=Source.DTRPG_LIBRARY, product_id="1")
        with patch("gui_tag_flow.find_candidates", return_value=[(meta, 95.0)]):
            responses = {
                "candidate": lambda payload: CandidateResult(action="pick", index=0),
                "confirm": lambda payload: ConfirmResult(action="confirm", meta=payload["meta"]),
            }
            controller = _drive_controller([pdf], _client(), responses, back_cover_to_page_2=True)

        with fitz.open(pdf) as moved:
            texts = [page.get_text("text").strip() for page in moved]
        assert texts == ["FRONT", "BACK", "BODY 1", "BODY 2", "BODY 3"], texts
        assert any("moved the back cover to PDF page 2" in line for line in controller.history), controller.history
    print("PASS: controller_back_cover_to_page_2")


def test_controller_back_cover_without_barcode_asks():
    """No barcode: the controller asks (a "back_cover" dialog request with
    the candidate page) and moves only on a "yes"."""
    import pymupdf as fitz

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        doc = fitz.open()
        for text in ("FRONT", "BODY 1", "BODY 2", "BACK"):
            doc.new_page().insert_text((72, 100), text)
        doc.save(pdf)
        doc.close()
        meta = ProductMetadata(title="T", series="S", description="d", source=Source.DTRPG_LIBRARY, product_id="1")
        asked = []

        def answer(payload):
            question = payload["question"]
            asked.append((question.page_index, question.front_png is not None, question.candidate_png is not None))
            return True

        with patch("gui_tag_flow.find_candidates", return_value=[(meta, 95.0)]):
            responses = {
                "candidate": lambda payload: CandidateResult(action="pick", index=0),
                "confirm": lambda payload: ConfirmResult(action="confirm", meta=payload["meta"]),
                "back_cover": answer,
            }
            _drive_controller([pdf], _client(), responses, back_cover_to_page_2=True)

        with fitz.open(pdf) as moved:
            texts = [page.get_text("text").strip() for page in moved]
        assert asked == [(3, True, True)], asked
        assert texts == ["FRONT", "BACK", "BODY 1", "BODY 2"], texts
    print("PASS: controller_back_cover_without_barcode_asks")


def test_back_cover_dialog_shows_both_pages() -> None:
    import pymupdf as fitz

    from back_cover import locate_back_cover, prepare_question
    from gui_tag_flow import BackCoverDialog

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        doc = fitz.open()
        for text in ("FRONT", "BODY", "BACK"):
            doc.new_page().insert_text((72, 100), text)
        doc.save(pdf)
        doc.close()
        location = locate_back_cover(pdf, "page-2")
        question = prepare_question(pdf, location, "page-2", Path(tmp))
        dialog = BackCoverDialog(None, {"book_title": "Book.pdf", "question": question})
        from PyQt6.QtWidgets import QLabel as _QLabel
        pictures = [label for label in dialog.findChildren(_QLabel) if label.pixmap() is not None and not label.pixmap().isNull()]
        assert len(pictures) == 2, "expected thumbnails of the front cover and the candidate page"
        QTest.mouseClick(dialog.yes_button, Qt.MouseButton.LeftButton)
        assert dialog.value is True
    print("PASS: back_cover_dialog_shows_both_pages")


def test_controller_page_labels_written():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        expected = _numbered_book(pdf)
        meta = ProductMetadata(title="T", series="S", description="d", source=Source.DTRPG_LIBRARY, product_id="1")
        with patch("gui_tag_flow.find_candidates", return_value=[(meta, 95.0)]):
            responses = {
                "candidate": lambda payload: CandidateResult(action="pick", index=0),
                "confirm": lambda payload: ConfirmResult(action="confirm", meta=payload["meta"]),
            }
            controller = _drive_controller([pdf], _client(), responses, page_labels=True)

        assert _labels(pdf) == expected, _labels(pdf)
        assert any("Page labels set" in line for line in controller.history), controller.history
    print("PASS: controller_page_labels_written")


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
                pdfs=[pdf], client=client, manual_overrides={}, known_urls={}, 
                metadata="grimmory", rename=False, root_mode=False, stop_event=threading.Event(),
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
            pdfs=[pdf], client=client, manual_overrides={}, known_urls={}, 
            metadata="grimmory", rename=False, root_mode=False, stop_event=threading.Event(),
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


def test_close_during_run_waits_for_current_book(app: QApplication) -> None:
    """Closing the window mid-run used to quit() the thread and wait 200 ms,
    then destroy a QThread whose worker was still busy -- which aborts the
    whole process ("QThread: Destroyed while thread is still running").
    The window must stay open, finish the current book, skip the next one,
    then close itself."""
    import time
    from gui_app import GuiApp
    from pdf_writer import WriteResult

    written = []

    def slow_write(path, row, **kwargs):
        time.sleep(0.6)  # long enough that the close lands mid-write
        written.append(path.name)
        return WriteResult(path.name, True, "ok")

    with tempfile.TemporaryDirectory() as tmp:
        for name in ("A.pdf", "B.pdf"):
            pikepdf.new().save(Path(tmp) / name)
        window = GuiApp({})
        tab = window._tag_tab
        tab.root_radio.setChecked(True)
        tab.path_edit.setText(tmp)
        tab.metadata_combo.setCurrentIndex(tab.metadata_combo.findData("off"))
        tab.page_labels_check.setChecked(True)  # something to do, so write_metadata() runs
        window.show()
        with (patch("gui_tag_flow.write_metadata", side_effect=slow_write),
              patch("gui_tag_flow.detect_watermark", return_value=None)):
            QTest.mouseClick(tab.start_button, Qt.MouseButton.LeftButton)
            deadline = time.time() + 2
            while tab._thread is None and time.time() < deadline:
                app.processEvents()
            time.sleep(0.2)
            assert not window.close(), "the window closed while the worker was still running"
            assert window.isVisible()
            assert "finishing the current book" in tab.log_widget.toPlainText()
            deadline = time.time() + 10
            while window.isVisible() and time.time() < deadline:
                app.processEvents()
        assert not window.isVisible(), "the window never closed after the run finished"
        assert tab._thread is None
        assert written == ["A.pdf"], f"closing must stop before the next book: {written}"
    print("PASS: close_during_run_waits_for_current_book")


def test_controller_metadata_off_dry_run_then_write():
    """With metadata tagging off, Tag replaces the old Rename and Page
    Labels tabs: no matching (no client, no dialogs), a dry run that shows
    the plan and leaves the file untouched (no .bak either), then a real run
    that sets the labels and renames from the metadata already on disk."""
    from sidecar_writer import write_grimmory_sidecar

    def no_dialogs(payload):
        raise AssertionError("metadata off must not ask anything")

    responses = {kind: no_dialogs for kind in ("batch_series", "candidate", "confirm", "series", "manual_entry")}
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        expected = _numbered_book(pdf)
        write_grimmory_sidecar(pdf, ReviewRow(filename="Book.pdf", matched_title="Title", series="Series"))
        original = pdf.read_bytes()
        options = dict(metadata="off", page_labels=True, rename=True, root_mode=True)

        controller = _drive_controller([pdf], None, responses, dry_run=True, **options)
        log = "\n".join(controller.history)
        assert "Would label: Book.pdf" in log and "PDF pages 3-7: 1 to 5" in log, log
        assert "Would rename: Book.pdf -> Series - Title.pdf" in log, log
        assert pdf.read_bytes() == original, "dry run modified the PDF"
        assert not pdf.with_suffix(".pdf.bak").exists(), "dry run made a backup"

        controller = _drive_controller([pdf], None, responses, **options)
        renamed = pdf.with_name("Series - Title.pdf")
        assert renamed.exists(), (list(Path(tmp).iterdir()), controller.history)
        assert _labels(renamed) == expected, _labels(renamed)
        assert renamed.with_suffix(".pdf.bak").read_bytes() == original
        assert renamed.with_name("Series - Title.metadata.json").exists(), "metadata off must keep the sidecar"
    print("PASS: controller_metadata_off_dry_run_then_write")


def test_tag_tab_dropdowns_pass_choices(app: QApplication) -> None:
    """Every Tag tab dropdown must offer its choices, start on its default,
    and pass the chosen value (the rule set's *name*, not its label, for
    --hyperlink) to the worker Start launches. TagWorker and QThread are
    replaced, so nothing actually runs -- this only checks the wiring.
    Metadata off, so no API key or client is needed."""
    from gui_tag_flow import TagTab
    from rpg_hyperlink import rule_sets

    rule_set_names = [r.name for r in rule_sets()]
    assert {"gurps", "mongoose", "deltagreen"} <= set(rule_set_names), rule_set_names
    expected = {
        "metadata_combo": (["off", "calibre", "grimmory", "bookorbit"], "grimmory"),
        "convert_combo": ([None, "rgb", "grayscale"], None),
        "hyperlink_combo": ([None, *rule_set_names], None),
        "background_combo": ([None, "layer", "remove"], None),
        "back_cover_combo": ([None, "page-2", "end"], None),
    }
    tab = TagTab({})
    for name, (choices, default) in expected.items():
        combo = getattr(tab, name)
        assert [combo.itemData(i) for i in range(combo.count())] == choices, (name, choices)
        assert combo.currentData() == default, f"{name} must start on {default!r}"
        assert all(combo.itemData(i, Qt.ItemDataRole.ToolTipRole) for i in range(combo.count())), \
            f"every {name} option needs its own tooltip"

    # Tooltips replace the old hint labels: the selected option's text
    # follows the selection, and a rule set's description is escaped.
    combo = tab.hyperlink_combo
    info = next(r for r in rule_sets() if r.name == "deltagreen")
    combo.setCurrentIndex(combo.findData("deltagreen"))
    assert html.escape(info.description) in combo.toolTip(), combo.toolTip()
    assert tab.page_labels_check.toolTip(), "checkboxes need a tooltip too"
    from gui_app import HelpIcon
    icons = tab.findChildren(HelpIcon)
    assert len(icons) == 9 and all(icon.toolTip() for icon in icons), [icon.toolTip() for icon in icons]
    assert any(html.escape(info.description) in icon.toolTip() for icon in icons), \
        "the hyperlink help icon must follow the selected rule set"

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        pikepdf.new().save(pdf)
        tab.file_radio.setChecked(True)
        tab.path_edit.setText(str(pdf))
        for name, value in (("metadata_combo", "off"), ("convert_combo", "grayscale"),
                            ("hyperlink_combo", "mongoose"), ("background_combo", "remove"),
                            ("back_cover_combo", "end")):
            combo = getattr(tab, name)
            combo.setCurrentIndex(combo.findData(value))
        with patch("gui_tag_flow.TagWorker") as worker, patch("gui_tag_flow.QThread"):
            QTest.mouseClick(tab.start_button, Qt.MouseButton.LeftButton)
        assert worker.called, "Start never launched a TagWorker"
        kwargs = worker.call_args.kwargs
        assert kwargs["metadata"] == "off" and kwargs["client"] is None, kwargs
        assert kwargs["convert_grayscale"] is True and kwargs["convert_images"] is False, kwargs
        assert kwargs["hyperlink"] == "mongoose", kwargs
        assert kwargs["remove_background"] is True and kwargs["background_layer"] is False, kwargs
        assert kwargs["back_cover_to_end"] is True and kwargs["back_cover_to_page_2"] is False, kwargs
    print("PASS: tag_tab_dropdowns_pass_choices")


NO_APP_TESTS = [
    test_controller_candidate_pick_write,
    test_controller_page_labels_written,
    test_controller_metadata_off_dry_run_then_write,
    test_controller_back_cover_to_page_2,
    test_controller_back_cover_without_barcode_asks,
    test_controller_watermark_detected_removed,
    test_controller_watermark_removal_backs_up_original_first,
    test_controller_manual_entry_write,
    test_controller_skip,
    test_controller_quit_mid_batch,
]

NO_ARG_APP_TESTS = [
    test_show_confirm_dialog_edit_before_confirm,
    test_back_cover_dialog_shows_both_pages,
    test_watermark_dialog_remove,
    test_watermark_dialog_leave,
    test_show_manual_entry_dialog_multiline_description,
    test_show_candidate_dialog_pick,
    test_show_candidate_dialog_use_url_button,
    test_candidate_dialog_escape_skips_not_quits,
]

APP_ARG_TESTS = [
    test_tag_worker_signal_round_trip,
    test_tag_worker_run_catches_exception,
    test_close_during_run_waits_for_current_book,
    test_tag_tab_dropdowns_pass_choices,
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
