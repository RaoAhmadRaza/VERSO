"""The profile store: active resume, equal-employment answers, and edits (PLAN.md §6.5).

Every test is tagged with its P-family edge case ID. All offline -- no model call. The store
path is redirected to a tmp file in every test, so the developer's own data/profile.json is
never read or written.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import pipeline, store
from app.extract.errors import ScannedDocumentError
from app.main import app
from app.models import Contact, Coverage, EqualEmployment, Meta, ParsedResume, Span
from app.structure.client import StructuringError
from app.web import profile


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PROFILE", tmp_path / "profile.json")
    return store.PROFILE


def _parse(sha: str = "abc") -> ParsedResume:
    span = Span(text="Ada Lovelace", source_lines=[1], verified=True, flag="ok")
    return ParsedResume(
        contact=Contact(name=span, source_lines=[1]),
        meta=Meta(filename="x.pdf", sha256=sha, file_type="pdf", pages=1, total_lines=1),
        coverage=Coverage(total_lines=1, non_blank_lines=1, claimed_lines=1, coverage_pct=100.0,
                          unassigned=[], flagged=[], represented_lines=1, represented_pct=100.0,
                          claimed_not_represented=[]),
    )


@pytest.fixture
def empty_disk(monkeypatch, tmp_path):
    """No parses on disk.

    `_newest_parse` reads `pipeline.PARSES`, so without this these tests assert against the
    developer's real 27 parses and "nothing to fall back to" is never actually the case.
    """
    monkeypatch.setattr(pipeline, "PARSES", tmp_path / "parses")
    monkeypatch.setattr(pipeline, "LINES", tmp_path / "lines")
    return tmp_path


@pytest.fixture
def served(monkeypatch):
    """A client whose `_load` always resolves, so route behaviour is isolated from disk."""
    from app import main

    monkeypatch.setattr(main, "_load", lambda sha: (_parse(sha), []))
    return TestClient(app)


# ------------------------------------------------------------------ P1-P2: no clobbering


def test_P1_saving_eeo_keeps_active_sha_and_overrides(isolated_store):
    """A blind overwrite here deletes the rest of the file. It used to."""
    store.set_active_sha("abc")
    store.set_overrides("abc", {"personal.name": "M. A. Raza"})

    store.save_eeo(EqualEmployment(answers={"veteran": ["No"]}))

    assert store.active_sha() == "abc"
    assert store.get_overrides("abc")["personal.name"].value == "M. A. Raza"


def test_P2_setting_active_sha_keeps_the_answers(isolated_store):
    store.save_eeo(EqualEmployment(answers={"veteran": ["No"]}))
    store.set_active_sha("abc")
    assert store.load_eeo().selected("veteran") == ["No"]


def test_P14_a_new_upload_keeps_the_answers_and_moves_the_pointer(isolated_store):
    store.save_eeo(EqualEmployment(answers={"pronouns": ["He/Him"]}))
    store.set_active_sha("first")
    store.set_active_sha("second")
    assert store.active_sha() == "second"
    assert store.load_eeo().selected("pronouns") == ["He/Him"]


# ------------------------------------------------------- P3-P4, P7: reading a bad file


def test_P3_the_legacy_eeo_only_file_still_loads(isolated_store):
    """The shape written before this store existed: `{"answers": {...}}` and nothing else."""
    isolated_store.write_text(json.dumps({"answers": {"pronouns": ["He/Him"]}}), encoding="utf-8")

    loaded = store.load_profile()

    assert loaded.eeo.selected("pronouns") == ["He/Him"]
    assert loaded.active_sha is None and loaded.overrides == {}


@pytest.mark.parametrize("content", ["", "{not json", '{"active_sha": "ab', "[]", "null"])
def test_P4_a_corrupt_file_reads_as_an_empty_profile(isolated_store, content):
    isolated_store.write_text(content, encoding="utf-8")
    assert store.load_profile().active_sha is None


def test_P4_a_corrupt_file_is_not_destroyed_by_reading_it(isolated_store):
    isolated_store.write_text("{not json", encoding="utf-8")
    store.load_profile()
    assert isolated_store.read_text() == "{not json"


def test_P16_a_validation_failure_does_not_leak_answer_values(isolated_store):
    """Pydantic errors echo field values, and this file holds protected characteristics."""
    isolated_store.write_text(json.dumps({"version": 1, "eeo": {"answers": {"gender": "Male"}}}))
    assert store.load_profile().eeo is None  # swallowed, not raised with the value attached


def test_P7_first_run_has_no_file_and_no_pointer(isolated_store):
    assert not isolated_store.exists()
    assert store.load_profile().active_sha is None
    assert store.load_eeo() is None


def test_P13_answers_can_exist_before_any_resume(isolated_store):
    store.save_eeo(EqualEmployment(answers={"veteran": ["No"]}))
    assert store.active_sha() is None
    assert store.load_eeo().selected("veteran") == ["No"]


# --------------------------------------------------------------- P5-P7: route fallbacks


def test_P5_a_stale_pointer_falls_back_to_the_dropzone(isolated_store, empty_disk):
    """An internal pointer, so a dangling one must not 404 the landing page every visit."""
    store.set_active_sha("deadbeef")
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Choose a file" in response.text


def test_P17_every_real_orphan_sha_would_trigger_that_fallback():
    """25 shas on disk have lines but no parse. None may resolve as an active profile."""
    lines = {p.stem for p in (pipeline.LINES).glob("*.json")}
    parses = {p.stem for p in (pipeline.PARSES).glob("*.json")}
    orphans = lines - parses
    assert orphans, "expected orphan checkpoints on disk"
    assert all(pipeline.cached_parse(sha) is None for sha in orphans)


def test_P7_no_pointer_shows_the_dropzone(isolated_store, empty_disk):
    assert "Choose a file" in TestClient(app).get("/").text


def test_P5_an_explicit_unknown_sha_is_still_a_404(isolated_store):
    """The fallback is for the implicit pointer only; a typed URL must still fail loudly."""
    assert TestClient(app).get("/?sha=deadbeef").status_code == 404


def test_active_pointer_resolves_to_the_profile(isolated_store, served, monkeypatch):
    from app import main

    monkeypatch.setattr(main.pipeline, "cached_parse", lambda sha: _parse(sha))
    monkeypatch.setattr(main.pipeline, "cached_lines", lambda sha: {"lines": []})
    store.set_active_sha("abc")
    store.save_eeo(EqualEmployment(answers={"pronouns": ["He/Him"]}))

    response = served.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?sha=abc"


def test_P12_an_explicit_sha_does_not_change_which_resume_is_active(isolated_store, served):
    store.set_active_sha("abc")
    store.save_eeo(EqualEmployment(answers={"pronouns": ["He/Him"]}))

    served.get("/?sha=other")

    assert store.active_sha() == "abc"


# ------------------------------------------------------------- P8: failures stay inactive


@pytest.mark.parametrize(
    "error",
    [
        ScannedDocumentError("This looks scanned."),
        StructuringError("Could not parse.", code="validation_failed"),
    ],
)
def test_P8_a_failed_upload_never_becomes_the_active_resume(isolated_store, monkeypatch, error):
    from app import main

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(main.pipeline, "parse_document", boom)
    response = TestClient(app).post(
        "/api/parse", files={"file": ("r.pdf", b"%PDF-1.4 junk", "application/pdf")}
    )

    assert response.status_code in (422, 502)
    assert store.active_sha() is None


# ------------------------------------------------------------------- P9, P11: overrides


def test_P9_an_override_never_touches_the_parse_on_disk(isolated_store, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "PARSES", tmp_path)
    monkeypatch.setattr(pipeline, "LINES", tmp_path)
    parse = _parse("abc")
    pipeline._write(tmp_path / "abc.json", parse.model_dump())
    before = (tmp_path / "abc.json").read_bytes()

    store.set_overrides("abc", {"personal.name": "M. A. Raza"})

    assert (tmp_path / "abc.json").read_bytes() == before


def test_P9_an_overridden_field_is_marked_as_edited():
    """Rendering user-typed text as if it were extracted is the failure this app exists to
    prevent -- hard rule #4 applied to the UI."""
    store.set_overrides("abc", {"personal.name": "M. A. Raza"})
    view = profile.personal(_parse(), store.get_overrides("abc"))
    assert view["name"] == "M. A. Raza"
    assert "name" in view["overridden"]


def test_P11_overrides_do_not_leak_between_resumes(isolated_store):
    store.set_overrides("sha_a", {"personal.name": "From A"})
    view = profile.personal(_parse(), store.get_overrides("sha_b"))
    assert view["name"] == "Ada Lovelace"
    assert view["overridden"] == frozenset()


def test_edit_route_refuses_a_key_that_addresses_nothing(isolated_store, served):
    """Edit keys arrive from a form, so they are untrusted input at a boundary."""
    served.post("/profile/edit?sha=abc", data={"personal.nope": "x", "../etc/passwd": "y"})
    assert store.get_overrides("abc") == {}


def test_edit_route_stores_a_valid_key(isolated_store, served):
    served.post("/profile/edit?sha=abc", data={"personal.name": "M. A. Raza"})
    assert store.get_overrides("abc")["personal.name"].value == "M. A. Raza"


# ----------------------------------------------------------------------- P15: torn writes


def test_P15_a_write_is_atomic(isolated_store):
    """tmp+rename: a reader never sees a half-written file, and no .tmp is left behind."""
    store.set_active_sha("abc")
    assert json.loads(isolated_store.read_text())["active_sha"] == "abc"
    assert list(Path(isolated_store).parent.glob("*.tmp")) == []


# ------------------------------------------------- P18-P20: getting back to the profile


def test_P18_an_existing_parse_is_reachable_without_a_pointer(isolated_store, monkeypatch):
    """27 parses predate `active_sha`, and CLI runs never set it.

    Requiring an upload to reach a profile strands every resume already on disk: Resume ->
    Profile lands on the dropzone and reads as broken.
    """
    from app import main

    monkeypatch.setattr(main.store, "PROFILE", isolated_store)
    monkeypatch.setattr(main, "_load", lambda sha: (_parse(sha), []))
    monkeypatch.setattr(main, "_newest_parse", lambda: "ondisk")
    store.save_eeo(EqualEmployment(answers={"pronouns": ["He/Him"]}))

    response = TestClient(app).get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?sha=ondisk"


def test_P18_falling_back_does_not_move_the_pointer(isolated_store, monkeypatch):
    """Resolving for display is not the same as choosing. Only an upload chooses (P12)."""
    from app import main

    monkeypatch.setattr(main.store, "PROFILE", isolated_store)
    monkeypatch.setattr(main, "_load", lambda sha: (_parse(sha), []))
    monkeypatch.setattr(main, "_newest_parse", lambda: "ondisk")
    store.save_eeo(EqualEmployment(answers={"pronouns": ["He/Him"]}))

    TestClient(app).get("/")

    assert store.active_sha() is None


def test_P19_new_file_reaches_the_dropzone_not_the_profile(isolated_store, monkeypatch):
    """`/` now resolves to a profile, so a "start over" link pointing there bounces back."""
    from app import main

    monkeypatch.setattr(main.store, "PROFILE", isolated_store)
    monkeypatch.setattr(main, "_load", lambda sha: (_parse(sha), []))
    body = TestClient(app).get("/proof?sha=abc").text
    assert 'href="/upload"' in body and 'class="reset" href="/"' not in body


def test_P20_with_no_parses_at_all_the_profile_link_is_disabled(isolated_store, monkeypatch):
    """Nothing to show, so the rail must say so rather than silently serve the dropzone."""
    from app import main

    monkeypatch.setattr(main.store, "PROFILE", isolated_store)
    monkeypatch.setattr(main, "_newest_parse", lambda: None)

    body = TestClient(app).get("/upload").text

    assert 'rail-item disabled" aria-disabled="true" title="No resume parsed yet"' in body


# --------------------------------------------- P21-P25: one resume, updated in place


def test_P21_the_rail_drops_resume_once_a_profile_exists(isolated_store, served, monkeypatch):
    """One profile, so once it exists there is no second thing to navigate to."""
    from app import main

    monkeypatch.setattr(main, "_newest_parse", lambda: "abc")
    store.save_eeo(EqualEmployment(answers={"pronouns": ["He/Him"]}))

    body = served.get("/?sha=abc").text

    assert ">Resume</a>" not in body and ">Resume</span>" not in body
    assert 'rail-item active" href="/?sha=abc">Profile</a>' in body


def test_P21_the_rail_offers_resume_when_there_is_no_profile(isolated_store, empty_disk):
    body = TestClient(app).get("/upload").text
    assert 'href="/upload">Resume</a>' in body


def test_P22_the_profile_offers_to_update_the_resume(isolated_store, served, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "_newest_parse", lambda: "abc")
    store.save_eeo(EqualEmployment(answers={"pronouns": ["He/Him"]}))
    assert 'href="/upload"' in served.get("/?sha=abc").text


def test_P22_the_update_screen_can_be_left_without_uploading(isolated_store, monkeypatch):
    """Changed your mind: going back must not require uploading something."""
    from app import main

    monkeypatch.setattr(main, "_newest_parse", lambda: "abc")
    body = TestClient(app).get("/upload").text
    assert "Back to profile" in body


def test_P23_edits_carry_to_the_new_resume_where_the_field_still_exists(isolated_store):
    store.set_active_sha("old")
    store.set_overrides("old", {"personal.name": "M. A. Raza", "experience.9.title": "Gone Ltd"})

    carried = store.carry_overrides("old", "new", {"personal.name", "experience.0.title"})

    assert carried == 1
    assert store.get_overrides("new")["personal.name"].value == "M. A. Raza"


def test_P23_an_edit_with_nowhere_to_land_is_dropped_not_reattached(isolated_store):
    """`experience.9` in the old resume is a different job from `experience.9` in the new one."""
    store.set_overrides("old", {"experience.9.title": "Gone Ltd"})

    store.carry_overrides("old", "new", {"experience.0.title"})

    assert "experience.9.title" not in store.get_overrides("new")


def test_P24_re_uploading_the_same_file_keeps_its_edits(isolated_store):
    store.set_overrides("same", {"personal.name": "M. A. Raza"})
    store.carry_overrides("same", "same", {"personal.name"})
    assert store.get_overrides("same")["personal.name"].value == "M. A. Raza"


def test_P25_the_old_resumes_edits_are_left_on_disk(isolated_store):
    """History, not garbage: the old parse is still viewable by its own sha."""
    store.set_overrides("old", {"personal.name": "M. A. Raza"})
    store.carry_overrides("old", "new", {"personal.name"})
    assert store.get_overrides("old")["personal.name"].value == "M. A. Raza"


def test_P26_an_edit_is_dropped_when_the_new_resume_changed_that_field(isolated_store):
    """A correction to a value the resume has since changed is stale, not authoritative.

    Carrying it would hide the new document's real content behind an edit made about
    different text -- the same reattachment failure as P23, one path deeper.
    """
    store.set_overrides("old", {"experience.0.title": "Aiva Creative"})

    store.carry_overrides(
        "old",
        "new",
        allowed={"experience.0.title", "personal.name"},
        old_values={"experience.0.title": "Aiva", "personal.name": "Ada Lovelace"},
        new_values={"experience.0.title": "Northwind", "personal.name": "Ada Lovelace"},
    )

    assert "experience.0.title" not in store.get_overrides("new")


def test_P26_an_edit_survives_when_the_field_is_unchanged(isolated_store):
    store.set_overrides("old", {"personal.name": "M. A. Raza"})

    store.carry_overrides(
        "old",
        "new",
        allowed={"personal.name"},
        old_values={"personal.name": "Ada Lovelace"},
        new_values={"personal.name": "Ada Lovelace"},
    )

    assert store.get_overrides("new")["personal.name"].value == "M. A. Raza"


def test_P27_the_rail_is_correct_even_if_the_flag_is_missing():
    """A stale server process left `has_profile` undefined, so Jinja took the else branch
    and showed "Resume" on the profile page itself. Any page rendering a sha has a profile
    by definition, so the rail must not depend on the flag alone."""
    from app.main import templates

    html = templates.get_template("_layout.html").render(
        sha="abc", active="profile", parse=None, provider="x"
    )
    assert ">Resume</a>" not in html
    assert 'rail-item active" href="/?sha=abc">Profile</a>' in html
