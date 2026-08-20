"""Coverage maths, span matching, and the fuzzy threshold (PLAN.md §10)."""

from app.models import DroppedLine, Line
from app.verify import coverage, spans


def _lines(*texts: str) -> list[Line]:
    return [
        Line(no=i, text=t, page=1, bbox=(0.0, float(i), 0.0, float(i) + 1.0))
        for i, t in enumerate(texts, start=1)
    ]


def _span(text: str, source_lines: list[int]) -> dict:
    return {
        "text": text,
        "source_lines": source_lines,
        "verified": False,
        "flag": "unverified",
    }


# ------------------------------------------------------------------------ span match


def test_exact_span_verifies():
    lines = _lines("Built the billing pipeline.")
    out, flagged = spans.verify({"s": _span("Built the billing pipeline.", [1])}, lines)
    assert out["s"]["flag"] == "ok" and out["s"]["verified"] is True
    assert flagged == []


def test_invented_text_is_flagged_hallucinated():
    lines = _lines("Built the billing pipeline.")
    out, flagged = spans.verify({"s": _span("Managed a team of twelve", [1])}, lines)
    assert out["s"]["flag"] == "hallucinated" and out["s"]["verified"] is False
    assert flagged == [(1, "hallucinated_span")]


def test_model_supplied_flag_is_never_trusted():
    """Hard rule #3: verified/flag are set by this stage, never by the model."""
    lines = _lines("Built the billing pipeline.")
    lying = {"text": "Total fabrication", "source_lines": [1], "verified": True, "flag": "ok"}
    out, _ = spans.verify({"s": lying}, lines)
    assert out["s"]["flag"] == "hallucinated"


def test_source_line_out_of_range_is_flagged_with_its_own_reason():
    lines = _lines("Built the billing pipeline.")
    _, flagged = spans.verify({"s": _span("Built the billing pipeline.", [99])}, lines)
    assert flagged == [(99, "source_line_out_of_range")]


def test_empty_source_lines_are_flagged():
    lines = _lines("Built the billing pipeline.")
    out, _ = spans.verify({"s": _span("Built the billing pipeline.", [])}, lines)
    assert out["s"]["flag"] == "hallucinated"


def test_smart_punctuation_still_matches_via_the_match_key():
    """E4 -- the source curls its quotes, the span does not. Same text."""
    lines = _lines("Led the team’s “payments” rewrite — shipped.")
    out, _ = spans.verify(
        {"s": _span('Led the team\'s "payments" rewrite - shipped.', [1])}, lines
    )
    assert out["s"]["flag"] == "ok"


def test_span_across_several_lines_matches_the_concatenation():
    lines = _lines("Built the billing pipeline", "handling 4M events per day.")
    out, _ = spans.verify(
        {"s": _span("Built the billing pipeline handling 4M events per day.", [1, 2])},
        lines,
    )
    assert out["s"]["flag"] == "ok"


def test_e3_unjoined_variant_is_accepted():
    """A span written the way the page reads still matches a line E3 joined."""
    line = Line(
        no=1,
        text="a high-throughput pipeline",
        page=1,
        bbox=(0, 0, 0, 1),
        unjoined=["a high-through-", "put pipeline"],
    )
    out, _ = spans.verify({"s": _span("a high-through- put pipeline", [1])}, [line])
    assert out["s"]["flag"] == "ok"


def test_threshold_is_95_and_tolerates_a_typo_but_not_a_rewrite():
    lines = _lines("Built the billing pipeline handling four million events per day.")
    near = _span("Built the biling pipeline handling four million events per day.", [1])
    far = _span("Worked on billing.", [1])
    out, _ = spans.verify({"near": near, "far": far}, lines)
    assert out["near"]["flag"] == "ok"
    assert out["far"]["flag"] == "hallucinated"


def test_span_contained_in_a_longer_source_line_verifies():
    """BEHAVIOUR CHANGE, deliberate. This previously asserted `hallucinated`.

    PLAN.md §2 specifies whole-string `ratio >= 95`. Measured against the real corpus that
    flagged a correct extraction on 20 of 20 resumes: every one packs contact details onto
    a single line, so the email span is a proper substring of the line it correctly cites.
    Containment is the normal case, not a defect, so it now verifies.
    """
    lines = _lines("Jan 2021 - Present   Built the billing pipeline handling 4M events.")
    out, _ = spans.verify({"s": _span("Built the billing pipeline handling 4M events.", [1])}, lines)
    assert out["s"]["flag"] == "ok"


def test_field_extracted_from_a_multi_field_contact_line_verifies():
    """The exact shape that produced 21 false positives on the real corpus."""
    lines = _lines("marcus.chen@ironwatchmail.com | (571) 555-0129 | Arlington, VA | linkedin.com/in/mc")
    out, _ = spans.verify(
        {
            "email": _span("marcus.chen@ironwatchmail.com", [1]),
            "location": _span("Arlington, VA", [1]),
        },
        lines,
    )
    assert out["email"]["flag"] == "ok"
    assert out["location"]["flag"] == "ok"


def test_containment_does_not_launder_a_fabrication():
    """Permitting substrings must not let invented text through."""
    lines = _lines("marcus.chen@ironwatchmail.com | (571) 555-0129 | Arlington, VA")
    out, _ = spans.verify({"s": _span("Managed a team of twelve engineers", [1])}, lines)
    assert out["s"]["flag"] == "hallucinated"


def test_a_very_short_span_cannot_match_by_containment():
    """Two characters carry no information; the length floor rejects them."""
    lines = _lines("Senior Infrastructure Engineer, Arlington VA")
    out, _ = spans.verify({"s": _span("IT", [1])}, lines)
    assert out["s"]["flag"] == "hallucinated"


def test_containment_requires_a_word_boundary():
    """"Java" must not verify against "JavaScript" -- that is a different skill."""
    lines = _lines("Built the frontend in JavaScript and TypeScript")
    out, _ = spans.verify({"s": _span("Java", [1])}, lines)
    assert out["s"]["flag"] == "hallucinated"


def test_short_city_state_field_verifies_inside_a_contact_line():
    """The real-corpus case: 9-character locations sit inside a combined contact line."""
    lines = _lines("carlos.mendoza@palomamail.com | (305) 555-0192 | Miami, FL | linkedin.com/in/cm")
    out, _ = spans.verify({"s": _span("Miami, FL", [1])}, lines)
    assert out["s"]["flag"] == "ok"


def test_off_by_one_source_lines_are_still_caught():
    """A real model error from the corpus: bullet text cited one line above where it lives.

    This must keep failing. Loosening matching until it passes would hide the exact
    provenance error the span contract exists to detect.
    """
    lines = _lines(
        "incidents per month.",
        "Built detection rules in Splunk that reduced mean-time-to-detect for lateral movement.",
    )
    out, _ = spans.verify(
        {"s": _span("Built detection rules in Splunk that reduced mean-time-to-detect for lateral movement.", [1])},
        lines,
    )
    assert out["s"]["flag"] == "hallucinated"


# -------------------------------------------------------------------- coverage maths


def test_coverage_counts_only_non_blank_lines():
    lines = _lines("JANE MORGAN", "", "EXPERIENCE", "")
    parse = {"a": _span("JANE MORGAN", [1]), "b": _span("EXPERIENCE", [3])}
    result = coverage.compute(parse, lines)
    assert result.total_lines == 4
    assert result.non_blank_lines == 2
    assert result.coverage_pct == 100.0
    assert result.unassigned == []


def test_unassigned_lines_are_reported():
    lines = _lines("JANE MORGAN", "Referred by M. Chen", "EXPERIENCE")
    parse = {"a": _span("JANE MORGAN", [1]), "b": _span("EXPERIENCE", [3])}
    result = coverage.compute(parse, lines)
    assert result.unassigned == [2]
    assert result.coverage_pct == round(200 / 3, 2)


def test_dropped_lines_are_excluded_from_the_denominator():
    """Page furniture removed on purpose must not count as a coverage miss."""
    lines = _lines("JANE MORGAN", "Page 1 of 3")
    dropped = [DroppedLine(no=2, text="Page 1 of 3", reason="repeated_page_footer")]
    result = coverage.compute({"a": _span("JANE MORGAN", [1])}, lines, dropped)
    assert result.non_blank_lines == 1
    assert result.coverage_pct == 100.0


def test_claimed_lines_are_collected_from_arbitrary_depth():
    """source_lines on a nested sub_role must count toward coverage."""
    parse = {
        "experience": [
            {
                "company": "Acme",
                "source_lines": [1],
                "sub_roles": [{"company": "Acme", "source_lines": [2, 3]}],
            }
        ]
    }
    assert coverage.claimed_lines(parse) == {1, 2, 3}


def test_coverage_of_an_empty_parse_is_zero_not_a_crash():
    result = coverage.compute({}, _lines("JANE MORGAN", "EXPERIENCE"))
    assert result.coverage_pct == 0.0
    assert result.unassigned == [1, 2]


def test_flagged_spans_reach_the_coverage_report():
    lines = _lines("Built the billing pipeline.")
    parse, flagged = spans.verify({"s": _span("Invented", [1])}, lines)
    result = coverage.compute(parse, lines, [], flagged)
    assert result.flagged == [(1, "hallucinated_span")]


# ------------------------------------------------------------- C6 sub_role nesting


def test_C6_subroles_from_a_different_company_are_promoted():
    """Observed on the corpus: three employers chained as nested sub_roles.

    Coverage cannot see this -- every line was still claimed -- so the structure is
    corrected in Python. Comparing two company names needs no judgement.
    """
    from app.pipeline import _flatten_foreign_subroles

    parsed = {
        "experience": [
            {
                "company": "Northlight Systems", "title": "Senior TPM", "source_lines": [16],
                "sub_roles": [
                    {
                        "company": "Halcyon Networks", "title": "TPM", "source_lines": [25],
                        "sub_roles": [
                            {"company": "Bluepeak Solutions", "title": "Coordinator",
                             "source_lines": [32], "sub_roles": []}
                        ],
                    }
                ],
            }
        ]
    }
    out = _flatten_foreign_subroles(parsed)
    companies = [r["company"] for r in out["experience"]]
    assert companies == ["Northlight Systems", "Halcyon Networks", "Bluepeak Solutions"]
    assert all(not r["sub_roles"] for r in out["experience"])


def test_C6_a_genuine_promotion_chain_stays_nested():
    """Same employer, three titles -- this is exactly what sub_roles are for."""
    from app.pipeline import _flatten_foreign_subroles

    parsed = {
        "experience": [
            {
                "company": "Acme Corp", "title": "Director", "source_lines": [5],
                "sub_roles": [
                    {"company": "Acme Corp", "title": "Manager", "source_lines": [9],
                     "sub_roles": [
                         {"company": "acme corp", "title": "Engineer",
                          "source_lines": [13], "sub_roles": []}
                     ]}
                ],
            }
        ]
    }
    out = _flatten_foreign_subroles(parsed)
    assert len(out["experience"]) == 1
    assert out["experience"][0]["sub_roles"][0]["company"] == "Acme Corp"
    assert out["experience"][0]["sub_roles"][0]["sub_roles"][0]["title"] == "Engineer"


def test_C6_flattening_preserves_every_source_line():
    """Restructuring must never drop provenance -- that would silently cut coverage."""
    from app.pipeline import _flatten_foreign_subroles
    from app.verify.coverage import claimed_lines

    parsed = {
        "experience": [
            {"company": "A", "title": "x", "source_lines": [1, 2], "sub_roles": [
                {"company": "B", "title": "y", "source_lines": [3, 4], "sub_roles": []}
            ]}
        ]
    }
    assert claimed_lines(_flatten_foreign_subroles(parsed)) == claimed_lines(parsed) == {1, 2, 3, 4}


def test_a_rewritten_employer_name_is_flagged():
    """`Role.company` is a bare str with no source_lines, so the span contract misses it.

    Observed live: a model renamed two employers to satisfy a prompt rule about nesting,
    and the parse still scored 100% coverage with zero flags.
    """
    lines = _lines("Technical Program Manager", "Halcyon Networks - San Diego, CA", "Jun 2018 - Feb 2021")
    role = {"company": "Northlight Systems", "title": "Technical Program Manager",
            "source_lines": [1, 2, 3], "sub_roles": [], "bullets": []}
    flagged = spans.verify_bare_fields({"experience": [role]}, {l.no: l for l in lines})
    assert (1, "unsourced_company") in flagged


def test_a_correct_employer_name_is_not_flagged():
    lines = _lines("Technical Program Manager", "Halcyon Networks - San Diego, CA", "Jun 2018 - Feb 2021")
    role = {"company": "Halcyon Networks", "title": "Technical Program Manager",
            "source_lines": [1, 2, 3], "sub_roles": [], "bullets": []}
    assert spans.verify_bare_fields({"experience": [role]}, {l.no: l for l in lines}) == []
