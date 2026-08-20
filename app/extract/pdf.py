"""PDF extraction via PyMuPDF (PLAN.md §3, §6.1, §6.3).

The line pipeline is built from `get_text("blocks")`, exactly as PLAN.md §3 specifies. A
second, separate `get_text("dict")` pass attaches font size and fill colour, which
`blocks` mode does not expose and without which C11 (largest font on page 1) and F9
(text coloured ~ background) cannot be implemented at all. No line ever originates from
the dict pass -- it only decorates blocks that `blocks` mode already produced.
"""

import pymupdf

from app.extract import ocr
from app.extract.errors import (
    CorruptFileError,
    EmptyDocumentError,
    FileTooLargeError,
    GarbledTextError,
    PasswordProtectedError,
    ScannedDocumentError,
)
from app.models import Block, ParseWarning

MAX_PAGES = 15  # F7 -- more than this and it is not a resume
MIN_CHARS_PER_PAGE = 100  # F1 -- below this there is no usable text layer
MAX_NONALNUM_RATIO = 0.30  # E6 -- above this the ToUnicode CMap is broken
MIN_TOTAL_CHARS = 50  # F8 -- below this it is not a resume
HEADER_BAND = 0.07  # L3 -- y0 < 7% of page height
FOOTER_BAND = 0.93  # L3 -- y1 > 93% of page height
WHITE = 0xFFFFFF
COLOR_TOLERANCE = 8  # F9 -- per-channel distance from the page background
MIN_TABLE_CELLS = 4  # L4 -- guards against a two-column layout reading as a table
MAX_MEAN_WORD_LEN = 15  # E7 -- above this the block lost its word spacing
# E11 -- below this the block is letter-spaced ("S U M M A R Y"): every "word" is one glyph.
MAX_LETTERSPACED_TOKEN_LEN = 1.6
MIN_LETTERSPACED_TOKENS = 4
# E11 -- a glyph-to-glyph distance this many times the median marks a real word break.
WORD_BREAK_FACTOR = 2.0
MIN_GAP_POINTS = 0.3  # E7 -- floor for the recovered word-gap threshold

# MuPDF writes recovery chatter straight to stderr. Repairs are surfaced as a
# ParseWarning instead, so silence the raw stream. The layout-package suggestion is a
# one-off notice on find_tables and would otherwise pollute every CLI run.
pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.no_recommend_layout()


def _channels(color: int) -> tuple[int, int, int]:
    return (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF


def _is_background(color: int, background: int = WHITE) -> bool:
    """F9 -- fill colour within tolerance of the page background."""
    return all(
        abs(a - b) <= COLOR_TOLERANCE
        for a, b in zip(_channels(color), _channels(background))
    )


def _span_metadata(page: pymupdf.Page) -> dict[int, tuple[float, bool]]:
    """Map block number -> (largest font size, every span is background-coloured).

    Block numbers are shared between `blocks` and `dict` mode, so this keys cleanly.
    """
    meta: dict[int, tuple[float, bool]] = {}
    for block in page.get_text("dict")["blocks"]:
        if "lines" not in block:
            continue
        spans = [s for line in block["lines"] for s in line["spans"]]
        if not spans:
            continue
        largest = max(s["size"] for s in spans)
        hidden = all(_is_background(s["color"]) for s in spans if s["text"].strip())
        meta[block["number"]] = (largest, hidden)
    return meta


def _table_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """L4 -- ruled tables only.

    PLAN.md suggests detecting borderless tables with the text strategy, but measured
    against this corpus `find_tables(strategy="text")` returns a table for *every*
    document -- including plain single-column resumes (45 rows x 4 cols on
    single_col_word.pdf). It clusters text into a grid rather than finding tables, so it
    cannot discriminate and would tag the whole corpus `from_table`.

    Ruled detection is precise, and the row-major ordering L4 actually asks for is
    delivered by the reading-order sort in `layout.py`, which orders grid-arranged text by
    (row, column) regardless of whether it was tagged. DOCX keeps accurate tagging because
    `w:tbl` is unambiguous.
    """
    try:
        found = page.find_tables()
    except Exception:
        return []
    return [
        pymupdf.Rect(t.bbox)
        for t in found.tables
        if t.row_count * t.col_count >= MIN_TABLE_CELLS
    ]


def _skill_bar_warning(page: pymupdf.Page, text_rects: list[pymupdf.Rect]) -> bool:
    """L8 -- filled vector bars with no text on them are unrecoverable skill ratings."""
    bars = 0
    for drawing in page.get_drawings():
        if not drawing.get("fill"):
            continue
        rect = pymupdf.Rect(drawing["rect"])
        if rect.width < 40 or rect.height > 20 or rect.height <= 0:
            continue
        if not any(rect.intersects(tr) for tr in text_rects):
            bars += 1
    return bars >= 3


def _mean_word_length(text: str) -> float:
    words = text.split()
    return sum(len(w) for w in words) / len(words) if words else 0.0


def _is_letterspaced(text: str) -> bool:
    """E11 -- CSS letter-spacing renders as real space glyphs between every character."""
    tokens = text.split()
    if len(tokens) < MIN_LETTERSPACED_TOKENS:
        return False
    return sum(len(t) for t in tokens) / len(tokens) <= MAX_LETTERSPACED_TOKEN_LEN


def _unspace_line(chars: list[dict]) -> str:
    """E11 -- rebuild words from glyph geometry when every letter is space-separated.

    Letter-spacing is applied by widening the gap uniformly, so the distance between
    consecutive glyphs is near-constant inside a word and markedly larger at a real word
    break. Measured on a letter-spaced heading: 1.79pt between letters, 5.84pt between
    words -- a 3x step that clusters cleanly. Splitting on the median times a factor
    recovers the words without guessing at content.
    """
    glyphs = [c for c in chars if c["c"].strip()]
    if len(glyphs) < 2:
        return "".join(c["c"] for c in chars).strip()

    distances = [
        glyphs[i + 1]["bbox"][0] - glyphs[i]["bbox"][2] for i in range(len(glyphs) - 1)
    ]
    positive = sorted(d for d in distances if d > 0)
    if not positive:
        return "".join(g["c"] for g in glyphs)
    median = positive[len(positive) // 2]
    threshold = median * WORD_BREAK_FACTOR

    out = [glyphs[0]["c"]]
    for glyph, distance in zip(glyphs[1:], distances):
        if distance > threshold:
            out.append(" ")
        out.append(glyph["c"])
    return "".join(out)


def _respace(page: pymupdf.Page, numbers: set[int]) -> dict[int, str]:
    """E7 -- rebuild word spacing from character x-gaps.

    Some producers position glyphs without emitting space characters. PyMuPDF infers a
    space at roughly 0.15em and anything narrower extracts as one run-on token, but the
    positional step between words survives in `rawdict`. Splitting at half the largest gap
    on each line recovers the words without a second PDF library.
    """
    out: dict[int, list[str]] = {n: [] for n in numbers}
    for block in page.get_text("rawdict")["blocks"]:
        if block.get("number") not in numbers or "lines" not in block:
            continue
        for line in block["lines"]:
            chars = [c for span in line["spans"] for c in span["chars"]]
            if len(chars) < 2:
                out[block["number"]].append("".join(c["c"] for c in chars))
                continue
            joined = "".join(c["c"] for c in chars)
            if _is_letterspaced(joined):  # E11
                out[block["number"]].append(_unspace_line(chars))
                continue
            gaps = [
                chars[i + 1]["bbox"][0] - chars[i]["bbox"][2] for i in range(len(chars) - 1)
            ]
            threshold = max(MIN_GAP_POINTS, 0.5 * max(gaps))
            pieces = [chars[0]["c"]]
            for char, gap in zip(chars[1:], gaps):
                if gap >= threshold and not char["c"].isspace():
                    pieces.append(" ")
                pieces.append(char["c"])
            out[block["number"]].append("".join(pieces))
    return {n: "\n".join(lines) for n, lines in out.items() if lines}


def extract(data: bytes) -> tuple[list[Block], int, list[ParseWarning]]:
    """Bytes -> blocks. Raises `ExtractionError` subclasses for every §6.3 rejection."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # F6 -- MuPDF could not even rebuild it
        raise CorruptFileError(
            "This PDF appears to be corrupt or truncated and could not be opened."
        ) from exc

    warnings: list[ParseWarning] = []
    try:
        if doc.needs_pass:  # F5
            raise PasswordProtectedError(
                "This PDF is password protected. Upload an unlocked copy."
            )
        if doc.page_count > MAX_PAGES:  # F7
            raise FileTooLargeError(
                f"This document is {doc.page_count} pages. Resumes are at most {MAX_PAGES}."
            )
        if doc.page_count == 0:
            raise CorruptFileError("This PDF contains no pages.")
        if doc.is_repaired:
            # F6, the dangerous branch: MuPDF silently rebuilt a damaged xref and will
            # hand back whatever survived. Say so rather than presenting it as a clean parse.
            warnings.append(
                ParseWarning(
                    code="pdf_repaired",
                    detail="This PDF was damaged and had to be repaired. Content may be missing.",
                )
            )

        blocks = _extract_blocks(doc, warnings)
        try:
            _guard_text_layer(doc, blocks)
        except (ScannedDocumentError, GarbledTextError):
            # F1 / E6 -- Phase 7. OCR is opt-in; without it the clear error stands, which
            # is what PLAN.md specifies for phases 1-6.
            if not (ocr.enabled() and ocr.available()):
                raise
            blocks = ocr.extract(doc)
            if not blocks:
                raise
            warnings.append(
                ParseWarning(
                    code="ocr_used",
                    detail="This document had no readable text layer, so the text was "
                    "recovered by OCR and may contain recognition errors.",
                )
            )
        return blocks, doc.page_count, warnings
    finally:
        doc.close()


def _extract_blocks(doc: pymupdf.Document, warnings: list[ParseWarning]) -> list[Block]:
    blocks: list[Block] = []
    for page_no, page in enumerate(doc, start=1):
        height = page.rect.height or 1.0
        width = page.rect.width or 1.0
        meta = _span_metadata(page)
        tables = _table_rects(page)
        raw = page.get_text("blocks")

        text_rects = [pymupdf.Rect(b[:4]) for b in raw if b[6] == 0]
        if _skill_bar_warning(page, text_rects):  # L8
            warnings.append(
                ParseWarning(
                    code="visual_skill_rating_detected",
                    detail=f"Page {page_no} shows skill levels as bars. "
                    "Those ratings carry no text and cannot be read.",
                )
            )

        # E7 (run-on) and E11 (letter-spaced) are opposite symptoms of the same defect:
        # the word boundaries in the text layer do not match the ones on the page. Both are
        # repaired from glyph geometry in the same pass.
        needs_respacing = {
            b[5]
            for b in raw
            if b[6] == 0
            and (
                _mean_word_length(b[4]) > MAX_MEAN_WORD_LEN
                or any(_is_letterspaced(line) for line in b[4].splitlines())
            )
        }
        respaced = _respace(page, needs_respacing) if needs_respacing else {}

        for x0, y0, x1, y1, text, number, block_type in raw:
            if block_type != 0 or not text.strip():
                continue  # image blocks carry no text layer
            text = respaced.get(number, text)  # E7
            font_size, hidden = meta.get(number, (0.0, False))
            rect = pymupdf.Rect(x0, y0, x1, y1)
            blocks.append(
                Block(
                    page=page_no,
                    bbox=(x0, y0, x1, y1),
                    text=text,
                    order=number,
                    zone=_zone(y0, y1, height),
                    from_table=any(rect in t or rect.intersects(t) for t in tables),
                    hidden_text=hidden,
                    max_font_size=font_size,
                    page_width=width,
                    page_height=height,
                )
            )
    return blocks


def _zone(y0: float, y1: float, height: float) -> str:
    """L3 -- tag header/footer bands. Never strip: contact info lives up there."""
    if y0 < HEADER_BAND * height:
        return "header"
    if y1 > FOOTER_BAND * height:
        return "footer"
    return "body"


def _guard_text_layer(doc: pymupdf.Document, blocks: list[Block]) -> None:
    """F1, F6, F8, E6 -- refuse to pretend an unreadable page was read.

    Order matters, and the thresholds are measured against the corpus rather than assumed.
    A character-density rule alone rejects legitimate CJK resumes (C16/E9): cjk_resume.pdf
    carries a complete resume in 82 non-blank characters, well under the 100-per-page
    figure that suits Latin text. Density is therefore only applied when the page actually
    contains an image, which is what a scan looks like.
    """
    text = "".join(b.text for b in blocks)
    non_blank = sum(1 for c in text if not c.isspace())
    has_images = any(page.get_images(full=True) for page in doc)

    if doc.is_repaired and non_blank < MIN_TOTAL_CHARS:  # F6, the silent-repair branch
        raise CorruptFileError(
            "This PDF is damaged. Only a fragment of its text could be recovered."
        )
    if non_blank == 0 or (has_images and non_blank / max(doc.page_count, 1) < MIN_CHARS_PER_PAGE):
        raise ScannedDocumentError(  # F1
            "This PDF has no text layer -- it's a scan. Upload a text version, "
            "or enable OCR."
        )
    if non_blank < MIN_TOTAL_CHARS:  # F8
        raise EmptyDocumentError(
            f"This document holds only {non_blank} characters of text. It isn't a resume."
        )

    body = [c for c in text if not c.isspace()]
    garbage = sum(1 for c in body if not c.isalnum()) / len(body)
    if garbage > MAX_NONALNUM_RATIO:  # E6
        raise GarbledTextError(
            "This PDF's embedded font mapping is broken, so the extracted text is "
            "garbage. Upload a different export, or enable OCR."
        )
