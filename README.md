# AIS app

An air draft monitor for vessels in Singapore's eastern approaches. It
watches live AIS traffic, looks up each vessel's air draft (height above the
waterline) on the open web, and flags any vessel tall enough to be a hazard
to aircraft using Changi's runways - on a live-updating dashboard.

The repo has two parts:

- **`app/`** - the actual service: a FastAPI app that ingests AIS, persists
  vessel state, resolves air drafts in the background, and serves the
  dashboard. This is what `Dockerfile`/`docker-compose.yml` build and run.
- **`aisstream.py`** and **`air_draft_lookup.py`** - two standalone, independent
  scripts kept at the repo root for quick manual testing. `app/ais_client.py`
  and `app/air_draft_resolver.py` are productionized ports of these (adding
  DB persistence, structured results, retries) - see each script's own
  section below for what they do on their own.

---

## `app/` - the dashboard service

### What it does

1. **Ingests** live AIS position + static-data messages from
   [aisstream.io](https://aisstream.io) for a configured monitoring zone
   (`app/ais_client.py`), reconnecting with backoff on drops.
2. **Persists** every vessel's latest position and identity to a local
   SQLite DB (`app/db.py`).
3. The first time a vessel's name becomes known, it's **queued for an air
   draft lookup** (`app/worker.py` + `app/air_draft_resolver.py`), which
   searches the web, fetches pages/PDFs (OCR'ing scanned ones), and marks
   the vessel `OK`, `FLAGGED` (air draft over threshold), or `UNKNOWN` (no
   public source found). Before accepting a match, it checks the fetched
   page really is about the target vessel - a document's own declared
   `NAME:` field (common on ship particulars sheets) takes priority when
   present, since a page can otherwise look relevant while actually
   describing a different, similarly-named vessel (e.g. searching
   "NORTHWIND" surfacing a document for "INCE NORTHWIND", a different ship).
   Failed lookups and stale `UNKNOWN`s are automatically retried on a sweep.
4. Serves a **dashboard** (`app/main.py` + `app/templates/dashboard.html`)
   listing every vessel seen recently, sorted with `FLAGGED` vessels first.
   It polls `GET /api/vessels` every 10s to stay live.

### Vessel lifecycle: visibility vs. retries

- **Dashboard visibility** is driven entirely by `last_position_at`, the
  timestamp of the last AIS message received for that vessel - a vessel drops
  off the dashboard once it's gone quiet for `SILENCE_WINDOW_MINUTES`. That's
  usually because it left the monitoring zone (aisstream only forwards
  messages for vessels inside the bounding box), but the app can't actually
  distinguish that from an AIS coverage gap, the vessel's transponder going
  quiet, or a brief reconnect after a dropped stream - it only knows "no
  message arrived recently," not why.
- **Air draft retries are scoped to the same visibility window as the
  dashboard.** The background sweep (`app/worker.py`, every 10 minutes) only
  re-queues a vessel if it's also within `SILENCE_WINDOW_MINUTES` - a vessel
  that's gone quiet longer than that won't be retried until it's heard from
  again, so workers aren't spent re-resolving vessels nobody can currently
  see.
- Within that sweep, `PENDING` vessels (never successfully checked - either
  not looked at yet, or every attempt so far errored out) are retried on the
  next sweep. `UNKNOWN` vessels (a lookup completed but found nothing
  conclusive) are only retried once `last_checked_at` is older than
  `UNKNOWN_RETRY_HOURS`.
- Either way, a vessel whose **last attempt failed outright** (a
  `LookupError` - the search or every page fetch errored, as opposed to a
  completed lookup that found nothing) is held back for
  `FAILED_LOOKUP_BACKOFF_MINUTES` before the next try, rather than re-queued
  on every 10-minute sweep. `last_checked_at` only advances on a *completed*
  lookup; `last_attempted_at` advances on every attempt including failures,
  and is what the backoff is measured from. When attempts have failed since
  the last completed check, the dashboard's "Last checked" column keeps
  showing the completed-check age (that's when the shown result is from) and
  appends a muted "· recheck failing" - so a vessel stuck retrying doesn't
  look like it hasn't been checked in days, without a second timestamp
  competing with the first. The vessel detail page spells out both times.
- An `OK`/`FLAGGED` result is otherwise final and never re-run - **except**
  when it was accepted on the weak `name_proximity` signal (only the
  vessel's name, not its IMO or the page's own `NAME:` field, tied the page
  to the vessel) and an IMO later arrives for that vessel (via AIS static
  data or `run_imo_backfill`). The stronger IMO cross-check can now be
  applied, so the result is reset to `PENDING` and re-resolved. This is what
  catches a same-named-vessel false positive (e.g. a bunker tanker "TRINITY"
  that picked up the 25 m mast height of the replica carrack *Nao Trinidad*
  from a blog, because the lookup ran before the tanker's IMO was known).
- **Vessels with no name at all** (AIS hasn't yet delivered a static-data
  message for them - only position reports so far, shown on the dashboard as
  `MMSI <n>`) are handled separately: the same sweep also tries to resolve a
  name from the MMSI alone (`resolve_vessel_name` in
  `app/air_draft_resolver.py`), by reading vessel-database sites' DuckDuckGo
  search results for one that's confirmed to reference that exact MMSI, then
  proceeds to the normal air draft lookup using the resolved name. This is
  best-effort and only accepts a *confirmed* match (the MMSI must actually
  appear in that result's URL or title) - if nothing confirms, it stays
  `PENDING` and is retried on the next sweep, same as any other pending
  vessel, rather than risk mislabeling it with a guess.

### Monitoring zone

The default zone is the smallest rectangle enclosing a ±30° cone that
extends 10 NM south from Changi's easternmost runway (02R/20L's southern
threshold, 1.32239°N 103.99985°E - the point on that runway closest to the
sea). AISStream only supports a rectangular bounding box subscription, so
the rectangle covers some area outside the cone as well as the cone itself;
nothing inside the rectangle is filtered out further.

Override it with `AIS_BOUNDING_BOX` (see Configuration below) if the zone
needs to move or resize - it's just `[[sw_lat, sw_lon], [ne_lat, ne_lon]]`.

### Requires

Everything in `requirements.txt` (`websockets`, `python-dotenv`, `fastapi`,
`uvicorn[standard]`, `jinja2`, plus the lookup stack: `requests`,
`beautifulsoup4`, `pypdf`, `pymupdf`, `pytesseract`, `pillow`), and the
Tesseract OCR engine with English trained data for scanned PDFs (see the
`air_draft_lookup.py` section below for the install command - same
requirement, since `app/air_draft_resolver.py` uses the same OCR fallback).

### Configuration

Copy `.env.example` to `.env` and fill in an aisstream.io API key:

```
AISSTREAM_API_KEY=your-key-here
```

All other variables are optional (defaults live in `app/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `AIS_BOUNDING_BOX` | rectangle enclosing the Changi cone (see above) | `[[sw_lat, sw_lon], [ne_lat, ne_lon]]` for the AIS subscription |
| `AIR_DRAFT_THRESHOLD_FT` | `70` | Air draft above this (in feet) is flagged |
| `SILENCE_WINDOW_MINUTES` | `30` | How long a vessel stays on the dashboard after its last position report |
| `UNKNOWN_RETRY_HOURS` | `1` | How long before a vessel marked `UNKNOWN` is retried |
| `FAILED_LOOKUP_BACKOFF_MINUTES` | `30` | How long to hold back a vessel whose last lookup errored out before retrying it |
| `LOOKUP_CONCURRENCY` | `3` | Number of concurrent air-draft lookup workers |
| `SEARCH_REQUEST_DELAY_SECONDS` | `2.0` | Delay between search-API / page-fetch requests within a single lookup |
| `DB_PATH` | `./ais.db` | SQLite file location (Docker sets this to `/data/ais.db`) |

### Run locally

```
conda activate ais
uvicorn app.main:app --reload --port 8000
```

or without activating:
```
C:\Users\wilso\anaconda3\envs\ais\python.exe -m uvicorn app.main:app --reload --port 8000
```

Then open [http://localhost:8000](http://localhost:8000).

### Run with Docker

```
docker compose up --build -d
```

Reads `AISSTREAM_API_KEY` (and any overrides) from `.env`, serves the
dashboard on `http://localhost:8000`, and persists the SQLite DB in the
`ais_data` named volume so state survives container restarts.

---

## `aisstream.py`

Connects to [aisstream.io](https://aisstream.io)'s WebSocket feed and prints
every AIS message received for vessels inside a fixed bounding box, live,
forever. Useful for quickly checking what raw messages look like for a given
area without spinning up the full service.

**Requires:** `websockets`

**Run:**
```
python aisstream.py
```

**Notes:**
- The API key is hardcoded in `API_KEY` at the top of the file. Since this
  directory isn't under git, that's low-risk here, but don't paste this key
  into a public repo or anywhere else it could leak.
- To watch a different area, edit `BoundingBoxes` (`[lat, lon]` pairs for the
  southwest and northeast corners).
- Runs until interrupted (Ctrl+C) - there's no message limit or timeout.

---

## `air_draft_lookup.py`

Exploratory web-crawling tool: given a vessel name, tries to find its **air
draft** (height above the waterline when laden - the figure that matters for
clearing bridges/canals, or for a height-clearance check like flagging ships
too tall to safely pass near an airport approach path).

Only a genuine air draft / air draught field counts as a match. Plain
draft/draught (depth *below* the waterline) is deliberately not accepted as
a fallback, even though it's far more commonly published - the two are only
loosely correlated (a lightly laden container ship can sit shallow in the
water while still being very tall), so treating draft as a stand-in risks
the dangerous case of a tall ship reading as "fine". An honest "not found" is
better than a wrong "close enough" here.

Air draft is rarely published by consumer AIS sites (VesselFinder,
MarineTraffic, etc.), so instead of querying a fixed list of sources, the
script:

1. Runs a handful of web searches (DuckDuckGo, no API key needed) for things
   like `"<vessel>" ship particulars air draft` and
   `"<vessel>" ship particulars filetype:pdf`.
2. Fetches whatever comes back - HTML pages or PDFs, including "ship
   particulars" sheets published by owners/managers/charterers.
3. If a PDF has no extractable text (common - many particulars sheets are
   flattened/scanned images), OCRs it with Tesseract. Table rows are
   reconstructed from OCR word positions so labels and values line up
   correctly (Tesseract's plain text output otherwise reads image tables
   column-by-column, silently mismatching every label with the wrong value).
4. Scans everything it fetched for a genuine air draft / air draught field
   (or equivalent phrasing: keel-to-mast, overhead/vertical clearance) and
   extracts its value.

**Requires:** `requests`, `beautifulsoup4`, `pypdf`, `pymupdf`, `pytesseract`,
`playwright` (+ `playwright install chromium` once), and the Tesseract OCR
engine with English trained data. The Windows installer needs admin/UAC;
installing via conda-forge avoids that:

```
conda install -n ais -c conda-forge tesseract -y
curl -L -o C:\Users\wilso\anaconda3\envs\ais\Library\share\tessdata\eng.traineddata ^
    https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata
```

**Run:**
```
python air_draft_lookup.py <vessel name>
```
e.g. `python air_draft_lookup.py PAC ALNATH`. With no argument it defaults to
`PAC ALNATH`.

**Output:**
- Console: each search query and result count, then per-page an
  `AIR DRAFT: <value>` line (or "no air draft field found"), followed by a
  summary table.
- File: the full console output is also written to
  `air_draft_summary_<vessel>_<timestamp>.txt` in this directory - one file
  per run, so results from different runs/vessels don't overwrite each other.

**Limitations:**
- Best-effort probe, not a guaranteed lookup - a lot of vessels simply have
  no public source for air draft, and the script says so honestly rather
  than guessing.
- Some sites (MarineTraffic, MyShipTracking, etc.) are behind Cloudflare and
  will block the automated fetch outright.
- Takes roughly 1-3 minutes per vessel: 4 live searches plus up to 12
  page/PDF fetches, some through a headless browser or OCR.

---

## Setup (both root scripts)

`aisstream.py` and `air_draft_lookup.py` are developed against the conda env
`ais`:

```
conda activate ais
```

or invoke the interpreter directly without activating:

```
C:\Users\wilso\anaconda3\envs\ais\python.exe <script>.py
```
