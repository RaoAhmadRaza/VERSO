"""OCR fallback for scanned PDFs (PLAN.md §9, Phase 7).

Only ever reached from the F1 (no text layer) and E6 (broken CMap) paths. Off by default:
set `VERSO_OCR=1`, and have the `tesseract` binary installed.

Word-level boxes are kept rather than raw text, so OCR output still carries geometry and
stays compatible with the column detection in `layout.py`. Every line is tagged
`confidence="ocr"` so the UI can say where the text came from.
"""

import os
from collections import defaultdict

import pymupdf

from app.models import Block

OCR_DPI = 300  # PLAN.md §6.3, F1
SCALE = 72.0 / OCR_DPI  # device pixels -> PDF points
MIN_WORD_CONFIDENCE = 30  # tesseract reports 0-100; below this it is guessing


def enabled() -> bool:
    return os.getenv("VERSO_OCR", "").strip().lower() in ("1", "true", "yes")


def available() -> bool:
    """True when both pytesseract and the tesseract binary are usable."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def extract(doc: pymupdf.Document) -> list[Block]:
    """Render each page and recover text with per-word geometry."""
    import pytesseract
    from PIL import Image

    blocks: list[Block] = []
    for page_no, page in enumerate(doc, start=1):
        pixmap = page.get_pixmap(dpi=OCR_DPI)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        data = pytesseract.image_to_data(
            image, output_type=pytesseract.Output.DICT
        )

        grouped: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for index, text in enumerate(data["text"]):
            if not text.strip():
                continue
            try:
                confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                continue
            if confidence < MIN_WORD_CONFIDENCE:
                continue
            key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
            grouped[key].append(index)

        for order, key in enumerate(sorted(grouped)):
            indices = grouped[key]
            words = [data["text"][i] for i in indices]
            x0 = min(data["left"][i] for i in indices) * SCALE
            y0 = min(data["top"][i] for i in indices) * SCALE
            x1 = max(data["left"][i] + data["width"][i] for i in indices) * SCALE
            y1 = max(data["top"][i] + data["height"][i] for i in indices) * SCALE
            blocks.append(
                Block(
                    page=page_no,
                    bbox=(x0, y0, x1, y1),
                    text=" ".join(words),
                    order=order,
                    confidence="ocr",
                    page_width=page.rect.width,
                    page_height=page.rect.height,
                )
            )
    return blocks
