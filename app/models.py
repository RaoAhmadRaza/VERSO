"""All Pydantic schemas (PLAN.md §5).

Two model families live here:

* `ParsedResumeLLM` -- what the model is allowed to return. It has no `coverage`, and its
  spans carry only claims. See CLAUDE.md hard rule #3: model output is never trusted before
  validation.
* `ParsedResume` -- `ParsedResumeLLM` plus the `coverage` block the verify stage computes.

Every extracted object carries `source_lines` (hard rule #4). A field without provenance is
unverifiable and therefore worthless.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app import eeo
from app.structure.taxonomy import SkillCategory

Zone = Literal["body", "header", "footer"]
Confidence = Literal["text", "ocr"]


# --------------------------------------------------------------------------- extraction


class Line(BaseModel):
    """One numbered line of extracted text.

    `no` is 1-based and blank lines are retained, so line numbers map onto the visual
    document. `text` is display text and is NEVER normalized for matching (hard rule #8) --
    `app.extract.normalize.match_key` builds matching keys separately.
    """

    no: int
    text: str
    page: int
    bbox: tuple[float, float, float, float]
    column: int | None = None  # 0 = left/main, 1 = sidebar

    # §6 tags. Carried through to the UI so the user can see why a line was treated a
    # particular way.
    zone: Zone = "body"
    is_bullet: bool = False  # E5, bullet glyph stripped
    from_table: bool = False  # L4
    is_bullet: bool = False  # E5 / w:numPr
    hidden_text: bool = False  # F9, fill colour ~ background
    script: str = "latin"  # E9, disables hyphen-rejoin for rtl/cjk
    confidence: Confidence = "text"  # F1, ocr-derived lines
    unjoined: list[str] = Field(default_factory=list)  # E3, pre-join halves for span match


class Block(BaseModel):
    """One extracted text block, before line numbering.

    This is the extraction stage's currency: `layout` assigns `column` and reading order,
    `normalize` explodes blocks into numbered `Line`s.
    """

    page: int
    bbox: tuple[float, float, float, float]
    text: str
    order: int = 0  # creation order within the page, the layout-engine's own sequence
    column: int | None = None
    zone: Zone = "body"
    from_table: bool = False  # L4
    is_bullet: bool = False  # E5 / w:numPr
    hidden_text: bool = False  # F9
    max_font_size: float = 0.0  # C11, largest-font-on-page-1 name heuristic
    page_width: float = 0.0  # 0 for formats with no geometry (docx, txt)
    page_height: float = 0.0
    confidence: Confidence = "text"


class DroppedLine(BaseModel):
    """A line intentionally removed. Auditable, per hard rule #2."""

    no: int
    text: str
    reason: str


class ParseWarning(BaseModel):
    """Something unrecoverable the user needs told about (e.g. L8 skill bars)."""

    code: str
    detail: str


# ------------------------------------------------------------------------------- claims


class Span(BaseModel):
    """A verbatim claim about source lines.

    The model never returns content -- it returns claims about spans. `verified` and `flag`
    are set by the verify stage and never by the model (PLAN.md §5).
    """

    text: str
    source_lines: list[int]
    verified: bool = False
    flag: Literal["ok", "hallucinated", "unverified"] = "unverified"


class DateField(BaseModel):
    """`raw` is ALWAYS retained and never overwritten (hard rule #6)."""

    raw: str
    year: int | None = None
    month: int | None = None
    precision: Literal["day", "month", "year", "season", "unknown"] = "unknown"
    is_present: bool = False  # C5


class Link(Span):
    """A contact link, plus the service it points at.

    `label` is filled in by `app.structure.links` from the URL, never by the model.
    Subclassing `Span` keeps provenance and span verification working unchanged.
    """

    label: str = "Link"


class PhoneField(BaseModel):
    """C13 -- store raw plus an E.164 attempt; never discard raw."""

    raw: str
    e164: str | None = None
    source_lines: list[int] = Field(default_factory=list)


class GPA(BaseModel):
    """C15 -- scales differ (4.0, 10, percent, UK class). Store raw plus the scale."""

    raw: str
    scale: Literal["4.0", "10", "percent", "uk_class", "unknown"] = "unknown"


# ------------------------------------------------------------------------------ sections


class Skill(BaseModel):
    name: str
    category: SkillCategory  # closed enum, hard rule #7
    source: Literal["skills_section", "inferred_from_experience", "inferred_from_project"]
    evidence: Span | None = None  # C9/C10 -- makes negated mentions reviewable
    source_lines: list[int]


class Role(BaseModel):
    company: str
    title: str
    location: str | None = None
    start: DateField
    end: DateField
    employment_type: Literal[
        "full_time", "contract", "internship", "freelance", "unknown"
    ] = "unknown"
    bullets: list[Span] = Field(default_factory=list)
    sub_roles: list["Role"] = Field(default_factory=list)  # C6 promotions, not flattened
    source_lines: list[int]


class Education(BaseModel):
    institution: str
    degree: str | None = None
    field: str | None = None
    location: str | None = None
    start: DateField | None = None
    end: DateField | None = None
    gpa: GPA | None = None
    details: list[Span] = Field(default_factory=list)
    source_lines: list[int]


class Project(BaseModel):
    name: str
    description: Span | None = None
    bullets: list[Span] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    url: str | None = None
    start: DateField | None = None
    end: DateField | None = None
    source_lines: list[int]


class OtherSection(BaseModel):
    """The catch-all. Anything the model cannot classify goes here -- never dropped."""

    heading: str
    lines: list[Span] = Field(default_factory=list)
    source_lines: list[int]


class Contact(BaseModel):
    name: Span | None = None
    name_confidence: Literal["high", "low"] = "high"  # C11 name vs first employer
    emails: list[Span] = Field(default_factory=list)  # C12 arrays, not scalars
    phones: list[PhoneField] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    location: Span | None = None
    source_lines: list[int] = Field(default_factory=list)


# -------------------------------------------------------------------------------- output


class Meta(BaseModel):
    """Pipeline-owned. The model never fills this in."""

    filename: str
    sha256: str
    file_type: Literal["pdf", "docx", "txt", "md"]
    pages: int
    total_lines: int
    language: str | None = None  # C16, the model's judgement, copied in by the pipeline
    warnings: list[ParseWarning] = Field(default_factory=list)
    dropped_lines: list[DroppedLine] = Field(default_factory=list)


class Coverage(BaseModel):
    total_lines: int
    non_blank_lines: int
    claimed_lines: int
    coverage_pct: float
    unassigned: list[int]
    flagged: list[tuple[int, str]]
    # A line counts as claimed if any object cites it, and containers cite their children's
    # lines, so `coverage_pct` cannot see span loss: emptying every span in the corpus left
    # it at 100% on 22 of 22 files (tests/test_metric_falsification.py). A line counts as
    # represented only if a verified span or a verified bare field (company, title, and the
    # rest of spans.BARE_FIELDS) reproduces it -- bare fields carry real content, so leaving
    # them out measured schema shape rather than content survival. Deleting every bullet
    # drops this by ~24 points; deleting every span drops it by ~47.
    represented_lines: int
    represented_pct: float
    claimed_not_represented: list[int]


class EqualEmployment(BaseModel):
    """Self-disclosed answers, keyed by `eeo.QUESTIONS` id. Collected, never parsed.

    Stored as id -> selected options so the question set stays the single source of truth
    in `app/eeo.py`; a field per question would duplicate it and drift.
    """

    answers: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("answers")
    @classmethod
    def _known_questions_and_choices(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for qid, picked in value.items():
            question = eeo.BY_ID.get(qid)
            if question is None:
                raise ValueError(f"unknown question: {qid}")
            if not question.multi and len(picked) > 1:
                raise ValueError(f"{qid} accepts one answer, got {len(picked)}")
            unknown = [p for p in picked if p not in question.options]
            if unknown:
                raise ValueError(f"{qid}: not an offered option: {unknown}")
        return value

    def selected(self, qid: str) -> list[str]:
        return self.answers.get(qid, [])


class Override(BaseModel):
    """One user-edited field. Carries no `source_lines` -- this text is not from the parse."""

    value: str
    edited_at: str  # UTC, ISO-8601


class ProfileStore(BaseModel):
    """The whole of `data/profile.json`.

    Every field defaults and unknown keys are ignored. That is the direct lesson from making
    `Coverage`'s fields required, which broke loading of every parse already on disk -- and it
    matters more here, because this file is read on the landing route rather than one page.

    `eeo` stays `| None` rather than defaulting to an empty `EqualEmployment`. The gate in
    `main.dashboard` needs three states: never asked, answered, and answered-declining-all. A
    default-empty instance collapses the first two, so the first upload -- which writes
    `active_sha` and therefore creates this file -- would silently skip the questions forever.
    """

    model_config = {"extra": "ignore"}

    version: int = 1
    active_sha: str | None = None
    eeo: EqualEmployment | None = None
    # sha -> json path into that parse -> the edit. Keyed by sha first so switching resumes
    # cannot reattach an edit meant for one document to a different one at the same path.
    overrides: dict[str, dict[str, Override]] = Field(default_factory=dict)


class ParsedResumeLLM(BaseModel):
    """The model-facing schema. No `coverage`, no `meta` -- those are ours."""

    language: str | None = None
    contact: Contact
    summary: Span | None = None
    experience: list[Role] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    certifications: list[Span] = Field(default_factory=list)
    publications: list[Span] = Field(default_factory=list)  # C17
    awards: list[Span] = Field(default_factory=list)
    languages: list[Span] = Field(default_factory=list)
    other_sections: list[OtherSection] = Field(default_factory=list)


class ParsedResume(ParsedResumeLLM):
    """What reaches disk and the UI."""

    meta: Meta
    coverage: Coverage


Role.model_rebuild()
