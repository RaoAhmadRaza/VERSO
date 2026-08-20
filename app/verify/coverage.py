"""Coverage accounting (PLAN.md §2).

Union every line number the model claimed, then diff against the non-blank lines the
extractor produced. Anything left over is `unassigned` and is surfaced in the UI rather
than quietly discarded -- this is the number the whole project is graded on.
"""

from app.models import Coverage, DroppedLine, Line
from app.verify.spans import is_span, verified_bare_lines


def claimed_lines(node) -> set[int]:
    """Every `source_lines` value anywhere in the parse tree, at any depth."""
    found: set[int] = set()
    if isinstance(node, dict):
        value = node.get("source_lines")
        if isinstance(value, list):
            found |= {n for n in value if isinstance(n, int)}
        for item in node.values():
            found |= claimed_lines(item)
    elif isinstance(node, list):
        for item in node:
            found |= claimed_lines(item)
    return found


def represented_lines(node) -> set[int]:
    """Line numbers cited by a span that survived verification (`flag == "ok"`).

    Spans only. `compute` counts bare fields too -- `company`, `title` and the rest carry
    real content and are verified by `spans.verified_bare_lines`, so excluding them measured
    schema shape rather than content survival. This is kept as the secondary diagnostic:
    the difference between it and `represented_pct` is how much of a resume lands in plain
    string fields instead of spans.
    """
    found: set[int] = set()
    if isinstance(node, dict):
        if is_span(node) and node.get("flag") == "ok":
            found |= {n for n in (node.get("source_lines") or []) if isinstance(n, int)}
        for item in node.values():
            found |= represented_lines(item)
    elif isinstance(node, list):
        for item in node:
            found |= represented_lines(item)
    return found


def compute(
    parse: dict,
    lines: list[Line],
    dropped: list[DroppedLine] | None = None,
    flagged: list[tuple[int, str]] | None = None,
) -> Coverage:
    """Coverage over non-blank lines.

    Deliberately dropped lines (repeated page furniture, L3/L7) are excluded from the
    denominator: they were removed on purpose and are auditable in `meta.dropped_lines`,
    so counting them as misses would penalise correct behaviour.
    """
    dropped_numbers = {d.no for d in (dropped or [])}
    non_blank = {
        line.no for line in lines if line.text.strip() and line.no not in dropped_numbers
    }
    claimed = claimed_lines(parse)
    covered = non_blank & claimed
    # A line is represented if a verified span OR a verified bare field reproduces it.
    index = {line.no: line for line in lines}
    represented = non_blank & (represented_lines(parse) | verified_bare_lines(parse, index))

    return Coverage(
        total_lines=len(lines),
        non_blank_lines=len(non_blank),
        claimed_lines=len(covered),
        coverage_pct=round(100.0 * len(covered) / len(non_blank), 2) if non_blank else 100.0,
        unassigned=sorted(non_blank - claimed),
        flagged=sorted(set(flagged or [])),
        represented_lines=len(represented),
        represented_pct=(
            round(100.0 * len(represented) / len(non_blank), 2) if non_blank else 100.0
        ),
        claimed_not_represented=sorted(covered - represented),
    )
