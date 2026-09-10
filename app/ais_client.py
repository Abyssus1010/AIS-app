import asyncio
import json
import logging
import random

import websockets
from websockets.exceptions import ConnectionClosed

from app import config, db

logger = logging.getLogger(__name__)

FILTER_MESSAGE_TYPES = [
    "PositionReport",
    "StandardClassBPositionReport",
    "ExtendedClassBPositionReport",
    "ShipStaticData",
    "StaticDataReport",
]

# Holds the currently open AISStream connection, if any - lets
# request_reconnect() (called from outside this module, after a UI-driven
# monitoring-zone change) force run_ingestion() to drop it and reconnect
# with a freshly-read bounding box, rather than only picking up the new zone
# whenever the connection next happens to drop on its own, which could be
# hours away. websockets' close() is safe to call from a different task than
# the one reading the connection - that's the officially supported way to
# interrupt it, but doing so while a recv() is in flight surfaces as a
# ConnectionClosedError on the reading side (observed in practice: "sent
# 1000 (OK); no close frame received") rather than the iterator just
# stopping cleanly - _reconnect_requested lets run_ingestion tell that kind
# of closure apart from a genuine dropped connection, so a deliberate zone
# change reconnects immediately and quietly instead of being logged as an
# error and going through the backoff delay meant for real outages.
_current_ws: websockets.WebSocketClientProtocol | None = None
_reconnect_requested = False


async def request_reconnect() -> None:
    global _reconnect_requested
    _reconnect_requested = True
    if _current_ws is not None:
        await _current_ws.close()


def _clean_name(raw) -> str | None:
    if not raw:
        return None
    name = raw.strip(" \x00")
    return name or None


def _loa_from_dimension(dimension: dict) -> float | None:
    """Length overall = bow distance + stern distance (Dimension.A/.B, in
    meters). A message that hasn't got a fix on the vessel's true dimensions
    yet reports these as 0 - treated the same way as the IMO-0 and MMSI-
    placeholder cases elsewhere in this app (see db._norm_imo): a 0 is a
    "not available" marker, not a real 0m-long vessel, so it must not be
    stored as if it were real data."""
    a, b = dimension.get("A"), dimension.get("B")
    if not a or not b:
        return None
    loa = a + b
    return loa if loa > 0 else None


async def _handle_message(data: dict) -> None:
    meta = data.get("MetaData") or {}
    mmsi = meta.get("MMSI")
    if mmsi is None:
        return

    lat = meta.get("latitude")
    lon = meta.get("longitude")
    if lat is not None and lon is not None:
        await asyncio.to_thread(db.upsert_position, mmsi, lat, lon)

    # MetaData.ShipName is populated on PositionReport messages too (not
    # just ShipStaticData) in practice, so a name is often available well
    # before the rarer ShipStaticData broadcast arrives.
    name = _clean_name(meta.get("ShipName"))
    imo = None
    ais_type = None
    loa_m = None
    message_type = data.get("MessageType")
    if message_type == "ShipStaticData":
        ssd = (data.get("Message") or {}).get("ShipStaticData") or {}
        imo = ssd.get("ImoNumber") or ssd.get("Imo")
        name = name or _clean_name(ssd.get("ShipName") or ssd.get("Name"))
        ais_type = ssd.get("Type")
        loa_m = _loa_from_dimension(ssd.get("Dimension") or {})
    elif message_type == "StaticDataReport":
        # Class B vessels (small craft, tugs, pilot boats, etc.) broadcast
        # their name via this message type instead of ShipStaticData - it
        # has no IMO number. The name lives in Part A of the report; Part B
        # carries other details (type, dimensions, callsign) with no name.
        sdr = (data.get("Message") or {}).get("StaticDataReport") or {}
        report_a = sdr.get("ReportA") or {}
        if report_a.get("Valid"):
            name = name or _clean_name(report_a.get("Name"))
        report_b = sdr.get("ReportB") or {}
        if report_b.get("Valid"):
            ais_type = report_b.get("ShipType")
            loa_m = _loa_from_dimension(report_b.get("Dimension") or {})

    if name:
        await asyncio.to_thread(db.upsert_static_data, mmsi, imo, name)

    if ais_type is not None or loa_m is not None:
        await asyncio.to_thread(db.save_type_dimension, mmsi, ais_type, loa_m)


async def run_ingestion() -> None:
    global _current_ws, _reconnect_requested
    backoff = 2.0
    while True:
        try:
            # Read fresh on every (re)connect, not once at import time - a
            # UI-driven monitoring-zone change (see db.set_bounding_box) only
            # takes effect starting with the next connection, which is why
            # request_reconnect() exists to force one immediately instead of
            # waiting for the current connection to drop on its own.
            bounding_box = await asyncio.to_thread(db.get_bounding_box)
            subscription = {
                "APIKey": config.API_KEY,
                "BoundingBoxes": [bounding_box],
                "FilterMessageTypes": FILTER_MESSAGE_TYPES,
            }
            async with websockets.connect("wss://stream.aisstream.io/v0/stream") as ws:
                _current_ws = ws
                await ws.send(json.dumps(subscription))
                logger.info("Connected to aisstream.io (zone=%s)", bounding_box)
                backoff = 2.0
                # A request_reconnect() call that raced with (and lost to)
                # this connection attempt already succeeding - the fresh
                # bounding_box read above already picked up whatever change
                # triggered it, so there's nothing left to act on. Clearing
                # it here stops it from misattributing some later, unrelated
                # disconnect to this one.
                _reconnect_requested = False
                async for raw in ws:
                    try:
                        await _handle_message(json.loads(raw))
                    except Exception:
                        logger.exception("Error handling AIS message")
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            if _reconnect_requested:
                # Our own request_reconnect() caused this - see its comment
                # above for why it surfaces as a ConnectionClosed here rather
                # than the async-for just stopping. Reconnect immediately,
                # no backoff and no scary log line: this wasn't an outage.
                _reconnect_requested = False
                logger.info("Reconnecting AIS stream for a monitoring-zone change")
            else:
                logger.warning(
                    "AIS connection lost, reconnecting in %.1fs", backoff, exc_info=True
                )
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.1))
                backoff = min(backoff * 2, 60.0)
        except Exception:
            logger.warning(
                "AIS connection lost, reconnecting in %.1fs", backoff, exc_info=True
            )
            await asyncio.sleep(backoff + random.uniform(0, backoff * 0.1))
            backoff = min(backoff * 2, 60.0)
        finally:
            _current_ws = None
