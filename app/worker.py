import asyncio
import logging

from app import config, db
from app.air_draft_resolver import LookupError, resolve_air_draft, resolve_vessel_name
from app.state import IN_FLIGHT

logger = logging.getLogger(__name__)

STALE_SWEEP_INTERVAL_SECONDS = 600


async def _resolve_and_save(mmsi: int, name: str | None) -> None:
    if name is None:
        resolved = await asyncio.to_thread(resolve_vessel_name, mmsi)
        if resolved is None:
            logger.warning("Could not resolve a name for mmsi=%s from MMSI alone, will retry later", mmsi)
            return
        name, imo = resolved
        await asyncio.to_thread(db.set_guessed_name, mmsi, name, imo)
        logger.info("Guessed name for mmsi=%s from MMSI search: %r (unverified)", mmsi, name)
    else:
        row = await asyncio.to_thread(db.get_vessel, mmsi)
        imo = row["imo"] if row else None

    try:
        result = await asyncio.to_thread(resolve_air_draft, name, imo)
    except LookupError:
        logger.warning("Lookup failed for %r (mmsi=%s), will retry later", name, mmsi)
        await asyncio.to_thread(db.record_failed_attempt, mmsi)
        return
    except Exception:
        logger.exception("Unexpected error resolving air draft for %r (mmsi=%s)", name, mmsi)
        await asyncio.to_thread(db.record_failed_attempt, mmsi)
        return

    await asyncio.to_thread(
        db.save_lookup_result,
        mmsi,
        result.status,
        result.value_raw,
        result.value_m,
        result.value_ft,
        result.source_url,
        result.source_title,
        result.context,
        result.identity,
    )
    logger.info("Resolved %r (mmsi=%s): %s", name, mmsi, result.status)


async def run_lookup_worker(queue: asyncio.Queue) -> None:
    while True:
        mmsi, name = await queue.get()
        try:
            await _resolve_and_save(mmsi, name)
        finally:
            IN_FLIGHT.discard(mmsi)
            queue.task_done()


async def run_stale_unknown_requeue(queue: asyncio.Queue) -> None:
    """Recovers PENDING vessels (e.g. after a restart, or a prior lookup that
    failed outright and was left PENDING), retries UNKNOWN vessels older
    than config.UNKNOWN_RETRY_HOURS, and retries vessels that still have no
    name at all (AIS hasn't delivered a static-data message for them - see
    resolve_vessel_name). All three are scoped to vessels still within
    SILENCE_WINDOW_MINUTES - a vessel that's gone quiet longer than that is no
    longer shown on the dashboard, so retrying it would just waste a lookup
    worker on a vessel nobody can currently see. A vessel whose last lookup
    failed outright is also held back for FAILED_LOOKUP_BACKOFF_MINUTES
    (see db.record_failed_attempt) rather than retried on every pass. Runs
    once immediately, then on a fixed interval."""
    while True:
        for row in await asyncio.to_thread(
            db.get_vessels_needing_name_lookup, config.SILENCE_WINDOW_MINUTES
        ):
            if row["mmsi"] not in IN_FLIGHT:
                IN_FLIGHT.add(row["mmsi"])
                await queue.put((row["mmsi"], None))

        for row in await asyncio.to_thread(
            db.get_vessels_needing_initial_lookup,
            config.SILENCE_WINDOW_MINUTES,
            config.FAILED_LOOKUP_BACKOFF_MINUTES,
        ):
            if row["mmsi"] not in IN_FLIGHT:
                IN_FLIGHT.add(row["mmsi"])
                await queue.put((row["mmsi"], row["name"]))

        for row in await asyncio.to_thread(
            db.get_stale_unknown_vessels,
            config.UNKNOWN_RETRY_HOURS,
            config.SILENCE_WINDOW_MINUTES,
            config.FAILED_LOOKUP_BACKOFF_MINUTES,
        ):
            if row["mmsi"] not in IN_FLIGHT:
                IN_FLIGHT.add(row["mmsi"])
                await queue.put((row["mmsi"], row["name"]))

        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)


async def run_imo_backfill() -> None:
    """Fills in IMO for vessels whose name is already known but whose IMO
    never arrived: Class B craft (tugs, pilot boats, salvage barges, etc.)
    broadcast via StaticDataReport, which has no IMO field at all (see
    ais_client.py) - so ShipStaticData, the only message type that carries
    one, is simply never coming for them. Reuses resolve_vessel_name's
    confirmed MMSI-in-URL/title matching, which already recovers IMO
    alongside name - it's just never invoked once the name half is already
    known. Runs independently of the main lookup queue: patching in an IMO
    shouldn't wait behind a re-resolve of an already-resolved air draft
    status. It can still *cause* one - db.set_imo resets a weakly-identified
    (name-proximity) OK/FLAGGED result to PENDING - but that re-resolve is
    picked up by the normal stale sweep on its next pass, not run inline
    here."""
    while True:
        for row in await asyncio.to_thread(
            db.get_vessels_needing_imo_lookup, config.SILENCE_WINDOW_MINUTES
        ):
            resolved = await asyncio.to_thread(resolve_vessel_name, row["mmsi"])
            if resolved is not None:
                _, imo = resolved
                if imo is not None:
                    await asyncio.to_thread(db.set_imo, row["mmsi"], imo)
                    logger.info("Backfilled IMO for mmsi=%s: %s", row["mmsi"], imo)

        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)
