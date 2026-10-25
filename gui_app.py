"""PyQt6 desktop GUI for dtrpg-metadata-download: a Tag tab and a
Preferences tab. `rename` and `page-labels` have no tab of their own:
the Tag tab does both with Metadata tagging off (no matching), including
a dry run.

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

Threading: the Tag tab's work (network calls, pikepdf I/O) runs on a
background QThread, with a blocking request/response round trip for its
interactive dialogs -- see gui_tag_flow.py.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGraphicsEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from dtrpg_client import DtrpgClient
from pdf_writer import DEFAULT_METADATA, write_page_labels_only
from preferences import (
    DEFAULT_PREFERENCES_PATH,
    Preferences,
    load_preferences,
    resolve_api_key,
    save_preferences,
)
from renamer import apply_rename, plan_rename
from rpg_hyperlink import rule_sets

# Shared checkbox explanatory text, used by TagTab (gui_tag_flow.py).
# "Metadata tagging" choices (pdf_writer.METADATA_MODES), each with the hint
# shown under the dropdown while it's selected.
METADATA_HINT = (
    "Chooses what book information (title, publisher, series, description, tags, ISBN and the "
    "DriveThruRPG product ID) is written, and where. Pick the one that matches the app you read your "
    "library in. Every choice except Off also removes a sidecar file it doesn't write, left by an "
    "earlier run with a different choice, so an old file can't disagree with the new one."
)
METADATA_CHOICES = (
    ("Off", "off",
     "Writes no book information, and doesn't look the book up on DriveThruRPG at all (no API key "
     "needed). Only the other options you pick run -- page labels, rename, hyperlinks and so on. "
     "Use it on books that are already tagged."),
    ("Calibre", "calibre",
     "Writes the book information into the PDF itself, in Calibre's format, and creates no other "
     "files. Calibre reads it, and so do apps that read Calibre-style PDF metadata: Kavita (everything "
     "but the ISBN), and BookOrbit with its default settings."),
    ("Grimmory", "grimmory",
     "Everything Calibre writes, plus a Grimmory sidecar file next to the PDF (<name>.metadata.json), "
     "which Grimmory reads. The default."),
    ("BookOrbit", "bookorbit",
     "Writes only a BookOrbit sidecar file next to the PDF (<name>.opf), and removes all metadata from "
     "the PDF itself. BookOrbit reads that file only if the library's metadata precedence (in its "
     "library settings) puts OPF file ahead of Embedded. With BookOrbit's default settings it reads "
     "the PDF instead and ignores the sidecar, so choose Calibre or Grimmory there."),
)
BOOKORBIT_URL = "https://bookorbit.app/"
CONVERT_IMAGES_HINT = (
    "RGB: converts all images in the PDF from CMYK to RGB, and all JPEG2000 images to JPEG. This can "
    "speed up file rendering on slower hardware, and can fix an issue on macOS with missing images "
    "in the PDF. This WILL increase the file size."
)
CONVERT_GRAYSCALE_HINT = (
    "Grayscale: converts the whole PDF to grayscale using a vendored script (requires Ghostscript "
    "-- 'gs' -- on PATH)."
)
RENAME_HINT = (
    "Renames the file to: Series Name - Book Name.pdf -- after the metadata update, from the "
    "confirmed match; with Metadata tagging off, from the metadata already in the file."
)
REFRESH_LIBRARY_HINT = (
    "Re-downloads your DriveThruRPG library listing before matching. It's cached and otherwise "
    "never refreshed, so tick this after buying new books."
)
DRY_RUN_HINT = (
    "Shows what Set page labels and Rename would do, without matching or changing any file. "
    "Every other option is ignored."
)
BACK_COVER_HINT = (
    "This doesn't search the book for the back cover: you need to know where it is now, and pick "
    "the move that starts from there. It only ever looks at the last page and PDF page 2. A retail "
    "barcode on one of them confirms it's the back cover; without one, you're shown the page and "
    "asked before anything moves. Books with a wraparound cover (front and back on one page) are "
    "left alone."
)
BACK_COVER_TO_PAGE_2_HINT = (
    "For a back cover that's currently the last page: moves it to right after the front cover."
)
BACK_COVER_TO_END_HINT = (
    "For a back cover that's currently PDF page 2, right after the front cover: moves it to the end "
    "of the book."
)
PAGE_LABELS_HINT = (
    "Sets the page numbers your PDF reader shows: \"Cover\" for the front and back cover, i, ii, iii "
    "before page 1, then the page numbers printed in the book, read from its headers and footers. "
    "Books whose numbering can't be read confidently keep their existing labels."
)
HYPERLINK_HINT = (
    "Auto-hyperlinks the book's own page and chapter references (e.g. \"see p. 208\"), plus its "
    "Table of Contents and Index, using one publisher's rules. Each rule set is tuned to its "
    "publisher's way of writing references, so only use it on that publisher's books."
)
BACKGROUND_HINT = "For Troll Lord Games PDFs (Castles and Crusades) only, using a vendored script."
BACKGROUND_LAYER_HINT = (
    "Layer: tags each page's full-page decorative background as a toggleable layer, so a PDF "
    "reader can hide it."
)
REMOVE_BACKGROUND_HINT = "Remove: deletes each page's full-page decorative background outright."

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


def _make_description_label(text: str) -> QLabel:
    """Short explanatory text at the top of a tab, saying what that tab
    does. Everything else is explained in tooltips (see _tooltip())."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def _tooltip(*paragraphs: str) -> str:
    """Tooltip text: escaped (rule-set descriptions and the like are shown
    as plain text, never markup) and wrapped in <p> so Qt treats it as
    rich text, which is what makes a long tooltip word-wrap instead of
    running off the screen."""
    return "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs if p)


class HelpIcon(QLabel):
    """The usual help badge -- a blue circle with a white "?" -- whose
    tooltip explains the option next to it. Hovering it shows the tooltip
    straight away rather than after Qt's usual delay, since the icon is
    there for nothing else. Painted rather than taken from QStyle: the
    standard question icon on macOS is the app-sized alert icon."""

    SIZE = 16
    BLUE = QColor("#0a84ff")  # macOS system blue

    def __init__(self, tip: str = ""):
        super().__init__()
        ratio = QApplication.instance().devicePixelRatio() if QApplication.instance() else 2.0
        pixmap = QPixmap(int(self.SIZE * ratio), int(self.SIZE * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.BLUE)
        painter.drawEllipse(QRectF(0, 0, self.SIZE, self.SIZE))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(QRectF(0, 0, self.SIZE, self.SIZE), Qt.AlignmentFlag.AlignCenter, "?")
        painter.end()
        self.setPixmap(pixmap)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setToolTip(tip)

    def enterEvent(self, event) -> None:
        QToolTip.showText(self.mapToGlobal(self.rect().bottomRight()), self.toolTip(), self)
        super().enterEvent(event)


def _add_check(layout: QVBoxLayout, text: str, tip: str) -> QCheckBox:
    """A checkbox with a HelpIcon after it; the explanation is the tooltip
    of both."""
    row = QHBoxLayout()
    check = QCheckBox(text)
    check.setToolTip(_tooltip(tip))
    row.addWidget(check)
    row.addWidget(HelpIcon(_tooltip(tip)))
    row.addStretch(1)
    layout.addLayout(row)
    return check


def _add_picker(
    layout: QVBoxLayout, label: str, items: list[tuple[str, object, str]],
    general_tip: str = "", default: object = None,
) -> QComboBox:
    """A labelled dropdown whose explanations are tooltips: each option has
    its own (shown while hovering it in the open list), and the dropdown,
    its label and the HelpIcon between them show `general_tip` and then
    every option's, the selected one marked.
    `items` is (shown text, currentData() value, tip)."""
    row = QHBoxLayout()
    row_label = QLabel(label)
    row.addWidget(row_label)
    help_icon = HelpIcon()
    row.addWidget(help_icon)
    combo = QComboBox()
    for i, (text, data, tip) in enumerate(items):
        combo.addItem(text, data)
        combo.setItemData(i, _tooltip(tip), Qt.ItemDataRole.ToolTipRole)
    row.addWidget(combo, 1)
    layout.addLayout(row)

    def show_tip() -> None:
        # Every choice, explained, with the selected one marked -- so the
        # tooltip answers "what would the others do?" too.
        current = combo.currentData()
        tip = _tooltip(general_tip) + "".join(
            f"<p><b>{html.escape(text)}</b>{' (selected)' if data == current else ''}: {html.escape(item_tip)}</p>"
            for text, data, item_tip in items
        )
        combo.setToolTip(tip)
        row_label.setToolTip(tip)
        help_icon.setToolTip(tip)

    combo.currentIndexChanged.connect(show_tip)
    combo.setCurrentIndex(max(combo.findData(default), 0))
    show_tip()
    return combo


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


def _add_back_cover_picker(layout: QVBoxLayout) -> QComboBox:
    """The back-cover move: Off, To page 2 (--back-cover-to-page-2) or To
    the end (--back-cover-to-end) -- one dropdown, since the two are
    opposite moves of the same page. `currentData()` is "page-2", "end" or
    None (back_cover.py's own names for the two directions)."""
    return _add_picker(layout, "Move back cover:", [
        ("Off", None, "Leaves the pages where they are."),
        ("To page 2 (--back-cover-to-page-2)", "page-2", BACK_COVER_TO_PAGE_2_HINT),
        ("To the end (--back-cover-to-end)", "end", BACK_COVER_TO_END_HINT),
    ], BACK_COVER_HINT)


def _add_metadata_picker(layout: QVBoxLayout) -> QComboBox:
    """The --metadata MODE choice. `currentData()` is the mode name; it
    starts on pdf_writer.DEFAULT_METADATA. The BookOrbit link is a real
    label under the dropdown, shown only while BookOrbit is selected -- a
    tooltip can't hold a clickable link."""
    combo = _add_picker(layout, "Metadata tagging (--metadata):", list(METADATA_CHOICES),
                        METADATA_HINT, default=DEFAULT_METADATA)
    link = _make_link_label(BOOKORBIT_URL)
    layout.addWidget(link)

    def show_link() -> None:
        link.setVisible(combo.currentData() == "bookorbit")

    combo.currentIndexChanged.connect(show_link)
    show_link()
    return combo


def _add_background_picker(layout: QVBoxLayout) -> QComboBox:
    """The page-background choice: Off, Layer (--background-layer) or
    Remove (--remove-background) -- one dropdown, since the two are
    opposite operations on the same background. `currentData()` is
    "layer", "remove" or None."""
    return _add_picker(layout, "Page background (Troll Lord Games PDFs only):", [
        ("Off", None, "Leaves the page backgrounds as they are."),
        ("Layer (--background-layer)", "layer", BACKGROUND_LAYER_HINT),
        ("Remove (--remove-background)", "remove", REMOVE_BACKGROUND_HINT),
    ], BACKGROUND_HINT)


def _add_convert_images_picker(layout: QVBoxLayout) -> QComboBox:
    """The "Convert images" choice: Off, RGB (--convert-images) or
    Grayscale (--convert-grayscale) -- one dropdown, since the two are
    opposite operations on the same images. `currentData()` is "rgb",
    "grayscale" or None."""
    return _add_picker(layout, "Convert images:", [
        ("Off", None, "Leaves the images as they are."),
        ("RGB (--convert-images)", "rgb", CONVERT_IMAGES_HINT),
        ("Grayscale (--convert-grayscale)", "grayscale", CONVERT_GRAYSCALE_HINT),
    ])


def _add_hyperlink_picker(layout: QVBoxLayout) -> QComboBox:
    """The --hyperlink RULES choice: "Off" plus every rule set found in
    hyperlink_rules/ (so a newly added one appears here with no GUI
    change), each with its own description as its tooltip. `currentData()`
    is the rule-set name, or None for off."""
    return _add_picker(layout, "Hyperlink references (--hyperlink):", [
        ("Off", None, "Adds no links."),
        *((info.label, info.name, info.description) for info in rule_sets()),
    ], HYPERLINK_HINT)


# ---------------------------------------------------------------------------
# Shared rename/page-label logic for the Tag tab's dry run.
# None of these functions touch Qt (or touched tkinter before them) --
# they take a plain `log` callable, so the Tk->Qt port needed no changes
# to their bodies at all, only to their callers.
# ---------------------------------------------------------------------------


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


def _label_one(pdf_path: Path, dry_run: bool, show_ranges: bool, log) -> str:
    """GUI twin of dtrpg-metadata-download.py's _label_one(), same reason
    _rename_one() above is duplicated."""
    result = write_page_labels_only(pdf_path, dry_run=dry_run)
    if result.status == "skipped":
        log(f"SKIP: {pdf_path.name} ({result.message})")
    elif result.status == "failed":
        log(f"FAILED: {pdf_path.name}: {result.message}")
    else:
        verb = "Would label" if dry_run else "Labeled"
        log(f"{verb}: {pdf_path.name} ({result.message})")
        if show_ranges:
            for line in result.plan.describe():
                log(f"    {line}")
    return result.status


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


class PreferencesTab(QWidget):
    """Saved API key + DriveThruRPG name -- see preferences.py's module
    docstring for why these live in their own gitignored, owner-only-
    permissioned file rather than config.yaml. The API key field is
    masked by default (a real secret), with a checkbox to reveal it,
    matching preferences_tui.py's equivalent in the TUI. The sibling
    scripts (GURPS/Mongoose hyperlinking, grayscale, background-layer,
    watermark removal) used to have their paths configured here too,
    before they were vendored as plain files in this project's own
    directory -- there's nothing left to configure for those."""

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
        prefs = Preferences(
            api_key=self.api_key_edit.text().strip(),
            dtrpg_name=self.name_edit.text().strip(),
        )
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


def _log_uncaught_exception(exc_type, exc_value, tb) -> None:
    # PyQt6 has no default handler for an exception escaping a slot on a
    # QThread -- verified directly: without this, one uncaught exception
    # (a network error, a bad write, anything a specific try/except in
    # this codebase doesn't already catch) aborts the whole process
    # (SIGABRT), not just the one background job that failed. Installing
    # this hook was independently confirmed to prevent that abort and
    # keep the rest of the GUI usable. The Tag tab's worker (TagWorker)
    # already catches and logs
    # per-job exceptions; this is only a backstop for whatever still
    # gets through.
    import traceback

    traceback.print_exception(exc_type, exc_value, tb)


def run_gui(config: dict) -> None:
    sys.excepthook = _log_uncaught_exception
    app = QApplication.instance() or QApplication(sys.argv)
    window = GuiApp(config)
    window.show()
    app.exec()
