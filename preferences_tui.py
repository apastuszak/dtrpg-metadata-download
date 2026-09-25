"""Textual TUI for viewing/editing preferences.yaml (API key and
DriveThruRPG account name) -- a small, standalone app, launched via the
`preferences` subcommand, separate from tag_tui.py's TagApp since it's a
wholly different concern (account settings, not tagging) with no reason
to share a screen stack with it.

Mirrors TagApp's own "auto-quit and print a plain summary to the real
terminal" pattern (see tag_tui.py's module docstring) for the same
reason: there's nothing left to do once the one thing this app exists for
-- saving or cancelling -- has happened, so it shouldn't need a "press q
to exit" step of its own.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static

from preferences import Preferences, load_preferences, save_preferences


class PreferencesApp(App[None]):
    CSS = """
    Screen { align: center middle; }
    Vertical { width: 70; height: auto; border: thick $primary; padding: 1 2; }
    """

    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "save", "Save")]

    def __init__(self, preferences_path: Path):
        super().__init__()
        self.preferences_path = preferences_path
        self.prefs = load_preferences(preferences_path)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("DriveThruRPG Preferences")
            yield Static(f"Saved to {self.preferences_path}", classes="dim")
            yield Label("API Key:")
            yield Input(value=self.prefs.api_key, password=True, id="api_key")
            yield Checkbox("Show API key", value=False, id="show_key")
            yield Label("DriveThruRPG Name:")
            yield Input(value=self.prefs.dtrpg_name, id="dtrpg_name")
            with Horizontal():
                yield Button("Save (ctrl+s)", id="save", variant="primary")
                yield Button("Cancel (Esc)", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#api_key", Input).focus()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "show_key":
            self.query_one("#api_key", Input).password = not event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_cancel()

    def action_save(self) -> None:
        api_key = self.query_one("#api_key", Input).value.strip()
        dtrpg_name = self.query_one("#dtrpg_name", Input).value.strip()
        save_preferences(Preferences(api_key=api_key, dtrpg_name=dtrpg_name), self.preferences_path)
        self.exit(message=f"Saved preferences to {self.preferences_path}")

    def action_cancel(self) -> None:
        self.exit(message="Cancelled -- no changes saved.")
