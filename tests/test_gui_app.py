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
# ]
# ///
"""Smoke tests for gui_app.py/gui_tag_flow.py -- a scoped exception to
this project's no-test-suite convention (see CLAUDE.md), same
justification as tests/test_tag_tui.py: the request/response queue
between TagFlowController's worker thread and TagTab's main-thread
dialogs is exactly the "wrong id / dropped handoff" class of bug that
file was created to catch for Textual's screen wiring, and it's easy to
get subtly wrong without actually driving it.

Needs a real display (tkinter.Tk() must be able to create a root
window) -- there's no Pilot-equivalent headless harness for Tkinter, so
this skips cleanly rather than failing when none is available (this
repo has no CI today). Run directly: ./tests/test_gui_app.py
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
    import tkinter as tk
    from tkinter import ttk
except ModuleNotFoundError as exc:
    print(f"SKIPPED: tkinter not available ({exc})")
    sys.exit(0)

from provenance import ProductMetadata, Source, Status  # noqa: E402
from review import ReviewRow, load_review, save_review  # noqa: E402
from tag_tui import CandidateResult, ConfirmResult, ManualEntryResult, build_manual_metadata  # noqa: E402
from gui_tag_flow import (  # noqa: E402
    TagFlowController,
    show_candidate_dialog,
    show_confirm_dialog,
    show_manual_entry_dialog,
)
from gui_app import ReviewTab  # noqa: E402


def _client(**overrides) -> MagicMock:
    client = MagicMock()
    client.search_library.return_value = []
    client.search_catalog.return_value = []
    for name, value in overrides.items():
        getattr(client, name).return_value = value
    return client


def _drive_controller(controller: TagFlowController, responses: dict) -> None:
    """Runs controller.run() on a background thread while this (the
    calling/main thread) answers each dialog request from a scripted
    `responses` mapping instead of building any real dialog -- tests the
    request/response queue protocol itself without needing a display."""
    thread = threading.Thread(target=controller.run, daemon=True)
    thread.start()
    while True:
        try:
            kind, payload, response_q = controller.request_queue.get(timeout=0.05)
        except queue.Empty:
            if not thread.is_alive():
                break
            continue
        responder = responses[kind]
        response_q.put(responder(payload) if callable(responder) else responder)
    thread.join(timeout=2)


def _find_widgets(widget, cls):
    found = []
    for child in widget.winfo_children():
        if isinstance(child, cls):
            found.append(child)
        found.extend(_find_widgets(child, cls))
    return found


def _latest_toplevel(root):
    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    return tops[-1]


# ---------------------------------------------------------------------------
# TagFlowController -- the request/response queue protocol, no real dialogs
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
            controller = TagFlowController(
                pdfs=[pdf], client=client, manual_overrides={}, known_urls={},
                thresholds={}, bookorbit_mode=False, rename=False, root_mode=False,
                request_queue=queue.Queue(), log=lambda line: None, stop_event=threading.Event(),
            )
            responses = {
                "candidate": lambda payload: CandidateResult(action="pick", index=0),
                "confirm": lambda payload: ConfirmResult(action="confirm", meta=payload["meta"]),
            }
            _drive_controller(controller, responses)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "Picked Title", m.get("dc:title")
        assert any("Wrote metadata" in line for line in controller.history), controller.history
    print("PASS: controller_candidate_pick_write")


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
            controller = TagFlowController(
                pdfs=[pdf], client=client, manual_overrides={}, known_urls={},
                thresholds={}, bookorbit_mode=False, rename=False, root_mode=False,
                request_queue=queue.Queue(), log=lambda line: None, stop_event=threading.Event(),
            )
            responses = {
                "candidate": lambda payload: CandidateResult(action="manual"),
                "manual_entry": lambda payload: ManualEntryResult(action="submit", meta=manual_meta),
                "confirm": lambda payload: ConfirmResult(action="confirm", meta=payload["meta"]),
                "series": lambda payload: "",
            }
            _drive_controller(controller, responses)

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
            controller = TagFlowController(
                pdfs=[pdf], client=client, manual_overrides={}, known_urls={},
                thresholds={}, bookorbit_mode=False, rename=False, root_mode=False,
                request_queue=queue.Queue(), log=lambda line: None, stop_event=threading.Event(),
            )
            responses = {"candidate": lambda payload: CandidateResult(action="skip")}
            _drive_controller(controller, responses)

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
            controller = TagFlowController(
                pdfs=[pdf1, pdf2], client=client, manual_overrides={}, known_urls={},
                thresholds={}, bookorbit_mode=False, rename=False, root_mode=True,
                request_queue=queue.Queue(), log=lambda line: None, stop_event=threading.Event(),
            )
            responses = {
                "batch_series": lambda payload: None,
                "candidate": lambda payload: CandidateResult(action="quit"),
            }
            _drive_controller(controller, responses)

        for pdf in (pdf1, pdf2):
            with pikepdf.open(pdf) as p:
                with p.open_metadata() as m:
                    assert m.get("dc:title") is None
        assert "Stopped." in controller.history
    print("PASS: controller_quit_mid_batch")


# ---------------------------------------------------------------------------
# Real dialogs -- the highest-value few, driven with actual widgets
# ---------------------------------------------------------------------------


def test_show_confirm_dialog_edit_before_confirm(root: tk.Tk) -> None:
    meta = ProductMetadata(
        title="Original", publisher="Pub", series="Series", description="Desc",
        tags=["a", "b"], isbn="1234567890", source=Source.DTRPG_LIBRARY, product_id="1",
    )
    payload = {"header": "About to write: Original", "meta": meta, "progress": ""}

    def interact():
        dialog = _latest_toplevel(root)
        entries = _find_widgets(dialog, ttk.Entry)
        entries[0].delete(0, "end")
        entries[0].insert(0, "Edited Title")
        texts = _find_widgets(dialog, tk.Text)
        texts[0].delete("1.0", "end")
        texts[0].insert("1.0", "Edited description")
        buttons = _find_widgets(dialog, ttk.Button)
        next(b for b in buttons if "Confirm" in b.cget("text")).invoke()

    root.after(100, interact)
    result = show_confirm_dialog(root, payload)

    assert result.action == "confirm"
    assert result.meta.title == "Edited Title", result.meta.title
    assert result.meta.description == "Edited description"
    assert result.meta.series == "Series"
    assert result.meta.product_id == "1"
    print("PASS: show_confirm_dialog_edit_before_confirm")


def test_show_manual_entry_dialog_multiline_description(root: tk.Tk) -> None:
    payload = {"path": Path("Book.pdf"), "progress": "", "default_series": None}

    def interact():
        dialog = _latest_toplevel(root)
        entries = _find_widgets(dialog, ttk.Entry)
        entries[0].insert(0, "Typed Title")
        texts = _find_widgets(dialog, tk.Text)
        texts[0].insert("1.0", "Line one\n\nLine two")
        buttons = _find_widgets(dialog, ttk.Button)
        next(b for b in buttons if "Submit" in b.cget("text")).invoke()

    root.after(100, interact)
    result = show_manual_entry_dialog(root, payload)

    assert result.action == "submit"
    assert result.meta.title == "Typed Title"
    assert result.meta.description == "Line one\n\nLine two"
    print("PASS: show_manual_entry_dialog_multiline_description")


def test_show_candidate_dialog_pick(root: tk.Tk) -> None:
    meta = ProductMetadata(title="Candidate One", source=Source.DTRPG_LIBRARY, product_id="1")
    payload = {"path": Path("Book.pdf"), "progress": "", "candidates": [(meta, 90.0)]}

    def interact():
        dialog = _latest_toplevel(root)
        _find_widgets(dialog, tk.Listbox)[0].selection_set(0)
        buttons = _find_widgets(dialog, ttk.Button)
        next(b for b in buttons if b.cget("text") == "Pick").invoke()

    root.after(100, interact)
    result = show_candidate_dialog(root, payload)

    assert result.action == "pick"
    assert result.index == 0
    print("PASS: show_candidate_dialog_pick")


def test_show_candidate_dialog_use_url_button(root: tk.Tk) -> None:
    # A real candidate is present AND highlighted -- pasting a URL and
    # clicking "Use URL" must use the pasted URL, not the highlighted
    # candidate (the bug this button was added to fix: clicking Pick out
    # of habit silently ignored a pasted URL).
    meta = ProductMetadata(title="Wrong Candidate", source=Source.DTRPG_LIBRARY, product_id="1")
    payload = {"path": Path("Book.pdf"), "progress": "", "candidates": [(meta, 90.0)]}

    def interact():
        dialog = _latest_toplevel(root)
        _find_widgets(dialog, tk.Listbox)[0].selection_set(0)
        entries = _find_widgets(dialog, ttk.Entry)
        entries[0].insert(0, "id:42")
        buttons = _find_widgets(dialog, ttk.Button)
        next(b for b in buttons if b.cget("text") == "Use URL").invoke()

    root.after(100, interact)
    result = show_candidate_dialog(root, payload)

    assert result.action == "url"
    assert result.product_id == "42"
    print("PASS: show_candidate_dialog_use_url_button")


# ---------------------------------------------------------------------------
# Review tab's overlay-edit mechanism
# ---------------------------------------------------------------------------


def test_review_tab_overlay_edit(root: tk.Tk) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        review_csv = Path(tmp) / "review.csv"
        row = ReviewRow(filename="Book.pdf", matched_title="Title", status=Status.NEEDS_REVIEW.value)
        save_review(review_csv, [row])

        # A withdrawn (unmapped) root never gets real screen geometry for
        # its children, so Treeview.bbox() can't return coordinates --
        # deiconify just for this test, which is the only one that packs
        # a widget straight into the root rather than a Toplevel dialog.
        root.deiconify()
        tab = ReviewTab(root, {"review_csv": str(review_csv)})
        tab.pack(fill="both", expand=True)
        root.update()
        try:
            bbox = tab.tree.bbox("Book.pdf", "status")
            assert bbox, "status column has no bbox -- row/column not realized"

            class _Event:
                pass

            event = _Event()
            event.x, event.y = bbox[0] + 2, bbox[1] + 2
            tab._begin_edit(event)
            assert tab._editor is not None, "no overlay editor created for the status column"
            tab._editor.set(Status.APPROVED.value)
            tab._editor.event_generate("<<ComboboxSelected>>")
            root.update()

            assert tab.rows_by_filename["Book.pdf"].status == Status.APPROVED.value
            assert tab.tree.set("Book.pdf", "status") == Status.APPROVED.value

            tab.save()
            reloaded = load_review(review_csv)
            assert reloaded[0].status == Status.APPROVED.value
        finally:
            tab.destroy()
            root.withdraw()
    print("PASS: review_tab_overlay_edit")


# No-Tk-needed tests (the request/response queue protocol) vs. tests that
# need the single shared root window (a dialog or the Review tab) -- kept
# separate because creating more than one tkinter.Tk() root per process
# turned out to be genuinely unstable in some environments (verified: it
# intermittently hung or crashed the interpreter outright here), even
# though the real app never does that itself -- GuiApp creates exactly
# one Tk() root for the whole run and only ever adds/destroys Toplevels
# under it. Sharing one root across these tests matches real usage and
# resolved the instability.
NO_ROOT_TESTS = [
    test_controller_candidate_pick_write,
    test_controller_manual_entry_write,
    test_controller_skip,
    test_controller_quit_mid_batch,
]

ROOT_TESTS = [
    test_show_confirm_dialog_edit_before_confirm,
    test_show_manual_entry_dialog_multiline_description,
    test_show_candidate_dialog_pick,
    test_show_candidate_dialog_use_url_button,
    test_review_tab_overlay_edit,
]


def main() -> None:
    failed = 0
    for test in NO_ROOT_TESTS:
        try:
            test()
        except Exception:
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()

    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError as exc:
        print(f"SKIPPED remaining {len(ROOT_TESTS)} test(s): no usable display ({exc})")
        total = len(NO_ROOT_TESTS)
        print(f"\n{total - failed}/{total} passed")
        sys.exit(1 if failed else 0)

    try:
        for test in ROOT_TESTS:
            try:
                test(root)
            except Exception:
                failed += 1
                print(f"FAIL: {test.__name__}")
                traceback.print_exc()
    finally:
        root.destroy()

    total = len(NO_ROOT_TESTS) + len(ROOT_TESTS)
    print(f"\n{total - failed}/{total} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
