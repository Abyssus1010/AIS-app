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
#
# Like DEFAULT_HEIGHT_THRESHOLD_FT below, this is only the SEED value for
# the DB-backed setting (db.init_schema) the first time the app runs against
# a given DB. The live zone - user-adjustable from the dashboard - lives in
# the `settings` table from then on (db.get_bounding_box/set_bounding_box);
# ais_client.py reads that on every (re)connect rather than this constant.
_HARDCODED_DEFAULT_BOUNDING_BOX = [[1.1558, 103.9165], [1.3224, 104.0832]]
DEFAULT_BOUNDING_BOX = (
    json.loads(os.environ["AIS_BOUNDING_BOX"])
    if os.environ.get("AIS_BOUNDING_BOX")
    else _HARDCODED_DEFAULT_BOUNDING_BOX
)

# Single flag threshold, compared against whichever height figure is
# authoritative for a vessel - a confirmed web-search air draft (see
# air_draft_resolver.py) when one was found, else the type+size table's
# estimate (see vessel_height_table.py) as a fallback. One shared threshold
# for both rather than reviving a separate AIR_DRAFT_THRESHOLD_FT, since a
# vessel can only have one live verdict at a time regardless of which method
# produced it.
#
# This is only the SEED value, used once to populate the DB-backed setting
# (db.init_schema) the first time the app ever runs against a given DB. The
# live value users actually change from the dashboard lives in the
# `settings` table from then on (db.get_height_threshold_ft /
# set_height_threshold_ft) and survives restarts/redeploys, unlike this
# module-level constant, which is fixed at process start from the
# environment. Every call site that needs the *current* threshold - not just
# the fallback seed - calls db.get_height_threshold_ft() instead of reading
# this constant directly.
DEFAULT_HEIGHT_THRESHOLD_FT = float(os.environ.get("HEIGHT_THRESHOLD_FT", 70))
SILENCE_WINDOW_MINUTES = float(os.environ.get("SILENCE_WINDOW_MINUTES", 30))
LOOKUP_CONCURRENCY = int(os.environ.get("LOOKUP_CONCURRENCY", 3))

# Master switch for the web-search air draft lookup (see
# air_draft_resolver.py / worker.run_air_draft_lookup) - it is far more
# expensive per vessel than the name/IMO/category lookups (up to 3 search
# queries, each fanning out to MAX_PAGES_TO_FETCH page/PDF fetches, some
# requiring OCR), so this exists to be able to turn it off without a code
# change if it ends up costing more LangSearch quota than expected.
AIR_DRAFT_LOOKUP_ENABLED = os.environ.get("AIR_DRAFT_LOOKUP_ENABLED", "true").lower() not in ("false", "0", "")

# How long to wait before retrying a vessel whose air draft search completed
# but found nothing confirmed (status UNKNOWN) - deliberately much longer
# than STALE_SWEEP_INTERVAL_SECONDS (worker.py's normal 10-minute sweep, used
# for cheap lookups and for vessels never yet attempted): an UNKNOWN result
# already cost a full multi-query, multi-page search, and a vessel with no
# public particulars sheet indexed anywhere is unlikely to have one indexed
# 10 minutes later - retrying that often would burn quota for no benefit.
AIR_DRAFT_RETRY_HOURS = float(os.environ.get("AIR_DRAFT_RETRY_HOURS", 12))

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
