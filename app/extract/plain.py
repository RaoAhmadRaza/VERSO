"""Plain text and Markdown extraction (.txt, .md).

No geometry, so every block gets a zero bbox and reading order is document order.
"""

from app.models import Block


def extract(data: bytes) -> tuple[list[Block], int]:
    text = data.decode("utf-8", errors="replace")
    blocks = [
        Block(page=1, bbox=(0.0, float(i), 0.0, float(i) + 1.0), text=line, order=i)
        for i, line in enumerate(text.splitlines())
    ]
    return blocks, 1
