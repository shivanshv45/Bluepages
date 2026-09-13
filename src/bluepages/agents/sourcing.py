"""Sourcing: finding a real product to buy for a new element.

When a revision introduces an element a department does not have, somebody has
to find the thing and price it. This module does that search for real, against
the live web, using Gemini's `google_search` grounding, and returns a product
with a retailer, a price and the source URLs the answer came from.

**What is real and what is not.** The product, the retailer and the price come
off the web and carry their citations, so a coordinator can click through and
check. Bluepages still places no orders: there is no vendor account, no payment
rail, and a record claiming a purchase happened would be a fabrication. The
result is a sourced, priced, citable proposal for a human to approve.

**Cost.** CLAUDE.md forbids an LLM call in an unbounded loop. Searches are
capped per run by `MAX_SEARCHES`, results are cached on disk by element name so
a re-run costs nothing, `max_tokens` is always set, and a failure degrades to
an unsourced line rather than retrying.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Grounded search is only worth spending on elements that must be obtained, and
# a feature-length revision can add many. This is the ceiling per run.
MAX_SEARCHES = 12

CACHE_DIR = Path(".cache/sourcing")

# Bumped whenever `Sourced` gains a field. Without it a cache written by an
# older version is served forever, and a newly added field (the product image)
# silently stays empty on every run.
CACHE_VERSION = 2

# The model that answered the grounding check. Kept here rather than in config
# because it is a capability choice (search grounding), not a tier choice.
SEARCH_MODEL = "gemini-2.5-flash"

PROMPT = """You are sourcing a real, purchasable item for a film production.

The script calls for: {element}
Department: {department}
Context: {context}

Search the web and find ONE specific real product that a production could
actually buy or rent for this. Prefer film-industry suppliers (prop houses,
picture vehicle suppliers, costume houses) where they exist, otherwise a normal
retailer.

Reply with ONLY a JSON object, no prose and no code fence:
{{"product": "specific product name",
  "supplier": "retailer or supplier name",
  "price": <number, USD, no currency symbol>,
  "unit": "each | day rate | week rental | pack of N",
  "lead_time": "realistic delivery or booking time",
  "note": "one short sentence a coordinator needs to know"}}

If you genuinely cannot find a price, use your best professional estimate and
say so in the note."""


@dataclass
class Sourced:
    """A product found on the web, with where the claim came from."""

    product: str
    supplier: str
    price: float
    unit: str
    lead_time: str
    note: str = ""
    sources: list[str] = field(default_factory=list)
    # The product page the listing came from, and a picture of it. Both are
    # taken off the real page rather than invented: a model cannot be trusted
    # to produce a working image URL, and a broken one looks worse than none.
    url: str = ""
    image: str = ""
    # False when the search failed and this is a plain estimate.
    grounded: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Used when the search fails or the budget for searches is spent. Named so the
# UI can say plainly that this line was not sourced.
def _unsourced(element: str, reason: str = "") -> Sourced:
    return Sourced(
        product=f"{element} (not sourced)",
        supplier="To be sourced",
        price=0.0,
        unit="unknown",
        lead_time="unknown",
        note=reason or "Web search unavailable. A coordinator should price this.",
        sources=[],
        grounded=False,
    )


def _cache_path(element: str, department: str) -> Path:
    key = hashlib.sha256(
        f"v{CACHE_VERSION}|{element}|{department}".encode()
    ).hexdigest()[:16]
    return CACHE_DIR / f"v{CACHE_VERSION}-{key}.json"


def _read_cache(path: Path) -> Sourced | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Sourced(**data)
    except (OSError, ValueError, TypeError):
        return None


def _write_cache(path: Path, sourced: Sourced) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sourced.to_dict()), encoding="utf-8")
    except OSError:
        pass


# Logos and sprites masquerade as product images on most retail pages.
_NOT_A_PRODUCT = re.compile(
    r"(logo|sprite|placeholder|icon|favicon|banner|brand\.|/brand)", re.I
)


# Hosts that serve images only to their own pages. Hotlinking these gives a
# 403 and a broken picture, which is worse than showing none.
_HOTLINK_BLOCKED = re.compile(r"(fbcdn\.net|instagram|licdn\.com|pinimg\.com)", re.I)


def _usable_image(url: str) -> bool:
    """Whether the image really loads for a browser that is not the retailer."""
    import httpx

    if _HOTLINK_BLOCKED.search(url):
        return False
    try:
        # A GET, not a HEAD. Some CDNs answer HEAD with an image content type
        # and then serve a text error body for the real request, which renders
        # as a broken frame or a "no image" placeholder.
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=12,
            headers={"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )},
        ) as response:
            if response.status_code != 200:
                return False
            if not response.headers.get("content-type", "").startswith("image/"):
                return False
            # A real product photo is never a few hundred bytes; a spacer or a
            # placeholder usually is.
            length = response.headers.get("content-length")
            if length and int(length) < 3000:
                return False
        return True
    except Exception:
        return False


def _resolve(redirect: str) -> tuple[str, str]:
    """Follow a grounding redirect to the real page and pull its product image.

    Returns (page url, image url). Either may be empty: this is best effort and
    a retailer that blocks us simply yields no picture.
    """
    import httpx

    try:
        response = httpx.get(
            redirect,
            follow_redirects=True,
            timeout=20,
            headers={"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )},
        )
        if response.status_code != 200:
            return "", ""
        page = str(response.url)
        html = response.text
    except Exception:
        return "", ""

    # Open Graph first, then Twitter, then a JSON-LD product image. Anything
    # that looks like chrome rather than the product is skipped.
    patterns = [
        r"<meta[^>]+(?:property|name)=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']og:image[\"']",
        r"<meta[^>]+(?:property|name)=[\"']twitter:image[\"'][^>]+content=[\"']([^\"']+)",
        r"[\"']image[\"']\s*:\s*[\"'](https?://[^\"']+)",
        # Last resort: any content image on the page.
        r"<img[^>]+src=[\"'](https?://[^\"']+\.(?:jpe?g|png|webp)[^\"']*)",
        r"<img[^>]+data-src=[\"'](https?://[^\"']+\.(?:jpe?g|png|webp)[^\"']*)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.I):
            candidate = match.group(1).strip()
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            if (
                candidate.startswith("http")
                and not _NOT_A_PRODUCT.search(candidate)
                and _usable_image(candidate)
            ):
                return page, candidate
    return page, ""


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the JSON object out of a reply that may be fenced or padded."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


class Sourcer:
    """Searches the web for products, within a per-run budget."""

    def __init__(self, api_key: str | None = None, max_searches: int = MAX_SEARCHES):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.remaining = max_searches
        self.searched = 0
        # fan_out shares one Sourcer across its thread pool, so the search
        # budget decrement has to be atomic across departments.
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def find(self, element: str, department: str, context: str = "") -> Sourced:
        """One product for one element. Cached, budgeted, and never raises."""
        path = _cache_path(element, department)
        cached = _read_cache(path)
        if cached is not None:
            return cached

        with self._lock:
            if not self.available or self.remaining <= 0:
                return _unsourced(element)
            self.remaining -= 1

        result = self._search(element, department, context)
        with self._lock:
            self.searched += 1
        if result.grounded:
            _write_cache(path, result)
        return result

    def _search(self, element: str, department: str, context: str) -> Sourced:
        import httpx

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{SEARCH_MODEL}:generateContent?key={self.api_key}"
        )
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": PROMPT.format(
                                element=element,
                                department=department,
                                context=context or "no additional context",
                            )
                        }
                    ]
                }
            ],
            "tools": [{"google_search": {}}],
            # Always capped, per the cost rule.
            "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.2},
        }

        try:
            response = httpx.post(url, json=body, timeout=45)
            if response.status_code == 429:
                # Quota, not a fault. Stop spending the rest of the run's
                # search budget on calls that will fail the same way.
                self.remaining = 0
                return _unsourced(
                    element,
                    "Search quota exhausted for today. Pricing needs doing by hand.",
                )
            if response.status_code != 200:
                return _unsourced(element)
            candidate = response.json()["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
        except Exception:
            # A sourcing failure must never take a pipeline run down with it.
            return _unsourced(element)

        data = _extract_json(text)
        if not data:
            return _unsourced(element)
        if not data.get("product") or not data.get("supplier"):
            # A truncated reply can parse but still be missing the fields that
            # make the line worth showing.
            return _unsourced(element)

        sources: list[str] = []
        redirects: list[str] = []
        for chunk in candidate.get("groundingMetadata", {}).get("groundingChunks", []):
            uri = chunk.get("web", {}).get("uri", "")
            title = chunk.get("web", {}).get("title", "")
            if uri:
                sources.append(title or uri)
                redirects.append(uri)

        # Only the first source is worth fetching. Walking all of them would
        # turn one sourcing call into four page loads for one picture.
        page_url, image = ("", "")
        for redirect in redirects[:3]:
            page_url, image = _resolve(redirect)
            if image:
                break

        try:
            price = float(str(data.get("price", 0)).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            price = 0.0

        return Sourced(
            product=str(data.get("product") or element),
            supplier=str(data.get("supplier") or "Unknown supplier"),
            price=price,
            unit=str(data.get("unit") or "each"),
            lead_time=str(data.get("lead_time") or "unknown"),
            note=str(data.get("note") or ""),
            sources=sources[:4],
            url=page_url,
            image=image,
            grounded=True,
        )
