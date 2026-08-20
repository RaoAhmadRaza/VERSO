"""One folder per person: `people/{person_id}/profile.json`.

A person owns equal-employment answers, which resume is active, and any fields they have
edited. `data/uploads|lines|parses` stay SHARED and sha256-keyed -- two people uploading the
same PDF get the same parse, which is correct. The consequence that governs deletion:
removing a person must never touch anything under `data/`, because another person may point
at the same sha.

Every write is load-merge-write. A blind overwrite deletes the other sections -- that is not
hypothetical, it is what `save_eeo` used to do, and answering the questions would have wiped
`active_sha` and every override (PLAN.md §6.5 P1).

`person_id` is a security boundary, not a formatting preference: it comes from user input and
becomes a filesystem path. `_person_dir` validates it on every read and every write.
"""

import json
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app import jsonio
from app.models import EqualEmployment, Override, ProfileStore
from app.pipeline import PARSES

# The single seam. `active_person.json` and the legacy file both derive from PEOPLE.parent,
# so redirecting this one constant redirects all three together -- one thing for tests to
# patch rather than three that can drift apart.
PEOPLE = Path(__file__).parent.parent / "people"

# Whitelist, not blacklist. One expression absorbs every attack rather than one branch each.
_PERSON_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SLUG_SPLIT_RE = re.compile(r"[^a-z0-9]+")


class InvalidPersonId(ValueError):
    """A bad person_id. Deliberately RAISES, unlike a corrupt file.

    "Never raises" is a promise about file *content*. An unusable id is a caller error at a
    trust boundary -- returning an empty profile for "../../etc" would make a traversal
    attempt indistinguishable from a brand-new person, which is the fail-open version of the
    bug this validation exists to prevent.
    """


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _data_dir() -> Path:
    return PEOPLE.parent / "data"


def _active_file() -> Path:
    return _data_dir() / "active_person.json"


def _legacy_file() -> Path:
    return _data_dir() / "profile.json"


def _person_dir(person_id: str) -> Path:
    """Validate `person_id` and return its directory. Called on reads as well as writes.

    The regex is the real defence:
      * "/", "\\", "..", ".", null bytes are simply not in the class, so "../../etc" never
        reaches a Path constructor to be resolved away.
      * Non-ASCII is refused, so a Cyrillic "a" cannot homoglyph an ASCII one.
      * Uppercase is refused, so "Alice" and "alice" cannot become two people that collide
        into one directory on a case-insensitive filesystem.

    `resolve()` + `is_relative_to` is defence in depth for what a name cannot express: a
    symlink inside people/ whose name is valid but which points elsewhere on disk.
    """
    if not _PERSON_ID_RE.fullmatch(person_id or ""):
        raise InvalidPersonId(person_id)
    base = PEOPLE.resolve()
    candidate = (PEOPLE / person_id).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise InvalidPersonId(person_id)
    return candidate


def _profile_file(person_id: str) -> Path:
    return _person_dir(person_id) / "profile.json"


def _slugify(display_name: str) -> str:
    """A dot-only name cannot survive this: dots collapse to "-", which is then stripped."""
    ascii_only = (
        unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode("ascii")
    )
    return _SLUG_SPLIT_RE.sub("-", ascii_only.lower()).strip("-")


def _migrate(raw: dict) -> dict:
    """The pre-ProfileStore file was `{"answers": {...}}` and nothing else."""
    if "version" not in raw and "answers" in raw:
        return {"eeo": {"answers": raw["answers"]}}
    return raw


def _ensure_migrated() -> None:
    """Move the single-profile layout into people/default/. Lazy, idempotent, non-destructive.

    Called from `list_people` and `active_person`, not at import and not from a startup hook:
    importing `app.main` would migrate the real data/ before any test fixture could redirect
    the paths, and the suite builds `TestClient(app)` without a `with` block, so lifespan
    hooks are not guaranteed to fire.

    Idempotent by construction rather than by bookkeeping -- after a successful run the legacy
    file is renamed so the first guard returns, and if the process died midway the second
    guard (people/ non-empty) returns.
    """
    legacy = _legacy_file()
    if not legacy.exists():
        return
    if PEOPLE.exists() and any(PEOPLE.iterdir()):
        return
    try:
        raw = _migrate(json.loads(legacy.read_text(encoding="utf-8")))
        migrated = ProfileStore.model_validate({**raw, "display_name": "Me"})
    except (json.JSONDecodeError, ValidationError, OSError, TypeError, AttributeError):
        return  # nothing safe to carry; the legacy file is left exactly as it was
    _save("default", migrated)
    # Prove the new file reloads before the old one is touched. A migration that "succeeds"
    # but writes something unreadable would otherwise leave neither file trustworthy.
    if load_profile("default") != migrated:
        return
    set_active_person("default")
    legacy.replace(legacy.with_name(legacy.name + ".migrated"))


# --------------------------------------------------------------------- one person's profile


def load_profile(person_id: str) -> ProfileStore:
    """Never raises on file content: missing, corrupt or legacy reads as an empty profile.

    The file is read on the landing route, so letting it raise would take the whole app down
    rather than one page. A bad file is left on disk untouched -- only a later successful save
    replaces it, so nothing is destroyed by merely looking. Validation errors are swallowed
    rather than surfaced because Pydantic echoes field values, and this file holds protected
    characteristics (P16).
    """
    path = _profile_file(person_id)
    if not path.exists():
        return ProfileStore()
    try:
        return ProfileStore.model_validate(_migrate(json.loads(path.read_text(encoding="utf-8"))))
    except (json.JSONDecodeError, ValidationError, OSError, TypeError, AttributeError):
        return ProfileStore()


# ponytail: last-writer-wins, no lock -- one machine, one operator at a time. Add a
# threading.Lock around load-modify-write if this ever serves more than one client.
def _save(person_id: str, profile: ProfileStore) -> None:
    jsonio.write_json(_profile_file(person_id), profile.model_dump())


def load_eeo(person_id: str) -> EqualEmployment | None:
    """None means the questions have never been answered -- not "answered with nothing"."""
    return load_profile(person_id).eeo


def save_eeo(person_id: str, answers: EqualEmployment) -> None:
    _save(person_id, load_profile(person_id).model_copy(update={"eeo": answers}))


def active_sha(person_id: str) -> str | None:
    return load_profile(person_id).active_sha


def set_active_sha(person_id: str, sha: str) -> None:
    _save(person_id, load_profile(person_id).model_copy(update={"active_sha": sha}))


def get_overrides(person_id: str, sha: str) -> dict[str, Override]:
    return load_profile(person_id).overrides.get(sha, {})


def set_overrides(person_id: str, sha: str, pairs: dict[str, str]) -> None:
    """Merge edits for one resume. Keys are json paths into that parse."""
    profile = load_profile(person_id)
    bucket = {
        **profile.overrides.get(sha, {}),
        **{key: Override(value=value, edited_at=_now()) for key, value in pairs.items()},
    }
    _save(person_id, profile.model_copy(update={"overrides": {**profile.overrides, sha: bucket}}))


def carry_overrides(
    person_id: str,
    old_sha: str,
    new_sha: str,
    allowed: set[str],
    old_values: dict[str, str] | None = None,
    new_values: dict[str, str] | None = None,
) -> int:
    """Move edits onto an updated resume. Keep only the ones still meaningful.

    Two ways an edit stops being meaningful:

    * The path is gone. `experience.9.title` in the old resume is a different job from
      `experience.9.title` in the new one, so an edit with nowhere to land is dropped rather
      than reattached to whatever now sits there.
    * The path survived but the resume changed that field. The edit was a correction about
      text that no longer exists, so carrying it would hide the new document's real content
      behind a stale one. The newer document wins.

    Returns how many carried. The old resume keeps its own edits -- it is still viewable by
    its own sha, and an update is not a deletion.
    """
    if old_sha == new_sha:
        return len(get_overrides(person_id, new_sha))
    old_values, new_values = old_values or {}, new_values or {}
    profile = load_profile(person_id)
    carried = {
        key: value
        for key, value in profile.overrides.get(old_sha, {}).items()
        if key in allowed and old_values.get(key) == new_values.get(key)
    }
    if not carried:
        return 0
    merged = {**profile.overrides.get(new_sha, {}), **carried}
    _save(
        person_id,
        profile.model_copy(update={"overrides": {**profile.overrides, new_sha: merged}}),
    )
    return len(carried)


# ------------------------------------------------------------------------------- the people


def person_exists(person_id: str) -> bool:
    """False for an unusable id as well as an unknown one -- callers get one question."""
    try:
        return _person_dir(person_id).is_dir()
    except InvalidPersonId:
        return False


def list_people() -> list[tuple[str, str]]:
    """(person_id, display_name), sorted by display name."""
    _ensure_migrated()
    if not PEOPLE.exists():
        return []
    people = [
        (entry.name, load_profile(entry.name).display_name)
        for entry in PEOPLE.iterdir()
        if entry.is_dir() and _PERSON_ID_RE.fullmatch(entry.name)
    ]
    return sorted(people, key=lambda pair: (pair[1].lower(), pair[0]))


def active_person() -> str | None:
    """Who the app defaults to. None means nobody has been chosen yet.

    Self-heals: a pointer at someone since deleted reads as None rather than 404ing the
    landing page (P34).
    """
    _ensure_migrated()
    path = _active_file()
    if not path.exists():
        return None
    try:
        person_id = json.loads(path.read_text(encoding="utf-8")).get("person_id")
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return None
    return person_id if isinstance(person_id, str) and person_exists(person_id) else None


def set_active_person(person_id: str) -> None:
    _person_dir(person_id)  # validate before writing the pointer
    jsonio.write_json(_active_file(), {"person_id": person_id})


def clear_active_person() -> None:
    _active_file().unlink(missing_ok=True)


def create_person(display_name: str) -> str:
    """Slug the name, resolve collisions with -2, -3, and write an empty profile."""
    base = _slugify(display_name)
    if not base:
        raise InvalidPersonId(display_name)
    taken = {person_id for person_id, _ in list_people()}
    person_id, suffix = base, 2
    while person_id in taken:
        person_id, suffix = f"{base}-{suffix}", suffix + 1
    _save(person_id, ProfileStore(display_name=display_name))
    return person_id


def delete_person(person_id: str) -> int:
    """Remove people/<id> ONLY, and report what is now referenced by nobody.

    Nothing under data/uploads|lines|parses is touched: those are shared and sha256-keyed, so
    another person may point at the same parse. The return value is a count, not a deletion --
    the same instinct as `dropped_lines`, surface it rather than act on it.
    """
    if person_exists(person_id):
        shutil.rmtree(_person_dir(person_id))
    if active_person() is None:
        clear_active_person()
    referenced: set[str] = set()
    for remaining, _ in list_people():
        profile = load_profile(remaining)
        if profile.active_sha:
            referenced.add(profile.active_sha)
        referenced |= set(profile.overrides)
    on_disk = {p.stem for p in PARSES.glob("*.json")} if PARSES.exists() else set()
    return len(on_disk - referenced)
