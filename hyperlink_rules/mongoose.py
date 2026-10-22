"""Mongoose Publishing: Traveller (1st and 2nd edition) and 2300AD.

Started as a fork of the GURPS rules; what differs:

  - References are spelled out as well as abbreviated: "page 42" /
    "pages 152-3" (an abbreviated range end, meaning 152-153).
  - Another book is cited with its title *after* the number ("page 149 of
    the Traveller Core Rulebook"), bare before it ("see Characters &
    Equipment page 38"), or as a sibling volume of a boxed set ("in Book2
    page 23"). All skipped, on top of the GURPS-style checks.
  - "Contents" and "Index" appear once, as the section's heading, not in
    every footer.
  - /Title metadata is often empty, an InDesign filename, or "Book 9:
    Robot", so the trigger word falls back to the copyright line
    ("Traveller ©2026 Mongoose...").
  - A book can start its own chapter titles with the trigger word ("Vargr
    Race"), so those checks ignore matches that are this book's own
    TOC/Index entries.
"""

from hyperlink_engine import RuleSet, checks


class Rules(RuleSet):
    label = "Mongoose Traveller / 2300AD"
    description = ('"page 42" / "p. 42", "Chapter 3", TOC and Index; skips "page 149 of the '
                   'Traveller Core Rulebook" and "in Book2 page 23"')

    page_numbers = "footer"
    toc = "heading"
    index = "heading"
    toc_entries_style = "trailing_number"
    toc_linked_when = "half_linked"
    index_numbers = "outside_footer"

    reference_style = "single"
    reference_words = ("p.", "pp.", "page", "pages")
    expand_short_ranges = True
    chapter_refs = True

    trigger_word = {"default": "traveller", "strip_series_prefix": True, "copyright_fallback": True}
    # Titles cited bare or in italics. "Core Rulebook" on its own is safe:
    # neither Core Rulebook PDF (1e or 2e) puts those words next to a page
    # number in its own text.
    known_titles = (
        "Traveller Core Rulebook", "Traveller Core Book", "Core Rulebook",
        "Characters & Equipment", "Characters and Equipment",
        "The Worlds of 2300AD",
        "Robot Handbook", "Central Supply Catalogue",
        "Vehicle Handbook", "Aerospace Engineer's Handbook",
        "Ships of the Frontier", "Tools for Frontier Living",
        "Traveller Companion", "High Guard", "Aliens of Charted Space",
    )
    other_book_checks = (
        checks.book_code(),
        checks.sibling_volume(r"^Book\d{1,2}$"),
        checks.title_after(connectors=("of", "in"), min_words=2),
        checks.known_title_before(),
        checks.trigger_word_before(ignore_own_sections=True),
        checks.italic_known_title_before(),
    )
    chapter_checks = (
        checks.trigger_word_before(ignore_own_sections=True),
        checks.italic_known_title_before(),
    )
