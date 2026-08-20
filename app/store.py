"""The single local profile file.

One user, so the profile belongs to the person rather than to a parse: equal-employment
answers, which resume is active, and any fields the user has edited. `data/profile.json` is
the whole store.

Every write is load-merge-write. A blind overwrite here deletes the other two sections --
that is not hypothetical, it is what `save_eeo` used to do, and answering the questions would
have wiped `active_sha` and every override (PLAN.md §6.5 P1).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app import jsonio
from app.models import EqualEmployment, Override, ProfileStore

PROFILE = Path(__file__).parent.parent / "data" / "profile.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate(raw: dict) -> dict:
    """The pre-ProfileStore file was `{"answers": {...}}` and nothing else."""
    if "version" not in raw and "answers" in raw:
        return {"eeo": {"answers": raw["answers"]}}
    return raw


def load_profile() -> ProfileStore:
    """Never raises. A missing, corrupt, or truncated file reads as an empty profile.

    The file is read on the landing route, so letting it raise would take the whole app down
    rather than one page. A bad file is left on disk untouched -- only a later successful save
    replaces it, so nothing is destroyed by merely looking. Validation errors are swallowed
    rather than surfaced because Pydantic echoes field values, and this file holds protected
    characteristics (P16).
    """
    if not PROFILE.exists():
        return ProfileStore()
    try:
        return ProfileStore.model_validate(
            _migrate(json.loads(PROFILE.read_text(encoding="utf-8")))
        )
    except (json.JSONDecodeError, ValidationError, OSError, TypeError):
        return ProfileStore()


# ponytail: last-writer-wins, no lock -- one local user, one process. Add a threading.Lock
# around load-modify-write if this ever serves more than one client.
def _save(profile: ProfileStore) -> None:
    jsonio.write_json(PROFILE, profile.model_dump())


def load_eeo() -> EqualEmployment | None:
    """None means the questions have never been answered -- not "answered with nothing"."""
    return load_profile().eeo


def save_eeo(answers: EqualEmployment) -> None:
    _save(load_profile().model_copy(update={"eeo": answers}))


def active_sha() -> str | None:
    return load_profile().active_sha


def set_active_sha(sha: str) -> None:
    _save(load_profile().model_copy(update={"active_sha": sha}))


def get_overrides(sha: str) -> dict[str, Override]:
    return load_profile().overrides.get(sha, {})


def set_overrides(sha: str, pairs: dict[str, str]) -> None:
    """Merge edits for one resume. Keys are json paths into that parse."""
    profile = load_profile()
    bucket = {
        **profile.overrides.get(sha, {}),
        **{key: Override(value=value, edited_at=_now()) for key, value in pairs.items()},
    }
    _save(profile.model_copy(update={"overrides": {**profile.overrides, sha: bucket}}))


def carry_overrides(
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
        return len(get_overrides(new_sha))
    old_values, new_values = old_values or {}, new_values or {}
    profile = load_profile()
    carried = {
        key: value
        for key, value in profile.overrides.get(old_sha, {}).items()
        if key in allowed and old_values.get(key) == new_values.get(key)
    }
    if not carried:
        return 0
    merged = {**profile.overrides.get(new_sha, {}), **carried}
    _save(profile.model_copy(update={"overrides": {**profile.overrides, new_sha: merged}}))
    return len(carried)
