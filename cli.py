"""VERSO command line -- every pipeline stage runnable standalone against a fixture.

    python cli.py extract   fixtures/resumes/x.pdf   # -> lines.json
    python cli.py structure fixtures/resumes/x.pdf   # -> parse.json
    python cli.py verify    fixtures/resumes/x.pdf   # -> coverage report
    python cli.py all       fixtures/resumes/        # batch, prints a coverage table

A stage that can only run inside a web request is a stage that cannot be debugged.
"""

import json
import statistics
import sys
from pathlib import Path

from dotenv import load_dotenv

# Must precede the app imports; see the note in app/main.py.
load_dotenv()

from app import pipeline  # noqa: E402
from app.extract.errors import ExtractionError  # noqa: E402
from app.structure.client import StructuringError  # noqa: E402

VERBS = ("extract", "structure", "verify", "all")


def _targets(path: Path) -> list[Path]:
    if path.is_dir():
        return [p for p in sorted(path.iterdir()) if p.is_file() and not p.name.startswith(".")]
    return [path]


def _extract(path: Path) -> dict:
    return pipeline.run_extract(path.read_bytes(), path.name)


def cmd_extract(path: Path) -> None:
    for target in _targets(path):
        try:
            checkpoint = _extract(target)
            print(json.dumps(checkpoint, indent=2, ensure_ascii=False))
        except ExtractionError as exc:
            print(f"{target.name}: [{exc.code}] {exc.message}", file=sys.stderr)


def cmd_structure(path: Path) -> None:
    for target in _targets(path):
        try:
            checkpoint = _extract(target)
            print(json.dumps(pipeline.run_structure(checkpoint), indent=2, ensure_ascii=False))
        except (ExtractionError, StructuringError) as exc:
            print(f"{target.name}: {exc}", file=sys.stderr)


def cmd_verify(path: Path) -> None:
    for target in _targets(path):
        try:
            result = pipeline.parse_document(target.read_bytes(), target.name)
        except (ExtractionError, StructuringError) as exc:
            print(f"{target.name}: {exc}", file=sys.stderr)
            continue
        report = result.coverage
        print(f"{target.name}")
        print(f"  coverage     {report.coverage_pct:.1f}%")
        print(f"  represented  {report.represented_pct:.1f}%")
        print(f"  claimed      {report.claimed_lines}/{report.non_blank_lines} non-blank lines")
        print(f"  unassigned   {report.unassigned}")
        print(f"  claimed !rep {report.claimed_not_represented}")
        print(f"  flagged      {report.flagged}")


def cmd_all(path: Path) -> None:
    """Batch. Prints the coverage table the ship gate is read from."""
    from app.structure.client import active_model

    targets = _targets(path)
    if not targets:
        print(f"  {path} is empty. Put resumes there, then run this again.")
        return

    print(f"  provider: {active_model()}   workers: {pipeline.DEFAULT_WORKERS}\n")

    results, failures = pipeline.parse_many(targets)

    rows: list[tuple[str, str, str]] = []
    for target in targets:  # iterate targets, not results, so order is deterministic
        name = target.name
        if name in results:
            report = results[name].coverage
            rows.append((
                name,
                f"{report.coverage_pct:6.1f}%{report.represented_pct:8.1f}%",
                f"{len(report.unassigned):3d} unassigned  "
                f"{len(report.claimed_not_represented):3d} claimed!rep  "
                f"{len(report.flagged):3d} flagged",
            ))
        else:
            exc = failures.get(name)
            code = getattr(exc, "code", "error")
            rows.append((name, "  --            ", f"{code}: {str(exc)[:70]}"))

    width = max(len(name) for name, _, _ in rows)
    print(f"  {'':<{width}}  {'cov':>7}{'repr':>9}")
    for name, score, note in rows:
        print(f"  {name:<{width}}  {score}  {note}")

    scores = [r.coverage.coverage_pct for r in results.values()]
    represented = [r.coverage.represented_pct for r in results.values()]
    if scores:
        print(f"\n  parsed {len(scores)} of {len(targets)} files")
        print(f"  mean coverage    {statistics.mean(scores):.2f}%   min {min(scores):.1f}%")
        # No gate on this yet -- the number is being measured before a threshold is set.
        print(
            f"  mean represented {statistics.mean(represented):.2f}%"
            f"   min {min(represented):.1f}%"
        )
        print(f"  mean gap         {statistics.mean(scores) - statistics.mean(represented):.2f} pts")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in VERBS:
        print(f"usage: python cli.py [{'|'.join(VERBS)}] <file-or-dir>", file=sys.stderr)
        return 2
    verb, target = argv[1], Path(argv[2])
    if not target.exists():
        print(f"no such path: {target}", file=sys.stderr)
        return 2
    {"extract": cmd_extract, "structure": cmd_structure, "verify": cmd_verify, "all": cmd_all}[
        verb
    ](target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
