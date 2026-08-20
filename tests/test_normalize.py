"""One test per §6.2 rule, with crafted input strings (PLAN.md §9, Phase 2 gate).

Crafted strings rather than fixtures: several of these codepoints cannot survive being
written into a PDF (PyMuPDF folds ligatures and PUA to their nearest standard glyph), and a
rule needs testing whether or not a generator can express it.
"""

import pytest

from app.extract.normalize import match_key, normalize_text, to_lines
from app.models import Block


def _text(source: str) -> str:
    return normalize_text(source)[0]


def _bullet(source: str) -> bool:
    return normalize_text(source)[1]


# ------------------------------------------------------------------------------ §6.2


def test_E1_ligatures_are_split():
    assert _text("proﬁle workﬂow staﬀ oﬃce") == "profile workflow staff office"


def test_E2_soft_hyphen_rejoins_the_word():
    assert _text("Speciali­sation") == "Specialisation"


def test_E3_hyphenated_line_break_rejoins_and_keeps_both_halves():
    """BEHAVIOUR CHANGE, deliberate. This previously asserted the hyphen was dropped.

    Measured against the real corpus, dropping it corrupts genuine compounds:
    aisha_bello.pdf breaks "top-requested" at its own hyphen, and stripping produced
    "toprequested" -- text the user never wrote, which hard rule #8 forbids. The hyphen is
    now kept; `unjoined` still carries both halves for span matching either way.
    """
    blocks = [Block(page=1, bbox=(0, 0, 0, 1), text="the company's top-\nrequested feature", order=0)]
    lines, _ = to_lines(blocks)
    assert lines[0].text == "the company's top-requested feature"
    assert lines[0].unjoined == ["the company's top-", "requested feature"]


def test_E3_joins_the_line_even_though_the_hyphen_is_kept():
    """The join itself still happens -- two physical lines become one logical line."""
    blocks = [Block(page=1, bbox=(0, 0, 0, 1), text="a high-through-\nput pipeline", order=0)]
    lines, _ = to_lines(blocks)
    assert len(lines) == 1
    assert lines[0].text == "a high-through-put pipeline"


def test_E3_is_not_applied_when_the_next_line_starts_uppercase():
    blocks = [Block(page=1, bbox=(0, 0, 0, 1), text="Senior Engineer-\nAcme Corp", order=0)]
    lines, _ = to_lines(blocks)
    assert len(lines) == 2


def test_E4_display_text_keeps_smart_punctuation():
    source = "the team’s “payments” — shipped"
    assert _text(source) == source  # hard rule #8: displayed text is never normalized


def test_E4_match_key_folds_punctuation_to_ascii():
    assert match_key("the team’s “payments” — shipped") == (
        "the team's \"payments\" - shipped"
    )


def test_E5_pua_bullet_sets_is_bullet_and_is_stripped():
    assert normalize_text(" Built the thing") == ("Built the thing", True)


def test_E6_and_E7_are_enforced_in_the_pdf_stage():
    # E6 (broken CMap) and E7 (missing word spacing) need page geometry, so they live in
    # app/extract/pdf.py and are covered by test_dispatch.py.
    pytest.skip("covered in test_dispatch.py -- both need page geometry")


def test_E8_mojibake_is_repaired():
    assert _text("the teamâ€™s work") == "the team’s work"


def test_E9_script_detection_marks_rtl_and_cjk():
    blocks = [
        Block(page=1, bbox=(0, 0, 0, 1), text="جين مورغان", order=0),
        Block(page=1, bbox=(0, 1, 0, 2), text="张伟", order=1),
        Block(page=1, bbox=(0, 2, 0, 3), text="Jane Morgan", order=2),
    ]
    lines, _ = to_lines(blocks)
    assert [line.script for line in lines] == ["rtl", "cjk", "latin"]


def test_E9_suppresses_hyphen_rejoin_for_cjk():
    blocks = [Block(page=1, bbox=(0, 0, 0, 1), text="张伟-\n软件", order=0)]
    lines, _ = to_lines(blocks)
    assert len(lines) == 2  # E3 must not fire across a CJK break


def test_E10_zero_width_and_nbsp_are_removed():
    assert _text("jane​.morgan﻿@x.com") == "jane.morgan@x.com"
    assert _text("a senior title") == "a senior title"


# ------------------------------------------------------------------------------ §6.1


def test_L3_header_repeated_on_every_page_is_dropped_with_a_reason():
    blocks = [
        Block(page=p, bbox=(0, 0, 0, 1), text="CONFIDENTIAL", order=0, zone="header")
        for p in (1, 2)
    ] + [Block(page=1, bbox=(0, 5, 0, 6), text="Jane Morgan", order=1)]
    lines, dropped = to_lines(blocks, page_count=2)
    assert [d.reason for d in dropped] == ["repeated_on_every_page"] * 2
    assert [line.text for line in lines] == ["Jane Morgan"]


def test_L3_header_contact_details_are_kept_not_stripped():
    # Contact info in a header appears once, so it must survive -- it is frequently the
    # only place an email is written.
    blocks = [
        Block(page=1, bbox=(0, 0, 0, 1), text="jane@example.com", order=0, zone="header"),
        Block(page=1, bbox=(0, 5, 0, 6), text="Jane Morgan", order=1),
    ]
    lines, dropped = to_lines(blocks, page_count=1)
    assert dropped == []
    assert "jane@example.com" in [line.text for line in lines]


def test_L6_role_spanning_a_page_break_is_joined():
    blocks = [
        Block(page=1, bbox=(0, 700, 0, 710), text="Built the billing pipeline that", order=0),
        Block(page=2, bbox=(0, 70, 0, 80), text="handles 4M events per day.", order=1),
    ]
    lines, _ = to_lines(blocks, page_count=2)
    assert lines[0].text == "Built the billing pipeline that handles 4M events per day."


def test_L6_does_not_join_across_a_heading():
    blocks = [
        Block(page=1, bbox=(0, 700, 0, 710), text="Wrote the regression suite", order=0),
        Block(page=2, bbox=(0, 70, 0, 80), text="EDUCATION", order=1),
    ]
    lines, _ = to_lines(blocks, page_count=2)
    assert len(lines) == 2


def test_L7_repeated_page_footer_is_dropped_but_logged():
    blocks = [
        Block(page=p, bbox=(0, 800, 0, 810), text=f"Page {p} of 3", order=p, zone="footer")
        for p in (1, 2, 3)
    ] + [Block(page=1, bbox=(0, 5, 0, 6), text="Jane Morgan", order=9)]
    lines, dropped = to_lines(blocks, page_count=3)
    assert len(dropped) == 3
    assert all(d.reason == "repeated_page_footer" for d in dropped)
    assert [line.text for line in lines] == ["Jane Morgan"]


def test_L9_icon_glyph_before_contact_is_not_a_bullet():
    assert normalize_text(" jane@example.com") == ("jane@example.com", False)
    assert normalize_text(" +1 415 555 0199") == ("+1 415 555 0199", False)


def test_bullet_dash_sets_is_bullet():
    assert _bullet("- Built the billing pipeline") is True
    assert _bullet("• Shipped it") is True


def test_line_numbers_are_one_based_and_contiguous():
    blocks = [Block(page=1, bbox=(0, 0, 0, 1), text="a\nb\nc", order=0)]
    lines, _ = to_lines(blocks)
    assert [line.no for line in lines] == [1, 2, 3]
