"""Sort a folder of PDFs into text-layer / scanned / broken before using them as fixtures.

Downloaded resume corpora are usually a mix. A scan and a native export exercise completely
different halves of this pipeline, so knowing which you have matters more than the count.

    python fixtures/triage.py ~/Downloads/kaggle-resumes
    python fixtures/triage.py ~/Downloads/kaggle-resumes --copy    # copy usable ones in
"""

import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.extract import dispatch  # noqa: E402
from app.extract.errors import ExtractionError  # noqa: E402

DEST = Path(__file__).parent / "resumes"
USABLE = "text-layer"


def classify(path: Path) -> tuple[str, str]:
    """Returns (verdict, detail). Uses the real pipeline, so it agrees with the parser."""
    try:
        lines, kind, pages, warnings, _ = dispatch.extract_lines(path.read_bytes(), path.name)
    except ExtractionError as exc:
        return exc.code, exc.message
    words = sum(len(line.text.split()) for line in lines)
    return USABLE, f"{kind} · {pages}p · {len(lines)} lines · {words} words"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"usage: python {Path(__file__).name} <folder> [--copy]", file=sys.stderr)
        return 2
    source = Path(argv[1]).expanduser()
    if not source.is_dir():
        print(f"not a folder: {source}", file=sys.stderr)
        return 2
    copy = "--copy" in argv

    files = [p for p in sorted(source.rglob("*")) if p.is_file() and not p.name.startswith(".")]
    if not files:
        print(f"no files in {source}")
        return 1

    tally: Counter[str] = Counter()
    usable: list[Path] = []
    for path in files:
        verdict, detail = classify(path)
        tally[verdict] += 1
        if verdict == USABLE:
            usable.append(path)
        else:
            print(f"  {verdict:24s} {path.name}")

    print(f"\n  {len(files)} files scanned")
    for verdict, count in tally.most_common():
        print(f"    {verdict:24s} {count:5d}")

    if not copy:
        print(f"\n  {len(usable)} usable. Re-run with --copy to put them in {DEST}/")
        return 0

    DEST.mkdir(parents=True, exist_ok=True)
    for path in usable:
        target = DEST / path.name
        # Downloaded corpora repeat filenames across category folders.
        if target.exists():
            target = DEST / f"{path.parent.name}_{path.name}"
        shutil.copy2(path, target)
    print(f"\n  copied {len(usable)} into {DEST}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
