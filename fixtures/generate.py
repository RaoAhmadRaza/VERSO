"""Deliberate-defect probes (PLAN.md §6, §9 Phase 0).

These are NOT resumes and are not pretending to be. Each file is a specific defect that a
real resume cannot supply on demand -- a password-protected PDF, an OLE2 blob, a file whose
extension lies, a broken font map, a layout engineered to break a naive column sweep. The
text inside them is filler; only their structure is under test.

Real resumes live in `fixtures/resumes/` and are what `coverage_pct` is measured on. Nothing
in this directory contributes to that number.

    python fixtures/generate.py
"""

import sys
import zipfile
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))

import _pdfgen as P  # noqa: E402
from _docxgen import bullet, para, table, textbox, tracked, write_docx  # noqa: E402

OUT = Path(__file__).parent / "synthetic"

# --------------------------------------------------------------- filler text
# Structural scaffolding only. These strings exist to give the probes something to lay out;
# no claim about parse quality is ever made against them.

HEAD = ["JANE MORGAN", "jane.morgan@example.com", "+1 415 555 0199", "San Francisco, CA"]
SUMMARY = ["SUMMARY", "Senior backend engineer with eight years on payment systems."]
EXPERIENCE = [
    "EXPERIENCE",
    "Senior Engineer",
    "Acme Corp - San Francisco, CA",
    "Jan 2021 - Present",
    "- Built the billing pipeline handling 4M events per day.",
    "- Cut p99 latency from 800ms to 120ms.",
    "",
    "Software Engineer",
    "Globex Inc - Remote",
    "Jun 2018 - Dec 2020",
    "- Shipped the customer portal used by 30k accounts.",
]
EDUCATION = [
    "EDUCATION",
    "BSc Computer Science",
    "University of Bristol",
    "2014 - 2018",
    "GPA 3.8/4.0",
]
SKILLS = ["SKILLS", "Python, Go, PostgreSQL, Kubernetes, AWS, Terraform"]
FULL = HEAD + [""] + SUMMARY + [""] + EXPERIENCE + [""] + EDUCATION + [""] + SKILLS


def _simple(path: Path, lines: list[str], **save_kw) -> None:
    doc = P.new_doc()
    P.put(P.page(doc), lines, 60, 70)
    P.save(doc, path, **save_kw)


# ------------------------------------------------------------------------ layout (§6.1)


def canva_2col() -> None:
    """L1 -- narrow sidebar left, work history right, both running the full page height.

    The geometry is deliberately realistic: a real two-column resume fills the page, and L1
    requires each column to span more than half of it. Sidebar lines are kept short so a
    genuine gutter exists -- a left-column line that runs under the right column is not a
    two-column layout, it is overlapping text.
    """
    doc = P.new_doc()
    pg = P.page(doc)
    side = [
        "CONTACT", "jane.morgan", "@example.com", "+1 415 555 0199",
        "San Francisco, CA", "", "SKILLS", "Python", "Go", "PostgreSQL",
        "Kubernetes", "AWS", "Terraform", "Kafka", "Redis", "gRPC", "",
        "EDUCATION", "BSc Computer Science", "University of", "Bristol",
        "2014 - 2018", "GPA 3.8/4.0", "", "CERTIFICATIONS", "AWS Solutions",
        "Architect, 2022", "CKA, 2021", "", "LANGUAGES", "English (native)",
        "Spanish (fluent)",
    ]
    main = [
        "JANE MORGAN", "Senior Backend Engineer", "",
        "SUMMARY",
        "Senior backend engineer with eight years on payment systems.",
        "", "EXPERIENCE",
        "Senior Engineer", "Acme Corp - San Francisco, CA", "Jan 2021 - Present",
        "- Built the billing pipeline handling 4M events per day.",
        "- Cut p99 latency from 800ms to 120ms.",
        "- Led the migration from monolith to six services.",
        "", "Software Engineer", "Globex Inc - Remote", "Jun 2018 - Dec 2020",
        "- Shipped the customer portal used by 30k accounts.",
        "- Reduced onboarding from three days to four hours.",
        "", "Junior Developer", "Initech - Austin, TX", "Sep 2016 - May 2018",
        "- Maintained the reporting service.",
        "- Wrote the regression suite still in use today.",
        "", "PROJECTS", "ledger-py -- an open-source double-entry ledger.",
        "- 1.2k stars, used by four payment startups.",
    ]
    P.put(pg, side, 40, 70, size=9, leading=22)
    P.put(pg, main, 250, 70, size=10, leading=22)
    P.save(doc, OUT / "canva_2col.pdf")


def three_col() -> None:
    """L2 -- three columns, the rightmost narrow enough to count as a sidebar."""
    doc = P.new_doc()
    pg = P.page(doc)
    left = [
        "JANE MORGAN", "", "SUMMARY", "Senior backend", "engineer, eight",
        "years on payments.", "", "EXPERIENCE", "Senior Engineer", "Acme Corp",
        "Jan 2021 - Now", "- Built billing", "  pipeline.", "- Cut p99 to 120ms.",
        "", "Software Eng", "Globex Inc", "2018 - 2020", "- Shipped portal.",
    ]
    middle = [
        "EDUCATION", "BSc Computer", "Science", "University of", "Bristol",
        "2014 - 2018", "GPA 3.8/4.0", "", "PROJECTS", "ledger-py",
        "Double-entry ledger.", "", "AWARDS", "Engineer of the", "Year, 2022",
        "", "PUBLICATIONS", "Latency-aware", "billing, 2023",
    ]
    right = [
        "CONTACT", "jane.morgan", "@example.com", "+1 415 555", "0199",
        "San Francisco", "", "SKILLS", "Python", "Go", "Kubernetes",
        "AWS", "Terraform", "", "LANGUAGES", "English", "Spanish",
    ]
    P.put(pg, left, 40, 70, size=9, leading=38)
    P.put(pg, middle, 235, 70, size=9, leading=38)
    P.put(pg, right, 455, 70, size=9, leading=38)
    P.save(doc, OUT / "three_col.pdf")


def header_contact() -> None:
    """L3 -- contact details live in the page header zone and must not be stripped."""
    doc = P.new_doc()
    pg = P.page(doc)
    P.put(pg, [HEAD[1], HEAD[2]], 60, 30, size=8)  # y < 0.07*H -> header zone
    P.put(pg, [HEAD[0], ""] + SUMMARY + [""] + EXPERIENCE + [""] + SKILLS, 60, 110)
    P.save(doc, OUT / "header_contact.pdf")


def footer_pagenum() -> None:
    """L7 -- an identical 'Page N of 3' footer on every page. Dropped, but logged."""
    doc = P.new_doc()
    chunks = [HEAD + [""] + SUMMARY, EXPERIENCE, EDUCATION + [""] + SKILLS]
    for i, chunk in enumerate(chunks, 1):
        pg = P.page(doc)
        P.put(pg, chunk, 60, 90)
        P.put(pg, [f"Page {i} of 3"], 270, 810, size=8)  # y > 0.93*H -> footer zone
    P.save(doc, OUT / "footer_pagenum.pdf")


def multipage_role() -> None:
    """L6 -- a single role's bullet list split across a page break."""
    doc = P.new_doc()
    pg1 = P.page(doc)
    P.put(pg1, HEAD + [""] + ["EXPERIENCE", "Senior Engineer", "Acme Corp"], 60, 70)
    P.put(pg1, ["Jan 2021 - Present", "- Built the billing pipeline that"], 60, 700)
    pg2 = P.page(doc)
    P.put(pg2, ["handles 4M events per day.", "- Cut p99 latency to 120ms."], 60, 70)
    P.put(pg2, [""] + EDUCATION + [""] + SKILLS, 60, 120)
    P.save(doc, OUT / "multipage_role.pdf")


def skill_bars() -> None:
    """L8 -- vector rating graphics carrying no text. Unrecoverable; warn the user."""
    doc = P.new_doc()
    pg = P.page(doc)
    P.put(pg, HEAD + [""] + EXPERIENCE, 60, 70)
    P.put(pg, ["SKILLS"], 60, 430)
    for i, filled in enumerate([0.9, 0.7, 0.5, 0.8]):
        y = 450 + i * 20
        pg.draw_rect(pymupdf.Rect(60, y, 260, y + 8), color=(0.8, 0.8, 0.8), fill=(0.9, 0.9, 0.9))
        pg.draw_rect(pymupdf.Rect(60, y, 60 + 200 * filled, y + 8), color=None, fill=(0.08, 0.35, 0.36))
    P.save(doc, OUT / "skill_bars.pdf")


def nospacing() -> None:
    """E7 -- glyphs butted together, so no space can be re-inferred from gaps."""
    doc = P.new_doc()
    pg = P.page(doc)
    P.put(pg, [HEAD[0], HEAD[1], "", "EXPERIENCE"], 60, 70)
    for i, text in enumerate(
        [
            "Senior Engineer at Acme Corp",
            "Built the billing pipeline handling four million events",
            "Cut p99 latency from 800ms to 120ms",
        ]
    ):
        P.put_chars_tight(pg, text, 60, 140 + i * 16)
    P.put(pg, SKILLS, 60, 210)
    P.save(doc, OUT / "nospacing.pdf")


def broken_cmap() -> None:
    """E6 -- simulates the *symptom* of a damaged ToUnicode CMap: a page whose extracted
    text is >30% non-alphanumeric garbage. Authoring a genuinely corrupt CMap would test
    the same detector via a far more brittle fixture."""
    lines = [
        "\xa4\xa7\xa8\xa9\xaa\xab\xac\xae\xaf\xb0\xb1\xb2\xb3\xb4\xb5\xb6",
        "\xb7\xb8\xb9\xba\xbb\xbc\xbd\xbe\xbf\xa1\xa2\xa3\xa5\xa6\xab\xbb",
        "\xa4\xa7\xa8\xa9\xaa\xab\xac\xae\xaf\xb0\xb1\xb2\xb3\xb4\xb5\xb6",
        "\xb0\xb1\xb2\xb3\xb4\xb5\xb6\xa4\xa7\xa8\xa9\xaa\xab\xac\xae\xaf",
    ] * 4
    _simple(OUT / "broken_cmap.pdf", lines)


def letterspaced() -> None:
    """E11 -- CSS letter-spacing on headings, emitted as real space glyphs.

    The inverse of E7: instead of missing word spaces, every letter is separated by one.
    Word boundaries survive only as a wider gap, so they must be recovered from geometry.
    """
    doc = P.new_doc()
    pg = P.page(doc)

    def spaced(text: str, x: float, y: float, size: float = 11.0) -> None:
        cursor = x
        for ch in text:
            if ch == " ":
                cursor += size * 0.34  # word gap, markedly wider than the letter gap
                continue
            pg.insert_text((cursor, y), ch, fontname="helv", fontsize=size)
            cursor += pymupdf.get_text_length(ch, fontname="helv", fontsize=size) + size * 0.16

    spaced("MUHAMMAD AHMAD RAZA", 60, 80, 13)
    P.put(pg, ["Mobile Software Engineer | Flutter, Kotlin, Swift"], 60, 100, size=9)
    spaced("TARGET ROLE", 60, 130)
    P.put(pg, ["Mobile engineer building across Flutter, Android and iOS."], 60, 150, size=9)
    spaced("SUMMARY", 60, 180)
    P.put(pg, ["Two years shipping production applications."], 60, 200, size=9)
    spaced("TECHNICAL SKILLS", 60, 230)
    P.put(pg, ["Dart, Kotlin, Swift, JavaScript, SQL"], 60, 250, size=9)
    spaced("EXPERIENCE", 60, 280)
    P.put(pg, ["Mobile Engineer, Acme Corp", "Jan 2023 - Present"], 60, 300, size=9)
    P.save(doc, OUT / "letterspaced.pdf")


def cjk_resume() -> None:
    """E9/C16 -- CJK content. Preserved verbatim; hyphen-rejoin must be disabled."""
    doc = P.new_doc()
    pg = P.page(doc)
    rows = [
        "张伟",
        "zhangwei@example.com",
        "",
        "工作经历",
        "高级软件工程师 - 阿里巴巴",
        "2021年1月 - 至今",
        "负责支付系统的架构设计。",
        "",
        "技能",
        "Python, Go, Kubernetes",
    ]
    for i, text in enumerate(rows):
        if text:
            pg.insert_text((60, 80 + i * 18), text, fontname="china-s", fontsize=11)
    P.save(doc, OUT / "cjk_resume.pdf")


# -------------------------------------------------------------------- file-level (§6.3)


def scanned() -> None:
    """F1 -- rendered to an image, so there is no text layer at all."""
    src = P.new_doc()
    P.put(P.page(src), FULL, 60, 70)
    pix = src[0].get_pixmap(dpi=150)
    src.close()
    doc = P.new_doc()
    pg = P.page(doc)
    pg.insert_image(pymupdf.Rect(0, 0, P.A4_W, P.A4_H), pixmap=pix)
    P.save(doc, OUT / "scanned.pdf")


def white_text() -> None:
    """F9 -- white-on-white keyword stuffing. Real content; extract it and flag it."""
    doc = P.new_doc()
    pg = P.page(doc)
    P.put(pg, FULL, 60, 70)
    P.put(
        pg,
        ["Java Scala Rust Haskell Erlang machine learning blockchain senior architect"],
        60, 760, size=7, color=(1, 1, 1),
    )
    P.save(doc, OUT / "white_text.pdf")


def locked() -> None:
    """F5 -- password protected."""
    doc = P.new_doc()
    P.put(P.page(doc), FULL, 60, 70)
    P.save(
        doc, OUT / "locked.pdf",
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret",
    )


def truncated() -> None:
    """F6 -- two branches, because MuPDF has two failure modes.

    `truncated.pdf` is damaged badly enough that `open()` raises FileDataError.
    `repaired.pdf` is damaged mildly: MuPDF silently rebuilds the xref, sets
    `is_repaired`, and hands back a page holding a fraction of the real text. The second
    is the dangerous one -- it looks like a successful parse.
    """
    tmp = OUT / "_tmp_truncate.pdf"
    _simple(tmp, FULL)
    data = tmp.read_bytes()
    tmp.unlink()
    (OUT / "truncated.pdf").write_bytes(data[:600])
    (OUT / "repaired.pdf").write_bytes(data[:953])


def huge() -> None:
    """F7 -- 20 pages. Not a resume."""
    doc = P.new_doc()
    for i in range(20):
        P.put(P.page(doc), [f"Page {i + 1}"] + FULL, 60, 70)
    P.save(doc, OUT / "huge.pdf")


def empty_pdf() -> None:
    """F8 -- under 50 characters."""
    _simple(OUT / "empty.pdf", ["Jane"])


def fake_pdf() -> None:
    """F2 -- DOCX bytes behind a .pdf extension. Dispatch on magic, never extension."""
    write_docx(OUT / "fake.pdf", "".join(para(line) for line in FULL if line))


def legacy_doc() -> None:
    """F3 -- OLE2 compound file (legacy .doc). Rejected with a clear message."""
    (OUT / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 4096)


def odt_and_rtf() -> None:
    """F4 -- explicitly rejected formats. Silent failure is the enemy."""
    with zipfile.ZipFile(OUT / "resume.odt", "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", "<office:document-content/>")
    (OUT / "resume.rtf").write_bytes(rb"{\rtf1\ansi Jane Morgan\par Senior Engineer\par}")


def textboxes_docx() -> None:
    """L5 -- a floating text box mid-document, emitted once, in document order."""
    body = (
        para(HEAD[0]) + para(HEAD[1])
        + para("EXPERIENCE")
        + textbox("KEY ACHIEVEMENT: reduced infrastructure spend by 40%.")
        + para("Senior Engineer, Acme Corp")
        + para("Jan 2021 - Present")
        + bullet("Built the billing pipeline handling 4M events per day.")
        + para("SKILLS") + para(SKILLS[1])
    )
    write_docx(OUT / "textboxes.docx", body)


def tracked_docx() -> None:
    """F10 -- keep w:ins, drop w:del, ignore comments."""
    body = (
        para(HEAD[0]) + para(HEAD[1])
        + para("EXPERIENCE")
        + tracked("Senior Engineer", "Junior Engineer", " at Acme Corp")
        + para("Jan 2021 - Present")
        + bullet("Built the billing pipeline.")
        + table([["Skill", "Years"], ["Python", "8"], ["Go", "4"]])
    )
    write_docx(
        OUT / "tracked.docx", body,
        header=para("jane.morgan@example.com"),
        footer=para("Page 1 of 1"),
        comments='<w:comment w:id="3"><w:p><w:r><w:t>tighten this</w:t></w:r></w:p></w:comment>',
    )


# ---------------------------------------------------------------- content semantics (§6.4)


def plain_files() -> None:
    """E8 mojibake, E9 RTL, and the E1/E2/E5/E10 codepoints that cannot survive a PDF."""
    # E8 -- UTF-8 read back as cp1252 and re-encoded, the classic round trip.
    (OUT / "mojibake.txt").write_text(
        "\n".join(FULL).replace("Jane", "Jane\u00e2\u20ac\u2122s team").replace("-", "\u00e2\u20ac\u201c"),
        encoding="utf-8",
    )
    # E1/E2/E5/E10 -- PyMuPDF folds ligatures and PUA codepoints to their nearest standard
    # glyph on write, so they cannot reach a generated PDF. A text fixture carries them
    # intact, and tests/test_normalize.py covers each rule with crafted strings.
    (OUT / "ligatures.txt").write_text(
        "JANE MORGAN\n"
        "jane.morgan@example.com\n\n"
        "SUMMARY\n"
        "Engineer with a strong pro\ufb01le in work\ufb02ow automation and sta\ufb00 mentoring.\n"
        "Speciali\u00adsation in distributed\u200b systems.\n"
        "Holds a\u00a0senior\u00a0title.\n\n"
        "EXPERIENCE\n"
        "\uf0b7 Designed a high-through-\n"
        "put ingestion pipeline.\n"
        "\u2022 Cut p99 latency to 120ms.\n\n"
        "SKILLS\nPython, Go, Kubernetes\n",
        encoding="utf-8",
    )
    # Extension routing only: .md must reach the plain-text extractor.
    (OUT / "plain.md").write_text(
        "# Heading\n\n- bullet one\n- bullet two\n\nSome body text for the routing probe.\n",
        encoding="utf-8",
    )
    # E9 -- Arabic. PyMuPDF ships no Arabic face, so RTL lives in a text fixture.
    (OUT / "rtl_arabic.txt").write_text(
        "\u062c\u064a\u0646 \u0645\u0648\u0631\u063a\u0627\u0646\n"
        "jane.morgan@example.com\n\n"
        "\u0627\u0644\u062e\u0628\u0631\u0629\n"
        "\u0645\u0647\u0646\u062f\u0633 \u0628\u0631\u0645\u062c\u064a\u0627\u062a \u0623\u0648\u0644 - \u0634\u0631\u0643\u0629 \u0623\u0643\u0645\u064a\n"
        "\u064a\u0646\u0627\u064a\u0631 2021 - \u0627\u0644\u0622\u0646\n"
        "\u0628\u0646\u0627\u0621 \u062e\u0637 \u0645\u0639\u0627\u0644\u062c\u0629 \u0627\u0644\u0641\u0648\u0627\u062a\u064a\u0631\n\n"
        "\u0627\u0644\u0645\u0647\u0627\u0631\u0627\u062a\nPython, Go, Kubernetes\n",
        encoding="utf-8",
    )


GENERATORS = [
    # geometry -- layouts that break a naive left-to-right sweep
    canva_2col, three_col, header_contact, footer_pagenum, multipage_role,
    # extraction defects
    skill_bars, nospacing, broken_cmap, letterspaced, cjk_resume, white_text,
    textboxes_docx, tracked_docx,
    # file-level rejections
    scanned, locked, truncated, huge, empty_pdf, fake_pdf, legacy_doc, odt_and_rtf,
    # encoding, in text files where the codepoints survive
    plain_files,
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for gen in GENERATORS:
        gen()
        print(f"  {gen.__name__}")
    files = sorted(p.name for p in OUT.iterdir() if p.is_file())
    print(f"\n{len(files)} defect probes in {OUT}")
    print("Real resumes go in fixtures/resumes/ -- that is what coverage is measured on.")


if __name__ == "__main__":
    main()
