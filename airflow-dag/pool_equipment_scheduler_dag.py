"""
pool_equipment_scheduler_dag.py — Schedule pool equipment on/off times.

Runs every minute. Each equipment task checks if current time is within
any of the configured on/off windows, reads the controller state, and toggles
only if needed (idempotent).

MAINTENANCE WINDOW
------------------
The scheduler is dormant from 07:00 AM to 12:30 AM (crosses midnight) so
that manual pool settings are not overridden.  Equipment tasks only apply
scheduled on/off changes during the active window of 12:30 AM – 06:59 AM
(i.e., effectively 1 AM – 6 AM).  The status-check task still runs during
the maintenance window for monitoring visibility.

Schedule configuration is stored in Airflow Variable `pool_schedules` (JSON).
Each equipment can have one or more time windows:

    {
      "pump": { "on": "05:00", "off": "23:00" },
      "heater": [
        { "on": "06:00", "off": "12:00" },
        { "on": "18:00", "off": "22:00" }
      ]
    }

A single dict or a list of dicts is accepted for each equipment.
The equipment is turned ON if the current time falls inside ANY window.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta

# Ensure dags folder is on import path so we can import pool_controller
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zoneinfo import ZoneInfo

from airflow import DAG
from airflow.sdk import Variable
from airflow.providers.standard.operators.python import PythonOperator
from airflow.providers.standard.operators.empty import EmptyOperator

# Import shared controller module
from pool_controller import (
    SLOT_DEFINITIONS,
    ensure_slot_state,
    format_status_report,
    get_status,
    toggle_super_chlorinator,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Maintenance window — DAG is dormant during these hours so manual pool
# settings are not overridden.  Active only outside this window
# (i.e., the scheduler applies equipment on/off between maintenance_end
# and maintenance_start).
# ---------------------------------------------------------------------------
# Dormant: 07:00 AM → 12:30 AM (past midnight)
# Active  : 12:30 AM → 06:59 AM  (1 AM – 6 AM falls inside this range)
MAINTENANCE_START = 7 * 60 + 0    # 07:00 in minutes (420)
MAINTENANCE_END = 0 * 60 + 30     # 00:30 in minutes (30)


def _get_timezone() -> ZoneInfo:
    tz_name = Variable.get("pool_timezone", default="America/New_York")
    return ZoneInfo(tz_name)


def _is_maintenance_window(now: datetime) -> bool:
    """Return True when the scheduler should be dormant (manual control hours).

    The maintenance window is 07:00 – 00:30 (crosses midnight), so during
    this period every equipment task is skipped and the pool controller
    is left untouched.
    """
    current_minutes = now.hour * 60 + now.minute
    # Window crosses midnight: dormant from MAINTENANCE_START through end-of-day,
    # and from start-of-day through MAINTENANCE_END.
    return current_minutes >= MAINTENANCE_START or current_minutes < MAINTENANCE_END


def _get_schedules() -> dict:
    raw = Variable.get("pool_schedules", default="{}")
    return json.loads(raw)


def _is_in_window(on_time: str, off_time: str, now: datetime) -> bool:
    """Check if current time is within [on_time, off_time) window.

    Handles overnight windows (e.g., 22:00 → 06:00).
    """
    on_h, on_m = map(int, on_time.split(":"))
    off_h, off_m = map(int, off_time.split(":"))

    current_minutes = now.hour * 60 + now.minute
    on_minutes = on_h * 60 + on_m
    off_minutes = off_h * 60 + off_m

    if on_minutes < off_minutes:
        # Normal window, e.g., 06:00 → 22:00
        return on_minutes <= current_minutes < off_minutes
    else:
        # Overnight window, e.g., 22:00 → 06:00
        return current_minutes >= on_minutes or current_minutes < off_minutes


def _is_in_any_window(schedules: list, now: datetime) -> bool:
    """Check if current time is inside ANY of the provided on/off windows.

    Handles overnight windows and returns True if current time matches
    at least one window.
    """
    return any(_is_in_window(s["on"], s["off"], now) for s in schedules)


def _normalize_schedules(raw_sched) -> list:
    """Normalize equipment schedule to a list of {on, off} dicts.

    Accepts:
      - A single dict: {"on": "05:00", "off": "23:00"}
      - A list of dicts: [{"on": "05:00", "off": "12:00"}, ...]

    Returns a list of dicts in all cases.
    """
    if isinstance(raw_sched, dict):
        return [raw_sched]
    return raw_sched


# ---------------------------------------------------------------------------
# Task callables
# ---------------------------------------------------------------------------
def check_status_task(**kwargs) -> None:
    """Fetch and log current controller status."""
    logger.info("Fetching controller status...")
    status = get_status()
    report = format_status_report(status)
    logger.info(report)


def make_equipment_task(equipment: str):
    """Factory for equipment toggle tasks.

    Each task reads the schedule (single window or multiple windows),
    determines desired state, and ensures the controller slot matches.

    Skips all changes during the maintenance window so manual pool
    settings are not overridden.
    """

    def _task(**kwargs) -> None:
        # Maintenance window guard — do not touch equipment during daytime
        tz = _get_timezone()
        now = datetime.now(tz)
        if _is_maintenance_window(now):
            logger.info(
                "%s: maintenance window (%s), skipping.",
                equipment,
                now.strftime("%H:%M"),
            )
            return

        schedules = _get_schedules()
        if equipment not in schedules:
            logger.warning("No schedule for %s, skipping.", equipment)
            return

        raw_sched = schedules[equipment]
        windows = _normalize_schedules(raw_sched)

        desired_on = _is_in_any_window(windows, now)

        window_summary = " | ".join(
            f"{w['on']}-{w['off']}" for w in windows
        )
        logger.info(
            "%s: current time=%s, windows=[%s], desired=%s",
            equipment,
            now.strftime("%H:%M"),
            window_summary,
            "ON" if desired_on else "OFF",
        )

        result = ensure_slot_state(equipment, desired_on)
        logger.info("%s", result)

    return _task


def super_chlorinator_task(**kwargs) -> None:
    """Toggle super chlorinator based on schedule.

    Skips during the maintenance window so manual pool settings are not
    overridden.
    """
    # Maintenance window guard
    tz = _get_timezone()
    now = datetime.now(tz)
    if _is_maintenance_window(now):
        logger.info(
            "super_chlorinator: maintenance window (%s), skipping.",
            now.strftime("%H:%M"),
        )
        return

    schedules = _get_schedules()
    if "super_chlorinator" not in schedules:
        logger.warning("No schedule for super_chlorinator, skipping.")
        return

    raw_sched = schedules["super_chlorinator"]
    windows = _normalize_schedules(raw_sched)

    desired_on = _is_in_any_window(windows, now)

    window_summary = " | ".join(
        f"{w['on']}-{w['off']}" for w in windows
    )
    logger.info(
        "super_chlorinator: current time=%s, windows=[%s], desired=%s",
        now.strftime("%H:%M"),
        window_summary,
        "ON" if desired_on else "OFF",
    )

    result = toggle_super_chlorinator(desired_on)
    logger.info("%s", result)


# ---------------------------------------------------------------------------
# DAG definition
# ---------------------------------------------------------------------------
default_args = {
    "owner": "airflow",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
    "execution_timeout": timedelta(minutes=5),
}

with DAG(
    dag_id="pool_equipment_scheduler",
    description="Hayward pool equipment scheduler — turns equipment on/off based on schedule.",
    schedule="* * * * *",  # Every minute
    start_date=datetime(2026, 5, 30, 0, 0, 0),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["pool", "hayward"],
) as dag:
    # Status check runs first
    check_status = PythonOperator(
        task_id="check_status",
        python_callable=check_status_task,
    )

    # Equipment tasks — all run in parallel after status check
    equipment_tasks = {}
    for eq_name in SLOT_DEFINITIONS:
        task = PythonOperator(
            task_id=f"turn_{eq_name}_state",
            python_callable=make_equipment_task(eq_name),
        )
        equipment_tasks[eq_name] = task
        check_status >> task

    # Super chlorinator — separate task (menu navigation, not a simple toggle)
    sc_task = PythonOperator(
        task_id="turn_super_chlorinator_state",
        python_callable=super_chlorinator_task,
    )
    check_status >> sc_task
