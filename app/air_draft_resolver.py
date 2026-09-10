"""Air draft resolver: searches the web for a vessel's own published air
draft (height above the waterline) before worker.run_air_draft_lookup falls
back to vessel_height_table's generic type+size estimate for that vessel.
Reinstated after a stretch where the app relied on the type+size table
alone - see git history (pre-b56a42c) for the original, self-contained
version of this file, and vessel_name_lookup.py's docstring for how MMSI-to-
name resolution split off from it as a separate concern.

Ported from the standalone air_draft_lookup.py CLI tool, with the
Playwright/JS-render fallback and the Windows/conda tesseract path shim
dropped (this only ever runs inside the Linux container - see the
Dockerfile's tesseract-ocr system package), and structured LookupResult
return values instead of print()/file logging.

Search backend: delegated entirely to vessel_name_lookup.langsearch_search /
.duckduckgo_search (LangSearch primary, DuckDuckGo fallback) rather than
this module making its own HTTP calls to either - both are also called by
the name/IMO/category lookups, and LangSearch's rate limit is tight enough
that two independently-throttled callers routinely trip 429s against each
other (see vessel_name_lookup._throttle's docstring); going through the same
shared throttle avoids reintroducing that exact problem for what would
otherwise be this module's own, uncoordinated search calls.

This module only owns what's genuinely specific to air draft: fetching a
candidate page or PDF (with OCR fallback for scanned ship-particulars
sheets), searching its text for an air-draft figure, and confirming the
figure actually belongs to the target vessel (not a same-named other one).
"""

import logging
import re
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Literal

import pymupdf
import pytesseract
import requests
from bs4 import BeautifulSoup
from PIL import Image
from pypdf import PdfReader

from app import db
from app.vessel_name_lookup import duckduckgo_search, langsearch_search

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

# "Primary" keywords name air draft directly (or another waterline-referenced
# equivalent). "Fallback" keywords are structurally different measurements
# (keel-to-X is measured from the bottom of the hull, not the waterline, so
# it's always larger than air draft by roughly the vessel's draft) that are
# only used as a last resort when no page names air draft directly - never
# preferred over a genuine air draft figure just because it happens to occur
# earlier in a page.
PRIMARY_KEYWORDS = [
    "air draft",
    "air draught",
    "airdraft",
    "airdraught",
    "height above waterline",
    "height above w/l",
]
FALLBACK_KEYWORDS = [
    "keel to mast",
    "keel to funnel",
    "keel to truck",
    "overhead clearance",
    "vertical clearance",
]

VALUE_PATTERN = re.compile(r"(\d[\d,]*\.?\d*)\s*(m|meters|metres|ft|feet)\b", re.IGNORECASE)
STANDALONE_VALUE_PATTERN = re.compile(r"^[\d,]+\.?\d*\s*(m|meters|metres|ft|feet)$", re.IGNORECASE)

# The vessel name is quoted as an exact phrase - tested head-to-head against
# LangSearch (see PR discussion): the quoted-name query surfaced the actual
# ship-particulars PDF; unquoted-name and IMO-number-only variants
# ("IMO 1234567 air draft", "1234567 air draft") did not return it at all,
# just unrelated vessels. IMO is a strong *confirmation* signal once a
# candidate page is fetched (see find_matches), but not a useful search term
# on this backend.
SEARCH_QUERIES = [
    '"{name}" air draft',
    '"{name}" air draught',
    '"{name}" ship pdf',
]

MAX_RESULTS_PER_QUERY = 8
MAX_PAGES_TO_FETCH = 12

# Courtesy delay between fetching different candidate pages (VesselFinder,
# PDFs, forum posts, etc.) - a separate concern from the LangSearch/
# DuckDuckGo *search-call* throttling handled inside vessel_name_lookup, this
# just avoids hammering whatever arbitrary external host each candidate URL
# happens to be on back-to-back.
PAGE_FETCH_DELAY_SECONDS = 1.5

FT_PER_M = 1 / 0.3048


class LookupError(Exception):
    """Raised when a lookup could not be completed at all (search or every
    page fetch failed) - distinct from a completed search that genuinely
    found nothing. Callers should treat this as "try again later", not as an
    UNKNOWN result."""


@dataclass
class LookupResult:
    status: Literal["OK", "FLAGGED", "UNKNOWN"]
    value_raw: str | None
    value_m: float | None
    value_ft: float | None
    source_url: str | None
    source_title: str | None
    context: str | None
    # How the fetched page was confirmed to be about the target vessel:
    # "declared_name" (the page's own NAME: field) and "imo" (the target's
    # IMO appears next to the match) are strong; "name_proximity" is weak -
    # only the vessel's name appears nearby, which collides readily on
    # short/common names (see find_matches). None for an UNKNOWN with no
    # source at all.
    identity: Literal["declared_name", "imo", "name_proximity", "unconfirmed"] | None = None


def _search_candidates(vessel_name: str) -> list[tuple[str, str]]:
    """Runs every SEARCH_QUERIES template through LangSearch, falling back to
    DuckDuckGo per-query if LangSearch errors out - same fallback pattern as
    vessel_name_lookup.resolve_vessel_name, just repeated across more
    queries. Returns deduplicated (title, url) pairs across all queries;
    snippets aren't used here (unlike name resolution) since this module
    fetches and parses each candidate's full page text instead."""
    seen_urls = set()
    candidates = []
    for template in SEARCH_QUERIES:
        query = template.format(name=vessel_name)
        try:
            results = langsearch_search(query, max_results=MAX_RESULTS_PER_QUERY)
        except Exception:
            logger.warning(
                "LangSearch air-draft search failed for query %r, falling back to DuckDuckGo",
                query, exc_info=True,
            )
            try:
                results = duckduckgo_search(query, max_results=MAX_RESULTS_PER_QUERY)
            except Exception:
                logger.warning("DuckDuckGo fallback also failed for query %r", query, exc_info=True)
                continue
        for title, url, _snippet in results:
            if url not in seen_urls:
                seen_urls.add(url)
                candidates.append((title, url))
    return candidates


def fetch_pdf_text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text.strip()) < 30:
        text = ocr_pdf_text(content)
    return text


def ocr_pdf_text(content: bytes, zoom: float = 3.0) -> str:
    doc = pymupdf.open(stream=content, filetype="pdf")
    matrix = pymupdf.Matrix(zoom, zoom)
    all_lines = []
    for page in doc:
        pixmap = page.get_pixmap(matrix=matrix)
        image = Image.open(BytesIO(pixmap.tobytes("png")))
        all_lines.extend(ocr_image_to_rows(image))
    doc.close()
    return "\n".join(all_lines)


def ocr_image_to_rows(image) -> list:
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if text and conf > 0:
            words.append(
                {
                    "text": text,
                    "left": data["left"][i],
                    "top": data["top"][i],
                    "height": data["height"][i],
                }
            )

    if not words:
        return []

    words.sort(key=lambda w: w["top"] + w["height"] / 2)
    heights = sorted(w["height"] for w in words)
    tolerance = max(heights[len(heights) // 2] * 0.6, 5)

    rows = []
    current_row = []
    row_center = None
    for w in words:
        center = w["top"] + w["height"] / 2
        if row_center is None or abs(center - row_center) <= tolerance:
            current_row.append(w)
            row_center = sum(x["top"] + x["height"] / 2 for x in current_row) / len(current_row)
        else:
            rows.append(current_row)
            current_row = [w]
            row_center = center
    if current_row:
        rows.append(current_row)

    return [" ".join(w["text"] for w in sorted(row, key=lambda w: w["left"])) for row in rows]


def find_value_near(lines, i: int, same_line_start: int):
    if same_line_start is not None:
        window = lines[i][same_line_start: same_line_start + 40]
        m = VALUE_PATTERN.search(window)
        if m:
            return f"{m.group(1)} {m.group(2)}"

    for j in range(i + 1, min(i + 3, len(lines))):
        candidate = lines[j].strip()
        if not candidate:
            continue
        if STANDALONE_VALUE_PATTERN.match(candidate):
            return candidate
        m = VALUE_PATTERN.search(candidate)
        if m and len(candidate) < 20:
            return f"{m.group(1)} {m.group(2)}"
        break
    return None


# How many lines around a candidate air-draft mention to search for the
# vessel's own name. Wide enough to span a multi-field particulars table/PDF
# (name in a header, air draft field dozens of lines later), but far short of
# spanning an entire long page - e.g. a forum thread where the vessel's name
# coincidentally appears once, in an unrelated post far from the actual
# number being extracted (see _name_pattern's docstring for a real example).
NAME_PROXIMITY_LINES = 100


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _name_pattern(vessel_name: str) -> re.Pattern:
    """Whitespace-flexible, case-insensitive pattern matching vessel_name as
    a phrase (not just its words appearing anywhere)."""
    words = _normalize_ws(vessel_name).split(" ")
    return re.compile(r"\s+".join(re.escape(w) for w in words), re.IGNORECASE)


# A vessel's own name is often a suffix of a completely different, unrelated
# vessel's name - e.g. searching for "NORTHWIND" can surface a ship
# particulars page for "INCE NORTHWIND", a different vessel that just
# happens to share that word. If a name match is immediately preceded (same
# line, separated only by plain spaces/tabs - a colon, comma, or newline
# usually marks a genuine field boundary instead) by another word that
# isn't a generic label/article, it's more likely the tail of that other,
# longer name than a real match - so it's disqualified unless another,
# unprefixed occurrence of the name exists elsewhere in the window.
# Deliberately asymmetric (checks only what precedes the name, not what
# follows): OCR-reconstructed table rows commonly place the *next* field's
# label directly after a name with only a space (no column separator
# survives OCR), which would otherwise cause frequent false rejections.
_NAME_PREFIX_ALLOWLIST = {
    "MV", "MS", "SS", "VSL", "THE", "VESSEL", "VESSELS", "SHIP", "SHIPS", "NAME", "NAMED", "OF", "FOR",
}
_PRECEDING_WORD_PATTERN = re.compile(r"([A-Za-z][A-Za-z/.'-]*)[ \t]+\Z")


def _has_disqualifying_prefix(text: str, match_start: int) -> bool:
    preceding = _PRECEDING_WORD_PATTERN.search(text[:match_start])
    if not preceding:
        return False
    normalized = re.sub(r"[^A-Za-z]", "", preceding.group(1)).upper()
    return bool(normalized) and normalized not in _NAME_PREFIX_ALLOWLIST


def _name_confirmed_nearby(window_text: str, name_re: re.Pattern) -> bool:
    matches = list(name_re.finditer(window_text))
    return any(not _has_disqualifying_prefix(window_text, m.start()) for m in matches)


# Ship particulars sheets (the exact kind of document this tool targets)
# almost always declare the vessel's full name in an explicit labeled field
# near the top, e.g. "NAME: M/V INCE NORTHWIND". That's a far more reliable
# identity signal than any substring/proximity heuristic: it lets a document
# about a different vessel be rejected outright (e.g. our target "NORTHWIND"
# vs. a document declaring "INCE NORTHWIND"), even when that different
# vessel's name - or an unrelated company name derived from it, like a
# "NORTHWIND SHIPPING PTE LTD" charterer - would otherwise pass the
# proximity check. Falls back to the proximity heuristic when no such field
# is found (common for pages that aren't a formal particulars sheet, e.g.
# forum posts).
_NAME_FIELD_LABEL_PATTERN = re.compile(
    r"^(?:vessel'?s?\s*name|ship'?s?\s*name|name\s+of\s+(?:the\s+)?(?:vessel|ship)|name)\s*[:\-]\s*",
    re.IGNORECASE,
)
_NAME_VALUE_PREFIX_PATTERN = re.compile(r"^(?:m\.?\s?/?\s?v\.?|m\.?\s?s\.?)\s+", re.IGNORECASE)


def _extract_declared_name(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        label_match = _NAME_FIELD_LABEL_PATTERN.match(line)
        if not label_match:
            continue
        value = line[label_match.end():].strip()
        value = _NAME_VALUE_PREFIX_PATTERN.sub("", value).strip()
        value = _normalize_ws(value)
        if value and not value[0].isdigit():
            return value
    return None


def _imo_pattern(imo: int) -> re.Pattern:
    """Whole-number match for a 7-digit IMO number - bounded on both sides by
    non-digits so it can't match as a substring of some longer number (e.g.
    a phone number, or a different vessel's ENI/callsign that happens to
    embed the same 7 digits)."""
    return re.compile(r"(?<!\d)" + re.escape(str(imo)) + r"(?!\d)")


def find_matches(text: str, vessel_name: str | None = None, imo: int | None = None):
    lines = [line.rstrip() for line in text.splitlines()]
    name_re = _name_pattern(vessel_name) if vessel_name else None
    imo_re = _imo_pattern(imo) if imo else None

    # Set when the document's own declared-name field confirms identity for
    # the whole document (see below) - every hit found under that condition
    # is tagged "declared_name" rather than going through the per-hit check.
    doc_identity = None

    if vessel_name:
        declared_name = _extract_declared_name(text)
        if declared_name is not None and declared_name.upper() == _normalize_ws(vessel_name).upper():
            name_re = None  # document's own declared name already confirms identity
            doc_identity = "declared_name"
        elif imo_re is not None and not imo_re.search(text):
            # No declared-name field confirms this document as ours (either
            # there isn't one, or it names a different vessel) - and we know
            # this vessel's IMO, yet it doesn't appear anywhere in the
            # document at all. A bare name-proximity match alone is too weak
            # to trust in that situation: a short/common vessel name (e.g.
            # "ESSENCE") readily collides with unrelated content that just
            # happens to be about a same-named *something else* - a yacht
            # brand's own product page, in one observed case, complete with
            # its own genuine "Air draft" spec for an unrelated small boat.
            # Real ship-particulars documents (what this tool actually
            # targets) essentially always state the IMO alongside other
            # specs, so this costs little real recall - it mainly screens
            # out exactly this kind of coincidental name collision. (When
            # the IMO is unknown, there's nothing to cross-check with, so
            # this check is skipped entirely and name-proximity remains the
            # only available signal, same as before.)
            return []
        # else: either the document declares a different vessel but our IMO
        # does appear somewhere in it (e.g. a fleet sheet listing several
        # sister ships, ours included further down), or IMO is unknown to us
        # entirely - fall through to the per-hit name-OR-IMO proximity check
        # below, so a hit is only accepted if OUR name or IMO is actually
        # near it.

    hits = []
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        low = stripped.lower()

        end_idx = None
        tier = None
        for keyword in PRIMARY_KEYWORDS:
            idx = low.find(keyword)
            if idx != -1:
                end_idx = idx + len(keyword)
                tier = "primary"
                break
        if end_idx is None:
            for keyword in FALLBACK_KEYWORDS:
                idx = low.find(keyword)
                if idx != -1:
                    end_idx = idx + len(keyword)
                    tier = "fallback"
                    break

        if end_idx is None:
            continue

        if name_re is not None:
            window = "\n".join(lines[max(0, i - NAME_PROXIMITY_LINES): i + NAME_PROXIMITY_LINES])
            name_ok = _name_confirmed_nearby(window, name_re)
            imo_ok = imo_re is not None and imo_re.search(window) is not None
            if not name_ok and not imo_ok:
                continue
            identity = "imo" if imo_ok else "name_proximity"
        else:
            identity = doc_identity or "unconfirmed"

        value = find_value_near(lines, i, end_idx)
        context = " | ".join(c.strip() for c in lines[max(0, i - 1): i + 3] if c.strip())
        hits.append({"line": stripped, "value": value, "context": context, "tier": tier, "identity": identity})
    return hits


def _parse_number(raw: str) -> float:
    raw = raw.strip()
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")  # comma = thousands separator
    elif "," in raw:
        raw = raw.replace(",", ".")  # comma = decimal separator (European format)
    return float(raw)


def parse_value_to_m_ft(value_str: str) -> tuple[float, float] | None:
    match = VALUE_PATTERN.search(value_str)
    if not match:
        return None
    number = _parse_number(match.group(1))
    unit = match.group(2).lower()
    if unit in ("ft", "feet"):
        return number * 0.3048, number
    return number, number * FT_PER_M


def _fetch_text(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "").lower()
    is_pdf = "pdf" in content_type or resp.content[:5] == b"%PDF-"
    if is_pdf:
        return fetch_pdf_text(resp.content)
    return BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True)


def resolve_air_draft(vessel_name: str, imo: int | None = None) -> LookupResult:
    candidates = _search_candidates(vessel_name)
    if not candidates:
        raise LookupError(f"no search results for {vessel_name!r}")

    keyword_only_hit = None
    fallback_hit = None
    any_fetch_succeeded = False

    for idx, (title, url) in enumerate(candidates[:MAX_PAGES_TO_FETCH]):
        if idx > 0:
            time.sleep(PAGE_FETCH_DELAY_SECONDS)
        try:
            text = _fetch_text(url)
        except Exception:
            continue
        any_fetch_succeeded = True

        for hit in find_matches(text, vessel_name, imo):
            if hit["value"]:
                parsed = parse_value_to_m_ft(hit["value"])
                if parsed:
                    meters, feet = parsed
                    status = "FLAGGED" if feet > db.get_height_threshold_ft() else "OK"
                    result = LookupResult(
                        status, hit["value"], meters, feet, url, title, hit["context"], hit["identity"]
                    )
                    if hit["tier"] == "primary":
                        return result
                    if fallback_hit is None:
                        fallback_hit = result
                    continue
            # A value-less hit is already the weakest kind of evidence (a
            # bare keyword with no parsed number), so it's only surfaced as
            # a "check this by hand" source when identity is confirmed by a
            # strong signal (the document's own declared name, or a matching
            # IMO) - not by name-proximity alone, which false-positives
            # readily on a short/common vessel name (e.g. "FOREVER") next to
            # an unrelated use of a keyword like "air draught" (which is
            # also just ordinary British English for a cold air current -
            # see e.g. draught-excluder/weatherstripping product listings).
            if keyword_only_hit is None and hit["identity"] in ("declared_name", "imo"):
                keyword_only_hit = (url, title, hit["context"], hit["identity"])

    if fallback_hit:
        return fallback_hit

    if keyword_only_hit:
        url, title, context, identity = keyword_only_hit
        return LookupResult("UNKNOWN", None, None, None, url, title, context, identity)

    if not any_fetch_succeeded:
        raise LookupError(f"all page fetches failed for {vessel_name!r}")

    return LookupResult("UNKNOWN", None, None, None, None, None, None)
