"""
metrics.py — SQLite-backed time-series store for pool metrics.

Tables
------
sensor_readings  – Air temperature and salt level at 30-min intervals.
equipment_states – Equipment ON/OFF snapshots at every controller poll (~15 s).

Retention: rows older than 90 days are pruned on each write.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

BASE_DIR = Path(config.__file__).resolve().parent
METRICS_DB_PATH = BASE_DIR / "metrics.db"

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(METRICS_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """Create tables and indexes if they do not exist."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,  -- ISO-8601 UTC
                air_temp_f  REAL,
                salt_ppm    REAL
            );

            CREATE TABLE IF NOT EXISTS equipment_states (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                equipment   TEXT    NOT NULL,
                state       TEXT    NOT NULL  -- 'ON', 'OFF', 'UNKNOWN'
            );

            -- Indexes for time-range queries
            CREATE INDEX IF NOT EXISTS ix_sr_ts ON sensor_readings(timestamp);
            CREATE INDEX IF NOT EXISTS ix_es_ts_eq ON equipment_states(equipment, timestamp);
            CREATE INDEX IF NOT EXISTS ix_es_ts   ON equipment_states(timestamp);
        """)


# ---------------------------------------------------------------------------
# Inserts
# ---------------------------------------------------------------------------

def insert_sensor(air_temp_f: float | None = None,
                  salt_ppm: float | None = None) -> int:
    """Insert a sensor reading. Returns row id."""
    ts = datetime.now(timezone.utc).isoformat()
    with _lock, _get_conn() as conn:
        prune(conn, "sensor_readings", 90)
        cur = conn.execute(
            "INSERT INTO sensor_readings (timestamp, air_temp_f, salt_ppm) VALUES (?, ?, ?)",
            (ts, air_temp_f, salt_ppm),
        )
        return cur.lastrowid


def insert_equipment_states(states: dict[str, str]) -> int:
    """Insert ON/OFF snapshots for all equipment at once. Returns count."""
    ts = datetime.now(timezone.utc).isoformat()
    rows = [(ts, eq, st) for eq, st in states.items()]
    with _lock, _get_conn() as conn:
        prune(conn, "equipment_states", 90)
        conn.executemany(
            "INSERT INTO equipment_states (timestamp, equipment, state) VALUES (?, ?, ?)",
            rows,
        )
        return len(rows)


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def prune(conn: sqlite3.Connection, table: str, days: int) -> None:
    """Delete rows older than *days*."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))


# ---------------------------------------------------------------------------
# Aggregation queries  — used by the dashboard API
# ---------------------------------------------------------------------------

def daily_air_temp(days: int = 30) -> list[dict]:
    """Return one row per day: date, avg / min / max air temp."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT
                date(timestamp) AS d,
                ROUND(AVG(air_temp_f), 1)  AS avg_f,
                ROUND(MIN(air_temp_f), 1)  AS min_f,
                ROUND(MAX(air_temp_f), 1)  AS max_f
            FROM sensor_readings
            WHERE timestamp >= date('now', ?)
              AND air_temp_f IS NOT NULL
            GROUP BY d ORDER BY d
        """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def daily_salt_level(days: int = 30) -> list[dict]:
    """Return one row per day: date, avg / min / max salt ppm."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT
                date(timestamp) AS d,
                ROUND(AVG(salt_ppm), 0)  AS avg_ppm,
                ROUND(MIN(salt_ppm), 0)  AS min_ppm,
                ROUND(MAX(salt_ppm), 0)  AS max_ppm
            FROM sensor_readings
            WHERE timestamp >= date('now', ?)
              AND salt_ppm IS NOT NULL
            GROUP BY d ORDER BY d
        """, (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def latest_sensor() -> dict:
    """Most recent sensor reading."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sensor_readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else {}


def equipment_daily_runtime(equipment: str, days: int = 30) -> list[dict]:
    """Minutes-ON per day for one piece of equipment.

    Each ON snapshot represents ~15 s (dashboard polls every 15 s).
    So minutes = COUNT × 15 / 60 = COUNT × 0.25.
    """
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT date(timestamp) AS d,
                   ROUND(COUNT(*) * 0.25, 0) AS minutes_on
              FROM equipment_states
             WHERE equipment = ?
               AND state = 'ON'
               AND timestamp >= datetime('now', ?)
             GROUP BY d ORDER BY d
        """, (equipment, f"-{days} days")).fetchall()
    return [dict(r) for r in rows]


def equipment_total_runtime(equipment: str, days: int = 7) -> dict:
    """Total hours-ON over the last N days."""
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT ROUND(COUNT(*) * 15.0 / 3600.0, 1) AS total_hours
              FROM equipment_states
             WHERE equipment = ? AND state = 'ON'
               AND timestamp >= datetime('now', ?)
        """, (equipment, f"-{days} days")).fetchone()
    return dict(row) if row else {"total_hours": 0}


def equipment_active_runtime(equipment: str, days: int = 7) -> dict:
    """Hours-ON for *equipment* only during timestamps when the filter is also ON.

    Pool and spa equipment only circulate water while the filter pump runs.
    This query counts an equipment's ON snapshots exclusively at moments
    where a corresponding filter='ON' snapshot exists at the same timestamp,
    giving a more accurate "actively running" measurement.
    """
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT ROUND(COUNT(*) * 15.0 / 3600.0, 1) AS active_hours
              FROM equipment_states es
             WHERE es.equipment = ? AND es.state = 'ON'
               AND es.timestamp >= datetime('now', ?)
               -- Only count this snapshot when filter was also ON at the same time
               AND EXISTS (
                   SELECT 1
                     FROM equipment_states ef
                    WHERE ef.timestamp = es.timestamp
                      AND ef.equipment = 'filter'
                      AND ef.state = 'ON'
               )
        """, (equipment, f"-{days} days")).fetchone()
    return dict(row) if row else {"active_hours": 0}


def last_7d_runtime_summary() -> list[dict]:
    """Total hours ON (last 7 days) for every known equipment.

    Returns two columns per row:
      - total_hours : raw hours-ON regardless of other equipment
      - active_hours : hours-ON only during timestamps when the filter was also
                       running (more accurate "actively circulating" measurement).
                       For 'filter' itself, active_hours equals total_hours.
    """
    equipment_list = [
        "filter", "pool", "spa", "heater", "cleaner",
        "waterfall", "lights", "spa_light", "blower",
    ]
    result: list[dict] = []
    with _get_conn() as conn:
        for eq in equipment_list:
            # Total hours (always useful)
            row = conn.execute("""
                SELECT ROUND(COUNT(*) * 15.0 / 3600.0, 1) AS total_hours
                  FROM equipment_states
                 WHERE equipment = ? AND state = 'ON'
                   AND timestamp >= datetime('now', '-7 days')
            """, (eq,)).fetchone()
            # Active hours — only when filter is ON (same for filter itself)
            row_active = conn.execute("""
                SELECT ROUND(COUNT(*) * 15.0 / 3600.0, 1) AS active_hours
                  FROM equipment_states es
                 WHERE es.equipment = ? AND es.state = 'ON'
                   AND es.timestamp >= datetime('now', '-7 days')
                   AND EXISTS (
                       SELECT 1 FROM equipment_states ef
                        WHERE ef.timestamp = es.timestamp
                          AND ef.equipment = 'filter' AND ef.state = 'ON'
                   )
            """, (eq,)).fetchone()
            result.append({
                "equipment": eq,
                "total_hours": row["total_hours"] if row else 0,
                "active_hours": row_active["active_hours"] if row_active else 0,
            })
    return result
