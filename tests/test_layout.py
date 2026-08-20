"""Column detection on synthetic bboxes and on the real two-column fixture (PLAN.md §10)."""

from pathlib import Path

from app.extract import dispatch
from app.extract.layout import order_blocks
from app.models import Block

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _column(x0: float, x1: float, count: int, page: int = 1) -> list[Block]:
    """A vertical run of blocks spanning most of the page height."""
    return [
        Block(
            page=page,
            bbox=(x0, 70.0 + i * 24, x1, 88.0 + i * 24),
            text=f"line {i}",
            order=i,
            page_width=595.0,
            page_height=842.0,
        )
        for i in range(count)
    ]


def test_L1_two_columns_are_detected_and_not_interleaved():
    left = _column(45, 200, 28)
    right = _column(265, 550, 28)
    ordered = order_blocks(left + right)
    columns = [b.column for b in ordered]
    assert set(columns) == {0, 1}
    # Every block of one column precedes every block of the other.
    assert columns == sorted(columns)


def test_L1_rejects_a_split_with_no_gutter():
    # Columns 10pt apart are not columns; MIN_GUTTER is 40.
    ordered = order_blocks(_column(45, 200, 28) + _column(210, 400, 28))
    assert {b.column for b in ordered} == {None}


def test_L1_rejects_a_split_that_does_not_span_the_page():
    # A short two-block-tall pair is a heading beside a date, not a column layout.
    ordered = order_blocks(_column(45, 200, 3) + _column(265, 550, 3))
    assert {b.column for b in ordered} == {None}


def test_L2_three_columns_are_detected():
    blocks = _column(40, 180, 20) + _column(240, 380, 20) + _column(450, 555, 20)
    ordered = order_blocks(blocks)
    assert set(b.column for b in ordered) == {0, 1, 2}


def test_L2_narrow_sidebar_is_emitted_last():
    # A sidebar under 30% of page width is emitted after the wide main column, which is
    # what PLAN.md L2 prescribes even when the sidebar sits on the left.
    sidebar = _column(40, 145, 28)  # ~18% of 595pt
    main = _column(250, 555, 28)
    ordered = order_blocks(sidebar + main)
    assert ordered[0].bbox[0] == 250.0  # main column first
    assert ordered[-1].bbox[0] == 40.0  # sidebar last


def test_single_column_is_the_default():
    ordered = order_blocks(_column(60, 500, 20))
    assert {b.column for b in ordered} == {None}


def test_formats_without_geometry_keep_document_order():
    blocks = [
        Block(page=1, bbox=(0, float(i), 0, float(i) + 1), text=f"p{i}", order=i)
        for i in range(5)
    ]
    assert [b.text for b in order_blocks(blocks)] == ["p0", "p1", "p2", "p3", "p4"]


def test_pages_stay_in_order():
    ordered = order_blocks(_column(60, 500, 10, page=2) + _column(60, 500, 10, page=1))
    assert [b.page for b in ordered] == [1] * 10 + [2] * 10


def test_L1_canva_fixture_matches_the_expected_reading_order():
    """The Phase 3 gate: the two-column fixture's order matches human reading order."""
    data = (FIXTURES / "synthetic" / "canva_2col.pdf").read_bytes()
    lines, *_ = dispatch.extract_lines(data, "canva_2col.pdf")
    expected = (FIXTURES / "expected" / "canva_2col.lines.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert [line.text for line in lines] == expected


def test_L1_sidebar_content_is_never_interleaved_into_experience():
    data = (FIXTURES / "synthetic" / "canva_2col.pdf").read_bytes()
    lines, *_ = dispatch.extract_lines(data, "canva_2col.pdf")
    texts = [line.text for line in lines]
    # Everything between EXPERIENCE and PROJECTS must belong to the work history --
    # a sidebar heading appearing in that range is the exact L1 failure.
    start, end = texts.index("EXPERIENCE"), texts.index("PROJECTS")
    assert not {"SKILLS", "EDUCATION", "CONTACT", "LANGUAGES"} & set(texts[start:end])
