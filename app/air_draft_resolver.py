"""Air draft resolver for the web app: ported from the standalone
air_draft_lookup.py CLI tool, with the Playwright/JS-render fallback and the
Windows/conda tesseract path shim dropped (this only ever runs inside the
Linux container), and structured LookupResult return values instead of
print()/file logging.

See air_draft_lookup.py's module docstring for the full rationale behind the
approach (why air draft isn't on VesselFinder/MarineTraffic, why plain draft
is never accepted as a fallback, why scanned PDFs need OCR).
"""

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

from app import config

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

SEARCH_QUERIES = [
    "{name} air draft",
    "{name} air draught",
    "{name} ship pdf",
]

MAX_RESULTS_PER_QUERY = 5
MAX_PAGES_TO_FETCH = 12

# Vessel-database sites (VesselFinder, MarineTraffic, etc.) consistently
# title their per-ship pages "NAME, Type - ... - IMO x, MMSI y - Site", so
# the vessel name is reliably the first comma/dash-delimited segment - except
# some sites (MarineTraffic observed in practice) inject a stray space into
# the name in the <title> itself (e.g. "GR ANDE SHANGHAI"), so a title match
# alone isn't trustworthy. URL slugs are cleaner and often embed the MMSI
# right next to the name (e.g. ".../grande-shanghai-mmsi-247416200-imo-...",
# ".../mmsi:247416200/.../vessel:GRANDE_SHANGHAI") - prefer those when the
# MMSI in the URL confirms the match, and only fall back to a title guess
# when no URL yields a confirmed one.
NAME_TITLE_SPLIT_PATTERN = re.compile(r"\s*[,–—-]\s*")
TITLE_IMO_PATTERN = re.compile(r"IMO\s*[:#]?\s*(\d{7})", re.IGNORECASE)
URL_SLUG_MMSI_PATTERN = re.compile(r"/([a-z0-9]+(?:-[a-z0-9]+)+)-mmsi-(\d+)(?:-imo-(\d+))?", re.IGNORECASE)
MAX_RESOLVED_NAME_LENGTH = 60

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


def duckduckgo_search(query: str, max_results: int = MAX_RESULTS_PER_QUERY):
    resp = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    for a in soup.select("a.result__a"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if href:
            results.append((title, href))
        if len(results) >= max_results:
            break
    return results


def _name_from_url(url: str, mmsi: int) -> tuple[str, int | None] | None:
    match = URL_SLUG_MMSI_PATTERN.search(url)
    if match and match.group(2) == str(mmsi):
        name = match.group(1).replace("-", " ").upper()
        imo = int(match.group(3)) if match.group(3) else None
        return name, imo

    fields = {}
    for segment in url.split("/"):
        for key in ("mmsi", "imo", "vessel"):
            prefix = f"{key}:"
            if segment.lower().startswith(prefix):
                fields[key] = segment[len(prefix):]
    if fields.get("mmsi") == str(mmsi) and fields.get("vessel"):
        name = fields["vessel"].replace("_", " ").replace("-", " ").strip().upper()
        imo = int(fields["imo"]) if fields.get("imo", "").isdigit() else None
        return name, imo
    return None


def _name_from_title(title: str) -> tuple[str, int | None] | None:
    name = NAME_TITLE_SPLIT_PATTERN.split(title, maxsplit=1)[0]
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip(" \"'")
    if not name or name.isdigit() or len(name) > MAX_RESOLVED_NAME_LENGTH:
        return None
    imo_match = TITLE_IMO_PATTERN.search(title)
    return name, (int(imo_match.group(1)) if imo_match else None)


def resolve_vessel_name(mmsi: int) -> tuple[str, int | None] | None:
    """Looks up a vessel's name (and IMO, if available) from its MMSI alone,
    for vessels AIS hasn't yet delivered a static-data message for. Reads
    DuckDuckGo's own search results rather than fetching the target page:
    vessel-database sites like VesselFinder/MarineTraffic are commonly
    Cloudflare-blocked for direct fetches (see air_draft_lookup.py's
    docstring), but the result listing itself is enough.

    Only returns a name when the searched-for MMSI is actually confirmed
    somewhere in that result (its URL or its title) - an unconfirmed guess
    (e.g. an unrelated page that happens to rank for the query) risks
    silently mislabeling the vessel, which is worse than leaving it PENDING
    for another retry. In testing, real MMSIs consistently resolve via a
    confirmed match, so this doesn't cost much recall."""
    try:
        results = duckduckgo_search(f"MMSI {mmsi}", max_results=5)
    except Exception:
        return None

    for title, url in results:
        from_url = _name_from_url(url, mmsi)
        if from_url:
            return from_url

        if str(mmsi) in title:
            from_title = _name_from_title(title)
            if from_title:
                return from_title
    return None


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


def find_matches(text: str, vessel_name: str | None = None):
    lines = [line.rstrip() for line in text.splitlines()]
    name_re = _name_pattern(vessel_name) if vessel_name else None

    if vessel_name:
        declared_name = _extract_declared_name(text)
        if declared_name is not None:
            if declared_name.upper() != _normalize_ws(vessel_name).upper():
                return []
            name_re = None  # document's own declared name already confirms identity

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
            window = lines[max(0, i - NAME_PROXIMITY_LINES): i + NAME_PROXIMITY_LINES]
            if not _name_confirmed_nearby("\n".join(window), name_re):
                continue

        value = find_value_near(lines, i, end_idx)
        context = " | ".join(c.strip() for c in lines[max(0, i - 1): i + 3] if c.strip())
        hits.append({"line": stripped, "value": value, "context": context, "tier": tier})
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


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def resolve_air_draft(vessel_name: str) -> LookupResult:
    seen_urls = set()
    candidates = []
    for i, template in enumerate(SEARCH_QUERIES):
        if i > 0:
            time.sleep(config.DDG_REQUEST_DELAY_SECONDS)
        query = template.format(name=vessel_name)
        try:
            results = duckduckgo_search(query)
        except Exception:
            continue
        for title, url in results:
            if url not in seen_urls:
                seen_urls.add(url)
                candidates.append((title, url))

    if not candidates:
        raise LookupError(f"no search results for {vessel_name!r}")

    keyword_only_hit = None
    fallback_hit = None
    any_fetch_succeeded = False

    for idx, (title, url) in enumerate(candidates[:MAX_PAGES_TO_FETCH]):
        if idx > 0:
            time.sleep(config.DDG_REQUEST_DELAY_SECONDS)
        try:
            text = _fetch_text(url)
        except Exception:
            continue
        any_fetch_succeeded = True

        for hit in find_matches(text, vessel_name):
            if hit["value"]:
                parsed = parse_value_to_m_ft(hit["value"])
                if parsed:
                    meters, feet = parsed
                    status = "FLAGGED" if feet > config.AIR_DRAFT_THRESHOLD_FT else "OK"
                    result = LookupResult(status, hit["value"], meters, feet, url, title, hit["context"])
                    if hit["tier"] == "primary":
                        return result
                    if fallback_hit is None:
                        fallback_hit = result
                    continue
            if keyword_only_hit is None:
                keyword_only_hit = (url, title, hit["context"])

    if fallback_hit:
        return fallback_hit

    if keyword_only_hit:
        url, title, context = keyword_only_hit
        return LookupResult("UNKNOWN", None, None, None, url, title, context)

    if not any_fetch_succeeded:
        raise LookupError(f"all page fetches failed for {vessel_name!r}")

    return LookupResult("UNKNOWN", None, None, None, None, None, None)
