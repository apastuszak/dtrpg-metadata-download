"""GURPS (Steve Jackson Games).

How GURPS books cite pages, and what that means here:

  - "p. 42" / "pp. 42-43", often with a narrow no-break space or none at
    all ("p.42"). "page" spelled out isn't used.
  - Another book is cited three ways, all skipped:
      * a book-code prefix: "p. B123" (Basic Set), "p. MA45" (Martial Arts);
      * the line name before the title: "GURPS Magic, p. 12" (the trigger
        word comes from the PDF's own title, falling back to "gurps");
      * an italic title with no line name, inside a supplement to it:
        "High-Tech, pp. 13-15" -- only for titles in known_titles.
  - "Contents" and "Index" run in the footer of every page of those
    sections, which is how they're found.
  - "Chapter 11" references link to that chapter, from the TOC's numbered
    entries.
  - The PDF's own /PageLabels are used when the printed footers confirm
    them (combined two-volume Basic Sets have two numbering runs).
"""

from hyperlink_engine import RuleSet, checks


class Rules(RuleSet):
    label = "GURPS (Steve Jackson Games)"
    description = '"p. 42" / "pp. 42-43", "Chapter 11", TOC and Index; skips "p. B123" and "GURPS Magic, p. 12"'

    page_numbers = "footer_or_labels"
    toc = "footer_word"
    index = "footer_word"
    toc_entries_style = "trailing_number"
    toc_linked_when = "half_linked"
    index_numbers = "outside_footer"

    reference_style = "single"
    reference_words = ("p.", "pp.")
    chapter_refs = True

    trigger_word = {"default": "gurps"}
    # GURPS 4e titles cited by name alone inside other GURPS books.
    # Necessarily incomplete, and that's the safe direction: an unlisted
    # title just isn't skipped. Extend it when a real citation turns up.
    known_titles = (
        "Basic Set", "Characters", "Campaigns",
        "Low-Tech", "High-Tech", "Ultra-Tech", "Bio-Tech",
        "Dungeon Fantasy", "Powers", "Magic", "Psionic Powers",
        "Martial Arts", "Horror", "Space", "Social Engineering",
        "Zombies", "Monster Hunters", "Action", "Banestorm",
        "Infinite Worlds", "Thaumatology", "Mysteries", "Supers",
        "Traveller", "Template Toolkit", "Power-Ups", "Pyramid",
        "Gun Fu", "Vehicles", "Steampunk",
    )
    other_book_checks = (
        checks.book_code(),
        checks.trigger_word_before(),
        checks.italic_known_title_before(),
    )
    chapter_checks = (
        checks.trigger_word_before(),
        checks.italic_known_title_before(),
    )
