"""Magic-byte dispatch and every §6.3 rejection path, plus E6/E7 (PLAN.md §10).

Runs against `fixtures/synthetic/` -- deliberate defects, not resumes. A real resume cannot
be password-protected, truncated, and normal at the same time, so these paths need files
built to be broken.
"""

from pathlib import Path

import pytest

from app.extract import dispatch
from app.extract.errors import (
    CorruptFileError,
    EmptyDocumentError,
    ExtractionError,
    FileTooLargeError,
    GarbledTextError,
    PasswordProtectedError,
    ScannedDocumentError,
    UnsupportedFormatError,
)

PROBES = Path(__file__).parent.parent / "fixtures" / "synthetic"


def _extract(name: str):
    return dispatch.extract_lines((PROBES / name).read_bytes(), name)


# ---------------------------------------------------------------------- dispatch (F2)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("header_contact.pdf", "pdf"),
        ("textboxes.docx", "docx"),
        ("mojibake.txt", "txt"),
        ("plain.md", "md"),
    ],
)
def test_dispatch_routes_by_magic_bytes(name, expected):
    assert _extract(name)[1] == expected


def test_F2_extension_that_lies_is_dispatched_on_content():
    """fake.pdf holds DOCX bytes. Dispatch must follow the bytes, not the name."""
    lines, kind, *_ = _extract("fake.pdf")
    assert kind == "docx"
    assert lines


# ------------------------------------------------------------------- rejections (§6.3)


@pytest.mark.parametrize(
    "name,error",
    [
        ("scanned.pdf", ScannedDocumentError),  # F1
        ("legacy.doc", UnsupportedFormatError),  # F3
        ("resume.odt", UnsupportedFormatError),  # F4
        ("resume.rtf", UnsupportedFormatError),  # F4
        ("locked.pdf", PasswordProtectedError),  # F5
        ("truncated.pdf", CorruptFileError),  # F6
        ("repaired.pdf", CorruptFileError),  # F6, the silent-repair branch
        ("huge.pdf", FileTooLargeError),  # F7
        ("empty.pdf", EmptyDocumentError),  # F8
        ("broken_cmap.pdf", GarbledTextError),  # E6
    ],
)
def test_rejections(name, error):
    with pytest.raises(error):
        _extract(name)


def test_every_rejection_carries_a_user_facing_message():
    for name in ("scanned.pdf", "locked.pdf", "legacy.doc", "huge.pdf", "empty.pdf"):
        with pytest.raises(ExtractionError) as caught:
            _extract(name)
        message = caught.value.message
        assert message.endswith((".", "!")) and len(message) > 20
        assert caught.value.code != "extraction_failed"


def test_F7_oversize_file_is_rejected_before_parsing():
    with pytest.raises(FileTooLargeError):
        dispatch.extract(b"%PDF-1.7" + b"\x00" * (11 * 1024 * 1024), "big.pdf")


def test_unknown_binary_is_rejected_not_guessed():
    with pytest.raises(UnsupportedFormatError):
        dispatch.extract(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200, "photo.png")


# -------------------------------------------------------------------------- tagging


def test_F9_hidden_white_text_is_extracted_and_flagged():
    lines, *_ = _extract("white_text.pdf")
    hidden = [line for line in lines if line.hidden_text]
    assert hidden, "white-on-white keyword stuffing must be extracted, not dropped"
    assert "Haskell" in " ".join(line.text for line in hidden)


def test_F10_tracked_changes_keep_insertions_and_drop_deletions():
    lines, *_ = _extract("tracked.docx")
    text = " ".join(line.text for line in lines)
    assert "Senior Engineer" in text  # w:ins
    assert "Junior Engineer" not in text  # w:del
    assert "tighten this" not in text  # comment body


def test_L4_docx_table_cells_are_tagged_and_row_major():
    lines, *_ = _extract("tracked.docx")
    cells = [line.text for line in lines if line.from_table]
    assert cells == ["Skill", "Years", "Python", "8", "Go", "4"]


def test_L5_docx_textbox_appears_once_in_document_order():
    lines, *_ = _extract("textboxes.docx")
    texts = [line.text for line in lines]
    boxed = [t for t in texts if t.startswith("KEY ACHIEVEMENT")]
    assert len(boxed) == 1, "mc:Choice and mc:Fallback both walked -> duplicate"
    assert texts.index(boxed[0]) == texts.index("EXPERIENCE") + 1


def test_L3_docx_header_and_footer_are_zoned():
    lines, *_ = _extract("tracked.docx")
    zones = {line.zone for line in lines}
    assert {"header", "footer", "body"} <= zones


def test_L7_repeated_footer_is_dropped_and_logged():
    lines, _, _, _, dropped = _extract("footer_pagenum.pdf")
    assert len(dropped) == 3
    assert all(d.reason == "repeated_page_footer" for d in dropped)
    assert not [line for line in lines if line.text.startswith("Page ")]


def test_L8_skill_bars_raise_a_warning_rather_than_failing_silently():
    _, _, _, warnings, _ = _extract("skill_bars.pdf")
    assert "visual_skill_rating_detected" in [w.code for w in warnings]


def test_E7_missing_word_spacing_is_recovered_from_glyph_geometry():
    lines, *_ = _extract("nospacing.pdf")
    text = " ".join(line.text for line in lines)
    assert "Senior Engineer at Acme Corp" in text
    assert "SeniorEngineer" not in text


def test_E9_cjk_resume_is_not_rejected_as_a_scan():
    """A complete CJK resume is only ~82 characters; a Latin density rule rejects it."""
    lines, *_ = _extract("cjk_resume.pdf")
    assert lines
    assert any(line.script == "cjk" for line in lines)


def test_E9_rtl_content_is_preserved_verbatim():
    lines, *_ = _extract("rtl_arabic.txt")
    assert any(line.script == "rtl" for line in lines)
    assert "جين مورغان" in [line.text for line in lines]


def test_no_fixture_crashes_unexpectedly():
    """Every probe either parses or raises a typed ExtractionError. Never crashes."""
    for path in sorted(PROBES.iterdir()):
        if not path.is_file():
            continue
        try:
            lines, *_ = dispatch.extract_lines(path.read_bytes(), path.name)
            assert lines, f"{path.name} produced no lines"
        except ExtractionError:
            pass


def test_E11_letterspaced_headings_are_recovered():
    """CSS letter-spacing emits a real space glyph between every character.

    Not in PLAN.md §6, found on a real resume: "S U M M A R Y" is unrecognisable as a
    heading, and the candidate's own name came through as "MU H A MMA D A H MA D R A ZA".
    The inverse of E7 -- word boundaries survive only as a wider gap.
    """
    lines, *_ = _extract("letterspaced.pdf")
    texts = [line.text for line in lines]
    assert "MUHAMMAD AHMAD RAZA" in texts
    assert "TARGET ROLE" in texts
    assert "SUMMARY" in texts
    assert "TECHNICAL SKILLS" in texts
    assert not any(" A " in t and t.isupper() for t in texts), "letter gaps survived"


def test_E11_does_not_disturb_ordinary_lines():
    """Normally spaced text must pass through the same pass untouched."""
    lines, *_ = _extract("letterspaced.pdf")
    texts = [line.text for line in lines]
    assert "Dart, Kotlin, Swift, JavaScript, SQL" in texts
    assert "Mobile Software Engineer | Flutter, Kotlin, Swift" in texts
