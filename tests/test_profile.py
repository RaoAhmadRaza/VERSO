"""The profile view and the equal-employment questions.

The profile is the landing view; the proof sheet moved to `/proof`. These cover the parts
that are new rather than moved: the question set's shape, the store round-trip, the gate
that asks before showing a profile, and the mapping from parse to profile entries.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import eeo, store
from app.main import app
from app.models import (
    Contact,
    Coverage,
    DateField,
    EqualEmployment,
    Meta,
    ParsedResume,
    Role,
    Span,
)
from app.web import profile


PID = "test-person"


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Never touch the real people/ tree."""
    monkeypatch.setattr(store, "PEOPLE", tmp_path / "people")
    (tmp_path / "people" / PID).mkdir(parents=True)
    return tmp_path / "people" / PID / "profile.json"


def _parse() -> ParsedResume:
    def span(text: str, no: int) -> Span:
        return Span(text=text, source_lines=[no], verified=True, flag="ok")

    promotion = Role(
        company="Aiva Creative",
        title="Junior Engineer",
        start=DateField(raw="2023", year=2023),
        end=DateField(raw="2024", year=2024),
        source_lines=[8],
    )
    return ParsedResume(
        contact=Contact(name=span("Ada Lovelace", 1), emails=[span("ada@example.com", 2)],
                        source_lines=[1]),
        experience=[
            Role(
                company="Aiva Creative",
                title="Mobile Engineer",
                start=DateField(raw="2024-08", year=2024, month=8),
                end=DateField(raw="Present", is_present=True),
                bullets=[span("Shipped the thing.", 6)],
                sub_roles=[promotion],
                source_lines=[5],
            )
        ],
        meta=Meta(filename="x.pdf", sha256="abc", file_type="pdf", pages=1, total_lines=9),
        coverage=Coverage(total_lines=9, non_blank_lines=9, claimed_lines=9, coverage_pct=100.0,
                          unassigned=[], flagged=[], represented_lines=9, represented_pct=100.0,
                          claimed_not_represented=[]),
    )


# ------------------------------------------------------------------ the questions


def test_every_question_can_be_declined():
    """These are protected characteristics; refusing has to be a storable answer."""
    assert all(eeo.DECLINE in q.options for q in eeo.QUESTIONS)


def test_question_ids_are_unique():
    assert len({q.id for q in eeo.QUESTIONS}) == len(eeo.QUESTIONS)


def test_answers_must_come_from_the_offered_options():
    with pytest.raises(ValueError):
        EqualEmployment(answers={"gender": ["Martian"]})


def test_single_choice_questions_reject_multiple_answers():
    with pytest.raises(ValueError):
        EqualEmployment(answers={"gender": ["Male", "Female"]})


def test_multi_choice_questions_accept_several():
    answers = EqualEmployment(answers={"sexual_orientation": ["Bisexual", "Queer"]})
    assert answers.selected("sexual_orientation") == ["Bisexual", "Queer"]


def test_unknown_question_is_rejected():
    with pytest.raises(ValueError):
        EqualEmployment(answers={"favourite_colour": ["Blue"]})


# ---------------------------------------------------------------------- the store


def test_store_round_trips(isolated_store):
    assert store.load_eeo(PID) is None
    store.save_eeo(PID, EqualEmployment(answers={"pronouns": ["He/Him"]}))
    assert store.load_eeo(PID).selected("pronouns") == ["He/Him"]


def test_stored_answers_are_readable_json(isolated_store):
    """Answers now live under `eeo` -- profile.json carries active_sha and overrides too."""
    store.save_eeo(PID, EqualEmployment(answers={"veteran": [eeo.DECLINE]}))
    written = json.loads(isolated_store.read_text())
    assert written["eeo"]["answers"]["veteran"] == [eeo.DECLINE]


# -------------------------------------------------------------- parse -> profile


def test_promotion_is_nested_under_its_employer_not_flattened():
    """C6 survives into the view: the sub-role is indented, not a second company."""
    entries = profile.experience(_parse())
    assert [e.depth for e in entries] == [0, 1]
    assert {e.title for e in entries} == {"Aiva Creative"}


def test_present_role_reads_as_present():
    assert profile.experience(_parse())[0].end == "Present"


def test_unparsed_date_falls_back_to_its_raw_string():
    """C3: the raw string is never lost just because components did not parse."""
    assert profile._when(DateField(raw="Summer 2019")) == "Summer 2019"


def test_personal_keeps_the_phone_raw():
    assert profile.personal(_parse())["emails"] == ["ada@example.com"]


# --------------------------------------------------------------------- the routes


def test_profile_is_not_shown_before_the_questions_are_answered(isolated_store, monkeypatch):
    """`store` is imported into main, so patch the name main actually calls."""
    from app import main

    monkeypatch.setattr(main, "_load", lambda sha: (_parse(), []))
    client = TestClient(app)
    response = client.get("/?sha=abc", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/eeo?person={PID}&sha=abc"


def test_profile_renders_once_the_questions_are_answered(isolated_store, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "_load", lambda sha: (_parse(), []))
    store.save_eeo(PID, EqualEmployment(answers={"pronouns": ["He/Him"]}))
    body = TestClient(app).get("/?sha=abc").text
    assert "Ada Lovelace" in body
    assert "Equal Employment" in body
    assert "He/Him" in body
    assert "/proof?sha=abc" in body       # the coverage evidence stays reachable


def test_the_dropzone_lives_at_its_own_url():
    """`/` resolves to the profile now, so the dropzone moved to `/upload`.

    Behaviour change, not a weakened assertion: `test_P7_no_pointer_shows_the_dropzone`
    covers `/` falling back when there is genuinely nothing to show.
    """
    assert "Choose a file" in TestClient(app).get("/upload").text


def test_rail_marks_unbuilt_sections_as_disabled():
    """A dead link that looks live is worse than one that says it is not built."""
    body = TestClient(app).get("/").text
    for section in ("Jobs", "Agent", "Coaching", "Interview"):
        assert f'aria-disabled="true" title="Not built yet">{section}<' in body
