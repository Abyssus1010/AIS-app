import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates

from app import config, db
from app.ais_client import run_ingestion
from app.worker import run_lookup_worker, run_stale_unknown_requeue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    queue: asyncio.Queue = asyncio.Queue()

    tasks = [asyncio.create_task(run_ingestion(queue))]
    tasks += [
        asyncio.create_task(run_lookup_worker(queue)) for _ in range(config.LOOKUP_CONCURRENCY)
    ]
    tasks.append(asyncio.create_task(run_stale_unknown_requeue(queue)))

    yield

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="AIS Air Draft Monitor", lifespan=lifespan)


def _serialize(row) -> dict:
    return {
        "mmsi": row["mmsi"],
        "imo": row["imo"],
        "name": row["name"] or f"MMSI {row['mmsi']}",
        "name_guessed": bool(row["name_guessed"]),
        "lat": row["last_lat"],
        "lon": row["last_lon"],
        "last_position_at": row["last_position_at"],
        "status": row["air_draft_status"],
        "value_m": row["air_draft_value_m"],
        "value_ft": row["air_draft_value_ft"],
        "source_url": row["air_draft_source_url"],
        "source_title": row["air_draft_source_title"],
        "last_checked_at": row["last_checked_at"],
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
            "threshold_ft": config.AIR_DRAFT_THRESHOLD_FT,
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
            "bounding_box": config.BOUNDING_BOX,
            "threshold_ft": config.AIR_DRAFT_THRESHOLD_FT,
        },
    )
