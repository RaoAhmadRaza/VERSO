"""VERSO web app.

PLAN.md §3 budgets four routes. The profile view adds three more -- `/upload`, `/proof`
and the `/eeo` pair -- because the profile became the landing view and the proof sheet,
the dropzone and the equal-employment form each need a URL of their own. Still no route
that does anything a page cannot; resist adding more.
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
async def api_parse(file: UploadFile = File(...)) -> JSONResponse:
    data = await file.read()
    try:
        result = pipeline.parse_document(data, file.filename or "resume")
    except ExtractionError as exc:
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=422)
    except StructuringError as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=502)
    # Only a parse that actually reached disk becomes the active one (P8). Set here rather
    # than in `pipeline`, so CLI batches and tests never repoint the user's profile.
    previous = store.active_sha()
    store.set_active_sha(result.meta.sha256)
    if previous:
        # Updating the resume, not starting a second profile: carry the user's edits across,
        # but only to fields the new document still has AND has not itself changed (P23/P26).
        older = pipeline.cached_parse(previous)
        store.carry_overrides(
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


def _base_context() -> dict:
    # Which model is running is invisible in the UI otherwise: the server reads
    # VERSO_PROVIDER from the environment at import, so a shell prefix on a CLI run does
    # not affect it. That mismatch cost a user a four-minute parse they thought was fast.
    return {
        "parse": None,
        "sha": None,
        "provider": structure_client.active_model(),
        # Drives the rail: with nothing parsed, "Profile" must say so rather than quietly
        # serving the dropzone, which reads as a broken link (P20).
        "has_profile": bool(_resolvable(store.active_sha()) or _newest_parse()),
    }


def _resolvable(sha: str | None) -> bool:
    """Both halves of a parse are on disk, so a redirect to it will not 404 (P5/P6)."""
    return bool(sha) and pipeline.cached_parse(sha) is not None and pipeline.cached_lines(sha) is not None


def _newest_parse() -> str | None:
    """The most recent parse on disk that can actually be served.

    `active_sha` is written only by a successful upload, so anything parsed before that
    pointer existed -- or by `cli.py` -- would otherwise be unreachable from the UI: Resume ->
    Profile would land on the dropzone with 27 parses sitting on disk. Resolving the newest
    one for display keeps them reachable, and resolving is not choosing: the pointer is left
    alone so a GET still never decides which resume is active (P12/P18).
    """
    if not pipeline.PARSES.exists():
        return None
    newest = sorted(pipeline.PARSES.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    return next((f.stem for f in newest if _resolvable(f.stem)), None)


def _load(sha: str) -> tuple:
    result = pipeline.cached_parse(sha)
    checkpoint = pipeline.cached_lines(sha)
    if result is None or checkpoint is None:
        raise HTTPException(status_code=404, detail="No parse for that hash.")
    return result, checkpoint["lines"]


@app.get("/upload")
async def upload(request: Request):
    """The dropzone. Its own URL now that `/` is the profile."""
    return templates.TemplateResponse(
        request=request, name="upload.html", context=_base_context() | {"active": "resume"}
    )


@app.get("/")
async def dashboard(request: Request, sha: str | None = None):
    """The profile: the active resume unless `sha` says otherwise, questions if never asked."""
    if not sha:
        # An internal convenience pointer, so a stale one falls back to the dropzone rather
        # than 404ing the landing page every visit (P5/P6). A typed `?sha=` still 404s.
        target = store.active_sha() if _resolvable(store.active_sha()) else _newest_parse()
        if target:
            return RedirectResponse(f"/?sha={target}", status_code=303)
        return await upload(request)

    result, _ = _load(sha)
    answers = store.load_eeo()
    if answers is None:
        # Asked once, before the profile is ever shown.
        return RedirectResponse(f"/eeo?sha={sha}", status_code=303)

    overrides = store.get_overrides(sha)
    context = _base_context() | {
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
    context = _base_context() | {
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
async def eeo_form(request: Request, sha: str):
    """The questions. Prefilled when they have been answered before."""
    result, _ = _load(sha)
    stored = store.load_eeo()
    context = _base_context() | {
        "parse": result,
        "sha": sha,
        "active": "profile",
        "has_profile": True,
        "questions": eeo_questions.QUESTIONS,
        "selected": stored.answers if stored else {},
    }
    return templates.TemplateResponse(request=request, name="eeo.html", context=context)


@app.post("/eeo")
async def save_eeo(request: Request, sha: str):
    """Validate against the question set, store, then show the profile."""
    form = await request.form()
    answers = {
        question.id: [v for v in form.getlist(question.id) if v]
        for question in eeo_questions.QUESTIONS
        if form.getlist(question.id)
    }
    store.save_eeo(EqualEmployment(answers=answers))
    return RedirectResponse(f"/?sha={sha}", status_code=303)


@app.post("/profile/edit")
async def edit_profile(request: Request, sha: str):
    """Store edits as overrides. The parse on disk is never touched (P9).

    One generic endpoint rather than one per field. Keys come from a form and are therefore
    untrusted, so anything that does not address a real field of THIS parse is refused.
    """
    result, _ = _load(sha)
    allowed = profile.overridable_paths(result)
    form = await request.form()
    edits = {
        key: str(value)
        for key, value in form.multi_items()
        if key in allowed and isinstance(value, str)
    }
    if edits:
        store.set_overrides(sha, edits)
    return RedirectResponse(f"/?sha={sha}", status_code=303)
