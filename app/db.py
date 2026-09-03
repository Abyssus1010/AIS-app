import sqlite3
from datetime import datetime, timedelta, timezone

from app import config, vessel_height_table

_conn: sqlite3.Connection | None = None

# Columns from the old web-search-based air draft lookup (dropped in favor
# of the type+size table - see vessel_height_table.py). Migrated away via
# DROP COLUMN below rather than left as dead weight in an existing DB.
_RETIRED_AIR_DRAFT_COLUMNS = [
    "air_draft_status",
    "air_draft_value_raw",
    "air_draft_value_m",
    "air_draft_value_ft",
    "air_draft_source_url",
    "air_draft_source_title",
    "air_draft_context",
    "air_draft_identity",
    "last_checked_at",
    "last_attempted_at",
]


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
            name_guessed            INTEGER NOT NULL DEFAULT 0,
            category_guessed        INTEGER NOT NULL DEFAULT 0,
            loa_guessed              INTEGER NOT NULL DEFAULT 0,
            ais_type                INTEGER,
            loa_m                   REAL,
            est_height_status       TEXT NOT NULL DEFAULT 'UNKNOWN'
                                     CHECK (est_height_status IN ('UNKNOWN','OK','FLAGGED')),
            est_height_ft           REAL,
            est_height_category     TEXT,
            est_height_bracket      TEXT,
            est_height_confidence   TEXT,
            est_height_note         TEXT,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        )
        """
    )
    _get_conn().execute(
        "CREATE INDEX IF NOT EXISTS idx_vessels_last_position ON vessels(last_position_at)"
    )
    existing_columns = {row["name"] for row in _get_conn().execute("PRAGMA table_info(vessels)")}
    if "name_guessed" not in existing_columns:
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN name_guessed INTEGER NOT NULL DEFAULT 0")
    if "category_guessed" not in existing_columns:
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN category_guessed INTEGER NOT NULL DEFAULT 0")
    if "loa_guessed" not in existing_columns:
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN loa_guessed INTEGER NOT NULL DEFAULT 0")
    if "ais_type" not in existing_columns:
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN ais_type INTEGER")
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN loa_m REAL")
        _get_conn().execute(
            "ALTER TABLE vessels ADD COLUMN est_height_status TEXT NOT NULL DEFAULT 'UNKNOWN'"
        )
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN est_height_ft REAL")
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN est_height_category TEXT")
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN est_height_bracket TEXT")
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN est_height_confidence TEXT")
        _get_conn().execute("ALTER TABLE vessels ADD COLUMN est_height_note TEXT")
    if any(column in existing_columns for column in _RETIRED_AIR_DRAFT_COLUMNS):
        _get_conn().execute("DROP INDEX IF EXISTS idx_vessels_status_checked")
    for column in _RETIRED_AIR_DRAFT_COLUMNS:
        if column in existing_columns:
            _get_conn().execute(f"ALTER TABLE vessels DROP COLUMN {column}")
    _get_conn().commit()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_imo(imo: int | None) -> int | None:
    """AIS transmits IMO 0 to mean "none assigned", and the name/IMO
    resolver can echo a tracker URL's `imo:0` placeholder - either way, a
    stored 0 is a trap: `set_imo` and the backfill query both look for
    `imo IS NULL`, so a 0 permanently blocks IMO recovery while carrying no
    information. Normalize any non-positive/non-7-digit value to NULL."""
    if imo is None or not (1_000_000 <= imo <= 9_999_999):
        return None
    return imo


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


def upsert_static_data(mmsi: int, imo: int | None, name: str | None) -> None:
    """Store name/IMO for a vessel, from AIS's own static-data broadcast -
    always treated as authoritative, clearing name_guessed even if a
    heuristic MMSI-search name (see set_guessed_name) was set earlier."""
    now = _utcnow_iso()
    imo = _norm_imo(imo)
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


def set_guessed_name(mmsi: int, name: str, imo: int | None) -> None:
    """Stores a name resolved from the MMSI alone (see
    vessel_name_lookup.resolve_vessel_name), flagged via name_guessed so the
    UI can prompt a human to double-check it. Only applies if the vessel
    still has no name - if AIS's own static-data broadcast (upsert_static_data)
    already set one in the meantime, that's authoritative and this is a
    no-op, avoiding a race where a slower guess clobbers a confirmed name."""
    now = _utcnow_iso()
    imo = _norm_imo(imo)
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


def set_imo(mmsi: int, imo: int) -> None:
    """Stores an IMO recovered via worker.run_imo_backfill (see its docstring
    for why this is needed - Class B craft never broadcast IMO at all, even
    once their name is known). Only applies if the vessel still has no IMO -
    if AIS's own static-data broadcast (upsert_static_data) already set one
    in the meantime, that's authoritative and this is a no-op."""
    imo = _norm_imo(imo)
    if imo is None:
        return
    now = _utcnow_iso()
    conn = _get_conn()
    conn.execute(
        "UPDATE vessels SET imo = ?, updated_at = ? WHERE mmsi = ? AND (imo IS NULL OR imo = 0)",
        (imo, now, mmsi),
    )
    conn.commit()


def save_type_dimension(mmsi: int, ais_type: int | None, loa_m: float | None) -> None:
    """Stores AIS-reported ship type / length-overall and recomputes the
    generic type+size height estimate (see vessel_height_table) from the
    merged values. COALESCE keeps whichever of type/LOA was already known if
    this particular message didn't carry one of them - in practice
    ShipStaticData and StaticDataReport.ReportB always report Type and
    Dimension together, so this only matters if an earlier message had one
    and a later one has the other (e.g. a corrected/re-sent static report)."""
    now = _utcnow_iso()
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO vessels (mmsi, ais_type, loa_m, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(mmsi) DO UPDATE SET
            ais_type = COALESCE(excluded.ais_type, vessels.ais_type),
            loa_m = COALESCE(excluded.loa_m, vessels.loa_m),
            updated_at = excluded.updated_at
        """,
        (mmsi, ais_type, loa_m, now, now),
    )
    row = conn.execute("SELECT ais_type, loa_m FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()
    estimate = vessel_height_table.estimate_height_ft(row["ais_type"], row["loa_m"])
    if estimate.height_ft is None:
        status = "UNKNOWN"
    else:
        status = "FLAGGED" if estimate.height_ft > config.HEIGHT_THRESHOLD_FT else "OK"
    conn.execute(
        """
        UPDATE vessels SET
            est_height_status = ?,
            est_height_ft = ?,
            est_height_category = ?,
            est_height_bracket = ?,
            est_height_confidence = ?,
            est_height_note = ?,
            category_guessed = CASE WHEN ? THEN 0 ELSE category_guessed END,
            loa_guessed = CASE WHEN ? THEN 0 ELSE loa_guessed END,
            updated_at = ?
        WHERE mmsi = ?
        """,
        (
            status,
            estimate.height_ft,
            estimate.category.value,
            estimate.bracket,
            estimate.confidence,
            estimate.note,
            row["ais_type"] is not None,
            loa_m is not None,
            now,
            mmsi,
        ),
    )
    conn.commit()


def set_category_from_web(mmsi: int, category: str) -> None:
    """Stores a height-estimate category resolved via a web search (see
    vessel_name_lookup.resolve_vessel_name) for a vessel AIS has never
    reported a type for. Only applies while ais_type is still NULL - if AIS's
    own static-data broadcast arrives in the meantime, that's authoritative
    and save_type_dimension already clears category_guessed and recomputes
    from the real ais_type, the same way upsert_static_data's real name
    overrides set_guessed_name's guess."""
    conn = _get_conn()
    row = conn.execute("SELECT ais_type, loa_m FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()
    if row is None or row["ais_type"] is not None:
        return
    estimate = vessel_height_table.estimate_height_from_web_category(
        vessel_height_table.Category(category), row["loa_m"]
    )
    if estimate.height_ft is None:
        status = "UNKNOWN"
    else:
        status = "FLAGGED" if estimate.height_ft > config.HEIGHT_THRESHOLD_FT else "OK"
    now = _utcnow_iso()
    conn.execute(
        """
        UPDATE vessels SET
            category_guessed = 1,
            est_height_status = ?,
            est_height_ft = ?,
            est_height_category = ?,
            est_height_bracket = ?,
            est_height_confidence = ?,
            est_height_note = ?,
            updated_at = ?
        WHERE mmsi = ? AND ais_type IS NULL
        """,
        (
            status,
            estimate.height_ft,
            estimate.category.value,
            estimate.bracket,
            estimate.confidence,
            estimate.note,
            now,
            mmsi,
        ),
    )
    conn.commit()


def set_loa_from_web(mmsi: int, loa_m: float) -> None:
    """Stores a length overall resolved via a web search (see
    vessel_name_lookup._length_m_from_text) for a vessel whose AIS Dimension
    came back empty (Dimension.A/B are 0 - see
    ais_client._loa_from_dimension). Only applies while loa_m is still NULL -
    a real AIS Dimension arriving later is authoritative and
    save_type_dimension's own COALESCE overwrites this unconditionally when
    that happens (and clears loa_guessed, mirroring category_guessed).

    If a category is already known (real ais_type, or an earlier web guess
    via set_category_from_web), recomputes the height estimate immediately
    using this LOA in place of the largest-bracket placeholder. If no
    category is known at all yet, just stores the value - whichever of
    save_type_dimension/set_category_from_web resolves the category next
    will pick this LOA up on its own (both already SELECT loa_m fresh)."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT ais_type, loa_m, est_height_category FROM vessels WHERE mmsi = ?", (mmsi,)
    ).fetchone()
    if row is None or row["loa_m"] is not None:
        return
    now = _utcnow_iso()

    if row["ais_type"] is None and row["est_height_category"] is None:
        conn.execute(
            "UPDATE vessels SET loa_m = ?, loa_guessed = 1, updated_at = ? WHERE mmsi = ? AND loa_m IS NULL",
            (loa_m, now, mmsi),
        )
        conn.commit()
        return

    if row["ais_type"] is not None:
        estimate = vessel_height_table.estimate_height_ft(row["ais_type"], loa_m)
        note = f"length overall sourced from a web search, not AIS ({estimate.note})"
        confidence = "low"
    else:
        estimate = vessel_height_table.estimate_height_from_web_category(
            vessel_height_table.Category(row["est_height_category"]), loa_m
        )
        note = estimate.note
        confidence = estimate.confidence

    if estimate.height_ft is None:
        status = "UNKNOWN"
    else:
        status = "FLAGGED" if estimate.height_ft > config.HEIGHT_THRESHOLD_FT else "OK"
    conn.execute(
        """
        UPDATE vessels SET
            loa_m = ?,
            loa_guessed = 1,
            est_height_status = ?,
            est_height_ft = ?,
            est_height_category = ?,
            est_height_bracket = ?,
            est_height_confidence = ?,
            est_height_note = ?,
            updated_at = ?
        WHERE mmsi = ? AND loa_m IS NULL
        """,
        (
            loa_m,
            status,
            estimate.height_ft,
            estimate.category.value,
            estimate.bracket,
            confidence,
            note,
            now,
            mmsi,
        ),
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
        ORDER BY (est_height_status = 'FLAGGED') DESC, last_position_at DESC
        """,
        (cutoff,),
    ).fetchall()


def get_vessels_needing_name_lookup(silence_window_minutes: float) -> list[sqlite3.Row]:
    """Vessels with a recent position but no name yet - AIS hasn't delivered
    a static-data message for them. Scoped to the same silence window as
    dashboard visibility: a vessel that's gone quiet longer than that won't
    be retried until it's heard from again, so workers aren't spent
    re-resolving vessels nobody can currently see."""
    position_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silence_window_minutes)).isoformat()
    conn = _get_conn()
    return conn.execute(
        """
        SELECT mmsi FROM vessels
        WHERE name IS NULL AND last_position_at >= ?
        """,
        (position_cutoff,),
    ).fetchall()


def get_vessels_needing_imo_lookup(silence_window_minutes: float) -> list[sqlite3.Row]:
    """Vessels whose name is known but whose IMO never arrived - see
    worker.run_imo_backfill for why. Scoped to the same silence window as
    dashboard visibility, same reasoning as get_vessels_needing_name_lookup."""
    position_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silence_window_minutes)).isoformat()
    conn = _get_conn()
    return conn.execute(
        """
        SELECT mmsi FROM vessels
        WHERE (imo IS NULL OR imo = 0) AND name IS NOT NULL AND last_position_at >= ?
        """,
        (position_cutoff,),
    ).fetchall()


def get_vessels_needing_category_lookup(silence_window_minutes: float) -> list[sqlite3.Row]:
    """Vessels AIS has never reported a type for, whose name+IMO are already
    both resolved - these are invisible to get_vessels_needing_imo_lookup
    (its trigger, imo IS NULL, is already false for them), so without a
    dedicated retry here they're stuck forever if the one search that
    resolved their name/IMO happened to hit a result title
    vessel_name_lookup._category_from_title couldn't parse (e.g. a
    different tracker site's title shape) - in practice the majority case,
    not a rare edge case."""
    position_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silence_window_minutes)).isoformat()
    conn = _get_conn()
    return conn.execute(
        """
        SELECT mmsi FROM vessels
        WHERE ais_type IS NULL AND category_guessed = 0
          AND name IS NOT NULL AND imo IS NOT NULL AND imo != 0
          AND last_position_at >= ?
        """,
        (position_cutoff,),
    ).fetchall()


def get_vessels_needing_loa_lookup(silence_window_minutes: float) -> list[sqlite3.Row]:
    """Vessels where AIS's own Dimension came back empty (ais_type IS NOT
    NULL confirms a real static-data message arrived, but loa_m is still
    NULL) and name/IMO are already both resolved too, so none of the other
    backfill loops would ever search for this vessel again. Vessels still
    missing name, IMO, or category get an LOA extraction attempt for free as
    a byproduct of those loops' own searches (see worker._guess_and_save /
    run_imo_backfill / run_category_backfill) - this covers only the
    otherwise-fully-resolved residual case."""
    position_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=silence_window_minutes)).isoformat()
    conn = _get_conn()
    return conn.execute(
        """
        SELECT mmsi FROM vessels
        WHERE ais_type IS NOT NULL AND loa_m IS NULL
          AND name IS NOT NULL AND imo IS NOT NULL AND imo != 0
          AND last_position_at >= ?
        """,
        (position_cutoff,),
    ).fetchall()


def purge_stale_vessels(retention_hours: float) -> int:
    """Deletes vessels that have been silent longer than retention_hours -
    see worker.run_stale_purge. Distinct from SILENCE_WINDOW_MINUTES, which
    only hides a vessel from the dashboard/lookup workers without ever
    freeing its row; nothing else deletes from this table. Falls back to
    created_at for the handful of rows that somehow never got a position at
    all (last_position_at IS NULL), so those can't survive forever either."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM vessels WHERE COALESCE(last_position_at, created_at) < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount
