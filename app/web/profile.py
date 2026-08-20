"""The parse, arranged as a profile (Personal / Education / Work / Skills).

`display.py` renders the same parse as a proof sheet -- every value beside the lines it
came from. This is the other reading: the resume as the user thinks of it. Provenance is
still carried on every entry so the proof sheet stays one click away.

User edits are merged in HERE, at render time, and never written back into the parse. The
parse is the verifiable record: `app.verify` computes coverage against it, and `/proof` reads
it directly. So the profile and the proof sheet are allowed to disagree -- that disagreement
is exactly what "edited" means, which is why an edited field must render marked.
"""

from dataclasses import dataclass, field

from app.models import DateField, Override, ParsedResume, Role

# Scalar fields only. Bullets, skills, emails, phones and links are list-shaped and need
# add/remove/reorder UI of their own.
# ponytail: scalar fields only, add list editing when asked.
ENTRY_FIELDS = ("title", "subtitle", "start", "end", "detail")
PERSONAL_FIELDS = ("name", "location", "summary")


@dataclass
class Entry:
    """One timeline item: a role or a qualification."""

    title: str
    subtitle: str = ""
    start: str = ""
    end: str = ""
    detail: str = ""
    bullets: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    depth: int = 0  # C6: a promotion nested under its employer
    path: str = ""  # json path into the parse, the override key prefix
    overridden: frozenset[str] = frozenset()


def _when(value: DateField | None) -> str:
    if value is None:
        return ""
    if value.is_present:
        return "Present"
    if value.year and value.month:
        return f"{value.year}-{value.month:02d}"
    if value.year:
        return str(value.year)
    return value.raw  # C3: never lose the raw string just because it did not parse


def _resolve(path: str, raw: dict[str, str], overrides: dict[str, Override]) -> tuple[dict, frozenset]:
    """Apply any overrides for this object, reporting which fields were edited."""
    out, edited = {}, set()
    for name, value in raw.items():
        override = overrides.get(f"{path}.{name}")
        out[name] = override.value if override else value
        if override:
            edited.add(name)
    return out, frozenset(edited)


def _role(role: Role, path: str, overrides: dict[str, Override], depth: int = 0) -> list[Entry]:
    raw = {
        "title": role.company,
        "subtitle": role.title,
        "start": _when(role.start),
        "end": _when(role.end),
        "detail": role.location or "",
    }
    resolved, edited = _resolve(path, raw, overrides)
    entry = Entry(
        **resolved,
        bullets=[b.text for b in role.bullets],
        lines=role.source_lines,
        depth=depth,
        path=path,
        overridden=edited,
    )
    # Nested sub_roles keep the PARSE path, not the flattened display position -- `experience`
    # returns promotions as extra rows, so display index and parse index diverge after C6.
    nested = [
        entry
        for index, sub in enumerate(role.sub_roles)
        for entry in _role(sub, f"{path}.sub_roles.{index}", overrides, depth + 1)
    ]
    return [entry, *nested]


def personal(parse: ParsedResume, overrides: dict[str, Override] | None = None) -> dict:
    overrides = overrides or {}
    contact = parse.contact
    raw = {
        "name": contact.name.text if contact.name else "",
        "location": contact.location.text if contact.location else "",
        "summary": parse.summary.text if parse.summary else "",
    }
    resolved, edited = _resolve("personal", raw, overrides)
    return {
        **resolved,
        "name_confidence": contact.name_confidence,  # C11
        "emails": [e.text for e in contact.emails],
        "phones": [p.raw for p in contact.phones],  # C13: raw, never the E.164 guess
        "links": [(link.label, link.text) for link in contact.links],
        "overridden": edited,
    }


def education(parse: ParsedResume, overrides: dict[str, Override] | None = None) -> list[Entry]:
    overrides = overrides or {}
    out = []
    for index, item in enumerate(parse.education):
        path = f"education.{index}"
        raw = {
            "title": item.institution,
            "subtitle": ", ".join(p for p in (item.degree, item.field) if p),
            "start": _when(item.start),
            "end": _when(item.end),
            "detail": item.gpa.raw if item.gpa else "",  # C15: raw scale kept
        }
        resolved, edited = _resolve(path, raw, overrides)
        out.append(
            Entry(
                **resolved,
                bullets=[d.text for d in item.details],
                lines=item.source_lines,
                path=path,
                overridden=edited,
            )
        )
    return out


def experience(parse: ParsedResume, overrides: dict[str, Override] | None = None) -> list[Entry]:
    overrides = overrides or {}
    return [
        entry
        for index, role in enumerate(parse.experience)
        for entry in _role(role, f"experience.{index}", overrides)
    ]


def skills(parse: ParsedResume) -> list[tuple[str, list[str]]]:
    """Grouped by the closed taxonomy, in first-appearance order."""
    grouped: dict[str, list[str]] = {}
    for skill in parse.skills:
        grouped.setdefault(skill.category.value.replace("_", " "), []).append(skill.name)
    return list(grouped.items())


def overridable_paths(parse: ParsedResume) -> set[str]:
    """Every key `POST /profile/edit` will accept, derived from this parse.

    Edit keys arrive from a form, so they are untrusted: anything not in this set is refused
    rather than stored, or the store fills with keys that address nothing.
    """
    paths = {f"personal.{name}" for name in PERSONAL_FIELDS}
    for entry in education(parse) + experience(parse):
        paths |= {f"{entry.path}.{name}" for name in ENTRY_FIELDS}
    return paths


def field_values(parse: ParsedResume) -> dict[str, str]:
    """The parsed value at every overridable path, before any override is applied."""
    view = personal(parse)
    values = {f"personal.{name}": view[name] for name in PERSONAL_FIELDS}
    for entry in education(parse) + experience(parse):
        for name in ENTRY_FIELDS:
            values[f"{entry.path}.{name}"] = getattr(entry, name)
    return values
