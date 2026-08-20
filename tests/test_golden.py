"""The ship gate (PLAN.md §9 Phase 5, §10).

Runs over whatever real resumes are in `fixtures/resumes/`. Every assertion here is an
invariant that must hold for *any* resume, so the gate does not care what you upload or
what the files are called.

Two things make this module skip:

* `fixtures/resumes/` is empty -- there is nothing to measure yet.
* The active provider's API key is unset -- the structuring stage cannot run.

The deterministic half of the pipeline is covered without either, by test_normalize /
test_layout / test_dispatch / test_coverage, which run against `fixtures/synthetic/`.

Compare providers on the same corpus:

    VERSO_PROVIDER=deepseek  uv run pytest tests/test_golden.py -q -s
    VERSO_PROVIDER=anthropic uv run pytest tests/test_golden.py -q -s
"""

import os
import statistics
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app import pipeline
from app.extract.errors import ExtractionError
from app.models import ParsedResume
from app.structure import client as structure_client

load_dotenv()

RESUMES = Path(__file__).parent.parent / "fixtures" / "resumes"
MEAN_COVERAGE_GATE = 95.0  # PLAN.md §9 Phase 5 -- the real ship gate
SINGLE_FIXTURE_FLOOR = 80.0

KEY_FOR_PROVIDER = {
    "deepseek": "DEEPSEEK_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
REQUIRED_KEY = KEY_FOR_PROVIDER.get(structure_client.PROVIDER, "DEEPSEEK_API_KEY")


def _corpus() -> list[Path]:
    if not RESUMES.exists():
        return []
    return [p for p in sorted(RESUMES.iterdir()) if p.is_file() and not p.name.startswith(".")]


pytestmark = [
    pytest.mark.skipif(
        not _corpus(),
        reason="fixtures/resumes/ is empty -- add real resumes to measure coverage",
    ),
    pytest.mark.skipif(
        not os.getenv(REQUIRED_KEY),
        reason=f"{REQUIRED_KEY} is not set; the structuring stage cannot run",
    ),
]


@pytest.fixture(scope="module")
def parses() -> dict[str, ParsedResume]:
    """Parse every resume once, concurrently. Results persist under data/."""
    results, failures = pipeline.parse_many(_corpus())
    if failures:
        print("\n  not parsed:")
        for name, exc in sorted(failures.items()):
            print(f"    {name:<34} [{getattr(exc, 'code', 'error')}] {exc}")
    if not results:
        pytest.skip("no resume in fixtures/resumes/ could be parsed")
    return results


def test_every_resume_in_the_corpus_parsed(parses):
    """A file that never parsed cannot contribute to coverage, so name it explicitly."""
    missing = {p.name for p in _corpus()} - set(parses)
    assert not missing, f"failed to parse: {sorted(missing)}"


# ------------------------------------------------------------------- the ship gate


def test_every_resume_produces_a_schema_valid_parse(parses):
    for name, parse in parses.items():
        ParsedResume.model_validate(parse.model_dump())  # round trip
        assert parse.meta.sha256 and parse.meta.total_lines > 0, name


def test_mean_coverage_meets_the_ship_gate(parses):
    scores = {name: parse.coverage.coverage_pct for name, parse in parses.items()}
    mean = statistics.mean(scores.values())
    worst = sorted(scores.items(), key=lambda kv: kv[1])[:5]
    assert mean >= MEAN_COVERAGE_GATE, (
        f"mean coverage {mean:.2f}% < {MEAN_COVERAGE_GATE}%. Worst: {worst}"
    )


def test_no_single_resume_collapses(parses):
    poor = {
        name: score
        for name, score in ((n, p.coverage.coverage_pct) for n, p in parses.items())
        if score < SINGLE_FIXTURE_FLOOR
    }
    assert not poor, f"resumes below {SINGLE_FIXTURE_FLOOR}%: {poor}"


def test_zero_unflagged_hallucinated_spans(parses):
    """Every span is either verified against its lines or explicitly flagged."""
    from app.verify.spans import walk

    offenders: list[tuple[str, str]] = []
    for name, parse in parses.items():

        def check(span: dict, _name: str = name) -> dict:
            unjudged = span["flag"] == "unverified"
            inconsistent = span["verified"] != (span["flag"] == "ok")
            if unjudged or inconsistent:
                offenders.append((_name, span["text"][:60]))
            return span

        walk(parse.model_dump(), check)
    assert not offenders, f"spans that escaped judgement: {offenders[:5]}"


def test_every_claimed_line_actually_exists(parses):
    from app.verify.coverage import claimed_lines

    for name, parse in parses.items():
        claimed = claimed_lines(parse.model_dump())
        assert claimed <= set(range(1, parse.meta.total_lines + 1)), (
            f"{name} cites lines outside the document"
        )


# ------------------------------------------------- invariants true of any real resume


def test_every_skill_category_comes_from_the_closed_enum(parses):
    """Hard rule #7 -- the model never invents a category."""
    from app.structure.taxonomy import SkillCategory

    valid = set(SkillCategory)
    for name, parse in parses.items():
        for skill in parse.skills:
            assert skill.category in valid, f"{name}: invented category {skill.category}"


def test_C3_C5_date_raw_is_always_retained(parses):
    """Hard rule #6 -- DateField.raw is never overwritten."""
    for name, parse in parses.items():
        for role in parse.experience:
            for date in (role.start, role.end):
                assert date.raw.strip() or date.is_present, f"{name}: empty DateField.raw"


def test_C6_promotion_chains_are_nested_not_flattened(parses):
    """One company appearing as several separate roles is the C6 failure."""
    for name, parse in parses.items():
        companies = [r.company.strip().lower() for r in parse.experience if r.company.strip()]
        duplicates = {c for c in companies if companies.count(c) > 1}
        assert not duplicates, (
            f"{name}: {duplicates} appear as separate roles; expected sub_roles nesting (C6)"
        )


def test_C6_subroles_never_hold_a_different_company(parses):
    """Nesting means "promoted at the same employer". Anything else is a structural error
    that coverage cannot see, since every line is still claimed."""
    offenders: list[str] = []

    def check(role, parent: str | None, name: str) -> None:
        if parent and role.company.strip().casefold() != parent.strip().casefold():
            offenders.append(f"{name}: {role.company!r} nested under {parent!r}")
        for sub in role.sub_roles:
            check(sub, role.company, name)

    for name, parse in parses.items():
        for role in parse.experience:
            check(role, None, name)
    assert not offenders, offenders


def test_C9_C10_inferred_skills_carry_evidence(parses):
    for name, parse in parses.items():
        for skill in parse.skills:
            if skill.source != "skills_section":
                assert skill.evidence is not None, (
                    f"{name}: inferred skill '{skill.name}' has no evidence span (C9/C10)"
                )


def test_C12_contact_details_are_arrays(parses):
    for name, parse in parses.items():
        assert isinstance(parse.contact.emails, list), name
        assert isinstance(parse.contact.phones, list), name


def test_C13_phone_raw_is_never_discarded(parses):
    for name, parse in parses.items():
        for phone in parse.contact.phones:
            assert phone.raw.strip(), f"{name}: a phone lost its raw form"


def test_C15_gpa_keeps_its_raw_form(parses):
    for name, parse in parses.items():
        for entry in parse.education:
            if entry.gpa:
                assert entry.gpa.raw.strip(), f"{name}: GPA lost its raw form"


def test_contact_name_was_found(parses):
    """A resume without a detected name usually means the header was mis-parsed."""
    missing = [n for n, p in parses.items() if not (p.contact.name and p.contact.name.text.strip())]
    assert not missing, f"no name extracted from: {missing}"


def test_experience_was_found(parses):
    """Zero roles on a real resume means the section detection failed outright."""
    empty = [n for n, p in parses.items() if not p.experience]
    assert not empty, f"no experience extracted from: {empty}"


def test_coverage_table_is_reportable(parses):
    """Prints the table the loop is read from. Always passes; it is a report."""
    rows = sorted(((p.coverage.coverage_pct, n) for n, p in parses.items()), reverse=True)
    print(f"\n  coverage table — provider: {structure_client.PROVIDER}")
    print(f"    {'':<34} {'cov':>6}  {'repr':>6}")
    for score, name in rows:
        parse = parses[name]
        print(
            f"    {name:<34} {score:6.1f}%  {parse.coverage.represented_pct:6.1f}%   "
            f"{len(parse.coverage.unassigned):3d} unassigned  "
            f"{len(parse.coverage.claimed_not_represented):3d} claimed!rep  "
            f"{len(parse.coverage.flagged):3d} flagged"
        )
    represented = [p.coverage.represented_pct for p in parses.values()]
    mean_coverage = statistics.mean(s for s, _ in rows)
    print(f"    {'MEAN':<34} {mean_coverage:6.2f}%  {statistics.mean(represented):6.2f}%")
