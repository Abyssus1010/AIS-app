"""Resolves a vessel's name (and IMO, if available) from its MMSI alone, for
vessels AIS hasn't yet delivered a static-data message for. This is what's
left of the old air_draft_resolver.py after the web-search-based air draft
lookup was dropped in favor of the type+size table (see
vessel_height_table.py) - MMSI-to-name resolution is a genuinely separate
concern (identifying an unnamed vessel, not measuring its height) that
happens to share the same LangSearch backend.

Search backend: LangSearch's Web Search API (https://api.langsearch.com/v1/web-search).
See the git history of air_draft_resolver.py for why this replaced
DuckDuckGo's HTML endpoint (html.duckduckgo.com was unreachable outright from
every network tested, not just blocked for one IP).
"""

import logging
import re
import threading
import time

import requests
from bs4 import BeautifulSoup

from app import config
from app.vessel_height_table import Category

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

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

# The same "NAME, Type - IMO x, MMSI y - Site" title shape used for name
# resolution also carries a human-readable vessel type as its second segment
# (e.g. "Crude Oil Tanker", "Bulk Carrier", "Ro-ro/passenger Ship") - captured
# up to the next " - IMO"/"- MMSI"/", MMSI" boundary rather than by a naive
# second comma/hyphen split, since the type text itself often contains
# hyphens ("Ro-ro"). ":" is in the character class for MyShipTracking's
# "Type (IMO: x, MMSI: y)" shape - without it the lazy match can't cross the
# colon in "IMO:"/"MMSI:" to reach the boundary at all, so it fails outright
# (observed in practice: silently dropped category on ~30 real vessels whose
# name/IMO resolved fine via the very same title).
TYPE_SEGMENT_PATTERN = re.compile(r"^\s*([A-Za-z][\w/&(): -]*?)\s*[-–—,]\s*(?:IMO|MMSI)\b", re.IGNORECASE)

# Checked in order, first match wins - more specific categories (tanker,
# passenger) before the CARGO catch-all, since a title like "Ro-ro/passenger
# Ship" would otherwise match CARGO's "ro-ro" keyword first.
_TYPE_TEXT_TO_CATEGORY: list[tuple[re.Pattern, Category]] = [
    (re.compile(r"\b(lng|lpg|oil|chemical|product|crude|tanker)\b", re.IGNORECASE), Category.TANKER),
    (re.compile(r"\b(passenger|cruise|ferry)\b", re.IGNORECASE), Category.PASSENGER),
    (re.compile(r"\bfishing\b", re.IGNORECASE), Category.FISHING),
    (
        re.compile(
            r"\b(tug|pilot|search and rescue|\bsar\b|port tender|law enforcement|"
            r"anti-pollution|offshore supply|supply vessel)\b",
            re.IGNORECASE,
        ),
        Category.SMALL_WORKBOAT,
    ),
    (re.compile(r"\bhigh[- ]?speed craft\b", re.IGNORECASE), Category.HIGH_SPEED_CRAFT),
    # "sailing" alone false-positives on MyShipTracking's boilerplate summary
    # sentence present on every vessel's page regardless of type ("... is a
    # Cargo It's sailing under the flag of [HK] Hong Kong") - observed in
    # practice misclassifying real container ships (e.g. OOCL HONG KONG) as
    # mast-driven. The negative lookahead excludes that specific "sailing
    # under (the flag)" phrasing while still matching genuine type text like
    # "Sailing Vessel" or bare "Sailing".
    (re.compile(r"\b(sailing\b(?!\s+under)|yacht|pleasure)\b", re.IGNORECASE), Category.MAST_DRIVEN),
    (re.compile(r"\b(cargo|bulk|container|ro-?ro|vehicles carrier)\b", re.IGNORECASE), Category.CARGO),
]


def _type_text_from_title(title: str) -> str | None:
    parts = NAME_TITLE_SPLIT_PATTERN.split(title, maxsplit=1)
    if len(parts) < 2:
        return None
    match = TYPE_SEGMENT_PATTERN.match(parts[1])
    if not match:
        return None
    text = match.group(1).strip()
    return text or None


def _category_from_title(title: str) -> Category | None:
    text = _type_text_from_title(title)
    if not text:
        return None
    for pattern, category in _TYPE_TEXT_TO_CATEGORY:
        if pattern.search(text):
            return category
    return None


def _category_from_text(text: str) -> Category | None:
    """Looser fallback for when the title's strict segment format doesn't
    parse (see _category_from_title) - just keyword-searches the result's
    full snippet/summary text, which prose-style listings (DuckDuckGo) and
    structured fact-sheet listings (LangSearch) both tend to state a type in
    somewhere even when the title doesn't."""
    for pattern, category in _TYPE_TEXT_TO_CATEGORY:
        if pattern.search(text):
            return category
    return None


# Search-result snippets come back with odd whitespace normalization around
# punctuation (observed in practice: "330 . 0 m", "152 , 740") - this allows
# whitespace inside the numeric part so "330 . 0" still parses as one number,
# and _clean_number strips it back out before float() sees it.
_NUM = r"(\d[\d\s]*(?:[.,]\s*\d+)?)"

# Checked in order - structured "label: value" listings (LangSearch's
# fact-sheet-style snippets) before looser prose patterns. Each must capture
# the LOA specifically, not beam/draught/tonnage, hence anchoring on the
# label text itself rather than just "a number followed by m".
_LOA_PATTERNS: list[re.Pattern] = [
    re.compile(rf"\bloa\s*:\s*{_NUM}", re.IGNORECASE),
    re.compile(rf"\blength\s*beam\s*:\s*{_NUM}\s*/\s*\d", re.IGNORECASE),
    re.compile(rf"\blength\s*:\s*{_NUM}\s*m\b", re.IGNORECASE),
    re.compile(rf"\btaille\s+{_NUM}\s*x\s*\d", re.IGNORECASE),  # MyShipTracking's French locale: "taille LxB m"
]

MIN_PLAUSIBLE_LOA_M = 5.0
MAX_PLAUSIBLE_LOA_M = 500.0


def _clean_number(raw: str) -> float | None:
    try:
        return float(re.sub(r"\s+", "", raw).replace(",", "."))
    except ValueError:
        return None


def _length_m_from_text(text: str) -> float | None:
    for pattern in _LOA_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = _clean_number(match.group(1))
        if value is not None and MIN_PLAUSIBLE_LOA_M <= value <= MAX_PLAUSIBLE_LOA_M:
            return value
    return None


_rate_limit_lock = threading.Lock()
_last_request_at = 0.0
MAX_429_RETRIES = 3


def _throttle() -> None:
    """Enforces SEARCH_REQUEST_DELAY_SECONDS between LangSearch calls across
    every caller (the LOOKUP_CONCURRENCY-sized name-guess pool and the IMO/
    category backfill loop all call langsearch_search independently, with no
    coordination otherwise) - a plain threading.Lock, not asyncio, since
    these run in worker threads via asyncio.to_thread."""
    global _last_request_at
    with _rate_limit_lock:
        wait = _last_request_at + config.SEARCH_REQUEST_DELAY_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def langsearch_search(query: str, max_results: int = 5):
    for attempt in range(MAX_429_RETRIES + 1):
        _throttle()
        resp = requests.post(
            LANGSEARCH_URL,
            json={"query": query, "count": max_results, "freshness": "noLimit"},
            headers={
                "Authorization": f"Bearer {config.LANGSEARCH_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        if resp.status_code == 429 and attempt < MAX_429_RETRIES:
            backoff = config.SEARCH_REQUEST_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "LangSearch rate-limited (attempt %d/%d), backing off %.1fs",
                attempt + 1, MAX_429_RETRIES, backoff,
            )
            time.sleep(backoff)
            continue
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 200:
            raise RuntimeError(f"LangSearch API error: {data.get('code')} {data.get('msg')}")

        values = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
        return [(v["name"], v["url"], v.get("summary") or v.get("snippet") or "") for v in values[:max_results]]


DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"

_ddg_rate_limit_lock = threading.Lock()
_ddg_last_request_at = 0.0


def _ddg_throttle() -> None:
    global _ddg_last_request_at
    with _ddg_rate_limit_lock:
        wait = _ddg_last_request_at + config.DUCKDUCKGO_REQUEST_DELAY_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _ddg_last_request_at = time.monotonic()


def duckduckgo_search(query: str, max_results: int = 5):
    """Fallback search backend for when LangSearch fails or turns up no
    confirmed match. Re-tested 2026-09 and found reachable again (the old
    air_draft_resolver-era block was network-specific, not permanent) -
    but only via POST: a GET with `q` as a query param (what a browser's
    address bar would send) gets back an empty bot-check shell (HTTP 202,
    no results), while POSTing form data the way the no-JS HTML page itself
    submits returns real, unwrapped result links in the same
    VesselFinder/MarineTraffic/etc. format LangSearch returns - so it plugs
    into the same _name_from_url/_name_from_title parsing untouched."""
    _ddg_throttle()
    resp = requests.post(
        DUCKDUCKGO_URL,
        data={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for result in soup.select(".result")[:max_results]:
        a = result.select_one(".result__a")
        if not a:
            continue
        href, title = a.get("href"), a.get_text(strip=True)
        if not (href and title):
            continue
        snippet = result.select_one(".result__snippet")
        results.append((title, href, snippet.get_text(" ", strip=True) if snippet else ""))
    return results


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


def _match_from_results(
    results, mmsi: int
) -> tuple[str, int | None, Category | None, float | None] | None:
    """Shared confirmed-match logic for either search backend's result
    listing - only returns a name when the searched-for MMSI is actually
    confirmed somewhere in that result (its URL or its title). An
    unconfirmed guess (e.g. an unrelated page that happens to rank for the
    query) risks silently mislabeling the vessel, which is worse than
    leaving it unnamed for another retry.

    Category and LOA both come from the same confirmed result - category
    from its title first, falling back to a looser keyword search over its
    snippet/summary text (see _category_from_title / _category_from_text);
    LOA only from the snippet/summary, since titles essentially never state
    it (see _length_m_from_text). Both are None whenever that text doesn't
    parse or doesn't contain a recognizable value, which callers must
    handle the same as "AIS hasn't said either"."""
    for title, url, text in results:
        from_url = _name_from_url(url, mmsi)
        if from_url:
            name, imo = from_url
            category = _category_from_title(title) or _category_from_text(text)
            return name, imo, category, _length_m_from_text(text)

        if str(mmsi) in title:
            from_title = _name_from_title(title)
            if from_title:
                name, imo = from_title
                category = _category_from_title(title) or _category_from_text(text)
                return name, imo, category, _length_m_from_text(text)
    return None


def resolve_vessel_name(
    mmsi: int,
) -> tuple[str, int | None, Category | None, float | None] | None:
    """Looks up a vessel's name, IMO, height-estimate category, and length
    overall from its MMSI alone. Reads the search engine's own result
    listing rather than fetching the target page: vessel-database sites like
    VesselFinder/MarineTraffic are commonly Cloudflare-blocked for direct
    fetches, but the result listing itself is enough.

    Tries LangSearch first, falling back to DuckDuckGo (see
    duckduckgo_search) if LangSearch errors out or comes back with no
    confirmed match - two independent backends cost little extra (DuckDuckGo
    is free, unauthenticated) and cover each other's outages/rate limits.

    The query is quoted as an exact phrase: a bare, unquoted "MMSI 1234567"
    measurably underperforms on LangSearch, returning generic MMSI-explainer/
    regulatory pages instead of the vessel's own tracker pages - quoting it
    consistently surfaces the real per-vessel pages (VesselFinder,
    MyShipTracking, etc.) instead."""
    query = f'"MMSI {mmsi}"'

    try:
        match = _match_from_results(langsearch_search(query, max_results=5), mmsi)
        if match is not None:
            return match
    except Exception:
        logger.warning("LangSearch lookup failed for mmsi=%s, falling back to DuckDuckGo", mmsi, exc_info=True)

    try:
        return _match_from_results(duckduckgo_search(query, max_results=5), mmsi)
    except Exception:
        logger.warning("DuckDuckGo fallback also failed for mmsi=%s, will retry later", mmsi, exc_info=True)
        return None
