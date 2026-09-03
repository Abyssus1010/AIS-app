import json
import os

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ["AISSTREAM_API_KEY"]
LANGSEARCH_API_KEY = os.environ["LANGSEARCH_API_KEY"]

# Monitoring zone: the smallest rectangle enclosing a +/-30deg cone that
# extends 10 NM south from Changi's easternmost runway (02R/20L southern
# threshold, 1.32239N 103.99985E - the point on that runway closest to the
# sea). AISStream only supports a rectangular bounding box, so this covers
# the cone plus some extra area outside it rather than the cone exactly.
_DEFAULT_BOUNDING_BOX = [[1.1558, 103.9165], [1.3224, 104.0832]]
BOUNDING_BOX = (
    json.loads(os.environ["AIS_BOUNDING_BOX"])
    if os.environ.get("AIS_BOUNDING_BOX")
    else _DEFAULT_BOUNDING_BOX
)

# Compared against the type+size table's height estimate (see
# vessel_height_table.py) - not a web-search-derived air draft anymore, but
# kept as a single flag threshold the way AIR_DRAFT_THRESHOLD_FT was.
HEIGHT_THRESHOLD_FT = float(os.environ.get("HEIGHT_THRESHOLD_FT", 70))
SILENCE_WINDOW_MINUTES = float(os.environ.get("SILENCE_WINDOW_MINUTES", 30))
LOOKUP_CONCURRENCY = int(os.environ.get("LOOKUP_CONCURRENCY", 3))

# Minimum spacing enforced between LangSearch API calls (see
# vessel_name_lookup._throttle) - LangSearch's rate limit is tight enough
# that LOOKUP_CONCURRENCY workers calling it independently trip 429s
# routinely; this serializes every caller (the name-guess pool, the IMO/
# category backfill loop) behind one shared minimum interval instead.
SEARCH_REQUEST_DELAY_SECONDS = float(os.environ.get("SEARCH_REQUEST_DELAY_SECONDS", 2.0))

# Same idea as SEARCH_REQUEST_DELAY_SECONDS but for the DuckDuckGo HTML
# fallback (see vessel_name_lookup.duckduckgo_search) - kept as a separate
# knob since it's a different, unauthenticated backend with its own
# (undocumented) tolerance for request rate.
DUCKDUCKGO_REQUEST_DELAY_SECONDS = float(os.environ.get("DUCKDUCKGO_REQUEST_DELAY_SECONDS", 2.0))

# How long a vessel's row is kept after its last position report before the
# purge sweep (worker.run_stale_purge) deletes it outright - independent of
# SILENCE_WINDOW_MINUTES, which only hides a vessel from the dashboard/lookup
# workers without ever removing its row. Comfortably larger than the silence
# window so nothing gets purged while it could still reappear as "active".
PURGE_AFTER_HOURS = float(os.environ.get("PURGE_AFTER_HOURS", 4))

DB_PATH = os.environ.get("DB_PATH", "./ais.db")
