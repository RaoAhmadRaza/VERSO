"""Span validation (PLAN.md §2, the span contract).

The model never returns content -- it returns claims about spans. Every verbatim string it
emits must be findable in the lines it cited. This module checks that claim in Python,
because a guarantee produced by an LLM is not a guarantee.

`verified` and `flag` are written only here. Whatever the model put in those fields is
discarded before checking, so a model that helpfully sets `flag: "ok"` gains nothing.
"""

import re

from rapidfuzz import fuzz

from app.extract.normalize import match_key
from app.models import Line

MATCH_THRESHOLD = 95  # PLAN.md §2
# Exact containment is strong evidence, so it is allowed at short lengths -- but only on
# word boundaries, or "IT" matches inside "digital" and every two-letter span verifies
# against anything. A floor still applies: one or two characters carry no information.
MIN_EXACT_LEN = 4
# Fuzzy containment is weaker, so it stays gated at a length where a 95% partial match
# actually means something.
MIN_PARTIAL_LEN = 12


def _contains_word(haystack: str, needle: str) -> bool:
    """Exact containment on word boundaries.

    A plain `in` test lets a short span match inside a longer word -- "it" inside "digital".
    Requiring boundaries keeps genuinely contained fields ("Miami, FL" inside a combined
    contact line) while refusing accidental substrings.
    """
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def is_span(node: object) -> bool:
    return isinstance(node, dict) and {"text", "source_lines", "flag"} <= set(node)


def walk(node, transform):
    """Rebuild a dumped model tree, applying `transform` to every span. Never mutates."""
    if is_span(node):
        return transform(node)
    if isinstance(node, dict):
        return {key: walk(value, transform) for key, value in node.items()}
    if isinstance(node, list):
        return [walk(item, transform) for item in node]
    return node


def _source_text(source_lines: list[int], index: dict[int, Line]) -> list[str]:
    """The claimed lines, plus the pre-join variant when E3 or L6 merged them.

    A span written against the document as the reader sees it will not match a line the
    extractor joined, and vice versa, so both forms are offered.
    """
    joined = " ".join(index[n].text for n in source_lines)
    variants = [joined]
    if any(index[n].unjoined for n in source_lines):
        variants.append(
            " ".join(
                " ".join(index[n].unjoined) if index[n].unjoined else index[n].text
                for n in source_lines
            )
        )
    return variants


def score_span(text: str, source_lines: list[int], index: dict[int, Line]) -> float:
    """Best fuzzy score of `text` against its claimed source lines."""
    if not source_lines or any(n not in index for n in source_lines):
        return 0.0
    claim = match_key(text)
    if not claim:
        return 0.0
    # PLAN.md §2 specifies `rapidfuzz.ratio >= 95`. Measured against the real corpus that
    # is too strict: every resume packs contact details onto one line
    # ("email | phone | city | linkedin"), so the email span is a proper substring of the
    # line it correctly cites and whole-string `ratio` scored it as hallucinated on 20 of
    # 20 files. Containment is the common, legitimate case -- a field extracted from a line
    # that holds several fields, or a bullet quoted from a line that also carries a date.
    #
    # Exact substring containment scores 100 with no fuzz involved, so it cannot launder a
    # fabrication. `partial_ratio` then covers near-containment (a stray character, a
    # normalisation difference), gated on length so a two-character span cannot match
    # everything. Genuine invention still fails all three: it appears nowhere in the line.
    best = 0.0
    for variant in _source_text(source_lines, index):
        key = match_key(variant)
        best = max(best, fuzz.ratio(claim, key))
        if len(claim) >= MIN_EXACT_LEN and _contains_word(key, claim):
            return 100.0
        if len(claim) >= MIN_PARTIAL_LEN:
            best = max(best, fuzz.partial_ratio(claim, key))
    return best


def verify(parse: dict, lines: list[Line]) -> tuple[dict, list[tuple[int, str]]]:
    """Return (parse with every span judged, flagged line numbers)."""
    index = {line.no: line for line in lines}
    flagged: list[tuple[int, str]] = []

    def judge(span: dict) -> dict:
        source_lines = span.get("source_lines") or []
        out_of_range = [n for n in source_lines if n not in index]
        score = score_span(span.get("text", ""), source_lines, index)
        ok = score >= MATCH_THRESHOLD

        if not ok:
            reason = (
                "source_line_out_of_range" if out_of_range else "hallucinated_span"
            )
            for n in source_lines or [0]:
                flagged.append((n, reason))

        return {**span, "verified": ok, "flag": "ok" if ok else "hallucinated"}

    return walk(parse, judge), flagged


# Fields the model returns as plain strings rather than spans. PLAN.md §5 types them `str`,
# so they carry no `source_lines` of their own and the span contract never sees them -- an
# employer name can be silently rewritten and still score 100% coverage. They are checked
# here against the lines their parent object cites.
BARE_FIELDS = ("company", "title", "institution", "degree", "name")


def verified_bare_lines(node, index: dict[int, Line]) -> set[int]:
    """Line numbers that a bare string field verifiably reproduces.

    Attribution is per line, deliberately. `verify_bare_fields` below scores a field against
    its object's whole `source_lines` block, which is the right question for "is this field
    sourced at all" -- but wrong for coverage accounting. A company name checked against a
    Role's ten-line block would mark all ten lines represented, bullet lines included, and
    would hide exactly the span loss `represented_pct` exists to catch. Scoring each line
    separately attributes the company line to the company and nothing else.
    """
    found: set[int] = set()
    if isinstance(node, dict):
        source_lines = node.get("source_lines")
        if isinstance(source_lines, list):
            for field in BARE_FIELDS:
                value = node.get(field)
                if not isinstance(value, str) or len(value.strip()) < MIN_EXACT_LEN:
                    continue
                found |= {
                    n
                    for n in source_lines
                    if n in index and score_span(value, [n], index) >= MATCH_THRESHOLD
                }
        for child in node.values():
            found |= verified_bare_lines(child, index)
    elif isinstance(node, list):
        for item in node:
            found |= verified_bare_lines(item, index)
    return found


def verify_bare_fields(node, index: dict[int, Line], path: str = "") -> list[tuple[int, str]]:
    """Flag plain-string fields whose text is absent from the lines their object cites."""
    flagged: list[tuple[int, str]] = []
    if isinstance(node, dict):
        source_lines = node.get("source_lines")
        if isinstance(source_lines, list) and source_lines:
            for field in BARE_FIELDS:
                value = node.get(field)
                if not isinstance(value, str) or len(value.strip()) < MIN_EXACT_LEN:
                    continue
                if score_span(value, source_lines, index) < MATCH_THRESHOLD:
                    flagged.append((source_lines[0], f"unsourced_{field}"))
        for key, child in node.items():
            flagged += verify_bare_fields(child, index, f"{path}.{key}")
    elif isinstance(node, list):
        for item in node:
            flagged += verify_bare_fields(item, index, path)
    return flagged
