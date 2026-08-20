"""Identify which service a contact link points at.

Derived in Python from the URL rather than asked of the model. The mapping is a fixed
lookup, so there is nothing here a model would do better and one less thing it can get
wrong -- the same reasoning as the closed skill taxonomy in PLAN.md §5.
"""

import re

# Ordered: the first match wins, so put specific hosts before generic ones.
LINK_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("GitHub", re.compile(r"\bgithub\.(com|io)\b", re.I)),
    ("GitLab", re.compile(r"\bgitlab\.com\b", re.I)),
    ("LinkedIn", re.compile(r"\blinkedin\.com\b|\blnkd\.in\b", re.I)),
    ("Stack Overflow", re.compile(r"\bstackoverflow\.com\b", re.I)),
    ("Google Play", re.compile(r"\bplay\.google\.com\b", re.I)),
    ("App Store", re.compile(r"\bapps\.apple\.com\b", re.I)),
    ("Kaggle", re.compile(r"\bkaggle\.com\b", re.I)),
    ("Hugging Face", re.compile(r"\bhuggingface\.co\b", re.I)),
    ("Behance", re.compile(r"\bbehance\.net\b", re.I)),
    ("Dribbble", re.compile(r"\bdribbble\.com\b", re.I)),
    ("Medium", re.compile(r"\bmedium\.com\b", re.I)),
    ("Substack", re.compile(r"\bsubstack\.com\b", re.I)),
    ("X", re.compile(r"\b(twitter\.com|x\.com)\b", re.I)),
    ("YouTube", re.compile(r"\byoutube\.com\b|\byoutu\.be\b", re.I)),
    ("ORCID", re.compile(r"\borcid\.org\b", re.I)),
    ("Google Scholar", re.compile(r"\bscholar\.google\.", re.I)),
    ("arXiv", re.compile(r"\barxiv\.org\b", re.I)),
]

# Resumes often write "GitHub: <url>" or "LinkedIn — <url>". When the URL itself is opaque
# (a shortener), that written label is the only signal left.
WRITTEN_LABEL = re.compile(
    r"\b(github|gitlab|linkedin|portfolio|website|blog|twitter|behance|dribbble"
    r"|medium|youtube|orcid|scholar|kaggle)\b",
    re.I,
)
SHORTENERS = re.compile(r"\b(bit\.ly|shorturl\.at|tinyurl\.com|t\.co|rb\.gy|lnkd\.in)\b", re.I)
CANONICAL = {
    "github": "GitHub", "gitlab": "GitLab", "linkedin": "LinkedIn",
    "portfolio": "Portfolio", "website": "Website", "blog": "Blog",
    "twitter": "X", "behance": "Behance", "dribbble": "Dribbble",
    "medium": "Medium", "youtube": "YouTube", "orcid": "ORCID",
    "scholar": "Google Scholar", "kaggle": "Kaggle",
}


def label_for(text: str, context: str = "") -> str:
    """Name the service a link points at.

    `context` is the source line, which carries the written label when the URL is a
    shortener and the host says nothing.
    """
    for name, pattern in LINK_PATTERNS:
        if pattern.search(text):
            return name

    # A shortened URL hides its destination, so fall back to how the resume labelled it.
    written = WRITTEN_LABEL.search(text) or WRITTEN_LABEL.search(context)
    if written:
        return CANONICAL.get(written.group(1).lower(), written.group(1).title())

    if SHORTENERS.search(text):
        return "Link"
    if re.search(r"https?://|\bwww\.", text, re.I):
        return "Website"
    return "Link"
