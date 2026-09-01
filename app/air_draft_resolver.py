"""Air draft resolver for the web app: ported from the standalone
air_draft_lookup.py CLI tool, with the Playwright/JS-render fallback and the
Windows/conda tesseract path shim dropped (this only ever runs inside the
Linux container), and structured LookupResult return values instead of
print()/file logging.

See air_draft_lookup.py's module docstring for the full rationale behind the
approach (why air draft isn't on VesselFinder/MarineTraffic, why plain draft
is never accepted as a fallback, why scanned PDFs need OCR).

Search backend: LangSearch's Web Search API (https://api.langsearch.com/v1/web-search),
not DuckDuckGo's HTML endpoint. html.duckduckgo.com turned out to be
unreachable outright (connection refused/timed out) from every network
tested, including a completely independent vantage point - not a scraping
block against one IP, but the endpoint itself being down or hostile at the
TCP level. Separately, a keyed API beats scraping an undocumented HTML
selector (`a.result__a`) that can change layout with no notice.
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

LANGSEARCH_URL = "https://api.langsearch.com/v1/web-search"

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
# Some vessel-tracker sites (Trackipi observed in practice) title a vessel
# they have no real name for as "MMSI 241436000, Tanker Vessel" instead of
# omitting a name entirely - that first segment isn't a name at all, just the
# MMSI we searched for spelled back out, so it must be rejected explicitly
# (name.isdigit() alone doesn't catch it, since "MMSI 241436000" isn't a
# pure-digit string).
MMSI_PLACEHOLDER_PATTERN = re.compile(r"^mmsi\s*[:#]?\s*\d+$", re.IGNORECASE)
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
    # How the fetched page was confirmed to be about the target vessel:
    # "declared_name" (the page's own NAME: field) and "imo" (the target's
    # IMO appears next to the match) are strong; "name_proximity" is weak -
    # only the vessel's name appears nearby, which collides readily on
    # short/common names (see find_matches). Persisted so a later-arriving
    # IMO can force a re-resolve of anything accepted on the weak signal
    # (see db.set_imo). None for an UNKNOWN with no source at all.
    identity: Literal["declared_name", "imo", "name_proximity", "unconfirmed"] | None = None


def langsearch_search(query: str, max_results: int = MAX_RESULTS_PER_QUERY):
    resp = requests.post(
        LANGSEARCH_URL,
        json={"query": query, "count": max_results, "freshness": "noLimit"},
        headers={
            "Authorization": f"Bearer {config.LANGSEARCH_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"LangSearch API error: {data.get('code')} {data.get('msg')}")

    values = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
    return [(v["name"], v["url"]) for v in values[:max_results]]


def _valid_imo(value) -> int | None:
    """Coerce a parsed IMO to a real one or None. Vessel-tracker URLs and
    titles routinely use `imo:0` / `-imo-0` / "IMO 0" as a placeholder for
    "no IMO on file" (MyShipTracking and MarineTraffic both do this for
    craft without one), and AIS itself transmits 0 the same way - storing
    that 0 is worse than storing nothing: it blocks the IMO backfill (which
    only looks at NULL rows) and can't serve as a cross-check. Anything
    outside the 7-digit IMO range is treated as absent."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1_000_000 <= n <= 9_999_999 else None


def _name_from_url(url: str, mmsi: int) -> tuple[str, int | None] | None:
    match = URL_SLUG_MMSI_PATTERN.search(url)
    if match and match.group(2) == str(mmsi):
        name = match.group(1).replace("-", " ").upper()
        return name, _valid_imo(match.group(3))

    fields = {}
    for segment in url.split("/"):
        for key in ("mmsi", "imo", "vessel"):
            prefix = f"{key}:"
            if segment.lower().startswith(prefix):
                fields[key] = segment[len(prefix):]
    if fields.get("mmsi") == str(mmsi) and fields.get("vessel"):
        name = fields["vessel"].replace("_", " ").replace("-", " ").strip().upper()
        return name, _valid_imo(fields.get("imo"))
    return None


def _name_from_title(title: str) -> tuple[str, int | None] | None:
    name = NAME_TITLE_SPLIT_PATTERN.split(title, maxsplit=1)[0]
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip(" \"'")
    if (
        not name
        or name.isdigit()
        or len(name) > MAX_RESOLVED_NAME_LENGTH
        or MMSI_PLACEHOLDER_PATTERN.match(name)
    ):
        return None
    imo_match = TITLE_IMO_PATTERN.search(title)
    return name, (_valid_imo(imo_match.group(1)) if imo_match else None)


def resolve_vessel_name(mmsi: int) -> tuple[str, int | None] | None:
    """Looks up a vessel's name (and IMO, if available) from its MMSI alone,
    for vessels AIS hasn't yet delivered a static-data message for. Reads the
    search engine's own result listing rather than fetching the target page:
    vessel-database sites like VesselFinder/MarineTraffic are commonly
    Cloudflare-blocked for direct fetches (see air_draft_lookup.py's
    docstring), but the result listing itself is enough.

    Only returns a name when the searched-for MMSI is actually confirmed
    somewhere in that result (its URL or its title) - an unconfirmed guess
    (e.g. an unrelated page that happens to rank for the query) risks
    silently mislabeling the vessel, which is worse than leaving it PENDING
    for another retry. In testing, real MMSIs consistently resolve via a
    confirmed match, so this doesn't cost much recall.

    The query is quoted as an exact phrase for the same reason SEARCH_QUERIES
    is: a bare, unquoted "MMSI 1234567" measurably underperforms on
    LangSearch, returning generic MMSI-explainer/regulatory pages instead of
    the vessel's own tracker pages - quoting it consistently surfaces the
    real per-vessel pages (VesselFinder, MyShipTracking, etc.) instead."""
    try:
        results = langsearch_search(f'"MMSI {mmsi}"', max_results=5)
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


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def resolve_air_draft(vessel_name: str, imo: int | None = None) -> LookupResult:
    seen_urls = set()
    candidates = []
    for i, template in enumerate(SEARCH_QUERIES):
        if i > 0:
            time.sleep(config.SEARCH_REQUEST_DELAY_SECONDS)
        query = template.format(name=vessel_name)
        try:
            results = langsearch_search(query)
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
            time.sleep(config.SEARCH_REQUEST_DELAY_SECONDS)
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
                    status = "FLAGGED" if feet > config.AIR_DRAFT_THRESHOLD_FT else "OK"
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
