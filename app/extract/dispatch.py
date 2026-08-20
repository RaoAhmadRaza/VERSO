"""Format dispatch on magic bytes, never on the extension (PLAN.md §6.3, F2).

An extension is a claim by whoever named the file. The first few bytes are what the file
actually is, and resumes arrive misnamed often enough to matter.
"""

import hashlib
import io
import zipfile
from pathlib import Path

from app.extract import docx, layout, normalize, pdf, plain
from app.extract.errors import (
    EmptyDocumentError,
    FileTooLargeError,
    UnsupportedFormatError,
)
from app.models import Block, DroppedLine, Line, ParseWarning

MAX_BYTES = 10 * 1024 * 1024  # F7
MIN_TOTAL_CHARS = 50  # F8

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"
OLE2_MAGIC = b"\xd0\xcf\x11\xe0"  # F3, legacy .doc
RTF_MAGIC = b"{\\rtf"  # F4


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff(data: bytes) -> str:
    """Return one of pdf | docx | txt, or raise for a format we deliberately refuse."""
    if data.startswith(PDF_MAGIC):
        return "pdf"

    if data.startswith(OLE2_MAGIC):  # F3
        raise UnsupportedFormatError(
            "This is a legacy .doc file. Open it in Word and save as .docx or PDF."
        )
    if data.startswith(RTF_MAGIC):  # F4
        raise UnsupportedFormatError(
            "RTF isn't supported. Export this resume as PDF or .docx."
        )

    if data.startswith(ZIP_MAGIC):
        return _sniff_zip(data)

    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedFormatError(
            "This file isn't a PDF, .docx, or plain text. Export the resume as PDF."
        ) from exc
    return "txt"


def _sniff_zip(data: bytes) -> str:
    """Several formats are ZIP containers. Look inside before deciding (F2, F4)."""
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormatError("This file looks like a damaged archive.") from exc

    if "word/document.xml" in names:
        return "docx"
    if "content.xml" in names or "mimetype" in names:  # F4
        raise UnsupportedFormatError(
            "OpenDocument (.odt) isn't supported. Export this resume as PDF or .docx."
        )
    if any(n.startswith("Index/") or n.endswith(".iwa") for n in names):  # F4
        raise UnsupportedFormatError(
            "Apple Pages files aren't supported. Export this resume as PDF or .docx."
        )
    raise UnsupportedFormatError(
        "This archive isn't a .docx file. Export the resume as PDF or .docx."
    )


def extract(data: bytes, filename: str = "") -> tuple[list[Block], str, int, list[ParseWarning]]:
    """Bytes -> (blocks, file_type, page_count, warnings).

    Every rejection path raises an `ExtractionError` carrying a sentence for the user.
    """
    if len(data) > MAX_BYTES:  # F7
        raise FileTooLargeError(
            f"This file is {len(data) / 1024 / 1024:.1f}MB. Resumes are under 10MB."
        )
    if not data:
        raise EmptyDocumentError("This file is empty.")

    kind = sniff(data)
    warnings: list[ParseWarning] = []

    if kind == "pdf":
        blocks, pages, warnings = pdf.extract(data)
    elif kind == "docx":
        blocks, pages = docx.extract(data)
    else:
        blocks, pages = plain.extract(data)
        kind = "md" if Path(filename).suffix.lower() == ".md" else "txt"

    if kind != "pdf":  # the PDF path runs its own richer guards in _guard_text_layer
        total = sum(len(b.text.strip()) for b in blocks)
        if total < MIN_TOTAL_CHARS:  # F8
            raise EmptyDocumentError(
                f"This document holds only {total} characters of text. It isn't a resume."
            )
    return blocks, kind, pages, warnings


def extract_lines(
    data: bytes, filename: str = ""
) -> tuple[list[Line], str, int, list[ParseWarning], list[DroppedLine]]:
    """The full deterministic half of the pipeline: bytes -> numbered lines.

    No model is involved here and none ever should be (CLAUDE.md hard rule #1). This is the
    stage whose output lands in `lines.json`, which is what makes a bug attributable to
    extraction rather than to structuring.
    """
    blocks, kind, pages, warnings = extract(data, filename)
    ordered = layout.order_blocks(blocks)
    lines, dropped = normalize.to_lines(ordered, pages)
    # `_drop_repeated_furniture` cannot know final numbering, so stamp the audit trail now.
    for index, entry in enumerate(dropped):
        dropped[index] = entry.model_copy(update={"no": -(index + 1)})
    return lines, kind, pages, warnings, dropped
