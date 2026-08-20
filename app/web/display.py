"""Flatten a `ParsedResume` into rows the template can render.

Kept out of `main.py` so the routes stay at the four PLAN.md §3 allows, and out of the
template so the linking contract -- every displayed value carries the line numbers it came
from -- is expressed once, in Python, where it can be read.
"""

from dataclasses import dataclass, field

from app.models import DateField, ParsedResume, Span


@dataclass
class Row:
    label: str
    value: str
    lines: list[int] = field(default_factory=list)
    flag: str = "ok"


@dataclass
class Item:
    title: str = ""
    subtitle: str = ""
    rows: list[Row] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)


@dataclass
class Section:
    title: str
    items: list[Item] = field(default_factory=list)


def _span_row(label: str, span: Span | None) -> Row | None:
    if span is None:
        return None
    return Row(label, span.text, span.source_lines, span.flag)


def _date(value: DateField | None) -> str:
    """Always show the raw string -- hard rule #6 keeps it, so the UI shows it."""
    if value is None:
        return ""
    return "Present" if value.is_present and not value.raw else value.raw


def _range(start: DateField | None, end: DateField | None) -> str:
    left, right = _date(start), _date(end)
    return f"{left} – {right}".strip(" –") if (left or right) else ""


def _contact(parse: ParsedResume) -> Section:
    contact = parse.contact
    rows: list[Row] = []
    name = _span_row("Name", contact.name)
    if name:
        if contact.name_confidence == "low":
            name.label = "Name (low confidence)"  # C11
        rows.append(name)
    rows += [r for r in (_span_row("Email", e) for e in contact.emails) if r]
    rows += [Row("Phone", p.raw, p.source_lines) for p in contact.phones]  # C13 raw
    rows += [Row(link.label, link.text, link.source_lines, link.flag)
             for link in contact.links]
    location = _span_row("Location", contact.location)
    if location:
        rows.append(location)
    return Section("Contact", [Item(rows=rows, lines=contact.source_lines)] if rows else [])


def _roles(roles, depth: int = 0) -> list[Item]:
    items: list[Item] = []
    for role in roles:
        rows = [Row("Dates", _range(role.start, role.end), role.source_lines)]
        if role.location:
            rows.append(Row("Location", role.location, role.source_lines))
        if role.employment_type != "unknown":
            rows.append(Row("Type", role.employment_type.replace("_", " "), role.source_lines))
        rows += [
            Row("", bullet.text, bullet.source_lines, bullet.flag) for bullet in role.bullets
        ]
        items.append(
            Item(
                title=("— " * depth) + role.title,
                subtitle=role.company,
                rows=rows,
                lines=role.source_lines,
            )
        )
        items += _roles(role.sub_roles, depth + 1)  # C6 promotion chains stay nested
    return items


def _education(entries) -> list[Item]:
    items = []
    for entry in entries:
        rows = [Row("Dates", _range(entry.start, entry.end), entry.source_lines)]
        if entry.degree:
            rows.append(Row("Degree", entry.degree, entry.source_lines))
        if entry.field:
            rows.append(Row("Field", entry.field, entry.source_lines))
        if entry.gpa:
            rows.append(Row("GPA", f"{entry.gpa.raw} ({entry.gpa.scale})", entry.source_lines))
        rows += [Row("", d.text, d.source_lines, d.flag) for d in entry.details]
        items.append(Item(title=entry.institution, rows=rows, lines=entry.source_lines))
    return items


def _skills(skills) -> list[Item]:
    """Grouped by the closed taxonomy, so the same skill always lands in the same bucket."""
    buckets: dict[str, list] = {}
    for skill in skills:
        buckets.setdefault(skill.category.value, []).append(skill)
    items = []
    for category, group in sorted(buckets.items()):
        rows = [
            Row(
                "inferred" if skill.source != "skills_section" else "",
                skill.name,
                skill.source_lines,
                skill.evidence.flag if skill.evidence else "ok",
            )
            for skill in group
        ]
        items.append(Item(title=category.replace("_", " "), rows=rows))
    return items


def _projects(projects) -> list[Item]:
    items = []
    for project in projects:
        rows = []
        if project.description:
            rows.append(Row("", project.description.text, project.description.source_lines,
                            project.description.flag))
        rows += [Row("", b.text, b.source_lines, b.flag) for b in project.bullets]
        if project.technologies:
            rows.append(Row("Tech", ", ".join(project.technologies), project.source_lines))
        items.append(Item(title=project.name, rows=rows, lines=project.source_lines))
    return items


def _span_section(title: str, spans: list[Span]) -> Section:
    return Section(title, [Item(rows=[Row("", s.text, s.source_lines, s.flag) for s in spans])]
                   if spans else [])


def sections(parse: ParsedResume) -> list[Section]:
    """Every section, in reading order. Empty ones are dropped by the template."""
    out = [_contact(parse)]
    summary = _span_row("", parse.summary)
    if summary:
        out.append(Section("Summary", [Item(rows=[summary])]))
    out += [
        Section("Experience", _roles(parse.experience)),
        Section("Education", _education(parse.education)),
        Section("Skills", _skills(parse.skills)),
        Section("Projects", _projects(parse.projects)),
        _span_section("Certifications", parse.certifications),
        _span_section("Publications", parse.publications),
        _span_section("Awards", parse.awards),
        _span_section("Languages", parse.languages),
    ]
    for other in parse.other_sections:  # the catch-all -- nothing is dropped
        out.append(
            Section(
                other.heading or "Unclassified",
                [Item(rows=[Row("", s.text, s.source_lines, s.flag) for s in other.lines],
                      lines=other.source_lines)],
            )
        )
    return [s for s in out if s.items and any(i.rows or i.title for i in s.items)]


def gutter(parse: ParsedResume, line_count: int) -> list[str]:
    """One state per document line, for the coverage strip (PLAN.md §8)."""
    unassigned = set(parse.coverage.unassigned)
    flagged = {n for n, _ in parse.coverage.flagged}
    return [
        "flag" if n in flagged else "unassigned" if n in unassigned else "claimed"
        for n in range(1, line_count + 1)
    ]
