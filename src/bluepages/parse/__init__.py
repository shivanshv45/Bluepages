"""Parser tiers.

Tier 1 `.fdx` is the primary path and gives typed elements directly. Tier 2 PDF
reconstructs them from coordinates. Tier 3 OCR comes later. The dispatcher below
is what every caller should use, so adding a tier does not change any call
site.
"""

from pathlib import Path

from bluepages.events import EventStream
from bluepages.model.script import Screenplay
from bluepages.parse.fdx import FdxParseError, parse_fdx, parse_fdx_string
from bluepages.parse.pdf import PdfParseError, parse_pdf


class UnsupportedFormatError(Exception):
    """No parser tier handles this file type yet."""


def parse_script(path: str | Path, stream: EventStream | None = None) -> Screenplay:
    """Parse any supported script file, choosing the tier by extension."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".fdx":
        return parse_fdx(path, stream=stream)
    if suffix == ".pdf":
        return parse_pdf(path, stream=stream)
    raise UnsupportedFormatError(
        f"{path.name}: unsupported script format {suffix!r}. Supported: .fdx, .pdf"
    )


__all__ = [
    "FdxParseError",
    "PdfParseError",
    "UnsupportedFormatError",
    "parse_fdx",
    "parse_fdx_string",
    "parse_pdf",
    "parse_script",
]
