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
# ]
# ///
"""Smoke tests for tag_tui.py -- the project's first committed test file.

Everything else in this project is verified with ad hoc throwaway
scripts (see CLAUDE.md's "No test suite" section); this is a scoped
exception specifically for tag_tui.py, whose screen/message wiring is
meaningfully more failure-prone than the print()/input() code it
replaced -- a wrong widget id or a dropped await silently breaks a
whole interactive path in a way that's easy to miss by reading the
code, and hard to catch without actually driving it.

Run directly: ./tests/test_tag_tui.py (from the repo root, or anywhere
-- paths below are relative to this file, not the cwd). No pytest --
matches the project's own "runnable script, not a framework" style
throughout. Each test builds a throwaway PDF, drives tag_tui's screens
via Textual's Pilot exactly the way a real user would (arrow keys,
typed text, button clicks), then reopens the actual PDF/sidecar files
with pikepdf/json and asserts on what was actually written -- not just
that the app ran without raising, per this project's own verification
standard (see CLAUDE.md: "not sufficient... verify against the real
thing").
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import MagicMock

import pikepdf
from textual.widgets import TextArea

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provenance import ProductMetadata, Source  # noqa: E402
from tag_tui import TagApp  # noqa: E402


def _client(**overrides) -> MagicMock:
    client = MagicMock()
    client.search_library.return_value = []
    client.search_catalog.return_value = []
    for name, value in overrides.items():
        getattr(client, name).return_value = value
    return client


async def test_candidate_pick_write():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Book.pdf"
        pikepdf.new().save(pdf)
        client = _client(
            search_library=[
                ProductMetadata(
                    title="Picked Title", series="Picked Series", description="d",
                    source=Source.DTRPG_LIBRARY, product_id="1",
                )
            ],
        )
        app = TagApp(pdfs=[pdf], client=client, manual_overrides={}, known_urls={},
                     thresholds={}, bookorbit_mode=False, rename=False, root_mode=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # select the (highlighted) first candidate
            await pilot.pause()
            await pilot.press("enter")  # confirm (default-focused button)
            await pilot.pause(delay=0.2)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "Picked Title", m.get("dc:title")
        assert any("Wrote metadata" in line for line in app.history), app.history
    print("PASS: candidate_pick_write")


async def test_manual_override_confirm():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Override.pdf"
        pikepdf.new().save(pdf)
        override = ProductMetadata(title="Override Title", series="Override Series", source=Source.MANUAL)
        app = TagApp(pdfs=[pdf], client=_client(), manual_overrides={pdf.name: override}, known_urls={},
                     thresholds={}, bookorbit_mode=False, rename=False, root_mode=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")  # confirm (no series prompt -- override already has one)
            await pilot.pause(delay=0.2)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "Override Title"
    print("PASS: manual_override_confirm")


async def test_known_url_auto_write_no_screen():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "URLBook.pdf"
        pikepdf.new().save(pdf)
        client = _client(get_product=ProductMetadata(title="URL Title", source=Source.DTRPG_CATALOG, product_id="9"))
        app = TagApp(pdfs=[pdf], client=client, manual_overrides={}, known_urls={pdf.name: "9"},
                     thresholds={}, bookorbit_mode=False, rename=False, root_mode=False)
        async with app.run_test() as pilot:
            # No key presses at all -- a known_urls match must write with
            # zero interaction, exactly like the old code's "deliberately
            # non-interactive end to end" known_urls branch.
            await pilot.pause(delay=0.3)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "URL Title"
    print("PASS: known_url_auto_write_no_screen")


async def test_manual_entry_multiline_description():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Manual.pdf"
        pikepdf.new().save(pdf)
        app = TagApp(pdfs=[pdf], client=_client(), manual_overrides={}, known_urls={},
                     thresholds={}, bookorbit_mode=False, rename=False, root_mode=False)
        multiline = "First paragraph.\n\nSecond paragraph, with more detail."
        async with app.run_test() as pilot:
            await pilot.pause()  # empty candidate screen (no candidates)
            await pilot.click("#manual")
            await pilot.pause()
            await pilot.click("#title")
            await pilot.press(*"Manually Typed Book")
            desc = pilot.app.screen.query_one("#description", TextArea)
            desc.text = multiline
            # The Submit button can fall below the visible area on a
            # standard terminal (this form has a lot of fields) -- use
            # the ctrl+s shortcut instead of clicking, exactly the fix
            # that was added after this test first caught the problem.
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("enter")  # the "About to write" confirm step
            await pilot.pause(delay=0.2)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "Manually Typed Book"
                assert m.get("dc:description") == multiline, repr(m.get("dc:description"))
        sidecar = json.loads((pdf.parent / "Manual.metadata.json").read_text())
        assert sidecar["metadata"]["description"] == multiline
    print("PASS: manual_entry_multiline_description (the actual feature)")


async def test_empty_state_url_paste():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "NoCandidates.pdf"
        pikepdf.new().save(pdf)
        client = _client(get_product=ProductMetadata(title="Via URL", source=Source.DTRPG_CATALOG, product_id="42"))
        app = TagApp(pdfs=[pdf], client=client, manual_overrides={}, known_urls={},
                     thresholds={}, bookorbit_mode=False, rename=False, root_mode=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#url")
            await pilot.press(*"id:42")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.click("#confirm")
            await pilot.pause(delay=0.2)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") == "Via URL"
    print("PASS: empty_state_url_paste")


async def test_skip():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Skip.pdf"
        pikepdf.new().save(pdf)
        client = _client(search_library=[ProductMetadata(title="X", description="d", source=Source.DTRPG_LIBRARY, product_id="1")])
        app = TagApp(pdfs=[pdf], client=client, manual_overrides={}, known_urls={},
                     thresholds={}, bookorbit_mode=False, rename=False, root_mode=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")  # skip at the candidate screen
            await pilot.pause(delay=0.2)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") is None
        assert any("Skipped" in line for line in app.history)
    print("PASS: skip")


async def test_quit_mid_batch_second_file_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        pdf1 = Path(tmp) / "A.pdf"
        pdf2 = Path(tmp) / "B.pdf"
        pikepdf.new().save(pdf1)
        pikepdf.new().save(pdf2)
        client = _client(search_library=[ProductMetadata(title="X", description="d", source=Source.DTRPG_LIBRARY, product_id="1")])
        app = TagApp(pdfs=[pdf1, pdf2], client=client, manual_overrides={}, known_urls={},
                     thresholds={}, bookorbit_mode=False, rename=False, root_mode=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#continue")  # blank batch series -> ask per book
            await pilot.pause()
            await pilot.press("q")  # quit at the very first candidate screen
            await pilot.pause(delay=0.2)

        with pikepdf.open(pdf1) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") is None
        with pikepdf.open(pdf2) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") is None
        assert "Stopped." in app.history
    print("PASS: quit_mid_batch_second_file_untouched")


async def test_rename_flag():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "RenameMe.pdf"
        pikepdf.new().save(pdf)
        client = _client(search_library=[ProductMetadata(
            title="Renamed Title", series="Renamed Series", description="d",
            source=Source.DTRPG_LIBRARY, product_id="1",
        )])
        app = TagApp(pdfs=[pdf], client=client, manual_overrides={}, known_urls={},
                     thresholds={}, bookorbit_mode=False, rename=True, root_mode=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(delay=0.2)

        expected = pdf.with_name("Renamed Series - Renamed Title.pdf")
        assert expected.exists(), list(Path(tmp).iterdir())
        assert not pdf.exists()
    print("PASS: rename_flag")


async def test_bookorbit_mode():
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "Combo.pdf"
        pikepdf.new().save(pdf)
        client = _client(search_library=[ProductMetadata(
            title="Combo Title", description="d", source=Source.DTRPG_LIBRARY, product_id="1",
        )])
        app = TagApp(pdfs=[pdf], client=client, manual_overrides={}, known_urls={},
                     thresholds={}, bookorbit_mode=True, rename=False, root_mode=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(delay=0.2)

        with pikepdf.open(pdf) as p:
            with p.open_metadata() as m:
                assert m.get("dc:title") is None, "bookorbit_mode should wipe embedded metadata"
        assert pdf.with_suffix(".opf").exists()
    print("PASS: bookorbit_mode")


async def test_batch_default_series_applied_silently():
    with tempfile.TemporaryDirectory() as tmp:
        pdf1 = Path(tmp) / "A.pdf"
        pdf2 = Path(tmp) / "B.pdf"
        pikepdf.new().save(pdf1)
        pikepdf.new().save(pdf2)
        client = _client(search_library=[ProductMetadata(title="X", description="d", source=Source.DTRPG_LIBRARY, product_id="1")])
        app = TagApp(pdfs=[pdf1, pdf2], client=client, manual_overrides={}, known_urls={},
                     thresholds={}, bookorbit_mode=False, rename=False, root_mode=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#series")
            await pilot.press(*"Shared Series")
            await pilot.click("#continue")
            await pilot.pause()
            # File 1: pick candidate, confirm -- no series Input should be
            # shown at all (needs_series_prompt False, applied silently).
            assert len(pilot.app.screen.query("#series")) == 0
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            # File 2: same -- still no series prompt.
            assert len(pilot.app.screen.query("#series")) == 0
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(delay=0.2)

        for pdf in (pdf1, pdf2):
            with pikepdf.open(pdf) as p:
                raw = p.Root.Metadata.read_bytes().decode("utf-8")
                assert "Shared Series" in raw, raw
    print("PASS: batch_default_series_applied_silently")


TESTS = [
    test_candidate_pick_write,
    test_manual_override_confirm,
    test_known_url_auto_write_no_screen,
    test_manual_entry_multiline_description,
    test_empty_state_url_paste,
    test_skip,
    test_quit_mid_batch_second_file_untouched,
    test_rename_flag,
    test_bookorbit_mode,
    test_batch_default_series_applied_silently,
]


async def main() -> None:
    failed = 0
    for test in TESTS:
        try:
            await test()
        except Exception:
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()
    total = len(TESTS)
    print(f"\n{total - failed}/{total} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
