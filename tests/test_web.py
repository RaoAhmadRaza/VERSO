"""Routes and the proof-sheet rendering (PLAN.md §3 routes, §8 UI).

Not in the PLAN.md §10 list, which predates the UI phase. The dashboard is where the
coverage guarantee is actually communicated, so it gets tests.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import pipeline, store
from app.main import app

PROBES = Path(__file__).parent.parent / "fixtures" / "synthetic"
FIXTURE = PROBES / "header_contact.pdf"
PID = "test-person"


@pytest.fixture(scope="module", autouse=True)
def isolated_store(tmp_path_factory):
    """Never touch the real data/profile.json.

    These tests POST real parses, and a successful parse now writes `active_sha`. Without
    this the suite would durably repoint the developer's own profile and flip
    `test_root_without_a_hash_is_still_the_dropzone` for every later run. Module-scoped, so
    `monkeypatch` (function-scoped) cannot be used here.
    """
    from app import store

    original = store.PEOPLE
    store.PEOPLE = tmp_path_factory.mktemp("store") / "people"
    (store.PEOPLE / PID).mkdir(parents=True)
    store.set_active_person(PID)
    yield store.PEOPLE
    store.PEOPLE = original


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def rendered(client) -> tuple[str, str]:
    """Render the dashboard from a hand-built parse, so no model call is needed."""
    checkpoint = pipeline.run_extract(FIXTURE.read_bytes(), FIXTURE.name)
    index = {line["no"]: line["text"] for line in checkpoint["lines"]}

    def span(no: int) -> dict:
        return {
            "text": index[no],
            "source_lines": [no],
            "verified": False,
            "flag": "unverified",
        }

    parse = {
        "language": "en",
        "contact": {
            "name": span(1),
            "emails": [span(2)],
            "phones": [{"raw": index[3], "source_lines": [3]}],
            "location": span(4),
            "source_lines": [1, 2, 3, 4],
        },
        "summary": span(6),
        "other_sections": [
            {
                "heading": "Everything else",
                "lines": [span(n) for n in index if n not in (1, 2, 3, 4, 6) and index[n].strip()],
                "source_lines": [n for n in index if index[n].strip()],
            }
        ],
    }
    result = pipeline.run_verify(checkpoint, parse)
    pipeline._write(pipeline.PARSES / f"{checkpoint['sha256']}.json", result.model_dump())
    return checkpoint["sha256"], client.get(f"/proof?sha={checkpoint['sha256']}").text


def test_empty_state_names_the_three_processing_stages(client):
    body = client.get("/upload").text
    assert "dropzone" in body
    for stage in ("Extracting", "Structuring", "Verifying"):
        assert stage in body


def test_empty_state_states_what_parses_best(client):
    assert "Single-column parses best." in client.get("/upload").text


def test_unknown_hash_is_a_404_not_a_blank_page(client):
    assert client.get("/api/parse/deadbeef").status_code == 404
    assert client.get("/api/lines/deadbeef").status_code == 404
    assert client.get("/?sha=deadbeef").status_code == 404


def test_api_lines_returns_the_numbered_checkpoint(client, rendered):
    sha, _ = rendered
    payload = client.get(f"/api/lines/{sha}").json()
    assert payload["lines"][0]["no"] == 1
    assert payload["sha256"] == sha


def test_api_parse_returns_the_cached_parse(client, rendered):
    sha, _ = rendered
    payload = client.get(f"/api/parse/{sha}").json()
    assert payload["meta"]["sha256"] == sha
    assert "coverage" in payload


def test_gutter_has_one_bar_per_document_line(client, rendered):
    sha, body = rendered
    lines = pipeline.cached_lines(sha)["lines"]
    assert body.count('class="bar ') == len(lines)


def test_every_gutter_bar_carries_a_line_number(rendered):
    _, body = rendered
    assert body.count('class="bar ') == body.count('data-line=') - body.count('class="src-line" data-line=')


def test_source_pane_renders_every_line_with_its_number(client, rendered):
    sha, body = rendered
    lines = pipeline.cached_lines(sha)["lines"]
    assert body.count('class="src-line"') == len(lines)


def test_parse_pane_rows_cite_their_source_lines(rendered):
    """The linking contract: a displayed value carries the lines it came from."""
    _, body = rendered
    assert 'data-lines="1"' in body
    assert "⌐" in body  # the citation marker


def test_rejection_returns_the_user_facing_message(client):
    response = client.post(
        "/api/parse",
        files={"file": ("locked.pdf", (PROBES / "locked.pdf").read_bytes(), "application/pdf")},
    )
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "password_protected"
    assert "password protected" in payload["error"].lower()


def test_scanned_rejection_names_the_actual_failure(client):
    response = client.post(
        "/api/parse",
        files={"file": ("scanned.pdf", (PROBES / "scanned.pdf").read_bytes(), "application/pdf")},
    )
    assert response.status_code == 422
    assert "scan" in response.json()["error"].lower()


def test_static_assets_are_served(client):
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


# ------------------------------------------------------------------- link labelling


def test_links_are_named_by_service():
    from app.structure.links import label_for

    assert label_for("https://github.com/RaoAhmadRaza") == "GitHub"
    assert label_for("linkedin.com/in/aishabello-pm") == "LinkedIn"
    assert label_for("https://play.google.com/store/apps/details?id=x") == "Google Play"
    assert label_for("https://scholar.google.com/citations?user=x") == "Google Scholar"


def test_a_shortened_link_falls_back_to_how_the_resume_labelled_it():
    """The URL hides its destination, so the written label is the only signal left."""
    from app.structure.links import label_for

    assert label_for("https://shorturl.at/Ou3qh", "LinkedIn: https://shorturl.at/Ou3qh") == "LinkedIn"
    assert label_for("https://bit.ly/abc", "Portfolio: https://bit.ly/abc") == "Portfolio"


def test_an_unrecognised_link_is_not_mislabelled():
    from app.structure.links import label_for

    assert label_for("https://some-unknown-host.example/x") == "Website"


def test_link_label_is_derived_in_python_not_taken_from_the_model():
    """A model-supplied label must be overwritten, like verified/flag (hard rule #3)."""
    from app import pipeline
    from app.models import Line

    lines = [Line(no=1, text="GitHub: https://github.com/x", page=1, bbox=(0, 0, 0, 1))]
    parsed = {
        "contact": {
            "links": [
                {"text": "https://github.com/x", "source_lines": [1],
                 "label": "Facebook", "verified": False, "flag": "unverified"}
            ]
        }
    }
    out = pipeline._label_links(parsed, lines)
    assert out["contact"]["links"][0]["label"] == "GitHub"


def test_link_keeps_provenance_and_still_verifies():
    from app.models import Link
    from app.verify import spans
    from app.models import Line

    lines = [Line(no=1, text="GitHub: https://github.com/x", page=1, bbox=(0, 0, 0, 1))]
    link = Link(text="https://github.com/x", source_lines=[1], label="GitHub").model_dump()
    out, flagged = spans.verify({"l": link}, lines)
    assert out["l"]["flag"] == "ok"
    assert out["l"]["label"] == "GitHub"
    assert flagged == []


# --------------------------------------------------------------- processing screen


def test_processing_screen_has_progress_clock_and_facts():
    """The wait must look alive, or a 60-second model call reads as a hung page."""
    from pathlib import Path

    html = (Path(__file__).parent.parent / "app/web/templates/upload.html").read_text()
    for element in ("id=\"fill\"", "id=\"elapsed\"", "id=\"remaining\"", "id=\"fact\""):
        assert element in html


def test_estimate_scales_with_file_size():
    """A fixed baseline called a correct 3-minute parse "taking longer than usual"."""
    from pathlib import Path

    js = (Path(__file__).parent.parent / "app/web/static/app.js").read_text()
    assert "ESTIMATE_PER_KB_MS" in js, "the estimate must scale, not be a constant"
    assert "BASELINE_MS = 60000" not in js
    assert "a detailed resume takes several minutes" in js


def test_progress_bar_never_completes_on_a_guess():
    """Only a real response may fill the bar; a timer must not claim completion.

    Asserts the mechanism, not the copy -- an earlier version of this test pinned the exact
    overrun wording and broke the moment the wording was improved.
    """
    from pathlib import Path

    js = (Path(__file__).parent.parent / "app/web/static/app.js").read_text()
    assert "Math.min(92" in js, "the eased bar must cap below 100"
    assert 'fill.style.width = "100%"' in js, "only a real response fills the bar"
    assert "fill.classList.add(\"overrun\")" in js, "overrun must be signalled"
    assert "0:00 left" not in js, "a countdown must never reach zero while still working"


def test_active_provider_is_visible_in_the_ui(client):
    """The server reads VERSO_PROVIDER at import, so a CLI shell prefix does not change it.
    That mismatch silently cost a four-minute parse; the running model must be on screen."""
    from app.structure import client as structure_client

    body = client.get("/upload").text
    assert structure_client.active_model() in body
    assert "/" in structure_client.active_model()
