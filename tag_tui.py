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
    CandidateScreen   <- _tag_one()'s candidate list + choice prompt
    ManualEntryScreen <- _prompt_manual_metadata()'s field-by-field prompts
    ConfirmScreen     <- the "About to write: X" / "Proceed? [y/N/q]" step,
                         shared by every path exactly as the old code
                         shared write_metadata()+_prompt_series() calls

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
also appended to self.history (a plain list) for tests and the final
summary screen to inspect, so nothing is lost, just displayed
differently than a naive port of the old print() calls would suggest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
    series: str | None = None


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


class BatchSeriesModal(ModalScreen[str | None]):
    """Once, only in --root mode with more than one file: "are all books
    in this batch the same series?" Result is None (ask per book, the old
    default_series = None) or a string -- possibly blank -- meaning
    "apply this to every book, never ask again" (see resolve_series's
    docstring for why a blank answer still counts as answered)."""

    DEFAULT_CSS = """
    BatchSeriesModal { align: center middle; }
    BatchSeriesModal > Vertical {
        width: 60; height: auto; border: thick $primary; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Are all books in this batch part of the same series?")
            with Horizontal():
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")
            yield Label("Series name (only used if Yes):")
            yield Input(id="series")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes":
            self.dismiss(self.query_one("#series", Input).value.strip())
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


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
                yield Label(f"{self.progress} -- Candidates for {self.path.name}:")
                yield OptionList(
                    *[
                        Option(format_candidate_lines(meta, score), id=str(i))
                        for i, (meta, score) in enumerate(self.candidates)
                    ],
                    id="candidates",
                )
            else:
                yield Label(f"{self.progress} -- No candidates found for {self.path.name}.")
            yield Label("Paste a DriveThruRPG URL, or type id:PRODUCT_ID, and press Enter:")
            yield Input(id="url", placeholder="https://www.drivethrurpg.com/... or id:12345")
            with Horizontal():
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
        text = event.value.strip()
        if not text:
            return
        product_id = extract_product_id(text)
        if product_id:
            self.dismiss(CandidateResult(action="url", product_id=product_id))
        else:
            self.notify("Could not parse a product ID/URL from that.", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "manual":
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
    shares in the old code (manual override, candidate pick, known-URL
    matches skip this entirely, matching their "deliberately non-
    interactive end to end" behavior). Only shows an editable series
    field when resolve_series() says one is actually needed -- silently
    applies an already-known or batch-default series otherwise, exactly
    like the old _prompt_series().
    """

    BINDINGS = [
        Binding("q", "quit_batch", "Quit"),
        Binding("escape", "skip", "Skip"),
    ]

    def __init__(self, header: str, meta: ProductMetadata, progress: str, needs_series_prompt: bool):
        super().__init__()
        self.header = header
        self.meta = meta
        self.progress = progress
        self.needs_series_prompt = needs_series_prompt

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(self.progress)
            yield Static(self.header)
            yield Static(format_candidate_lines(self.meta))
            if self.needs_series_prompt:
                yield Label("Series (Enter to leave blank):")
                yield Input(id="series")
            with Horizontal():
                yield Button("Confirm", id="confirm", variant="primary")
                yield Button("Skip (Esc)", id="skip")
                yield Button("Quit (q)", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        if self.needs_series_prompt:
            self.query_one("#series", Input).focus()
        else:
            self.query_one("#confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._confirm()
        elif event.button.id == "skip":
            self.action_skip()
        elif event.button.id == "quit":
            self.action_quit_batch()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._confirm()

    def _confirm(self) -> None:
        series = self.query_one("#series", Input).value.strip() if self.needs_series_prompt else None
        self.dismiss(ConfirmResult(action="confirm", series=series))

    def action_skip(self) -> None:
        self.dismiss(ConfirmResult(action="skip"))

    def action_quit_batch(self) -> None:
        self.dismiss(ConfirmResult(action="quit"))


class SummaryScreen(Screen[None]):
    """Shown once, after the last file (or after a mid-batch quit), so
    the app doesn't just vanish the instant the last write finishes --
    matches every other subcommand in this project ending with a printed
    summary line (e.g. cmd_rename's "X renamed, Y skipped, Z failed")."""

    BINDINGS = [Binding("q", "quit_app", "Quit")]

    def __init__(self, lines: list[str]):
        super().__init__()
        self.lines = lines

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static("Run summary (press q to exit):")
            for line in self.lines:
                yield Static(line)
        yield Footer()

    def action_quit_app(self) -> None:
        self.app.exit()


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
    ):
        super().__init__()
        self.pdfs = pdfs
        self.client = client
        self.manual_overrides = manual_overrides
        self.known_urls = known_urls
        self.thresholds = thresholds
        self.bookorbit_mode = bookorbit_mode
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

        await self.push_screen_wait(SummaryScreen(list(self.history)))
        self.exit()

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
        # doesn't skip that just because it collected its own fields.
        # needs_series_prompt is always False here: series was already
        # resolved inside ManualEntryScreen (matching the old
        # _prompt_manual_metadata(), which never calls _prompt_series()
        # separately), so there's nothing left for ConfirmScreen to ask.
        confirm = await self.push_screen_wait(
            ConfirmScreen(f"About to write: {result.meta.title}", result.meta, progress, needs_series_prompt=False)
        )
        if confirm.action == "quit":
            return True
        if confirm.action == "skip":
            self._log("Skipped.")
            return False
        row = row_from_match(path.name, result.meta, 100.0, Status.APPROVED)
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
        _, needs_prompt = resolve_series(meta.series, default_series)
        result = await self.push_screen_wait(ConfirmScreen(header, meta, progress, needs_prompt))
        if result.action == "quit":
            return True
        if result.action == "skip":
            self._log("Skipped.")
            return False

        row = row_from_match(path.name, meta, score, status)
        if needs_prompt:
            if result.series:
                row.series = result.series
        else:
            row.series, _ = resolve_series(meta.series, default_series)
        await self._write_and_maybe_rename(path, row)
        return False

    async def _write_and_maybe_rename(self, path: Path, row: ReviewRow) -> None:
        result = await self._call(write_metadata, path, row, bookorbit_mode=self.bookorbit_mode)
        self._log(f"Wrote metadata to {path.name}" if result.success else f"FAILED: {result.message}")
        if result.success and self.rename:
            outcome = await self._call(do_rename, path)
            if outcome:
                self._log(outcome)

    async def _call(self, fn, *args, **kwargs):
        """Run a blocking call (pikepdf/requests I/O) off the event loop
        thread so the UI doesn't freeze while it's in flight."""
        return await asyncio.to_thread(fn, *args, **kwargs)
