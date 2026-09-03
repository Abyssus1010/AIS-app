import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app import config, db, vessel_height_table
from app.ais_client import run_ingestion
from app.worker import (
    run_category_backfill,
    run_imo_backfill,
    run_loa_backfill,
    run_lookup_worker,
    run_stale_purge,
    run_stale_unknown_requeue,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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
    tasks.append(asyncio.create_task(run_stale_purge()))

    yield

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="AIS Vessel Height Monitor", lifespan=lifespan)


def _serialize(row) -> dict:
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
        "est_height_status": row["est_height_status"],
        "est_height_ft": row["est_height_ft"],
        "est_height_confidence": row["est_height_confidence"],
        "est_height_note": row["est_height_note"],
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


@app.get("/api/vessels")
async def api_vessels():
    rows = await asyncio.to_thread(db.get_active_vessels, config.SILENCE_WINDOW_MINUTES)
    return [_serialize(r) for r in rows]


@app.get("/")
async def dashboard(request: Request):
    rows = await asyncio.to_thread(db.get_active_vessels, config.SILENCE_WINDOW_MINUTES)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "vessels": [_serialize(r) for r in rows],
            "threshold_ft": config.HEIGHT_THRESHOLD_FT,
            "silence_window_minutes": config.SILENCE_WINDOW_MINUTES,
        },
    )


@app.get("/vessel/{mmsi}")
async def vessel_detail(request: Request, mmsi: int):
    row = await asyncio.to_thread(db.get_vessel, mmsi)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No vessel with MMSI {mmsi}")
    return templates.TemplateResponse(
        request,
        "vessel_detail.html",
        {
            "vessel": _serialize(row),
            "height_explainer": _height_explainer(row),
            "full_table": vessel_height_table.full_table(),
            "bounding_box": config.BOUNDING_BOX,
            "threshold_ft": config.HEIGHT_THRESHOLD_FT,
        },
    )
