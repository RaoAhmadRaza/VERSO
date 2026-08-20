"""Atomic JSON writes, shared by `app.pipeline` and `app.store`.

A plain `write_text` leaves a truncated file if the process dies mid-write. `profile.json`
is read on every request once it drives the landing route, so a torn read there breaks the
whole app rather than a single parse.

Write to a sibling temp file, then rename: `Path.replace` is atomic on POSIX.
"""

import json
from pathlib import Path


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
