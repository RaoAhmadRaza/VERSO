"""Unicode repair and line assembly (PLAN.md §6.1 L3/L6/L7, §6.2 E1--E10).

Two distinct jobs live here, and conflating them is the bug this module exists to avoid:

* `normalize_text` produces **display text**. The user sees exactly this.
* `match_key` produces a **matching key**, used only by the verify stage. Case folding and
  ASCII punctuation folding happen here and nowhere else (E4).

CLAUDE.md hard rule #8 -- displayed text is never normalized -- governs the split.
"""

import re
import unicodedata

import ftfy

from app.models import Block, DroppedLine, Line

# E5 / L9 -- private use areas, where symbol fonts hide bullet and icon glyphs.
PUA = re.compile(r"[-\U000F0000-\U000FFFFD\U00100000-\U0010FFFD]")
# E10 -- zero width and byte-order marks.
ZERO_WIDTH = re.compile(r"[​-‍﻿⁠]")
SOFT_HYPHEN = "­"  # E2
BULLET_CHARS = "•·◦▪‣∙⁃▸►−*‐-–—"
LEADING_BULLET = re.compile(rf"^[{re.escape(BULLET_CHARS)}]+\s+")
WHITESPACE = re.compile(r"[ \t  -   　]+")

# E1 -- Latin ligature block. See the note in `normalize_text`.
LIGATURES = "ﬀﬁﬂﬃﬄﬅﬆ"

# E4 -- folded to ASCII in the matching key only, never in display text.
PUNCT_FOLD = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"',
        "–": "-", "—": "-", "‒": "-", "−": "-", "―": "-",
        "…": "...", " ": " ",
    }
)

# L9 -- icon glyphs sit in front of contact details. What follows the glyph is the only
# thing that separates an icon from a bullet, since both are PUA at the line start.
CONTACT_LIKE = re.compile(
    r"^\s*(?:[^@\s]+@[^@\s]+\.\w+|\+?[\d()][\d\s()./-]{6,}|(?:https?://|www\.)\S+|"
    r"(?:linkedin|github|gitlab|twitter|x)\.com/\S+)",
    re.IGNORECASE,
)
PAGE_NUMBER = re.compile(r"\d+")
SENTENCE_END = re.compile(r"[.!?:;]$")
FTFY_CONFIG = ftfy.TextFixerConfig(uncurl_quotes=False, unescape_html=False)


def _script(text: str) -> str:
    """E9 -- coarse script detection. Drives the hyphen-rejoin suppression."""
    for char in text:
        code = ord(char)
        if 0x0590 <= code <= 0x08FF or 0xFB1D <= code <= 0xFDFF or 0xFE70 <= code <= 0xFEFF:
            return "rtl"
        if (
            0x3040 <= code <= 0x30FF
            or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF
        ):
            return "cjk"
    return "latin"


def normalize_text(text: str) -> tuple[str, bool]:
    """Repair one line of display text. Returns (text, is_bullet).

    Order matters: mojibake repair has to run before anything inspects individual
    codepoints, and bullet stripping has to run after PUA stripping or a symbol-font
    bullet never reaches the bullet test.
    """
    # E8 -- mojibake from a bad encoding round trip. Quotes are left curled because E4
    # restricts ASCII folding to the matching key.
    text = ftfy.fix_text(text, config=FTFY_CONFIG)

    # E5 / L9 -- a PUA glyph at the start is a bullet; elsewhere it is an icon sitting in
    # front of an email or phone number, so drop the glyph and keep the text.
    stripped = text.lstrip()
    pua_bullet = bool(stripped) and bool(PUA.match(stripped))
    text = PUA.sub("", text)

    text = ZERO_WIDTH.sub("", text)  # E10
    text = text.replace(SOFT_HYPHEN, "")  # E2 -- removing it rejoins the word

    # E1 -- PLAN.md §6.2 specifies unicodedata.normalize("NFKD") to split ligatures.
    # CONFLICT: CLAUDE.md hard rule #8 says displayed text is never normalized, and NFKD
    # also decomposes every accented character in the line, which shifts span offsets on
    # non-English resumes (C16/E9). Implemented literally as specified; if a fixture
    # regresses on this, the conflict is real and needs a decision rather than a patch.
    text = unicodedata.normalize("NFKD", text)

    # L9 -- a PUA glyph in front of an email or phone number is an icon, not a bullet.
    is_bullet = pua_bullet and not CONTACT_LIKE.match(text)
    if LEADING_BULLET.match(text):
        text = LEADING_BULLET.sub("", text, count=1)
        is_bullet = not CONTACT_LIKE.match(text)

    text = WHITESPACE.sub(" ", text).strip()
    return text, is_bullet


def match_key(text: str) -> str:
    """E4 -- the normalized form used for span matching. Never displayed."""
    text = ftfy.fix_text(text, config=FTFY_CONFIG)
    text = PUA.sub("", ZERO_WIDTH.sub("", text)).replace(SOFT_HYPHEN, "")
    text = unicodedata.normalize("NFKD", text.translate(PUNCT_FOLD))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return WHITESPACE.sub(" ", text).strip().casefold()


def _is_heading(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and text.isupper() and len(text) < 40


def _explode(blocks: list[Block]) -> list[dict]:
    """Blocks -> one record per physical line, before any joining."""
    records: list[dict] = []
    for block in blocks:
        for piece in block.text.rstrip("\n").split("\n"):
            text, bullet = normalize_text(piece)
            records.append(
                {
                    "text": text,
                    "page": block.page,
                    "bbox": block.bbox,
                    "column": block.column,
                    "zone": block.zone,
                    "is_bullet": bullet or block.is_bullet,
                    "from_table": block.from_table,
                    "hidden_text": block.hidden_text,
                    "script": _script(text),
                    "confidence": block.confidence,
                    "unjoined": [],
                }
            )
    return records


def _drop_repeated_furniture(
    records: list[dict], page_count: int
) -> tuple[list[dict], list[DroppedLine]]:
    """L7 and L3 -- remove page furniture, but log every removal.

    L7: a running footer like "Page 2 of 3" differs per page, so digits are masked before
    counting repetitions. Two pages is enough.
    L3: any other header/footer text is stripped only when it appears identically on every
    page -- contact details in a header must survive, and frequently they are the only
    place an email appears.
    """
    masked: dict[str, set[int]] = {}
    exact: dict[str, set[int]] = {}
    for record in records:
        if record["zone"] == "body" or not record["text"]:
            continue
        masked.setdefault(PAGE_NUMBER.sub("#", record["text"]), set()).add(record["page"])
        exact.setdefault(record["text"], set()).add(record["page"])

    kept: list[dict] = []
    dropped: list[DroppedLine] = []
    for record in records:
        text = record["text"]
        if record["zone"] != "body" and text:
            key = PAGE_NUMBER.sub("#", text)
            if key != text and len(masked.get(key, ())) >= 2:  # L7
                dropped.append(
                    DroppedLine(no=0, text=text, reason="repeated_page_footer")
                )
                continue
            if page_count > 1 and len(exact.get(text, ())) >= page_count:  # L3
                dropped.append(
                    DroppedLine(no=0, text=text, reason="repeated_on_every_page")
                )
                continue
        kept.append(record)
    return kept, dropped


def _join_hyphenated(records: list[dict]) -> list[dict]:
    """E3 -- a word split across a line break. Suppressed for RTL and CJK (E9)."""
    out: list[dict] = []
    index = 0
    while index < len(records):
        current = records[index]
        nxt = records[index + 1] if index + 1 < len(records) else None
        if (
            nxt
            and current["text"].endswith("-")
            and not current["text"].endswith("--")
            and len(current["text"]) > 1
            and nxt["text"][:1].islower()
            and current["page"] == nxt["page"]
            and current["column"] == nxt["column"]
            and current["script"] == "latin"
            and nxt["script"] == "latin"
        ):
            merged = dict(current)
            # BEHAVIOUR CHANGE, deliberate. This previously dropped the hyphen.
            #
            # PLAN.md's E3 example ("high-through-\nput") implies removing it, but measured
            # against the real corpus that corrupts words: aisha_bello.pdf contains
            # "top-\nrequested", a genuine compound that happens to break at its own hyphen,
            # and dropping it produced "toprequested" -- a word not in the document, which
            # violates hard rule #8.
            #
            # The two cases are indistinguishable without a dictionary, so this picks the
            # commoner one. Renderers that produce resumes (HTML/Word -> PDF) break at
            # existing hyphens; true auto-hyphenation emits a soft hyphen, which E2 already
            # removes before we get here. The failure mode is also milder: a spurious hyphen
            # is visible and recoverable, a silently fused word is neither.
            merged["text"] = f"{current['text']}{nxt['text']}"
            # Keep both halves so the verify stage can match a span written either way.
            merged["unjoined"] = [current["text"], nxt["text"]]
            out.append(merged)
            index += 2
            continue
        out.append(current)
        index += 1
    return out


def _join_page_breaks(records: list[dict]) -> list[dict]:
    """L6 -- a sentence continuing across a page break, joined before numbering."""
    out: list[dict] = []
    index = 0
    while index < len(records):
        current = records[index]
        nxt = records[index + 1] if index + 1 < len(records) else None
        if (
            nxt
            and nxt["page"] == current["page"] + 1
            and current["text"]
            and nxt["text"]
            and not SENTENCE_END.search(current["text"])
            and not _is_heading(current["text"])
            and not _is_heading(nxt["text"])
            and not nxt["is_bullet"]
            and current["script"] == "latin"
        ):
            merged = dict(current)
            merged["text"] = f"{current['text']} {nxt['text']}"
            merged["unjoined"] = [current["text"], nxt["text"]]
            out.append(merged)
            index += 2
            continue
        out.append(current)
        index += 1
    return out


def to_lines(
    blocks: list[Block], page_count: int = 1
) -> tuple[list[Line], list[DroppedLine]]:
    """Ordered blocks -> numbered lines, plus an audit trail of what was removed.

    Line numbers are 1-based and include blank lines, so they map onto the visual document.
    """
    records = _explode(blocks)
    records, dropped = _drop_repeated_furniture(records, page_count)
    records = _join_hyphenated(records)
    records = _join_page_breaks(records)

    lines = [
        Line(
            no=index,
            text=record["text"],
            page=record["page"],
            bbox=record["bbox"],
            column=record["column"],
            zone=record["zone"],
            is_bullet=record["is_bullet"],
            from_table=record["from_table"],
            hidden_text=record["hidden_text"],
            script=record["script"],
            confidence=record["confidence"],
            unjoined=record["unjoined"],
        )
        for index, record in enumerate(records, start=1)
    ]
    return lines, dropped
