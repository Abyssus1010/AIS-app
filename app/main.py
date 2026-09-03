import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import config, db, vessel_height_table
from app.ais_client import run_ingestion
from app.worker import (
    run_air_draft_lookup,
    run_category_backfill,
    run_imo_backfill,
    run_loa_backfill,
    run_lookup_worker,
    run_stale_purge,
    run_stale_unknown_requeue,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    queue: asyncio.Queue = asyncio.Queue()

    tasks = [asyncio.create_task(run_ingestion())]
    tasks += [
        asyncio.create_task(run_lookup_worker(queue)) for _ in range(config.LOOKUP_CONCURRENCY)
    ]
    tasks.append(asyncio.create_task(run_stale_unknown_requeue(queue)))
    tasks.append(asyncio.create_task(run_imo_backfill()))
    tasks.append(asyncio.create_task(run_category_backfill()))
    tasks.append(asyncio.create_task(run_loa_backfill()))
    tasks.append(asyncio.create_task(run_air_draft_lookup()))
    tasks.append(asyncio.create_task(run_stale_purge()))

    yield

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="AIS Vessel Height Monitor", lifespan=lifespan)


def _effective_height(row) -> dict:
    """Picks the height verdict that actually drives the OK/FLAGGED badge:
    a confirmed web-search air draft (air_draft_resolver.py) when one was
    found, else the type+size table's estimate (vessel_height_table.py) as a
    fallback - the read-side counterpart of the write-side "search first"
    order (worker.run_air_draft_lookup only ever populates est_height_* via
    the table, and db.set_air_draft_result only ever writes air_draft_* once
    a search completes, so this is the one place that decides which of the
    two governs).

    Confidence/note are re-derived here too, not just passed through from
    est_height_*: a web-confirmed value has no "bracket" to be uncertain
    about, but its *identity* match can still be weak (name-proximity rather
    than a declared name or IMO match) - reusing the existing "low
    confidence" concept (and the dashboard's warning-icon UI for it) for
    that case, rather than inventing a separate signal, means a
    weakly-confirmed web result gets flagged for a human to double-check the
    exact same way a low-confidence table estimate already does."""
    if row["air_draft_status"] in ("OK", "FLAGGED"):
        weak = row["air_draft_identity"] == "name_proximity"
        return {
            "source": "air_draft_search",
            "status": row["air_draft_status"],
            "height_ft": row["air_draft_value_ft"],
            "confidence": "low" if weak else None,
            "note": (
                f"Confirmed via web search ({row['air_draft_value_raw']}, source: "
                f"{row['air_draft_source_title'] or row['air_draft_source_url']}) - "
                f"identity match: {row['air_draft_identity']}"
                + (" (weak - name matched nearby, not a declared NAME/IMO field; verify manually)" if weak else "")
            ),
        }
    return {
        "source": "type_size_table",
        "status": row["est_height_status"],
        "height_ft": row["est_height_ft"],
        "confidence": row["est_height_confidence"],
        "note": row["est_height_note"],
    }


def _serialize(row) -> dict:
    """Note: est_height_status/ft/confidence/note below are the type+size
    table's OWN values (unlike the version of this function two commits ago,
    which overwrote them with whichever was effective) - the dashboard now
    shows the table estimate and the air-draft search result as two separate
    columns, so callers no longer need one field to silently mean either
    depending on the row. `status` is the one field that still needs to
    reflect the effective (search-first, table-fallback) verdict, since
    that's the actual OK/FLAGGED badge the dashboard's Status column and its
    FLAGGED-first sort are built around."""
    effective = _effective_height(row)
    return {
        "mmsi": row["mmsi"],
        "imo": row["imo"],
        "name": row["name"] or f"MMSI {row['mmsi']}",
        "name_guessed": bool(row["name_guessed"]),
        "lat": row["last_lat"],
        "lon": row["last_lon"],
        "last_position_at": row["last_position_at"],
        "ais_type": row["ais_type"],
        "loa_m": row["loa_m"],
        "status": effective["status"],
        "height_source": effective["source"],
        "est_height_status": row["est_height_status"],
        "est_height_ft": row["est_height_ft"],
        "est_height_confidence": row["est_height_confidence"],
        "est_height_note": row["est_height_note"],
        "air_draft_status": row["air_draft_status"],
        "air_draft_value_ft": row["air_draft_value_ft"],
        "air_draft_value_raw": row["air_draft_value_raw"],
        "air_draft_source_title": row["air_draft_source_title"],
        "air_draft_source_url": row["air_draft_source_url"],
        "air_draft_identity": row["air_draft_identity"],
    }


def _height_explainer(row) -> dict:
    """Everything the vessel-detail page needs to show *why* a vessel got
    its type/size height estimate - not just the number. Built once here
    (rather than in the template) so the SVG-scale layout math stays in
    Python; left out of _serialize since the dashboard's 10s poll doesn't
    need this much detail for every row."""
    category = row["est_height_category"]
    loa_m = row["loa_m"]
    height_ft = row["est_height_ft"]
    return {
        "ais_type": row["ais_type"],
        "category": category,
        "category_label": vessel_height_table.CATEGORY_LABELS.get(category) if category else None,
        "loa_m": loa_m,
        "bracket": row["est_height_bracket"],
        "bracket_range_label": vessel_height_table.bracket_range_label(category, row["est_height_bracket"]),
        "height_ft": height_ft,
        "status": row["est_height_status"],
        "size_scale": vessel_height_table.size_scale(category, loa_m),
    }


def _air_draft_explainer(row) -> dict:
    """Everything the vessel-detail page needs to show the web-search air
    draft lookup's own outcome, separately from the type+size table
    explainer above - kept as its own dict rather than folded into
    _height_explainer since it describes a different method entirely
    (air_draft_resolver.py, not vessel_height_table.py), even though
    _effective_height may end up preferring this one for the actual
    OK/FLAGGED verdict."""
    return {
        "effective": _effective_height(row),
        "status": row["air_draft_status"],
        "value_raw": row["air_draft_value_raw"],
        "value_m": row["air_draft_value_m"],
        "value_ft": row["air_draft_value_ft"],
        "source_url": row["air_draft_source_url"],
        "source_title": row["air_draft_source_title"],
        "context": row["air_draft_context"],
        "identity": row["air_draft_identity"],
        "checked_at": row["air_draft_checked_at"],
    }


@app.get("/api/vessels")
async def api_vessels():
    rows = await asyncio.to_thread(db.get_active_vessels, config.SILENCE_WINDOW_MINUTES)
    return [_serialize(r) for r in rows]


class HeightThresholdUpdate(BaseModel):
    # Upper bound is a sanity guard, not a real domain limit: this app's own
    # table tops out around 250ft (large cruise ships) and its whole purpose
    # is flagging vessels tall enough to threaten Changi approach/departure
    # paths, so a value in the thousands is almost certainly a fat-fingered
    # entry (e.g. meters typed into a feet field) rather than an intentional
    # threshold - one that would silently mark every vessel OK instead of
    # erroring loudly. Raise this if a legitimate use case ever needs more.
    threshold_ft: float = Field(gt=0, le=1000)


@app.get("/api/settings/height-threshold")
async def get_height_threshold():
    return {"threshold_ft": await asyncio.to_thread(db.get_height_threshold_ft)}


@app.post("/api/settings/height-threshold")
async def set_height_threshold(payload: HeightThresholdUpdate):
    """Lets a user change the live OK/FLAGGED flag threshold from the
    dashboard instead of only via the HEIGHT_THRESHOLD_FT env var at
    container start - see db.set_height_threshold_ft for how this also
    retroactively re-flags every vessel already computed under the old
    threshold, not just ones processed from now on."""
    await asyncio.to_thread(db.set_height_threshold_ft, payload.threshold_ft)
    logger.info("Height flag threshold changed to %.0f ft via the dashboard", payload.threshold_ft)
    return {"threshold_ft": payload.threshold_ft}


@app.get("/")
async def dashboard(request: Request):
    rows = await asyncio.to_thread(db.get_active_vessels, config.SILENCE_WINDOW_MINUTES)
    threshold_ft = await asyncio.to_thread(db.get_height_threshold_ft)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "vessels": [_serialize(r) for r in rows],
            "threshold_ft": threshold_ft,
            "silence_window_minutes": config.SILENCE_WINDOW_MINUTES,
        },
    )


@app.get("/vessel/{mmsi}")
async def vessel_detail(request: Request, mmsi: int):
    row = await asyncio.to_thread(db.get_vessel, mmsi)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No vessel with MMSI {mmsi}")
    threshold_ft = await asyncio.to_thread(db.get_height_threshold_ft)
    return templates.TemplateResponse(
        request,
        "vessel_detail.html",
        {
            "vessel": _serialize(row),
            "height_explainer": _height_explainer(row),
            "air_draft": _air_draft_explainer(row),
            "full_table": vessel_height_table.full_table(),
            "bounding_box": config.BOUNDING_BOX,
            "threshold_ft": threshold_ft,
        },
    )
