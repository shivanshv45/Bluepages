"""Fetch the real screenplay PDFs used to validate the tier 2 parser.

These are third-party scripts, published freely but not ours to redistribute, so
they are downloaded on demand rather than committed. The tests that use them
skip when they are absent, so a clean checkout still passes; run this to enable
the full validation set.

    python scripts/fetch_fixtures.py

What they are for: tier 2 margins are calibrated per document rather than
hardcoded, and these two files are the evidence for that decision. They use
different columns from each other, and neither matches the textbook figures.

    element         Social Network   Code 8    convention
    action              1.32"         1.50"      1.5"
    dialogue            2.32"         2.50"      2.5"
    character cue       3.32"         3.50"      3.7"
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

SOURCES: list[tuple[str, str, str]] = [
    (
        "real-screenplay.pdf",
        "https://assets.scriptslug.com/live/pdf/scripts/the-social-network-2010.pdf",
        "The Social Network (2010). Dialogue-dense; tight 1.32\" columns.",
    ),
    (
        "real-screenplay-2.pdf",
        "https://archive.org/download/code8featurefilmscreenplay/"
        "CODE%208%20%28Feature%20Film%29%20Screenplay.pdf",
        "Code 8 (feature). Standard 1.50\" columns, CC on the Internet Archive.",
    ),
]


def fetch(name: str, url: str, note: str) -> bool:
    target = FIXTURES / name
    if target.exists():
        print(f"  have  {name}  ({target.stat().st_size:,} bytes)")
        return True

    print(f"  get   {name}  <- {url}")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "bluepages/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  FAIL  {name}: {exc}")
        return False

    if not data.startswith(b"%PDF"):
        print(f"  FAIL  {name}: not a PDF (got {len(data):,} bytes, likely an error page)")
        return False

    target.write_bytes(data)
    print(f"  ok    {name}  ({len(data):,} bytes)  {note}")
    return True


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    print(f"fixtures -> {FIXTURES}\n")
    results = [fetch(*source) for source in SOURCES]

    got = sum(results)
    print(f"\n{got}/{len(SOURCES)} available")
    if got < len(SOURCES):
        print(
            "\nTests needing the missing files will skip. The .fdx fixtures and the\n"
            "rendered feature-draft-1.pdf are committed, so the tier-equivalence\n"
            "test still runs without these."
        )
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main())
