import sqlite3
from datetime import datetime, timedelta, timezone

from app import config

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


def init_schema() -> None:
    _get_conn().execute(
        """
        CREATE TABLE IF NOT EXISTS vessels (
            mmsi                    INTEGER PRIMARY KEY,
            imo                     INTEGER,
            name                    TEXT,
            last_lat                REAL,
            last_lon                REAL,
            last_position_at        TEXT,
            air_draft_status        TEXT NOT NULL DEFAULT 'PENDING'
                                     CHECK (air_draft_status IN ('PENDING','OK','FLAGGED','UNKNOWN')),
            air_draft_value_raw     TEXT,
            air_draft_value_m       REAL,
            air_draft_value_ft      REAL,
            air_draft_source_url    TEXT,
            air_draft_source_title  TEXT,
            air_draft_context       TEXT,
            last_checked_at         TEXT,
            name_guessed            INTEGER NOT NULL DEFAULT 0,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        )
        """
    )
    _get_conn().execute(
        "CREATE INDEX IF NOT EXISTS idx_vessels_last_position ON vessels(last_position_at)"
    )
    _get_conn().execute(
        "CREATE INDEX IF NOT EXISTS idx_vessels_status_checked ON vessels(air_draft_status, last_checked_at)"
    )
    existing_columns = {row["name"] for row in _get_conn().execute("PRAGMA table_info(vessels)")}
    if "name_guessed" not in existing_columns:
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN name_guessed INTEGER NOT NULL DEFAULT 0")
    _get_conn().commit()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_position(mmsi: int, lat: float, lon: float) -> None:
    now = _utcnow_iso()
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO vessels (mmsi, last_lat, last_lon, last_position_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(mmsi) DO UPDATE SET
            last_lat = excluded.last_lat,
            last_lon = excluded.last_lon,
            last_position_at = excluded.last_position_at,
            updated_at = excluded.updated_at
        """,
        (mmsi, lat, lon, now, now, now),
    )
    conn.commit()


def upsert_static_data(mmsi: int, imo: int | None, name: str | None) -> bool:
    """Store name/IMO for a vessel, from AIS's own static-data broadcast -
    always treated as authoritative, clearing name_guessed even if a
    heuristic MMSI-search name (see set_guessed_name) was set earlier.
    Returns True if this is the first time a name became known for this
    vessel (i.e. it should be enqueued for an air draft lookup)."""
    now = _utcnow_iso()
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO vessels (mmsi, imo, name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(mmsi) DO UPDATE SET
            imo = COALESCE(excluded.imo, vessels.imo),
            name = COALESCE(excluded.name, vessels.name),
            name_guessed = CASE WHEN excluded.name IS NOT NULL THEN 0 ELSE vessels.name_guessed END,
            updated_at = excluded.updated_at
        """,
        (mmsi, imo, name, now, now),
    )
    conn.commit()

    row = conn.execute(
        "SELECT name, air_draft_status, last_checked_at FROM vessels WHERE mmsi = ?",
        (mmsi,),
    ).fetchone()
    return bool(
        row
        and row["name"]
        and row["air_draft_status"] == "PENDING"
        and row["last_checked_at"] is None
    )


def set_guessed_name(mmsi: int, name: str, imo: int | None) -> None:
    """Stores a name resolved from the MMSI alone (see
    air_draft_resolver.resolve_vessel_name), flagged via name_guessed so the
    UI can prompt a human to double-check it. Only applies if the vessel
    still has no name - if AIS's own static-data broadcast (upsert_static_data)
    already set one in the meantime, that's authoritative and this is a
    no-op, avoiding a race where a slower guess clobbers a confirmed name."""
    now = _utcnow_iso()
    conn = _get_conn()
    conn.execute(
        """
        UPDATE vessels SET
            name = ?,
            imo = COALESCE(imo, ?),
            name_guessed = 1,
            updated_at = ?
        WHERE mmsi = ? AND name IS NULL
        """,
        (name, imo, now, mmsi),
    )
    conn.commit()


def save_lookup_result(
    mmsi: int,
    status: str,
    value_raw: str | None,
    value_m: float | None,
    value_ft: float | None,
    source_url: str | None,
    source_title: str | None,
    context: str | None,
) -> None:
    now = _utcnow_iso()
    conn = _get_conn()
    conn.execute(
        """
        UPDATE vessels SET
            air_draft_status = ?,
            air_draft_value_raw = ?,
            air_draft_value_m = ?,
            air_draft_value_ft = ?,
            air_draft_source_url = ?,
            air_draft_source_title = ?,
            air_draft_context = ?,
            last_checked_at = ?,
            updated_at = ?
        WHERE mmsi = ?
        """,
        (status, value_raw, value_m, value_ft, source_url, source_title, context, now, now, mmsi),
    )
    conn.commit()


def get_vessel(mmsi: int) -> sqlite3.Row | None:
    conn = _get_conn()
    return conn.execute("SELECT * FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()


def get_active_vessels(silence_window_minutes: float) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silence_window_minutes)).isoformat()
    conn = _get_conn()
    return conn.execute(
        """
        SELECT * FROM vessels
        WHERE last_position_at >= ?
        ORDER BY (air_draft_status = 'FLAGGED') DESC, last_position_at DESC
        """,
        (cutoff,),
    ).fetchall()


def get_vessels_needing_name_lookup(silence_window_minutes: float) -> list[sqlite3.Row]:
    """Vessels with a recent position but no name yet - AIS hasn't delivered
    a static-data message for them. Scoped to the same silence window as
    dashboard visibility, same reasoning as get_vessels_needing_initial_lookup."""
    position_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silence_window_minutes)).isoformat()
    conn = _get_conn()
    return conn.execute(
        """
        SELECT mmsi FROM vessels
        WHERE name IS NULL AND last_position_at >= ?
        """,
        (position_cutoff,),
    ).fetchall()


def get_vessels_needing_initial_lookup(silence_window_minutes: float) -> list[sqlite3.Row]:
    position_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silence_window_minutes)).isoformat()
    conn = _get_conn()
    return conn.execute(
        """
        SELECT mmsi, name FROM vessels
        WHERE air_draft_status = 'PENDING' AND name IS NOT NULL AND last_checked_at IS NULL
          AND last_position_at >= ?
        """,
        (position_cutoff,),
    ).fetchall()


def get_stale_unknown_vessels(unknown_retry_hours: float, silence_window_minutes: float) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=unknown_retry_hours)).isoformat()
    position_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silence_window_minutes)).isoformat()
    conn = _get_conn()
    return conn.execute(
        """
        SELECT mmsi, name FROM vessels
        WHERE air_draft_status = 'UNKNOWN' AND last_checked_at <= ?
          AND last_position_at >= ?
        """,
        (cutoff, position_cutoff),
    ).fetchall()
