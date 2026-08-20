# VERSO — state of the project

Snapshot: **2026-08-20**. Feature 1 (resume parser + profile). One user, local only.

Every number here was verified directly, not taken from a summary. Where a check contradicted
an earlier claim, the verified value is the one recorded.

---

## At a glance

| | |
|---|---|
| Phases 0–6 | Built and exercised |
| Phase 7 (OCR) | Written, **never run** — 0 tests, disabled by default |
| Tests | **195 pass, 1 skipped** offline · 17 more in the network ship gate |
| Ship gate | Green — mean coverage 100.00%, 0 unflagged hallucinated spans |
| Production LOC | 3,628 (`app/` + `cli.py`) · tests 2,385 · frontend 931 |
| Version control | **Not a git repo** |
| Server | `uvicorn app.main:app --port 8000 --reload` |
| Provider | `gemini/gemini-3.5-flash-lite` (code default is `deepseek`) |

---

## The two metrics

`coverage_pct` reads **100.00% on all 27 parses**. That number cannot detect content loss and
never could: container `source_lines` already span their children's, so deleting every span in
the corpus still leaves it at 100.00% on 22 of 22 files. It is a citation-wellformedness check.
`tests/test_metric_falsification.py` pins this as a permanent, deliberate limitation.

`represented_pct` is the one that moves — a line counts only if a verified span **or** a verified
bare field reproduces it. Deleting all bullets drops it ~24 points; deleting every span drops it
~47.

| Group | Files | Coverage | Represented |
|---|---|---|---|
| All | 27 | 100.00% | **86.25%** |
| Real resumes (`Muhammad*`) | 4 | 100.00% | **90.80%** |
| Fixtures | 23 | 100.00% | **85.46%** |

Range 66.67% (`mei_lin_wong.pdf`) → 95.65% (`single_col_word.pdf`). The remaining ~14 points are
mostly **structural, not loss**: date lines, section headings and skill-group labels live in
plain-string fields that `spans.BARE_FIELDS` does not check.

**The ship gate still keys off `coverage_pct`.** No threshold has been set for `represented_pct`
— deliberately, pending a decision on whether `heading` and `DateField.raw` join `BARE_FIELDS`.
Per-file variance between runs reached ±20 points, so a per-file floor would flap; a mean gate
is far more stable.

---

## Architecture

Four stages, each with AI either mandatory or banned — the one load-bearing design decision:

| Stage | AI | Where |
|---|---|---|
| bytes → text | banned | `extract/dispatch.py` → `pdf.py` / `docx.py` / `plain.py` |
| text → numbered lines | banned | `extract/layout.py`, `extract/normalize.py` |
| lines → structure | **only** | `structure/client.py` (temp 0, validated retry, max 2) |
| structure → verified | banned | `verify/spans.py`, `verify/coverage.py` |

`parse_document` orchestrates; artefacts are sha256-keyed under `data/uploads|lines|parses/`.

### Module sizes

| Subsystem | Files | LOC | Largest |
|---|---|---|---|
| `extract/` | 8 | 1,188 | `pdf.py` 345 |
| `structure/` | 4 | 728 | `client.py` **497** |
| `verify/` | 2 | 260 | `spans.py` 178 |
| `web/` | 2 | 352 | `display.py` 182 |
| top-level | 6 | 964 | `models.py` 324 |

Nothing over the 800-line ceiling. Zero TODO/FIXME/HACK. Two deliberate `ponytail:` markers:
`store.py:54` (no lock, single user) and `web/profile.py:19` (scalar-only edits).

---

## Web surface — 8 routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Profile. No `sha` → resolves `active_sha`, else newest servable parse, else dropzone |
| GET | `/upload` | Dropzone. Retitled "Update your resume" when a profile exists |
| GET | `/proof` | Proof sheet — source pane, coverage gutter, bidirectional hover |
| GET/POST | `/eeo` | Equal-employment questions (10, every one declinable) |
| POST | `/profile/edit` | Stores edits as overrides; keys validated against the parse |
| POST | `/api/parse` | Upload → parse → sets `active_sha` on success only |
| GET | `/api/parse/{sha}`, `/api/lines/{sha}` | Cached parse / numbered lines |

PLAN §3 budgeted four; the profile view took it to eight. Recorded in `main.py`'s docstring.

### First-run flow

```
/upload → POST /api/parse → /?sha=… → 303 /eeo?sha=… → POST → 303 /?sha=… → profile
```

Once a profile exists the rail drops "Resume" entirely — one profile, so there is no second
destination. Re-uploading updates it in place, reached from "Update resume" on the profile.

### Persistence

`data/profile.json` — `version`, `active_sha`, `eeo`, `overrides{sha → json path → edit}`.
Every write is load-merge-write, written atomically via tmp+rename. Loading never raises: a
missing, corrupt, or legacy file reads as an empty profile and is left untouched on disk.

**Edits never touch the parse.** Overrides are applied at render time in `web/profile.py`;
`pipeline.py` and `verify/` never import the store, so coverage is always computed against what
the model produced. Edited fields render marked — showing user-typed text as if extracted is the
exact failure this project exists to prevent.

On resume update, an edit carries only if its path still exists **and** the resume has not itself
changed that field. A correction about text that no longer exists is dropped; the newer document
wins.

---

## Edge-case register

| Family | Defined | Tested | Gap |
|---|---|---|---|
| L — layout | 9 | 9 | — |
| E — encoding | 11 | 11 | — |
| F — file-level | 10 | 10 | — |
| C — content semantics | 19 | **9** | **10 untested** |
| P — persistence | 27 | **25** | P6, P10 |

Untested C cases: **C1, C2, C4, C7, C8, C11, C14, C16, C17, C19.** All are LLM-judgement calls,
which is why they were skipped — they need golden-set assertions or crafted fixtures rather than
unit tests.

---

## Open items, ranked

### 1. Skill provenance drift on real PDFs — C19 and worse

Two real resumes carry validation flags: `Resume_ATS.pdf` **46 × unsourced_name**,
`Resume(1).pdf` **7 ×**. Underneath that, on `ATS.pdf`, **189 of 258 skill names cite the wrong
line** (off by −1 near the top, drifting to −6 down the page) and **7 appear nowhere in the
document at all**.

Trigger is line-wrapped skill lists in PDFs. The DOCX with the *same 258 skills* has **zero**
drift, because each group is one unwrapped table-cell paragraph. That is the mechanism, isolated
by a natural control.

Neither headline metric sees it — the wrong citations still land on real, nearby lines.
`coverage_pct` reads 100.0%, `represented_pct` 92.4%.

Related: `coverage.flagged` is `sorted(set(...))`, so 196 distinct failures collapse to 46
reported flags — severity is under-reported ~4×.

### 2. L1 has never been tested against a real document

The two-column path is exercised only by synthetic `canva_2col.pdf`. All three files in
`fixtures/layouts/` extract with **`columns=none`** — none is a genuine two-column PDF, and their
content differs (Jaccard 0.11–0.13), so they cannot serve as the same-content/different-geometry
comparison they were collected for. PLAN calls L1 "the single biggest win in the whole project".

Still needed: a real Canva/Figma-style resume with a coloured sidebar.

### 3. Phase 7 OCR is unexercised

`extract/ocr.py` exists, `tesseract` is installed and reachable, but `VERSO_OCR` is off and
**zero tests reference the module**. It is written, not verified.

### 4. Smaller

- `BARE_FIELDS` excludes `heading` and `DateField.raw`, which is most of the represented gap.
- `MIN_EXACT_LEN = 4` blocks verification of short skill names (AWS, GCP, Go, SQL, CI, ML, QA).
  `_contains_word` already requires word boundaries, so a lower floor may be safe for exact
  containment only — needs its own test first.
- P10: re-uploading identical bytes re-runs the model instead of short-circuiting on
  `cached_parse`.
- 25 orphan `lines/` checkpoints with no parse. Harmless; retention deliberately out of scope.
- `.env.example` lacks `GEMINI_API_KEY` and still lists the provider set without Gemini.
- **Not a git repo** — no history, no way to diff or revert.

---

## Disk

```
data/     2.3M   uploads 52 · lines 52 · parses 27 · profile.json
fixtures/ 1.0M   resumes 21 · synthetic 27 · layouts 3 · expected 1
```

`profile.json` currently holds 10 EEO answers and `active_sha: None` — it predates the pointer,
so the first upload through the running server will set it.

---

## Commands

```bash
uv run uvicorn app.main:app --port 8000 --reload   # --reload matters: without it,
                                                   # Python edits are ignored while
                                                   # templates still reload, which
                                                   # renders half-updated pages

python cli.py extract|structure|verify <file>      # one stage, standalone
python cli.py all fixtures/resumes/                # batch, prints both metrics

pytest --ignore=tests/test_golden.py               # 195 offline, no API calls
pytest tests/test_golden.py -q -s                  # ship gate, hits the network
```
