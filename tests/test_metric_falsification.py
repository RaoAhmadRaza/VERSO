"""Falsification of the coverage metric (PLAN.md §2, CLAUDE.md §7).

`coverage_pct` returned 100.00% on every resume in the corpus from the first run onwards.
An alarm that has never rung is not a proven alarm, so this module breaks known-good
parses on purpose and asserts the number actually moves.

It also pins down what the metric provably *cannot* see. Measured on the corpus: deleting
every span from every parse leaves `coverage_pct` at exactly 100.00% on 22 of 22 files,
because container `source_lines` already span their children's. That is the finding that
motivates the second metric, and the last two tests here are its permanent record.

Offline. No model is called. Cached parses under `data/` are read and deep-copied before
mutation -- nothing on disk is written.
"""

import copy
import json
from pathlib import Path

import pytest

from app.models import DroppedLine, Line
from app.verify import coverage, spans

DATA = Path(__file__).parent.parent / "data"
PARSES = DATA / "parses"
LINES = DATA / "lines"

# Span lists hang off their container under these keys; `evidence` is a lone span.
SPAN_LISTS = ("bullets", "details", "lines")


def _load(parse_path: Path) -> tuple[dict, list[Line], list[DroppedLine]]:
    parse = json.loads(parse_path.read_text(encoding="utf-8"))
    checkpoint = json.loads((LINES / parse_path.name).read_text(encoding="utf-8"))
    lines = [Line.model_validate(item) for item in checkpoint["lines"]]
    dropped = [DroppedLine.model_validate(d) for d in checkpoint["dropped_lines"]]
    return parse, lines, dropped


def _usable(parse: dict, lines: list[Line], dropped: list[DroppedLine]) -> bool:
    """A parse these mutations can say something about.

    Needs two roles (so deleting one leaves a parse behind), bullets (the span-loss case),
    a non-empty `other_sections`, and a recomputed baseline of 100% -- without that last
    condition "coverage must drop" has no fixed point to drop from.
    """
    roles = parse.get("experience") or []
    return (
        len(roles) >= 2
        and any(role.get("bullets") for role in roles)
        and any(section.get("source_lines") for section in parse.get("other_sections") or [])
        and coverage.compute(parse, lines, dropped).coverage_pct == 100.0
    )


@pytest.fixture(scope="module")
def subject() -> tuple[dict, list[Line], list[DroppedLine]]:
    """The first cached parse with enough structure to mutate. Never mutated itself."""
    for parse_path in sorted(PARSES.glob("*.json")) if PARSES.exists() else []:
        if not (LINES / parse_path.name).exists():
            continue
        loaded = _load(parse_path)
        if _usable(*loaded):
            return loaded
    pytest.skip("no cached parse in data/parses/ has the structure these mutations need")


def _judge(parse: dict, lines: list[Line]) -> tuple[dict, list[tuple[int, str]]]:
    """The real verify stage: span flags plus the bare-string-field flags."""
    verified, flagged = spans.verify(parse, lines)
    flagged += spans.verify_bare_fields(verified, {line.no: line for line in lines})
    return verified, flagged


def _count_spans(node) -> int:
    if spans.is_span(node):
        return 1
    if isinstance(node, dict):
        return sum(_count_spans(value) for value in node.values())
    if isinstance(node, list):
        return sum(_count_spans(item) for item in node)
    return 0


def _strip_every_span(node):
    """Empty every span list and every `evidence`, leaving containers and their citations."""
    if isinstance(node, dict):
        stripped = {}
        for key, value in node.items():
            if key in SPAN_LISTS and isinstance(value, list):
                stripped[key] = []
            elif key == "evidence":
                stripped[key] = None
            else:
                stripped[key] = _strip_every_span(value)
        return stripped
    if isinstance(node, list):
        return [_strip_every_span(item) for item in node]
    return node


# --------------------------------------------------------------- alarms that do ring


def test_deleting_a_whole_role_drops_coverage_and_grows_unassigned(subject):
    parse, lines, dropped = subject
    before = coverage.compute(parse, lines, dropped)

    mutated = copy.deepcopy(parse)
    mutated["experience"].pop(0)
    after = coverage.compute(mutated, lines, dropped)

    assert after.coverage_pct < before.coverage_pct
    assert len(after.unassigned) > len(before.unassigned)


def test_emptying_other_sections_drops_coverage(subject):
    """The catch-all is where unclassifiable lines land, so losing it must be visible."""
    parse, lines, dropped = subject
    before = coverage.compute(parse, lines, dropped)

    mutated = copy.deepcopy(parse)
    mutated["other_sections"] = []
    after = coverage.compute(mutated, lines, dropped)

    assert after.coverage_pct < before.coverage_pct
    assert len(after.unassigned) > len(before.unassigned)


def test_invented_span_text_is_flagged(subject):
    """Fabricated content is caught by span matching, not by coverage."""
    parse, lines, dropped = subject
    mutated = copy.deepcopy(parse)
    mutated["experience"][0]["bullets"][0]["text"] = "Piloted a submarine across the Nevada desert"

    verified, flagged = _judge(mutated, lines)

    assert flagged
    assert any(reason == "hallucinated_span" for _, reason in flagged)
    # Coverage is blind to it: the line is still cited, only its content is invented.
    assert coverage.compute(verified, lines, dropped, flagged).coverage_pct == 100.0


def test_span_pointing_out_of_range_is_flagged(subject):
    parse, lines, dropped = subject
    mutated = copy.deepcopy(parse)
    mutated["experience"][0]["bullets"][0]["source_lines"] = [99999]

    verified, flagged = _judge(mutated, lines)

    assert flagged
    assert any(reason == "source_line_out_of_range" for _, reason in flagged)


# ------------------------------------------------------- the limitation, asserted
#
# Rejected alternative, so it is not relitigated: making `coverage_pct` sensitive to span
# loss by counting a line as claimed only by the innermost object citing it (leaf-only
# attribution). It does not work. A Role legitimately cites lines no leaf span covers --
# the company line, the location line, the date line -- and leaf-only attribution would
# report every one of them as unassigned. That trades a metric that under-reports loss for
# one that invents it. Two metrics with different jobs is the right shape: `coverage_pct`
# checks that citations are well formed, `represented_pct` checks that content survived.


def test_coverage_pct_cannot_detect_span_loss(subject):
    """Documents the limitation that motivates represented_pct.

    Container source_lines already span their children's, so bullets contribute
    almost no unique line numbers (measured: 2 of 40 on yuki_tanaka). Deleting
    every span therefore leaves coverage_pct at 100. This is not a bug in
    coverage.compute -- it is what makes a second metric necessary.

    If this test ever fails, someone changed coverage_pct's semantics. Read the
    two-metric design in PLAN.md before treating that as an improvement.
    """
    parse, lines, dropped = subject
    mutated = copy.deepcopy(parse)
    deleted = 0
    for role in mutated["experience"]:
        deleted += len(role.get("bullets") or [])
        role["bullets"] = []

    assert deleted > 0, "fixture selection should have guaranteed bullets to delete"
    before = coverage.compute(parse, lines, dropped)
    after = coverage.compute(mutated, lines, dropped)
    assert after.coverage_pct == 100.0
    assert after.unassigned == []

    # The second metric is the one that notices.
    assert after.represented_pct < before.represented_pct
    assert len(after.claimed_not_represented) > len(before.claimed_not_represented)


def test_coverage_pct_survives_deleting_every_span(subject):
    """The limitation at full strength: no span content at all, still 100%.

    Measured across the corpus at 22 of 22 files. Coverage is a citation-wellformedness
    check; it says nothing about whether any text was actually captured.
    """
    parse, lines, dropped = subject
    mutated = _strip_every_span(copy.deepcopy(parse))

    # Guard against a vacuous assertion: the mutation must really have removed content.
    assert _count_spans(mutated) < _count_spans(parse)
    assert all(not role["bullets"] for role in mutated["experience"])
    assert all(not section["lines"] for section in mutated["other_sections"])

    before = coverage.compute(parse, lines, dropped)
    after = coverage.compute(mutated, lines, dropped)
    assert after.coverage_pct == 100.0
    assert after.unassigned == []

    # represented_pct exists precisely so this case is not invisible.
    assert after.represented_pct < before.represented_pct
    assert len(after.claimed_not_represented) > len(before.claimed_not_represented)
