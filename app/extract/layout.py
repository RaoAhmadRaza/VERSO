"""Column detection and reading order (PLAN.md §6.1, L1--L3).

A PDF has no concept of a column. Parsers sweep left-to-right across the full page width
and interleave a sidebar into the work history -- PLAN.md calls fixing this "the single
biggest win in the whole project".

Detection is 1-D k-means over block `x0`. Clustering in one dimension over a few dozen
points does not justify a numerical dependency, so it is written out here.
"""

from app.models import Block

MIN_GUTTER = 40.0  # L1 -- clear horizontal gap between columns, in points
MIN_HEIGHT_SPAN = 0.50  # L1 -- each column must cover half the page vertically
NARROW_COLUMN = 0.30  # L2 -- a cluster under 30% of page width is a sidebar
ROW_TOLERANCE = 2.0  # points; groups blocks that sit on the same visual line
MIN_BLOCKS_FOR_COLUMNS = 4


def _kmeans_1d(values: list[float], k: int, iterations: int = 25) -> list[int] | None:
    """Assign each value to one of `k` clusters. Deterministic min/max seeding."""
    low, high = min(values), max(values)
    if low == high or len(values) < k:
        return None
    centroids = [low + (high - low) * i / (k - 1) for i in range(k)]

    labels = [0] * len(values)
    for _ in range(iterations):
        labels = [
            min(range(k), key=lambda c: abs(value - centroids[c])) for value in values
        ]
        moved = False
        for c in range(k):
            members = [v for v, label in zip(values, labels) if label == c]
            if members:
                centre = sum(members) / len(members)
                if centre != centroids[c]:
                    centroids[c] = centre
                    moved = True
        if not moved:
            break

    if len({*labels}) < k:
        return None
    # Relabel left-to-right so column 0 is always the leftmost.
    order = sorted(range(k), key=lambda c: centroids[c])
    remap = {old: new for new, old in enumerate(order)}
    return [remap[label] for label in labels]


def _valid_columns(page_blocks: list[Block], labels: list[int], k: int) -> bool:
    """L1 -- a real column split needs a clear gutter and full-height columns."""
    height = page_blocks[0].page_height or 1.0
    groups = [
        [b for b, label in zip(page_blocks, labels) if label == c] for c in range(k)
    ]
    if any(not g for g in groups):
        return False

    for left, right in zip(groups, groups[1:]):
        if min(b.bbox[0] for b in right) - max(b.bbox[2] for b in left) < MIN_GUTTER:
            return False

    for group in groups:
        span = max(b.bbox[3] for b in group) - min(b.bbox[1] for b in group)
        if span / height < MIN_HEIGHT_SPAN:
            return False
    return True


def _detect_columns(page_blocks: list[Block]) -> list[int] | None:
    """Try three columns, then two. Single column is the conservative default."""
    if len(page_blocks) < MIN_BLOCKS_FOR_COLUMNS or not page_blocks[0].page_height:
        return None
    x0s = [b.bbox[0] for b in page_blocks]
    for k in (3, 2):  # L2 before L1: a 3-column page also splits validly into 2
        labels = _kmeans_1d(x0s, k)
        if labels and _valid_columns(page_blocks, labels, k):
            return labels
    return None


def _column_sort_key(page_blocks: list[Block], labels: list[int]) -> dict[int, int]:
    """L2 -- a narrow sidebar is emitted after the wide columns."""
    width = page_blocks[0].page_width or 1.0
    keys: dict[int, int] = {}
    for column in {*labels}:
        group = [b for b, label in zip(page_blocks, labels) if label == column]
        span = max(b.bbox[2] for b in group) - min(b.bbox[0] for b in group)
        keys[column] = (1 if span / width < NARROW_COLUMN else 0, column)
    return {c: rank for rank, c in enumerate(sorted(keys, key=lambda c: keys[c]))}


def order_blocks(blocks: list[Block]) -> list[Block]:
    """Assign `column` and return blocks in human reading order.

    Formats without geometry (DOCX, plain text) keep document order untouched.
    """
    if not blocks:
        return []
    if not any(b.page_height for b in blocks):
        return sorted(blocks, key=lambda b: (b.page, b.order))

    ordered: list[Block] = []
    for page in sorted({b.page for b in blocks}):
        page_blocks = [b for b in blocks if b.page == page]
        labels = _detect_columns(page_blocks)

        if labels is None:
            for block in page_blocks:
                ordered.append(block.model_copy(update={"column": None}))
            continue

        rank = _column_sort_key(page_blocks, labels)
        for block, label in zip(page_blocks, labels):
            ordered.append(block.model_copy(update={"column": rank[label]}))

    def key(block: Block) -> tuple:
        # Within a column, top-to-bottom then left-to-right. The row bucket keeps blocks
        # that sit on the same visual line together, which is what row-major means for a
        # grid-arranged table (L4).
        return (
            block.page,
            block.column if block.column is not None else 0,
            round(block.bbox[1] / ROW_TOLERANCE),
            block.bbox[0],
        )

    return sorted(ordered, key=key)
