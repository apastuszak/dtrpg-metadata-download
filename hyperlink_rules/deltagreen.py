"""Delta Green (Arc Dream Publishing), measured across 10 real books.

  - References use the word "page": "See COMBAT on page 48", "(page 164)",
    "pages 12-13", "pages 294, 296 and 300". ("p."/"pp." are accepted too,
    but almost never used.) A capitalized "Page"/"PAGE" is an in-fiction
    document's own pagination, and isn't a reference.
  - A same-book reference names a section in ALL CAPS ("See HUGE, page
    60") and is linked. Another book is named in mixed case, usually italic,
    after the number ("page 85 of the Agent's Handbook") or before it ("see
    the Handler's Guide, page 312"), and is skipped -- unless the title is
    this book's own. "page 1 of 3" (a handout's pagination) is skipped.
  - The TOC and the Index are each linked only if they have no internal
    links; InDesign exports links as named destinations, and every test
    book already had a linked TOC that way.
  - Index entries run into the header/footer band, so only the page's own
    printed number is skipped there. Link boxes are trimmed 12% top and
    bottom, since tightly set index lines otherwise overlap.
"""

from hyperlink_engine import RuleSet, checks


class Rules(RuleSet):
    label = "Delta Green (Arc Dream)"
    description = ('"page 48" / "pages 12, 14 and 16", TOC and Index if unlinked; skips '
                   '"page 85 of the Agent\'s Handbook"')

    page_numbers = "header_and_footer"
    toc = "front_matter"
    index = "back_half_heading"
    toc_entries_style = "dot_leader_lines"
    toc_linked_when = "any_link"
    index_skip_if_linked = True
    index_numbers = "all_but_own_number"

    reference_style = "list"
    reference_words = ("page", "pages", "p.", "pp.")
    skip_capitalized_reference_word = True
    body_refs_on_toc_and_index = False
    link_inset = 0.12
    toc_status = "added"

    line_name = "Delta Green"
    # Books Delta Green cites by name. Only needed for a title *before* the
    # number, where a plain capitalized phrase could just as well be a
    # same-book character ("see Sheryl Krieger, page 68"); a title after
    # "of"/"in" is recognised by its mixed case alone.
    known_titles = (
        "Agent's Handbook", "Handler's Guide", "Impossible Landscapes", "Need to Know",
        "Black Sites", "Control Group", "Static Protocol", "The Complex", "The Labyrinth",
        "A Night at the Opera", "Conspiracy", "Countdown",
        "God's Teeth", "Iconoclasts", "Extremophilia", "Observer Effect", "Viscid",
        "Sweetness", "Kali Ghati", "Lover in the Ice", "PX Poker Night", "The Last Equation",
        "Hourglass", "Ex Oblivione", "The Child", "Music from a Darkened Room",
    )
    other_book_checks = (
        checks.mixed_case_title_after(),
        checks.title_before_comma(),
    )
