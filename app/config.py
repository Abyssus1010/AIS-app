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

AIR_DRAFT_THRESHOLD_FT = float(os.environ.get("AIR_DRAFT_THRESHOLD_FT", 70))
SILENCE_WINDOW_MINUTES = float(os.environ.get("SILENCE_WINDOW_MINUTES", 30))
UNKNOWN_RETRY_HOURS = float(os.environ.get("UNKNOWN_RETRY_HOURS", 1))
# How long to wait before re-queuing a vessel whose last air draft lookup
# failed outright (LookupError - the search or every page fetch errored, as
# opposed to a completed lookup that found nothing). Without this, such a
# vessel is re-queued on every 10-minute sweep and, if the failure is
# persistent (e.g. every search result is an un-fetchable giant PDF), burns
# a worker slot forever. Set well above the sweep interval so a transient
# blip costs at most one wasted attempt.
FAILED_LOOKUP_BACKOFF_MINUTES = float(os.environ.get("FAILED_LOOKUP_BACKOFF_MINUTES", 30))
LOOKUP_CONCURRENCY = int(os.environ.get("LOOKUP_CONCURRENCY", 3))
SEARCH_REQUEST_DELAY_SECONDS = float(os.environ.get("SEARCH_REQUEST_DELAY_SECONDS", 2.0))

DB_PATH = os.environ.get("DB_PATH", "./ais.db")
