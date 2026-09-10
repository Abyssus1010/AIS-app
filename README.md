# AIS app

A height monitor for vessels in Singapore's eastern approaches. It watches
live AIS traffic, estimates each vessel's height above the waterline from its
AIS-broadcast type and length, and flags any vessel tall enough to be a
hazard to aircraft using Changi's runways - on a live-updating dashboard.

Each vessel's height is resolved two ways, in order. First, a web search for
the vessel's own published air draft (fetching ship-particulars pages/PDFs,
OCR'ing scanned ones - `app/air_draft_resolver.py`). A genuine,
publicly-published air draft figure only exists for a minority of vessels
however thorough the search, so this was dropped for a stretch in favor of
the second method alone; it's since been reinstated as a first pass rather
than the sole method, specifically so its occasional real per-vessel
precision isn't left on the table. Whatever it can't confirm falls back
instantly to a preset type+size table (`app/vessel_height_table.py`) driven
by AIS-broadcast type and length alone - no network call, no per-vessel
search, and available for every vessel with AIS static data. See that
module's docstring for the table itself and its confidence caveats, and
`app/worker.py`'s `run_air_draft_lookup` for how the two are sequenced.

The repo has two parts:

- **`app/`** - the actual service: a FastAPI app that ingests AIS, persists
  vessel state, estimates height from type+size, and serves the dashboard.
  This is what `Dockerfile`/`docker-compose.yml` build and run.
- **`aisstream.py`** and **`air_draft_lookup.py`** - two standalone, independent
  scripts kept at the repo root for quick manual testing. `app/ais_client.py`
  began as a productionized port of `aisstream.py` (adding DB persistence);
  `app/air_draft_resolver.py` is likewise a productionized port of
  `air_draft_lookup.py` (LangSearch as primary search backend instead of
  DuckDuckGo alone, structured results instead of print()/file output). See
  each script's own section below for what they do on their own.

---

## `app/` - the dashboard service

### What it does

1. **Ingests** live AIS position + static-data messages from
   [aisstream.io](https://aisstream.io) for a configured monitoring zone
   (`app/ais_client.py`), reconnecting with backoff on drops.
2. **Persists** every vessel's latest position, identity, AIS type, and
   length overall (LOA) to a local SQLite DB (`app/db.py`).
3. The moment a vessel's AIS type and LOA are known, its height is
   **estimated instantly from a preset type+size table**
   (`app/vessel_height_table.py`) - no network call, no per-vessel search.
   The vessel is marked `OK`, `FLAGGED` (estimated height over
   `HEIGHT_THRESHOLD_FT`), or `UNKNOWN` (type/LOA not yet known, or a type
   the table doesn't cover, e.g. sailing vessels whose height is set by mast
   rigging rather than hull size). See that module's docstring for why
   several categories carry a `low` confidence estimate.
4. Separately, once a vessel has a name, `app/worker.py`'s
   `run_air_draft_lookup` **searches the web for that vessel's own published
   air draft** (`app/air_draft_resolver.py`) - LangSearch, falling back to
   DuckDuckGo, for ship-particulars pages/PDFs (OCR'd if scanned), confirmed
   against the vessel's own name/IMO before being trusted. A confirmed
   result *overrides* the type+size table's estimate for that vessel's
   `OK`/`FLAGGED` verdict (`app/main.py`'s `_effective_height`); anything
   this can't confirm just leaves the table estimate as the vessel's
   verdict, which was already computed for free in step 3. This is far more
   expensive per vessel than every other lookup here (up to 3 search
   queries, each fanning out to a dozen page/PDF fetches) - see
   `AIR_DRAFT_LOOKUP_ENABLED`/`AIR_DRAFT_RETRY_HOURS` below to disable it or
   tune its retry backoff.
5. Serves a **dashboard** (`app/main.py` + `app/templates/dashboard.html`)
   listing every vessel seen recently, sorted with `FLAGGED` vessels first,
   and a **vessel detail page** that visually breaks down how each estimate
   was derived (type → category → size bracket → table value → threshold
   verdict). The dashboard polls `GET /api/vessels` every 10s to stay live.
6. Separately, **vessels with no name at all** (AIS hasn't yet delivered a
   static-data message for them - only position reports so far, shown on the
   dashboard as `MMSI <n>`) are queued for a best-effort name guess from the
   MMSI alone (`app/worker.py` + `app/vessel_name_lookup.py`) - see below.

### Vessel lifecycle: visibility vs. retries

- **Dashboard visibility** is driven entirely by `last_position_at`, the
  timestamp of the last AIS message received for that vessel - a vessel drops
  off the dashboard once it's gone quiet for `SILENCE_WINDOW_MINUTES`. That's
  usually because it left the monitoring zone (aisstream only forwards
  messages for vessels inside the bounding box), but the app can't actually
  distinguish that from an AIS coverage gap, the vessel's transponder going
  quiet, or a brief reconnect after a dropped stream - it only knows "no
  message arrived recently," not why.
- The height estimate itself needs no retry logic - it's recomputed
  synchronously every time a static-data message updates a vessel's type or
  LOA, so it's never stale beyond the AIS feed's own latency.
- **Name-guess retries are scoped to the same visibility window as the
  dashboard.** The background sweep (`app/worker.py`, every 10 minutes) only
  re-queues a nameless vessel if it's also within `SILENCE_WINDOW_MINUTES` -
  a vessel that's gone quiet longer than that won't be retried until it's
  heard from again, so workers aren't spent re-resolving vessels nobody can
  currently see. This is best-effort and only accepts a *confirmed* match
  (the MMSI must actually appear in the search result's URL or title) - if
  nothing confirms, the vessel stays nameless and is retried on the next
  sweep rather than risk mislabeling it with a guess.

### Monitoring zone

The default zone is the smallest rectangle enclosing a ±30° cone that
extends 10 NM south from Changi's easternmost runway (02R/20L's southern
threshold, 1.32239°N 103.99985°E - the point on that runway closest to the
sea). AISStream only supports a rectangular bounding box subscription, so
the rectangle covers some area outside the cone as well as the cone itself;
nothing inside the rectangle is filtered out further.

`AIS_BOUNDING_BOX` (see Configuration below) only sets the *default* zone
used the first time the app ever runs against a given DB - from then on,
the live zone is user-adjustable straight from the dashboard's "Edit
monitoring zone" panel (drag the two corner markers on its map, or type
coordinates directly - both stay in sync), persists in the DB across
restarts, and takes effect immediately by forcing an AIS reconnect
(`app/ais_client.py`'s `request_reconnect`) rather than waiting for the
current connection to drop on its own. Same idea for the height flag
threshold - the dashboard's threshold field next to "Flagging vessels with
a height >" edits the live, DB-backed value, not the `HEIGHT_THRESHOLD_FT`
env var directly (see `app/db.py`'s `get_height_threshold_ft`/
`set_height_threshold_ft` and `get_bounding_box`/`set_bounding_box`).

### Requires

`websockets`, `python-dotenv`, `fastapi`, `uvicorn[standard]`, `jinja2`,
`requests` (for the MMSI-to-name search fallback), `beautifulsoup4`,
`pypdf`, `pymupdf`, `pytesseract`, and `pillow` - the last five are for
`app/air_draft_resolver.py`'s web-search air draft lookup (fetching and, for
scanned PDFs, OCR'ing ship-particulars pages), which needs the `tesseract-ocr`
system package too (see the Dockerfile). The standalone `air_draft_lookup.py`
script below shares this same set of dependencies.

### Configuration

Copy `.env.example` to `.env` and fill in an aisstream.io API key:

```
AISSTREAM_API_KEY=your-key-here
```

All other variables are optional (defaults live in `app/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `AIS_BOUNDING_BOX` | rectangle enclosing the Changi cone (see above) | `[[sw_lat, sw_lon], [ne_lat, ne_lon]]` seed for the AIS subscription - only used the first time the app runs against a given DB; edit the live zone from the dashboard afterward |
| `HEIGHT_THRESHOLD_FT` | `70` | Seed for the flag threshold (height above this, in feet, is flagged) - same deal, edit the live value from the dashboard afterward |
| `SILENCE_WINDOW_MINUTES` | `30` | How long a vessel stays on the dashboard after its last position report |
| `LOOKUP_CONCURRENCY` | `3` | Number of concurrent MMSI-to-name lookup workers |
| `AIR_DRAFT_LOOKUP_ENABLED` | `true` | Master switch for the web-search air draft lookup (`app/air_draft_resolver.py`) - set `false` to rely on the type+size table alone |
| `AIR_DRAFT_RETRY_HOURS` | `12` | How long to wait before retrying a vessel whose air draft search came back `UNKNOWN` |
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
