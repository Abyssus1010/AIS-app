import asyncio
import logging

from app import air_draft_resolver, config, db
from app.state import IN_FLIGHT
from app.vessel_name_lookup import resolve_vessel_name

logger = logging.getLogger(__name__)

STALE_SWEEP_INTERVAL_SECONDS = 600


async def _guess_and_save(mmsi: int) -> None:
    resolved = await asyncio.to_thread(resolve_vessel_name, mmsi)
    if resolved is None:
        logger.warning("Could not resolve a name for mmsi=%s from MMSI alone, will retry later", mmsi)
        return
    name, imo, category, loa_m = resolved
    await asyncio.to_thread(db.set_guessed_name, mmsi, name, imo)
    logger.info("Guessed name for mmsi=%s from MMSI search: %r (unverified)", mmsi, name)
    if category is not None:
        await asyncio.to_thread(db.set_category_from_web, mmsi, category.value)
        logger.info("Guessed category for mmsi=%s from the same search: %s (unverified)", mmsi, category.value)
    if loa_m is not None:
        await asyncio.to_thread(db.set_loa_from_web, mmsi, loa_m)
        logger.info("Guessed length overall for mmsi=%s from the same search: %.0f m (unverified)", mmsi, loa_m)


async def run_lookup_worker(queue: asyncio.Queue) -> None:
    while True:
        mmsi = await queue.get()
        try:
            await _guess_and_save(mmsi)
        finally:
            IN_FLIGHT.discard(mmsi)
            queue.task_done()


async def run_stale_unknown_requeue(queue: asyncio.Queue) -> None:
    """Retries vessels that still have no name at all (AIS hasn't delivered a
    static-data message for them - see resolve_vessel_name), scoped to
    vessels still within SILENCE_WINDOW_MINUTES - a vessel that's gone quiet
    longer than that is no longer shown on the dashboard, so retrying it
    would just waste a lookup worker on a vessel nobody can currently see.
    Runs once immediately, then on a fixed interval."""
    while True:
        for row in await asyncio.to_thread(
            db.get_vessels_needing_name_lookup, config.SILENCE_WINDOW_MINUTES
        ):
            if row["mmsi"] not in IN_FLIGHT:
                IN_FLIGHT.add(row["mmsi"])
                await queue.put(row["mmsi"])

        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)


async def run_imo_backfill() -> None:
    """Fills in IMO for vessels whose name is already known but whose IMO
    never arrived: Class B craft (tugs, pilot boats, salvage barges, etc.)
    broadcast via StaticDataReport, which has no IMO field at all (see
    ais_client.py) - so ShipStaticData, the only message type that carries
    one, is simply never coming for them. Reuses resolve_vessel_name's
    confirmed MMSI-in-URL/title matching, which already recovers IMO
    alongside name - it's just never invoked once the name half is already
    known.

    The same search also recovers a height-estimate category and length
    overall (see vessel_name_lookup._category_from_title/_length_m_from_text)
    as a side effect of resolving IMO - but that's only a best-effort bonus
    here, not a guarantee: if the confirmed result's text doesn't parse into
    one, this vessel drops out of this query for good the moment its IMO
    lands, with nothing left to retry that specifically. See
    run_category_backfill / run_loa_backfill for those retries."""
    while True:
        for row in await asyncio.to_thread(
            db.get_vessels_needing_imo_lookup, config.SILENCE_WINDOW_MINUTES
        ):
            resolved = await asyncio.to_thread(resolve_vessel_name, row["mmsi"])
            if resolved is not None:
                _, imo, category, loa_m = resolved
                if imo is not None:
                    await asyncio.to_thread(db.set_imo, row["mmsi"], imo)
                    logger.info("Backfilled IMO for mmsi=%s: %s", row["mmsi"], imo)
                if category is not None:
                    await asyncio.to_thread(db.set_category_from_web, row["mmsi"], category.value)
                    logger.info(
                        "Guessed category for mmsi=%s from the same search: %s (unverified)",
                        row["mmsi"], category.value,
                    )
                if loa_m is not None:
                    await asyncio.to_thread(db.set_loa_from_web, row["mmsi"], loa_m)
                    logger.info(
                        "Guessed length overall for mmsi=%s from the same search: %.0f m (unverified)",
                        row["mmsi"], loa_m,
                    )

        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)


async def run_category_backfill() -> None:
    """Retries category resolution for vessels whose name+IMO are already
    both resolved but ais_type never arrived - the gap run_imo_backfill's
    docstring describes: once IMO lands, that vessel is invisible to
    run_imo_backfill forever, so if the search that resolved it didn't yield
    a parseable category, nothing else would ever try again. Scoped via
    db.get_vessels_needing_category_lookup, which excludes vessels already
    covered by run_imo_backfill (imo still missing) to avoid duplicate
    searches for the same vessel."""
    while True:
        for row in await asyncio.to_thread(
            db.get_vessels_needing_category_lookup, config.SILENCE_WINDOW_MINUTES
        ):
            resolved = await asyncio.to_thread(resolve_vessel_name, row["mmsi"])
            if resolved is not None:
                _, _, category, loa_m = resolved
                if category is not None:
                    await asyncio.to_thread(db.set_category_from_web, row["mmsi"], category.value)
                    logger.info(
                        "Guessed category for mmsi=%s from a retry search: %s (unverified)",
                        row["mmsi"], category.value,
                    )
                if loa_m is not None:
                    await asyncio.to_thread(db.set_loa_from_web, row["mmsi"], loa_m)
                    logger.info(
                        "Guessed length overall for mmsi=%s from the same retry search: %.0f m (unverified)",
                        row["mmsi"], loa_m,
                    )

        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)


async def run_loa_backfill() -> None:
    """Retries LOA resolution for vessels whose category is already known
    (a real ais_type arrived) but AIS's own Dimension came back empty, and
    whose name+IMO are already resolved too - see
    db.get_vessels_needing_loa_lookup for why nothing else would ever search
    for these again."""
    while True:
        for row in await asyncio.to_thread(
            db.get_vessels_needing_loa_lookup, config.SILENCE_WINDOW_MINUTES
        ):
            resolved = await asyncio.to_thread(resolve_vessel_name, row["mmsi"])
            if resolved is not None:
                _, _, _, loa_m = resolved
                if loa_m is not None:
                    await asyncio.to_thread(db.set_loa_from_web, row["mmsi"], loa_m)
                    logger.info(
                        "Guessed length overall for mmsi=%s from a retry search: %.0f m (unverified)",
                        row["mmsi"], loa_m,
                    )

        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)


async def run_air_draft_lookup() -> None:
    """Searches the web for each vessel's own published air draft (see
    air_draft_resolver.resolve_air_draft), ahead of the type+size table's
    generic estimate - db.set_air_draft_result only writes while a vessel is
    still PENDING/UNKNOWN, and main._effective_height prefers a confirmed
    air_draft_* result over est_height_* whenever one exists, so the type+
    size table naturally stays the fallback for anything this loop hasn't
    resolved yet (or never will).

    Gated on config.AIR_DRAFT_LOOKUP_ENABLED since this is far more expensive
    per vessel than the other lookups here (see its docstring). A vessel
    needs a name before it's queryable at all (db.
    get_vessels_needing_air_draft_lookup already filters on that), so this
    naturally runs after a name has resolved - real or guessed - rather than
    racing it.

    Deliberately sequential, not a concurrent pool like run_lookup_worker:
    LangSearch calls are already serialized behind one shared throttle
    regardless (see vessel_name_lookup._throttle), so concurrency here would
    only parallelize the page-fetch/OCR portion at the cost of hammering
    external hosts harder - matches run_imo_backfill/run_category_backfill/
    run_loa_backfill's own single-sequential-loop style for this same reason."""
    if not config.AIR_DRAFT_LOOKUP_ENABLED:
        logger.info("Air draft web-search lookup disabled (AIR_DRAFT_LOOKUP_ENABLED=false)")
        return

    while True:
        for row in await asyncio.to_thread(
            db.get_vessels_needing_air_draft_lookup, config.SILENCE_WINDOW_MINUTES, config.AIR_DRAFT_RETRY_HOURS
        ):
            try:
                result = await asyncio.to_thread(
                    air_draft_resolver.resolve_air_draft, row["name"], row["imo"]
                )
            except air_draft_resolver.LookupError:
                logger.warning(
                    "Air draft lookup failed entirely for mmsi=%s (%r), will retry later",
                    row["mmsi"], row["name"],
                )
                continue
            await asyncio.to_thread(db.set_air_draft_result, row["mmsi"], result)
            logger.info(
                "Air draft lookup for mmsi=%s (%r): %s%s",
                row["mmsi"], row["name"], result.status,
                f" ({result.value_raw} from {result.source_url})" if result.value_raw else "",
            )

        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)


async def run_stale_purge() -> None:
    """Deletes vessels that have been silent longer than PURGE_AFTER_HOURS
    (see db.purge_stale_vessels) - nothing else ever removes a row, so
    without this the vessels table grows forever even though
    SILENCE_WINDOW_MINUTES already hides old vessels from the dashboard."""
    while True:
        purged = await asyncio.to_thread(db.purge_stale_vessels, config.PURGE_AFTER_HOURS)
        if purged:
            logger.info("Purged %d vessel(s) silent for over %.1f hours", purged, config.PURGE_AFTER_HOURS)
        await asyncio.sleep(STALE_SWEEP_INTERVAL_SECONDS)
