"""Textual TUI for `tag`'s interactive flow.

Presentation-layer replacement for the old print()/input()-based
_tag_one()/_manual_entry_flow()/_prompt_manual_metadata() functions that
used to live in dtrpg-metadata-download.py -- built specifically so
manual metadata entry can use a real multi-line TextArea for Description,
which input() can't do. Everything else about the matching/writing/
renaming pipeline is untouched: this module only calls into
matcher.find_candidates()/row_from_match()/extract_product_id(),
pdf_writer.write_metadata(), and renamer.plan_rename()/apply_rename() --
the exact same functions the old code called, with the exact same
ProductMetadata/ReviewRow objects. scan/write-pdfs/all/rename are not
interactive today and have no TUI involvement at all.

Screens replace print()/input() calls one for one:
    BatchSeriesModal  <- cmd_tag()'s "same series for the whole batch?"
    SeriesModal       <- _prompt_series()'s per-book "Series (blank=none):"
                         ask, shown after ConfirmScreen closes, only if
                         the confirmed metadata's series is still blank
    CandidateScreen   <- _tag_one()'s candidate list + choice prompt
    ManualEntryScreen <- _prompt_manual_metadata()'s field-by-field prompts
    ConfirmScreen     <- the "About to write: X" / "Proceed? [y/N/q]" step,
                         shared by every path exactly as the old code
                         shared write_metadata()+_prompt_series() calls;
                         now a full editable form of every field about
                         to be written, not just series

Orchestration is one linear coroutine (TagApp._run_flow, run as a
Textual worker) that `await self.push_screen_wait(...)`s through each
file in turn -- a direct async translation of the old cmd_tag()/
_tag_one() control flow, not a message-passing state machine, which
made it far easier to verify against the original logic line by line.

Outcome lines ("Wrote metadata to X", "Renamed: X -> Y", "Skipped.",
etc.) are shown as toast notifications (self.notify) rather than a
persistent on-screen log -- Textual's screen stack fully occludes
whatever's beneath a pushed screen, so a permanently-visible sidebar
would fight the framework rather than use it. Every outcome line is
also appended to self.history (a plain list) for tests to inspect and
for the final summary. The app quits itself the moment the run (or a
mid-batch quit) is done -- no "press q to exit" screen -- and the same
history is handed to App.exit(message=...), which Textual prints to
the real terminal after it's torn down the alternate screen, not
inside the TUI itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from dtrpg_client import DtrpgClient
from matcher import extract_product_id, find_candidates, row_from_match
from pdf_writer import write_metadata
from provenance import ProductMetadata, Source, Status
from renamer import apply_rename, plan_rename
from review import ReviewRow
from rpg_hyperlink import DEFAULT_GURPS_HYPERLINK_SCRIPT, DEFAULT_MONGOOSE_HYPERLINK_SCRIPT

# ---------------------------------------------------------------------------
# Pure helpers -- no Textual dependency, so they're testable without
# driving keystrokes through a screen.
# ---------------------------------------------------------------------------


def resolve_series(row_series: str, default_series: str | None) -> tuple[str, bool]:
    """Mirrors the old _prompt_series()'s three branches exactly:
    the match already has a series (no prompt needed), a batch-wide
    default was already answered (apply it silently, even if blank,
    never prompt again), or neither (needs an interactive prompt).
    Returns (series_to_use, needs_prompt).
    """
    if row_series:
        return row_series, False
    if default_series is not None:
        return default_series, False
    return "", True


def build_manual_metadata(
    title: str,
    publisher: str,
    series: str,
    series_index: str,
    description: str,
    tags_raw: str,
    isbn: str,
    product_url: str,
) -> ProductMetadata | None:
    """Mirrors the old _prompt_manual_metadata()'s validation/construction.
    Returns None if Title is blank -- the same "cancel" signal as before.
    """
    title = title.strip()
    if not title:
        return None
    tags = [t.strip() for t in tags_raw.split(";") if t.strip()]
    return ProductMetadata(
        title=title,
        series=series.strip(),
        series_index=series_index.strip(),
        publisher=publisher.strip(),
        tags=tags,
        description=description.strip(),
        product_url=product_url.strip(),
        source=Source.MANUAL,
        isbn=isbn.strip(),
    )


def apply_edits(
    meta: ProductMetadata,
    *,
    title: str,
    publisher: str,
    series: str,
    series_index: str,
    description: str,
    tags_raw: str,
    isbn: str,
    product_url: str,
) -> ProductMetadata | None:
    """Rebuilds meta from ConfirmScreen's edited field values. Preserves
    source/authors/product_id -- the fields the edit form doesn't expose,
    since they aren't hand-typed data (product_id in particular must stay
    whatever was actually matched/fetched, so dc:identifier and --rename
    still point at the right DriveThruRPG page even if the user tweaks
    the title). Returns None if Title was cleared, the same "cancel"
    signal as build_manual_metadata.
    """
    title = title.strip()
    if not title:
        return None
    tags = [t.strip() for t in tags_raw.split(";") if t.strip()]
    return replace(
        meta,
        title=title,
        publisher=publisher.strip(),
        series=series.strip(),
        series_index=series_index.strip(),
        description=description.strip(),
        tags=tags,
        isbn=isbn.strip(),
        product_url=product_url.strip(),
    )


def format_candidate_lines(meta: ProductMetadata, score: float | None = None) -> str:
    """Mirrors _print_candidate()'s field selection/order as a single
    renderable string instead of print() calls."""
    lines = [meta.title if score is None else f"({score:.1f}) {meta.title}"]
    if meta.publisher:
        lines.append(f"publisher: {meta.publisher}")
    if meta.authors:
        lines.append(f"authors:   {meta.authors_str()}")
    if meta.series:
        lines.append(f"series:    {meta.series} #{meta.series_index}")
    lines.append(f"source:    {meta.source.value if hasattr(meta.source, 'value') else meta.source}")
    return "\n".join(lines)


def do_rename(path: Path) -> str | None:
    """Same rename-one-file logic dtrpg-metadata-download.py's own
    _rename_one() implements for the standalone `rename` subcommand
    (plan_rename() + apply_rename(), both untouched), reimplemented here
    since _rename_one() lives in a script file with hyphens in its name
    and can't be imported by name, and its print()-based reporting
    doesn't fit a TUI. Returns a one-line outcome message, or None on
    success with nothing worth reporting beyond the caller's own message.
    """
    plan = plan_rename(path)
    result = apply_rename(plan, dry_run=False)
    if not result.success:
        if plan.reason is not None:
            return f"Rename skipped for {path.name}: {plan.reason}"
        return f"Rename failed for {path.name}: {result.message}"
    return f"Renamed: {path.name} -> {result.new_pdf.name}"


# ---------------------------------------------------------------------------
# Screen result types
# ---------------------------------------------------------------------------


@dataclass
class CandidateResult:
    action: Literal["pick", "url", "manual", "skip", "quit"]
    index: int | None = None
    product_id: str | None = None


@dataclass
class ManualEntryResult:
    action: Literal["submit", "cancel"]
    meta: ProductMetadata | None = None


@dataclass
class ConfirmResult:
    action: Literal["confirm", "skip", "quit"]
    meta: ProductMetadata | None = None


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class BatchSeriesModal(ModalScreen[str | None]):
    """Once, only in --root mode with more than one file: asks for a
    batch-wide series name up front. A non-blank answer is applied to
    every book in the run with no further prompting; a blank answer
    means None -- the books aren't all one series, so each book is
    prompted individually instead (see resolve_series's docstring)."""

    DEFAULT_CSS = """
    BatchSeriesModal { align: center middle; }
    BatchSeriesModal > Vertical {
        width: 60; height: auto; border: thick $primary; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Series name for this batch:")
            yield Label("Leave blank if all books are not in the same series.")
            yield Input(id="series")
            yield Button("Continue", id="continue", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        self.dismiss(self.query_one("#series", Input).value.strip() or None)


class SeriesModal(ModalScreen[str]):
    """Shown once per file, right after ConfirmScreen closes, only if
    the confirmed metadata's series is still blank at that point -- the
    user had every chance to fill it in on ConfirmScreen itself (which
    starts pre-filled with any batch default or match-provided series),
    so this is a final deliberate-choice check, not a first ask. Called
    out as its own dialog rather than a second silent pass over the
    same field. Leaving it blank here means no series, same convention
    as BatchSeriesModal.
    """

    DEFAULT_CSS = """
    SeriesModal { align: center middle; }
    SeriesModal > Vertical {
        width: 60; height: auto; border: thick $primary; padding: 1 2;
    }
    """

    def __init__(self, book_title: str):
        super().__init__()
        self.book_title = book_title

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Series for {self.book_title}:")
            yield Label("Leave blank for no series.")
            yield Input(id="series")
            yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#series", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        self.dismiss(self.query_one("#series", Input).value.strip())


class CandidateScreen(Screen[CandidateResult]):
    """Replaces _tag_one()'s candidate list + choice prompt. Shown even
    when there are no candidates at all, with just the URL/id: input plus
    manual-entry/skip/quit -- unlike the old y/N/q-only empty state, this
    keeps the URL-paste escape hatch available exactly when it matters
    most (search found nothing, but the user might still have the link).
    """

    BINDINGS = [
        Binding("m", "manual", "Manual entry"),
        Binding("q", "quit_batch", "Quit"),
        Binding("escape", "skip", "Skip"),
    ]

    def __init__(self, path: Path, progress: str, candidates: list[tuple[ProductMetadata, float]]):
        super().__init__()
        self.path = path
        self.progress = progress
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            if self.candidates:
                yield Label(f"{self.progress} -- Choose a Book from the List Provided ({self.path.name}):")
                yield OptionList(
                    *[
                        Option(format_candidate_lines(meta, score), id=str(i))
                        for i, (meta, score) in enumerate(self.candidates)
                    ],
                    id="candidates",
                )
            else:
                yield Label(f"{self.progress} -- No candidates found for {self.path.name}.")
            yield Label("Paste a DriveThruRPG URL, or type id:PRODUCT_ID (Enter or Use URL to submit):")
            yield Input(id="url", placeholder="https://www.drivethrurpg.com/... or id:12345")
            with Horizontal():
                yield Button("Use URL", id="use_url")
                yield Button("Manual entry (m)", id="manual")
                yield Button("Skip (Esc)", id="skip")
                yield Button("Quit (q)", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        if self.candidates:
            self.query_one("#candidates", OptionList).focus()
        else:
            self.query_one("#url", Input).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        assert event.option.id is not None
        self.dismiss(CandidateResult(action="pick", index=int(event.option.id)))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit_url()

    def _submit_url(self) -> None:
        text = self.query_one("#url", Input).value.strip()
        if not text:
            return
        product_id = extract_product_id(text)
        if product_id:
            self.dismiss(CandidateResult(action="url", product_id=product_id))
        else:
            self.notify("Could not parse a product ID/URL from that.", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "use_url":
            self._submit_url()
        elif event.button.id == "manual":
            self.action_manual()
        elif event.button.id == "skip":
            self.action_skip()
        elif event.button.id == "quit":
            self.action_quit_batch()

    def action_manual(self) -> None:
        self.dismiss(CandidateResult(action="manual"))

    def action_skip(self) -> None:
        self.dismiss(CandidateResult(action="skip"))

    def action_quit_batch(self) -> None:
        self.dismiss(CandidateResult(action="quit"))


class ManualEntryScreen(Screen[ManualEntryResult]):
    """Replaces _prompt_manual_metadata()'s field-by-field input()
    prompts -- the reason this whole rewrite exists: Description is a
    real multi-line TextArea here, not a single input() line. No quit
    binding here (the old code never offered one mid-prompt either --
    'q' isn't safe as a global binding on a screen that's almost entirely
    free-text fields, since a title/description could legitimately
    contain the letter q); Escape cancels, matching the old blank-Title
    cancel path -- both mean "return to the caller without writing
    anything," never "go back to the candidate list."
    """

    # ctrl+s submits regardless of scroll position -- this form has enough
    # fields (Title through Product URL, plus a multi-line Description)
    # that the Submit button can fall below the visible area on a
    # standard-size terminal, so relying on click-the-button alone isn't
    # enough; found by testing, not assumed.
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "submit", "Submit")]

    def __init__(self, path: Path, progress: str, default_series: str | None):
        super().__init__()
        self.path = path
        self.progress = progress
        self.default_series = default_series

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(f"{self.progress} -- Enter metadata for {self.path.name}")
            yield Label("Title (required):")
            yield Input(id="title")
            yield Label("Publisher:")
            yield Input(id="publisher")
            if self.default_series is not None:
                yield Label(f"Series (locked to this batch's answer): {self.default_series or '(blank)'}")
            else:
                yield Label("Series:")
                yield Input(id="series")
            yield Label("Series index:")
            yield Input(id="series_index")
            yield Label("Description:")
            yield TextArea(id="description")
            yield Label("Tags, semicolon-separated:")
            yield Input(id="tags")
            yield Label("ISBN:")
            yield Input(id="isbn")
            yield Label("Product URL (for your own reference):")
            yield Input(id="product_url")
            with Horizontal():
                yield Button("Submit (ctrl+s)", id="submit", variant="primary")
                yield Button("Cancel (Esc)", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#title", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.action_submit()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(ManualEntryResult(action="cancel"))

    def action_submit(self) -> None:
        if self.default_series is not None:
            series = self.default_series
        else:
            series = self.query_one("#series", Input).value
        meta = build_manual_metadata(
            title=self.query_one("#title", Input).value,
            publisher=self.query_one("#publisher", Input).value,
            series=series,
            series_index=self.query_one("#series_index", Input).value,
            description=self.query_one("#description", TextArea).text,
            tags_raw=self.query_one("#tags", Input).value,
            isbn=self.query_one("#isbn", Input).value,
            product_url=self.query_one("#product_url", Input).value,
        )
        if meta is None:
            self.notify("Title is required.", severity="error")
            self.query_one("#title", Input).focus()
            return
        self.dismiss(ManualEntryResult(action="submit", meta=meta))


class ConfirmScreen(Screen[ConfirmResult]):
    """The "About to write: X" / "Proceed? [y/N/q]" step every path
    shares in the old code (manual override, candidate pick; known-URL
    matches skip this entirely, matching their "deliberately non-
    interactive end to end" behavior). Shows every field that's about
    to be written -- not just series -- as an editable form pre-filled
    with the match's current values (including any already-resolved
    batch-default series), so nothing has to be exactly right in the
    original DriveThruRPG listing before it's confirmed. Authors/source
    are shown read-only underneath: informational only, since neither
    is actually written (see CLAUDE.md on dc:creator/publisher policy)
    or hand-editable here.
    """

    BINDINGS = [
        Binding("q", "quit_batch", "Quit"),
        Binding("escape", "skip", "Skip"),
        Binding("ctrl+s", "confirm", "Confirm"),
    ]

    def __init__(self, header: str, meta: ProductMetadata, progress: str):
        super().__init__()
        self.header = header
        self.meta = meta
        self.progress = progress

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(self.progress)
            yield Static(self.header)
            if self.meta.authors:
                yield Static(f"authors: {self.meta.authors_str()}")
            yield Static(f"source: {self.meta.source.value if hasattr(self.meta.source, 'value') else self.meta.source}")
            yield Label("Title (required):")
            yield Input(id="title", value=self.meta.title)
            yield Label("Publisher:")
            yield Input(id="publisher", value=self.meta.publisher)
            yield Label("Series:")
            yield Input(id="series", value=self.meta.series)
            yield Label("Series index:")
            yield Input(id="series_index", value=self.meta.series_index)
            yield Label("Description:")
            yield TextArea(self.meta.description, id="description")
            yield Label("Tags, semicolon-separated:")
            yield Input(id="tags", value=self.meta.tags_str())
            yield Label("ISBN:")
            yield Input(id="isbn", value=self.meta.isbn)
            yield Label("Product URL:")
            yield Input(id="product_url", value=self.meta.product_url)
            with Horizontal():
                yield Button("Confirm (ctrl+s)", id="confirm", variant="primary")
                yield Button("Skip (Esc)", id="skip")
                yield Button("Quit (q)", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.action_confirm()
        elif event.button.id == "skip":
            self.action_skip()
        elif event.button.id == "quit":
            self.action_quit_batch()

    def action_confirm(self) -> None:
        edited = apply_edits(
            self.meta,
            title=self.query_one("#title", Input).value,
            publisher=self.query_one("#publisher", Input).value,
            series=self.query_one("#series", Input).value,
            series_index=self.query_one("#series_index", Input).value,
            description=self.query_one("#description", TextArea).text,
            tags_raw=self.query_one("#tags", Input).value,
            isbn=self.query_one("#isbn", Input).value,
            product_url=self.query_one("#product_url", Input).value,
        )
        if edited is None:
            self.notify("Title is required.", severity="error")
            self.query_one("#title", Input).focus()
            return
        self.dismiss(ConfirmResult(action="confirm", meta=edited))

    def action_skip(self) -> None:
        self.dismiss(ConfirmResult(action="skip"))

    def action_quit_batch(self) -> None:
        self.dismiss(ConfirmResult(action="quit"))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class TagApp(App[None]):
    """One persistent instance for the whole run -- a single file or a
    --root batch both go through this, so there's one interactive code
    path instead of two (the old code kept single-file tagging on plain
    print()/input() while --root used the same functions in a loop; here
    both just differ in whether BatchSeriesModal/progress-numbering is
    used, per root_mode).
    """

    CSS = """
    Screen { align: center top; }
    VerticalScroll { width: 90%; max-width: 100; padding: 1 2; }
    """

    def __init__(
        self,
        pdfs: list[Path],
        client: DtrpgClient,
        manual_overrides: dict,
        known_urls: dict[str, str],
        thresholds: dict,
        bookorbit_mode: bool,
        rename: bool,
        root_mode: bool,
        convert_images: bool = False,
        hyperlink_gurps: bool = False,
        gurps_hyperlink_script: str | Path = DEFAULT_GURPS_HYPERLINK_SCRIPT,
        hyperlink_mongoose: bool = False,
        mongoose_hyperlink_script: str | Path = DEFAULT_MONGOOSE_HYPERLINK_SCRIPT,
    ):
        super().__init__()
        self.pdfs = pdfs
        self.client = client
        self.manual_overrides = manual_overrides
        self.known_urls = known_urls
        self.thresholds = thresholds
        self.bookorbit_mode = bookorbit_mode
        self.convert_images = convert_images
        self.hyperlink_gurps = hyperlink_gurps
        self.gurps_hyperlink_script = gurps_hyperlink_script
        self.hyperlink_mongoose = hyperlink_mongoose
        self.mongoose_hyperlink_script = mongoose_hyperlink_script
        self.rename = rename
        self.root_mode = root_mode
        self.history: list[str] = []

    def on_mount(self) -> None:
        self.run_flow()

    def _log(self, line: str) -> None:
        self.history.append(line)
        self.notify(line, timeout=4)

    def _progress(self, i: int) -> str:
        return f"[{i}/{len(self.pdfs)}]" if self.root_mode else ""

    @work
    async def run_flow(self) -> None:
        default_series: str | None = None
        if self.root_mode and len(self.pdfs) > 1:
            default_series = await self.push_screen_wait(BatchSeriesModal())

        for i, path in enumerate(self.pdfs, 1):
            stop = await self._process_one(path, self._progress(i), default_series)
            if stop:
                self._log("Stopped.")
                break

        summary = "\n".join(["Run summary:", *self.history]) if self.history else "Run summary: nothing to report."
        self.exit(message=summary)

    async def _process_one(self, path: Path, progress: str, default_series: str | None) -> bool:
        """Returns True if the user asked to stop the whole batch --
        the direct async equivalent of the old _tag_one()'s bool return."""
        if path.name in self.manual_overrides:
            return await self._confirm_and_write(
                path,
                progress,
                self.manual_overrides[path.name],
                header=f"Manual override found for {path.name}:",
            )

        if path.name in self.known_urls:
            product_id = self.known_urls[path.name]
            meta = await self._call(self.client.get_product, product_id)
            if meta is not None:
                row = row_from_match(path.name, meta, 100.0, Status.APPROVED)
                series, _ = resolve_series(row.series, default_series)
                row.series = series
                self._log(f"Known URL matched: {meta.title}")
                await self._write_and_maybe_rename(path, row)
                return False
            self._log(f"Known URL for {path.name} could not be fetched; falling back to search.")

        candidates = await self._call(find_candidates, path, self.client)
        result = await self.push_screen_wait(CandidateScreen(path, progress, candidates))

        if result.action == "quit":
            return True
        if result.action == "skip":
            self._log(f"Skipped: {path.name}")
            return False
        if result.action == "manual":
            return await self._manual_entry(path, progress, default_series)

        if result.action == "pick":
            assert result.index is not None
            meta, score = candidates[result.index]
            if meta.source == Source.DTRPG_LIBRARY and not meta.description:
                try:
                    meta = await self._call(self.client.enrich, meta)
                except Exception as exc:
                    self._log(f"(couldn't fetch full details: {exc}; proceeding with what we have)")
        else:
            assert result.product_id is not None
            meta = await self._call(self.client.get_product, result.product_id)
            if meta is None:
                self._log(f"Could not fetch product {result.product_id} from DriveThruRPG -- skipping.")
                return False
            score = 100.0

        status = Status.AUTO_ACCEPTED if score >= self.thresholds.get("high_confidence_threshold", 90.0) else Status.APPROVED
        return await self._confirm_and_write(
            path, progress, meta, header=f"About to write: {meta.title}", score=score, status=status, default_series=default_series,
        )

    async def _manual_entry(self, path: Path, progress: str, default_series: str | None) -> bool:
        result = await self.push_screen_wait(ManualEntryScreen(path, progress, default_series))
        if result.action == "cancel":
            self._log(f"Cancelled: {path.name}")
            return False
        assert result.meta is not None
        # Still one more confirm step, matching the old _manual_entry_flow()
        # ("About to write: {title}" / "Proceed? [y/N/q]") -- manual entry
        # doesn't skip that just because it collected its own fields; it
        # also gives one more chance to tweak a field before writing.
        confirm = await self.push_screen_wait(
            ConfirmScreen(f"About to write: {result.meta.title}", result.meta, progress)
        )
        if confirm.action == "quit":
            return True
        if confirm.action == "skip":
            self._log("Skipped.")
            return False
        assert confirm.meta is not None
        confirmed_meta = await self._maybe_ask_series(confirm.meta)
        row = row_from_match(path.name, confirmed_meta, 100.0, Status.APPROVED)
        await self._write_and_maybe_rename(path, row)
        return False

    async def _confirm_and_write(
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
        result = await self.push_screen_wait(ConfirmScreen(header, meta, progress))
        if result.action == "quit":
            return True
        if result.action == "skip":
            self._log("Skipped.")
            return False

        assert result.meta is not None
        confirmed_meta = await self._maybe_ask_series(result.meta)
        row = row_from_match(path.name, confirmed_meta, score, status)
        await self._write_and_maybe_rename(path, row)
        return False

    async def _maybe_ask_series(self, meta: ProductMetadata) -> ProductMetadata:
        """Called right after ConfirmScreen closes, for every path that
        goes through it: if the confirmed metadata's series is still
        blank at that point, ask once more with a dedicated dialog
        before writing, rather than silently writing no series just
        because ConfirmScreen's own Series field was left blank."""
        if meta.series:
            return meta
        series = await self.push_screen_wait(SeriesModal(meta.title))
        return replace(meta, series=series) if series else meta

    async def _write_and_maybe_rename(self, path: Path, row: ReviewRow) -> None:
        # write_metadata() (and the image conversion inside it) runs on a
        # worker thread via self._call() below -- self.notify()/self.history
        # aren't safe to touch from a thread that isn't the app's own event
        # loop, so this log callback marshals back via call_from_thread()
        # (verified with a throwaway script: a plain background thread
        # calling self.call_from_thread(self._log, line) correctly lands in
        # self.history with no crash/hang) rather than calling self._log
        # directly from inside write_metadata's thread.
        def log_line(line: str) -> None:
            self.call_from_thread(self._log, line)

        result = await self._call(
            write_metadata, path, row, bookorbit_mode=self.bookorbit_mode, convert_images=self.convert_images,
            hyperlink_gurps=self.hyperlink_gurps, gurps_hyperlink_script=self.gurps_hyperlink_script,
            hyperlink_mongoose=self.hyperlink_mongoose, mongoose_hyperlink_script=self.mongoose_hyperlink_script,
            log=log_line,
        )
        self._log(f"Wrote metadata to {path.name}" if result.success else f"FAILED: {result.message}")
        if result.success and self.rename:
            outcome = await self._call(do_rename, path)
            if outcome:
                self._log(outcome)

    async def _call(self, fn, *args, **kwargs):
        """Run a blocking call (pikepdf/requests I/O) off the event loop
        thread so the UI doesn't freeze while it's in flight."""
        return await asyncio.to_thread(fn, *args, **kwargs)
