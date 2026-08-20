"""Minimal DOCX writer for fixtures -- stdlib zipfile only, no python-docx.

We build the XML by hand for the same reason the parser reads it by hand (PLAN.md §4):
python-docx silently skips text boxes and headers, which is exactly the content these
fixtures exist to prove we do not lose.
"""

import zipfile

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'mc:Ignorable="w14 wp14"'
)

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

# A fixed timestamp keeps regenerated fixtures byte-identical.
_ZIP_DATE = (2024, 1, 1, 0, 0, 0)


def para(*runs: str, style: str = "") -> str:
    """A plain `w:p` with one `w:t` per run."""
    body = "".join(f"<w:r><w:t xml:space=\"preserve\">{r}</w:t></w:r>" for r in runs)
    return f"<w:p>{style}{body}</w:p>"


def bullet(text: str) -> str:
    """A numbered/bulleted paragraph -- `w:numPr` is the is_bullet signal."""
    ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
    return f'{ppr and ""}<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def textbox(inner_text: str) -> str:
    """L5 -- a floating text box, wrapped the way Word actually emits one.

    The same content appears twice: once under `mc:Choice` (modern DrawingML) and once
    under `mc:Fallback` (legacy VML). A walker that does not prefer Choice emits it twice.
    """
    inner = f'<w:txbxContent><w:p><w:r><w:t xml:space="preserve">{inner_text}</w:t></w:r></w:p></w:txbxContent>'
    choice = (
        '<mc:Choice Requires="wps"><w:r><w:drawing><wp:inline><a:graphic><a:graphicData>'
        f"<wps:wsp><wps:txbx>{inner}</wps:txbx></wps:wsp>"
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></mc:Choice>"
    )
    fallback = (
        f"<mc:Fallback><w:r><w:pict><v:shape><v:textbox>{inner}"
        "</v:textbox></v:shape></w:pict></w:r></mc:Fallback>"
    )
    return f"<w:p><mc:AlternateContent>{choice}{fallback}</mc:AlternateContent></w:p>"


def tracked(inserted: str, deleted: str, normal: str) -> str:
    """F10 -- keep `w:ins`, drop `w:del`, ignore comment anchors."""
    return (
        "<w:p>"
        f'<w:ins w:id="1" w:author="R"><w:r><w:t xml:space="preserve">{inserted}</w:t></w:r></w:ins>'
        f'<w:del w:id="2" w:author="R"><w:r><w:delText xml:space="preserve">{deleted}</w:delText></w:r></w:del>'
        f'<w:r><w:t xml:space="preserve">{normal}</w:t></w:r>'
        '<w:commentRangeStart w:id="3"/><w:commentRangeEnd w:id="3"/>'
        '<w:r><w:commentReference w:id="3"/></w:r>'
        "</w:p>"
    )


def table(rows: list[list[str]]) -> str:
    """L4 -- a layout table. Cells are flattened row-major on extraction."""
    out = ["<w:tbl>"]
    for row in rows:
        out.append("<w:tr>")
        for cell in row:
            out.append(f"<w:tc>{para(cell)}</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def _part(body: str, root: str = "w:document", wrap: str | None = "w:body") -> str:
    """Serialise one OOXML part. `wrap` is None for parts that are their own container."""
    inner = f"<{wrap}>{body}</{wrap}>" if wrap else body
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<{root} {NS}>{inner}</{root}>"
    )


def write_docx(
    path, body: str, header: str = "", footer: str = "", comments: str = ""
) -> None:
    """Write a minimal but structurally real .docx."""
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": RELS,
        "word/document.xml": _part(body),
        "word/header1.xml": _part(header or para(""), "w:hdr", None),
        "word/footer1.xml": _part(footer or para(""), "w:ftr", None),
    }
    if comments:
        parts["word/comments.xml"] = _part(comments, "w:comments", None)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
