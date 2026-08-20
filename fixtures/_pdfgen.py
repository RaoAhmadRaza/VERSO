"""Minimal PDF writer for fixtures -- PyMuPDF only, no new dependency.

PyMuPDF already ships as the parser's PDF reader (PLAN.md §4), so using it to author
fixtures costs nothing. Page geometry is A4 and every helper takes explicit coordinates,
because several fixtures (L1, L2, L3) are *about* geometry.
"""

import pymupdf

A4_W, A4_H = 595.0, 842.0
# PyMuPDF infers a space at roughly 0.15em. A narrower gap extracts as a run-on token
# while still leaving a measurable step between words in rawdict -- the real E7 shape.
SUBWORD_GAP = 0.10
FIXED_META = {
    "title": "fixture",
    "author": "verso",
    "creationDate": "D:20240101000000Z",
    "modDate": "D:20240101000000Z",
}


def new_doc() -> pymupdf.Document:
    return pymupdf.open()


def page(doc: pymupdf.Document) -> pymupdf.Page:
    return doc.new_page(width=A4_W, height=A4_H)


def put(
    pg: pymupdf.Page,
    lines: list[str],
    x: float,
    y: float,
    size: float = 10.0,
    leading: float = 14.0,
    font: str = "helv",
    color: tuple[float, float, float] = (0, 0, 0),
) -> float:
    """Draw `lines` top-down from (x, y). Returns the y after the last line."""
    for i, text in enumerate(lines):
        if text:
            pg.insert_text(
                (x, y + i * leading), text, fontname=font, fontsize=size, color=color
            )
    return y + len(lines) * leading


def put_chars_tight(pg: pymupdf.Page, text: str, x: float, y: float, size: float = 10.0) -> None:
    """E7 -- place each glyph individually with no space characters between words.

    Words are separated by a positional step too small for PyMuPDF's own space inference
    (~0.15em), so `get_text` returns one run-on token -- but `rawdict` still shows a clear
    step at each word boundary, which is what makes E7 recoverable.
    """
    cursor = x
    for ch in text:
        if ch == " ":
            cursor += size * SUBWORD_GAP  # too small for PyMuPDF, big enough to recover
            continue
        pg.insert_text((cursor, y), ch, fontname="helv", fontsize=size)
        cursor += pymupdf.get_text_length(ch, fontname="helv", fontsize=size)


def save(doc: pymupdf.Document, path, **kw) -> None:
    doc.set_metadata(FIXED_META)
    doc.save(str(path), garbage=4, deflate=True, **kw)
    doc.close()
