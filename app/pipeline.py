"""Stage orchestration: bytes -> lines -> structure -> verified parse.

Lives here rather than in `main.py` so the CLI and the web app run byte-identical code.
PLAN.md §3 budgets `main.py` at four routes and ~80 lines; this is what keeps it there.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.extract.dispatch import extract_lines, sha256
from app.jsonio import write_json as _write
from app.extract.errors import ExtractionError
from app.models import Line, Meta, ParsedResume
from app.structure.client import StructuringError, structure
from app.structure.links import label_for
from app.verify import coverage, spans

# Each parse is one API call, so the batch is I/O-bound and threads are the right tool.
# Conservative default: enough to cut a 20-file run to a couple of minutes, low enough that
# a burst does not trip rate limiting. Each worker holds one in-flight request.
DEFAULT_WORKERS = int(os.getenv("VERSO_WORKERS", "6"))

DATA = Path(__file__).parent.parent / "data"
UPLOADS = DATA / "uploads"
PARSES = DATA / "parses"
LINES = DATA / "lines"





def run_extract(data: bytes, filename: str, persist: bool = True) -> dict:
    """Stage 1-3. The inspectable checkpoint PLAN.md calls non-negotiable."""
    lines, kind, pages, warnings, dropped = extract_lines(data, filename)
    digest = sha256(data)
    payload = {
        "sha256": digest,
        "filename": filename,
        "file_type": kind,
        "pages": pages,
        "warnings": [w.model_dump() for w in warnings],
        "dropped_lines": [d.model_dump() for d in dropped],
        "lines": [line.model_dump() for line in lines],
    }
    if persist:
        UPLOADS.mkdir(parents=True, exist_ok=True)
        (UPLOADS / f"{digest}{Path(filename).suffix}").write_bytes(data)
        _write(LINES / f"{digest}.json", payload)
    return payload


def run_structure(checkpoint: dict) -> dict:
    """Stage 4. The only stage that involves a model (hard rule #1)."""
    lines = [Line.model_validate(item) for item in checkpoint["lines"]]
    return structure(lines).model_dump()


def run_verify(checkpoint: dict, parsed: dict) -> ParsedResume:
    """Stage 5. Span matching plus coverage -- the guarantee, computed in Python."""
    lines = [Line.model_validate(item) for item in checkpoint["lines"]]
    dropped = [d for d in checkpoint["dropped_lines"]]
    from app.models import DroppedLine

    dropped_models = [DroppedLine.model_validate(d) for d in dropped]

    parsed = _flatten_foreign_subroles(parsed)
    parsed = _label_links(parsed, lines)
    verified, flagged = spans.verify(parsed, lines)
    # Plain-string fields (company, title, institution) carry no spans of their own.
    flagged += spans.verify_bare_fields(verified, {line.no: line for line in lines})
    report = coverage.compute(verified, lines, dropped_models, flagged)

    meta = Meta(
        filename=checkpoint["filename"],
        sha256=checkpoint["sha256"],
        file_type=checkpoint["file_type"],
        pages=checkpoint["pages"],
        total_lines=len(lines),
        language=verified.get("language"),
        warnings=checkpoint["warnings"],
        dropped_lines=dropped_models,
    )
    return ParsedResume.model_validate({**verified, "meta": meta, "coverage": report})


def _flatten_foreign_subroles(parsed: dict) -> dict:
    """C6 -- `sub_roles` nest promotions within ONE employer. Promote anything else.

    Observed on the corpus: three separate companies chained as nested sub_roles
    (Bluepeak under Halcyon under Northlight), which reads as one long promotion track at
    a single employer. Coverage cannot catch this -- every line was still claimed -- so it
    is corrected here rather than left to the prompt. Comparing two company names needs no
    judgement, so it belongs in Python.
    """
    roles = parsed.get("experience") or []
    if not roles:
        return parsed

    def split(role: dict) -> list[dict]:
        """Return [role, *promoted], with the role keeping only same-employer sub_roles."""
        company = (role.get("company") or "").strip().casefold()
        kept, promoted = [], []
        for sub in role.get("sub_roles") or []:
            branch = split(sub)
            if (sub.get("company") or "").strip().casefold() == company:
                kept.append(branch[0])
                promoted += branch[1:]
            else:
                promoted += branch
        return [{**role, "sub_roles": kept}, *promoted]

    flattened: list[dict] = []
    for role in roles:
        flattened += split(role)
    return {**parsed, "experience": flattened}


def _label_links(parsed: dict, lines: list[Line]) -> dict:
    """Name each contact link from its URL. Deterministic, so the model is not asked."""
    contact = parsed.get("contact") or {}
    links = contact.get("links") or []
    if not links:
        return parsed
    index = {line.no: line.text for line in lines}
    labelled = [
        {
            **link,
            "label": label_for(
                link.get("text", ""),
                " ".join(index.get(n, "") for n in link.get("source_lines") or []),
            ),
        }
        for link in links
    ]
    return {**parsed, "contact": {**contact, "links": labelled}}


def parse_document(data: bytes, filename: str, persist: bool = True) -> ParsedResume:
    """The whole pipeline."""
    checkpoint = run_extract(data, filename, persist)
    parsed = run_structure(checkpoint)
    result = run_verify(checkpoint, parsed)
    if persist:
        _write(PARSES / f"{checkpoint['sha256']}.json", result.model_dump())
    return result


def cached_parse(digest: str) -> ParsedResume | None:
    path = PARSES / f"{digest}.json"
    if not path.exists():
        return None
    return ParsedResume.model_validate(json.loads(path.read_text(encoding="utf-8")))


def cached_lines(digest: str) -> dict | None:
    path = LINES / f"{digest}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def parse_many(
    paths: list[Path], workers: int | None = None, persist: bool = True
) -> tuple[dict[str, ParsedResume], dict[str, Exception]]:
    """Parse documents concurrently. Returns (results, failures), both keyed by filename.

    One file's failure never kills the batch -- a rejected or unparsable resume is recorded
    and the rest continue, which is what makes the coverage table useful. Each parse writes
    to its own sha-keyed path, so there is no write contention between workers.
    """
    results: dict[str, ParsedResume] = {}
    failures: dict[str, Exception] = {}

    def one(path: Path) -> ParsedResume:
        return parse_document(path.read_bytes(), path.name, persist)

    with ThreadPoolExecutor(max_workers=workers or DEFAULT_WORKERS) as pool:
        futures = {pool.submit(one, path): path for path in paths}
        for future in as_completed(futures):
            name = futures[future].name
            try:
                results[name] = future.result()
            except (ExtractionError, StructuringError) as exc:
                failures[name] = exc

    return results, failures
