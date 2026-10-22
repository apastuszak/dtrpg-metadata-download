"""Shadowrun Sixth World (Catalyst Game Labs) -- drafted by
hyperlink_survey.py from 13 books, then reviewed: known_titles gained
"Shadowrun, Sixth World" and "Slip Streams" (cited in the books, but too
rarely for the survey to list), and lost the core rulebook's filename-shaped
title. Every setting below says why it was chosen; RuleSet in
hyperlink_engine.py documents what each one does.

Catalyst names a cited book next to the page number, usually by code:
"(p. 94 and 96, SR6)", "(p. 124, 6WC)", "(pp. 132-3, Sixth World
Companion)" -- those are skipped, never linked to this book's own page.
"""

from hyperlink_engine import RuleSet, checks


class Rules(RuleSet):
    label = 'Shadowrun Sixth World (Catalyst)'
    description = ('"p. 94" references, TOC and Index; skips citations of other books '
                   '("(p. 94, SR6)", "p. 283 of Shadowrun, Sixth World")')

    # one numbering run per book; numbers seen in the header on 2103 pages,
    # the footer on 178
    page_numbers = 'header_and_footer'

    # books where each method found a TOC -- footer_word: 10/13, front_matter:
    # 1/13, heading: 12/13
    toc = 'heading'

    # books where each method found an Index -- footer_word: 4/13,
    # back_half_heading: 2/13, heading: 4/13
    index = 'footer_word'

    # TOC entries found: 1549 as a trailing number, 3289 as whole dot-leader
    # lines
    toc_entries_style = 'dot_leader_lines'

    # 9/13 books already have a linked TOC; never add a second set
    toc_linked_when = 'any_link'

    # 0/13 books already have a linked Index
    index_skip_if_linked = False

    # 204 of 3597 Index numbers sit in the header/footer band
    index_numbers = 'all_but_own_number'

    # 1128 of 3597 Index numbers' boxes overlap the line below
    link_inset = 0.12

    # 9 of 1312 references list several numbers ("pages 12, 14 and 16"); 3 use
    # "page"/"pages"
    reference_style = 'single'

    # forms seen -- "p.": 1298, "pp.": 10, "page": 3, "pg.": 1
    reference_words = (
        'p.',
        'pp.',
        'page',
        'pages',
    )

    # 3 ranges with a shortened end ("152-3")
    expand_short_ranges = True

    # 0 "Chapter N" references; 0 numbered chapters in the TOCs
    chapter_refs = False

    # words shared by most book titles: 'Shadowrun'
    line_name = 'Shadowrun'

    # the surveyed books' own titles, then "of the <Title>" phrases cited at
    # least twice that aren't the citing book's own sections; the last two
    # added in review
    known_titles = (
        'Body Shop',
        'Double Clutch',
        'Emerald City',
        'Firing Squad',
        'Hack and Slash',
        'No Future',
        'Power Plays',
        'Shadow Cast',
        'Sixth World Companion',
        'Street Wyrd',
        'The Neo-Anarchist Streetpedia',
        'Wild Life',
        'Collapsing Now',
        'Lockdown',
        'Run Faster',
        'Shadowrun, Sixth World',
        'Slip Streams',
    )

    # italic title before the number: 10 (only known titles are skipped); a
    # book named after the number inside the parentheses ("(p. 94, SR6)"):
    # 658; codes seen at least twice: SR6, SR5, 6WC; a known title after
    # "of"/"in" (added in review)
    other_book_checks = (
        checks.italic_known_title_before(),
        checks.title_in_parentheses(codes=('SR6', 'SR5', '6WC')),
        checks.known_title_after(),
    )
