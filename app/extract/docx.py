"""DOCX extraction: stdlib zipfile + lxml, walking the XML ourselves (PLAN.md §4).

`python-docx` is deliberately not used. It silently skips text boxes and headers, which is
exactly the content that goes missing from resumes and exactly what L5 and L3 are about.

The walk is a single document-order traversal. A `w:p` closes the current buffer, so
paragraphs nested inside a text box emit as their own blocks at the position the text box
appears -- not appended at the end.
"""

import zipfile

from lxml import etree

from app.extract.errors import CorruptFileError
from app.models import Block

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# F10 -- deleted runs and comment anchors never become content. `w:delText` is inside
# `w:del`, but list it too in case a producer emits it bare.
DROP_TAGS = {
    "del", "delText", "commentReference", "commentRangeStart", "commentRangeEnd",
    "footnoteReference", "endnoteReference", "proofErr", "bookmarkStart", "bookmarkEnd",
}


def _localname(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


class _Ctx:
    """Traversal state: the open paragraph buffer plus its flags."""

    def __init__(self, page: int, zone: str):
        self.blocks: list[Block] = []
        self.buf: list[str] = []
        self.bullet = False
        self.in_table = False
        self.page = page
        self.zone = zone

    def flush(self) -> None:
        text = "".join(self.buf).strip()
        self.buf.clear()
        if not text:
            return
        index = len(self.blocks)
        self.blocks.append(
            Block(
                page=self.page,
                # DOCX carries no geometry. A synthetic y keeps document order stable
                # through the reading-order sort in layout.py.
                bbox=(0.0, float(index), 0.0, float(index) + 1.0),
                text=text,
                order=index,
                zone=self.zone,
                from_table=self.in_table,
                is_bullet=self.bullet,
            )
        )


def _walk(element, ctx: _Ctx) -> None:
    tag = _localname(element.tag)

    if tag in DROP_TAGS:
        return

    if tag == "AlternateContent":
        # Word emits the same text box twice: modern DrawingML under mc:Choice and legacy
        # VML under mc:Fallback. Taking both duplicates every text box.
        chosen = None
        for child in element:
            name = _localname(child.tag)
            if name == "Choice":
                chosen = child
                break
            if name == "Fallback" and chosen is None:
                chosen = child
        if chosen is not None:
            for child in chosen:
                _walk(child, ctx)
        return

    if tag == "p":
        ctx.flush()  # close whatever preceded this paragraph
        was_bullet = ctx.bullet
        ctx.bullet = element.find(f".//{W}numPr") is not None
        for child in element:
            _walk(child, ctx)
        ctx.flush()
        ctx.bullet = was_bullet
        return

    if tag == "tbl":
        ctx.flush()
        was_in_table = ctx.in_table
        ctx.in_table = True  # L4 -- unambiguous in DOCX, unlike the PDF case
        for child in element:
            _walk(child, ctx)
        ctx.flush()
        ctx.in_table = was_in_table
        return

    if tag == "t":
        ctx.buf.append(element.text or "")
        return
    if tag == "tab":
        ctx.buf.append("\t")
        return
    if tag in ("br", "cr"):
        ctx.buf.append("\n")
        return

    for child in element:
        _walk(child, ctx)


def _part_blocks(archive: zipfile.ZipFile, name: str, page: int, zone: str) -> list[Block]:
    try:
        root = etree.fromstring(archive.read(name))
    except (KeyError, etree.XMLSyntaxError):
        return []
    ctx = _Ctx(page=page, zone=zone)
    _walk(root, ctx)
    ctx.flush()
    return ctx.blocks


def extract(data: bytes) -> tuple[list[Block], int]:
    """Bytes -> blocks. Headers first, then the body, then footers.

    DOCX has no page geometry, so `page` is always 1 and L3 zoning comes from which part
    the text lived in rather than from a y coordinate.
    """
    try:
        archive = zipfile.ZipFile(__import__("io").BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CorruptFileError("This .docx file is corrupt and could not be opened.") from exc

    with archive:
        names = set(archive.namelist())
        if "word/document.xml" not in names:
            raise CorruptFileError("This .docx file has no document body.")

        blocks: list[Block] = []
        for name in sorted(n for n in names if n.startswith("word/header")):
            blocks += _part_blocks(archive, name, 1, "header")  # L3
        blocks += _part_blocks(archive, "word/document.xml", 1, "body")
        for name in sorted(n for n in names if n.startswith("word/footer")):
            blocks += _part_blocks(archive, name, 1, "footer")  # L3

    for index, block in enumerate(blocks):
        blocks[index] = block.model_copy(
            update={"order": index, "bbox": (0.0, float(index), 0.0, float(index) + 1.0)}
        )
    return blocks, 1
