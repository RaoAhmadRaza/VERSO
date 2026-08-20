"""VERSO web app.

PLAN.md §3 budgets four routes. Twelve now: the profile view added `/upload`, `/proof` and
the `/eeo` pair, and multi-profile added `/people/create|switch|delete`. Still no route that
does anything a page cannot; resist adding more.
"""

from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE importing anything under app/. Settings are read with os.getenv at
# module scope, so importing first and loading second leaves every one of them on its
# default -- which silently kept the server on the default provider after .env said
# otherwise, and cost a four-minute parse to notice.
load_dotenv()

from fastapi import FastAPI, File, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from app import eeo as eeo_questions  # noqa: E402
from app import pipeline, store  # noqa: E402
from app.extract.errors import ExtractionError  # noqa: E402
from app.models import EqualEmployment  # noqa: E402
from app.structure import client as structure_client  # noqa: E402
from app.structure.client import StructuringError  # noqa: E402
from app.web import display, profile  # noqa: E402

WEB = Path(__file__).parent / "web"
app = FastAPI(title="VERSO")
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
templates = Jinja2Templates(directory=WEB / "templates")


@app.post("/api/parse")
async def api_parse(file: UploadFile = File(...), person: str | None = None) -> JSONResponse:
    pid = _person(person)
    if pid is None:
        return JSONResponse({"error": "Create a person first.", "code": "no_person"}, status_code=400)
    data = await file.read()
    try:
        result = pipeline.parse_document(data, file.filename or "resume")
    except ExtractionError as exc:
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=422)
    except StructuringError as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=502)
    # Only a parse that actually reached disk becomes the active one (P8). Set here rather
    # than in `pipeline`, so CLI batches and tests never repoint the user's profile.
    previous = store.active_sha(pid)
    store.set_active_sha(pid, result.meta.sha256)
    if previous:
        # Updating the resume, not starting a second profile: carry the user's edits across,
        # but only to fields the new document still has AND has not itself changed (P23/P26).
        older = pipeline.cached_parse(previous)
        store.carry_overrides(
            pid,
            previous,
            result.meta.sha256,
            allowed=profile.overridable_paths(result),
            old_values=profile.field_values(older) if older else {},
            new_values=profile.field_values(result),
        )
    return JSONResponse(
        {
            "sha256": result.meta.sha256,
            "coverage": result.coverage.model_dump(),
            "parse": result.model_dump(),
        }
    )


@app.get("/api/parse/{sha}")
async def api_cached_parse(sha: str) -> JSONResponse:
    result = pipeline.cached_parse(sha)
    if result is None:
        raise HTTPException(status_code=404, detail="No parse for that hash.")
    return JSONResponse(result.model_dump())


@app.get("/api/lines/{sha}")
async def api_lines(sha: str) -> JSONResponse:
    """Debug view: the raw numbered lines, exactly as the model received them."""
    checkpoint = pipeline.cached_lines(sha)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="No extraction for that hash.")
    return JSONResponse(checkpoint)


TABS = (
    ("personal", "Personal"),
    ("education", "Education"),
    ("experience", "Work Experience"),
    ("skills", "Skills"),
    ("eeo", "Equal Employment"),
)


def _base_context(person: str | None) -> dict:
    # Which model is running is invisible in the UI otherwise: the server reads
    # VERSO_PROVIDER from the environment at import, so a shell prefix on a CLI run does
    # not affect it. That mismatch cost a user a four-minute parse they thought was fast.
    people = store.list_people()
    return {
        "parse": None,
        "sha": None,
        "provider": structure_client.active_model(),
        "person": person,
        "people": people,
        "display_name": store.load_profile(person).display_name if person else "",
        # Drives the rail: with nothing parsed, "Profile" must say so rather than quietly
        # serving the dropzone, which reads as a broken link (P20).
        "has_profile": bool(
            person and (_resolvable(store.active_sha(person)) or _newest_parse(person))
        ),
    }


def _person(requested: str | None) -> str | None:
    """The one resolution every person-aware route uses (PLAN.md §6.5).

    1. `?person=` if it names someone real -- and if it does not, 404. A typo in the URL must
       never silently render a different person: showing Ali's resume when the URL said Sara
       is the worst failure available here.
    2. else the stored default
    3. else the only person, if there is exactly one
    4. else None -- the caller shows the create-person screen
    """
    if requested is not None:
        if not store.person_exists(requested):
            raise HTTPException(status_code=404, detail=f"No such person: {requested!r}")
        return requested
    stored = store.active_person()
    if stored:
        return stored
    people = store.list_people()
    return people[0][0] if len(people) == 1 else None


def _resolvable(sha: str | None) -> bool:
    """Both halves of a parse are on disk, so a redirect to it will not 404 (P5/P6)."""
    return bool(sha) and pipeline.cached_parse(sha) is not None and pipeline.cached_lines(sha) is not None


def _newest_parse(person: str) -> str | None:
    """The most recent servable parse THIS PERSON has touched.

    `active_sha` is written only by a successful upload, so anything parsed before that
    pointer existed -- or by `cli.py` -- would otherwise be unreachable from the UI: Resume ->
    Profile would land on the dropzone with parses sitting on disk. Resolving the newest one
    for display keeps them reachable, and resolving is not choosing: the pointer is left alone
    so a GET still never decides which resume is active (P12/P18).

    Scoped to this person's own shas, deliberately. A global scan would let someone with no
    pointer fall back to ANOTHER person's resume -- the same leak `?person=` 404s to prevent,
    arriving through the pointer instead of the URL (P36).
    """
    if not pipeline.PARSES.exists():
        return None
    profile = store.load_profile(person)
    mine = {sha for sha in {profile.active_sha, *profile.overrides} if sha}
    if not mine:
        return None
    newest = sorted(
        (f for f in pipeline.PARSES.glob("*.json") if f.stem in mine),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return next((f.stem for f in newest if _resolvable(f.stem)), None)


def _load(sha: str) -> tuple:
    result = pipeline.cached_parse(sha)
    checkpoint = pipeline.cached_lines(sha)
    if result is None or checkpoint is None:
        raise HTTPException(status_code=404, detail="No parse for that hash.")
    return result, checkpoint["lines"]


@app.get("/upload")
async def upload(request: Request, person: str | None = None):
    """The dropzone. Its own URL now that `/` is the profile."""
    pid = _person(person)
    if pid is None:
        return _create_person_screen(request)
    return templates.TemplateResponse(
        request=request, name="upload.html", context=_base_context(pid) | {"active": "resume"}
    )


@app.get("/")
async def dashboard(request: Request, sha: str | None = None, person: str | None = None):
    """The profile: the active resume unless `sha` says otherwise, questions if never asked."""
    pid = _person(person)
    if pid is None:
        return _create_person_screen(request)
    if not sha:
        # An internal convenience pointer, so a stale one falls back to the dropzone rather
        # than 404ing the landing page every visit (P5/P6). A typed `?sha=` still 404s.
        mine = store.active_sha(pid)
        target = mine if _resolvable(mine) else _newest_parse(pid)
        if target:
            return RedirectResponse(f"/?person={pid}&sha={target}", status_code=303)
        return await upload(request, person=pid)

    result, _ = _load(sha)
    answers = store.load_eeo(pid)
    if answers is None:
        # Asked once, before the profile is ever shown.
        return RedirectResponse(f"/eeo?person={pid}&sha={sha}", status_code=303)

    overrides = store.get_overrides(pid, sha)
    context = _base_context(pid) | {
        "parse": result,
        "sha": sha,
        "active": "profile",
        "has_profile": True,
        "tabs": TABS,
        "personal": profile.personal(result, overrides),
        "education": profile.education(result, overrides),
        "experience": profile.experience(result, overrides),
        "skills": profile.skills(result),
        "eeo_rows": [(q, answers.selected(q.id)) for q in eeo_questions.QUESTIONS],
    }
    return templates.TemplateResponse(request=request, name="index.html", context=context)


@app.get("/proof")
async def proof(request: Request, sha: str):
    """The proof sheet -- source lines, coverage gutter, bidirectional linking."""
    result, lines = _load(sha)
    context = _base_context(store.active_person()) | {
        "parse": result,
        "sha": sha,
        "active": "profile",
        "has_profile": True,
        "lines": lines,
        "sections": display.sections(result),
        "gutter": display.gutter(result, len(lines)),
        "unassigned": {n: t for n, t in
                       ((line["no"], line["text"]) for line in lines)
                       if n in set(result.coverage.unassigned)},
        "claimed_not_represented": {n: t for n, t in
                                    ((line["no"], line["text"]) for line in lines)
                                    if n in set(result.coverage.claimed_not_represented)},
    }
    return templates.TemplateResponse(request=request, name="proof.html", context=context)


@app.get("/eeo")
async def eeo_form(request: Request, sha: str, person: str | None = None):
    """The questions. Prefilled when they have been answered before."""
    pid = _person(person)
    if pid is None:
        return _create_person_screen(request)
    result, _ = _load(sha)
    stored = store.load_eeo(pid)
    context = _base_context(pid) | {
        "parse": result,
        "sha": sha,
        "active": "profile",
        "has_profile": True,
        "questions": eeo_questions.QUESTIONS,
        "selected": stored.answers if stored else {},
    }
    return templates.TemplateResponse(request=request, name="eeo.html", context=context)


@app.post("/eeo")
async def save_eeo(request: Request, sha: str, person: str | None = None):
    """Validate against the question set, store, then show the profile."""
    pid = _person(person)
    if pid is None:
        raise HTTPException(status_code=400, detail="Create a person first.")
    form = await request.form()
    answers = {
        question.id: [v for v in form.getlist(question.id) if v]
        for question in eeo_questions.QUESTIONS
        if form.getlist(question.id)
    }
    store.save_eeo(pid, EqualEmployment(answers=answers))
    return RedirectResponse(f"/?person={pid}&sha={sha}", status_code=303)


@app.post("/profile/edit")
async def edit_profile(request: Request, sha: str, person: str | None = None):
    """Store edits as overrides. The parse on disk is never touched (P9).

    One generic endpoint rather than one per field. Keys come from a form and are therefore
    untrusted, so anything that does not address a real field of THIS parse is refused.
    """
    pid = _person(person)
    if pid is None:
        raise HTTPException(status_code=400, detail="Create a person first.")
    result, _ = _load(sha)
    allowed = profile.overridable_paths(result)
    form = await request.form()
    edits = {
        key: str(value)
        for key, value in form.multi_items()
        if key in allowed and isinstance(value, str)
    }
    if edits:
        store.set_overrides(pid, sha, edits)
    return RedirectResponse(f"/?person={pid}&sha={sha}", status_code=303)


def _create_person_screen(request: Request):
    """Zero people: a resume needs somewhere to go before it is uploaded."""
    return templates.TemplateResponse(
        request=request,
        name="people_new.html",
        context={
            "parse": None, "sha": None, "person": None, "people": [],
            "display_name": "", "has_profile": False,
            "provider": structure_client.active_model(),
        },
    )


@app.post("/people/create")
async def people_create(request: Request):
    form = await request.form()
    display_name = str(form.get("display_name", "")).strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="A name is required.")
    try:
        person_id = store.create_person(display_name)
    except store.InvalidPersonId:
        raise HTTPException(status_code=400, detail="That name has no usable letters or digits.")
    store.set_active_person(person_id)
    return RedirectResponse(f"/upload?person={person_id}", status_code=303)


@app.post("/people/switch")
async def people_switch(request: Request):
    form = await request.form()
    person_id = str(form.get("person_id", ""))
    if not store.person_exists(person_id):
        raise HTTPException(status_code=404, detail=f"No such person: {person_id!r}")
    store.set_active_person(person_id)
    return RedirectResponse(f"/?person={person_id}", status_code=303)


@app.post("/people/delete")
async def people_delete(request: Request):
    """Removes people/<id> only. Typing the display name is the confirmation.

    Nothing under data/uploads|lines|parses is touched -- shared and sha256-keyed, so another
    person may point at the same parse. The orphan count is reported, never acted on.
    """
    form = await request.form()
    person_id = str(form.get("person_id", ""))
    if not store.person_exists(person_id):
        raise HTTPException(status_code=404, detail=f"No such person: {person_id!r}")
    if str(form.get("confirm", "")).strip() != store.load_profile(person_id).display_name:
        raise HTTPException(status_code=400, detail="Type the person's name exactly to confirm.")
    orphaned = store.delete_person(person_id)
    remaining = store.list_people()
    if not remaining:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(
        f"/?person={remaining[0][0]}&orphaned={orphaned}", status_code=303
    )
