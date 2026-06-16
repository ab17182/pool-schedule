"""
app.py — FastAPI web frontend for Pool Equipment Scheduler.

Provides dashboard UI, schedule editing, and action history.
Syncs schedule changes to Airflow via REST API.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from airflow_client import AirflowClient
from controller import (
    get_status, get_equipment_status_dict, ensure_slot_state,
    SLOT_DEFINITIONS, SCHEDULABLE_ITEMS, lcd_read_all_pages,
)
from local_store import LocalStore
import metrics as metrics_db

# -------------------------------------------------------------------
# Setup
# -------------------------------------------------------------------

app = FastAPI(title="Pool Equipment Scheduler")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Prevent stale browser caches from serving old JS/CSS.
# Browsers revalidate via ETag/Last-Modified but don't serve from cache blindly.
from fastapi.responses import Response


@app.middleware("http")
async def add_cache_control_headers(request, call_next):
    response = await call_next(request)
    # Prevent browser caching of HTML pages and static assets during development.
    if request.url.path.startswith("/static") or response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

airflow_client = AirflowClient()
local_store = LocalStore()

logging.basicConfig(
    filename=os.path.join(BASE_DIR, "logs", "app.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("poolschedule")

# Initialise the metrics SQLite database (idempotent).
metrics_db.init_db()

# -------------------------------------------------------------------
# Background equipment-state poller
# -------------------------------------------------------------------
# Equipment ON/OFF snapshots must be recorded continuously — not only when
# someone has the dashboard open.  This daemon thread polls the Hayward
# controller every 15 s and writes states to the metrics DB so that the
# Analytics runtime bar chart reflects actual runtimes (e.g., overnight
# spa / cleaner runs that happen while nobody is viewing the page).

_EQUIPMENT_POLL_INTERVAL = 15  # seconds — matches the multiplier in metrics.py


def _equipment_state_poller() -> None:
    """Continuously poll the controller and record equipment states."""
    thread_name = threading.current_thread().name
    while True:
        try:
            status = get_status()
            equipment_dict = get_equipment_status_dict(status)
            metrics_db.insert_equipment_states(equipment_dict)
        except Exception as e:
            logger.warning("[%s] Failed to record equipment states: %s", thread_name, e)
        time.sleep(_EQUIPMENT_POLL_INTERVAL)


_eq_poller_thread = threading.Thread(
    target=_equipment_state_poller,
    name="equipment-poller",
    daemon=True,
)
_eq_poller_thread.start()
logger.info("Background equipment-state poller started (%d s interval)", _EQUIPMENT_POLL_INTERVAL)

# -------------------------------------------------------------------
# Pages
# -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    # Slot-based equipment (physical controller keys — shown in Status tab)
    slot_equipment = [
        {"name": name, "slot": info["slot"], "label": info["label"], "hex_code": info["hex_code"]}
        for name, info in SLOT_DEFINITIONS.items()
    ]
    # All schedulable equipment (includes super_chlorinator — shown in Schedule tab)
    schedulable_equipment = [
        {"name": name, "slot": info["slot"], "label": info["label"], "hex_code": info["hex_code"]}
        for name, info in SCHEDULABLE_ITEMS.items()
    ]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "slot_equipment_list": slot_equipment,
            "schedulable_equipment_list": schedulable_equipment,
            "refresh_interval": config.DASHBOARD_REFRESH_INTERVAL,
        },
    )

# -------------------------------------------------------------------
# API: Status
# -------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    """Fetch current Hayward controller status and return equipment ON/OFF.

    Note: Equipment state recording is handled by the background poller thread
    so that runtimes are tracked continuously — not only when the dashboard is
    open in a browser.
    """
    try:
        status = get_status()
        equipment_dict = get_equipment_status_dict(status)

        return {
            "lcd_line1": status.line1,
            "lcd_line2": status.line2,
            "check_system": status.check_system,
            "equipment": equipment_dict,
            "slot_labels": {name: info["label"] for name, info in SLOT_DEFINITIONS.items()},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller error: {e}")

@app.post("/api/toggle/{equipment}")
async def api_toggle(equipment: str):
    """Toggle a single piece of equipment on/off."""
    if equipment not in SLOT_DEFINITIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown equipment: {equipment}. Available: {list(SLOT_DEFINITIONS.keys())}",
        )

    try:
        # Get current state to determine desired toggle
        status = get_status()
        slot_info = SLOT_DEFINITIONS[equipment]
        current_state = status.key_states[slot_info["slot"]]
        is_on = current_state in ("WEBS_ON", "WEBS_BLINK")
        desired_on = not is_on

        result = ensure_slot_state(equipment, desired_on)

        local_store.append_history({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "manual_toggle",
            "equipment": equipment,
            "details": result,
        })

        return {"status": "ok", "result": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Toggle error: {e}")

# -------------------------------------------------------------------
# API: Schedules
# -------------------------------------------------------------------

@app.get("/api/schedules")
async def api_get_schedules():
    """Return current schedules from local JSON."""
    return local_store.load_schedules()

@app.put("/api/schedules")
async def api_update_schedules(new_schedules: dict):
    """Update schedules: write local file, sync to Airflow variable, log action.

    Disabled equipment is omitted from the dict entirely (not stored as null).
    Each equipment can have a single window or a list of windows.

    Merge with existing data: equipment NOT present in the request retains its
    previously saved schedule so that partial saves (e.g., incomplete windows)
    do not accidentally delete existing schedules.
    """
    # Debug: log received payload to diagnose time-input tracking issues
    logger.info("[DEBUG] PUT /api/schedules received: %s", json.dumps(new_schedules))
    try:
        old_schedules = local_store.load_schedules()

        # Validate schedule format — accept all schedulable items (including super_chlorinator)
        valid_equipment = set(SCHEDULABLE_ITEMS.keys())
        for eq_name, sched in new_schedules.items():
            if eq_name not in valid_equipment:
                raise ValueError(
                    f"Unknown equipment '{eq_name}'. Valid: {', '.join(sorted(valid_equipment))}"
                )
            if sched is not None:
                # Accept: {"on": "05:00", "off": "23:00"}
                # Or:      [{"on": "05:00", "off": "12:00"}, {"on": "13:00", "off": "18:00"}]
                windows = sched if isinstance(sched, list) else [sched]
                for window in windows:
                    if not isinstance(window, dict):
                        raise ValueError(f"{eq_name} schedule window must be a dict")
                    if "on" not in window or "off" not in window:
                        raise ValueError(f"{eq_name} schedule window needs 'on' and 'off' keys")

        # Filter out disabled equipment (empty list / None entries) so the JSON
        # only contains keys for *enabled* equipment.
        filtered_schedules = {}
        disabled_keys = set()
        for eq, sched in new_schedules.items():
            if sched is None:
                disabled_keys.add(eq)  # explicit disable — remove old schedule
                continue
            if isinstance(sched, list) and len(sched) == 0:
                continue
            filtered_schedules[eq] = sched

        # Merge with existing schedules so that equipment missing from the
        # request (e.g., all windows incomplete) keeps its previous schedule
        # instead of being silently deleted. Explicitly disabled keys are removed.
        merged_schedules = {**old_schedules, **filtered_schedules}
        for key in disabled_keys:
            merged_schedules.pop(key, None)

        # Write to local JSON
        if not local_store.save_schedules(merged_schedules):
            raise RuntimeError("Failed to write local schedules file")

        # Sync to Airflow variable
        sync_status = "synced"
        try:
            airflow_client.set_variable(config.AIRFLOW_VARIABLE_KEY, json.dumps(merged_schedules))
        except Exception as sync_err:
            sync_status = f"sync_failed: {sync_err}"
            logger.warning("Failed to sync schedules to Airflow: %s", sync_err)

        # Log action — compare old vs. merged (not filtered) so the log
        # accurately reflects what was actually saved.
        changed = []
        for eq in valid_equipment:
            old_val = old_schedules.get(eq)
            new_val = merged_schedules.get(eq)
            if old_val != new_val:
                changed.append(f"{eq}: {old_val} -> {new_val}")

        local_store.append_history({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "schedule_updated",
            "equipment": list(filtered_schedules.keys()),
            "sync_status": sync_status,
            "details": "Changes: " + "; ".join(changed) if changed else "No changes",
        })

        return {"status": "ok", "sync": sync_status}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error updating schedules")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

# -------------------------------------------------------------------
# API: History & DAG Runs
# -------------------------------------------------------------------

@app.get("/api/history")
async def api_history(limit: int = 50):
    """Return recent action history from local log."""
    return local_store.get_history(limit=limit)

@app.get("/api/dag_runs")
async def api_dag_runs(limit: int = 20):
    """Return recent DAG runs from Airflow API."""
    try:
        runs = airflow_client.get_dag_runs(config.AIRFLOW_DAG_ID, limit=limit)
        return {"dag_runs": runs}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Airflow API error: {e}")

# -------------------------------------------------------------------
# API: Health
# -------------------------------------------------------------------

@app.get("/api/health")
async def api_health():
    """Health check: controller reachability + Airflow API connectivity."""
    health = {"controller": "unknown", "airflow": "unknown", "timestamp": datetime.now(timezone.utc).isoformat()}

    try:
        _ = get_status()
        health["controller"] = "ok"
    except Exception as e:
        health["controller"] = f"error: {e}"

    try:
        _ = airflow_client.get_variable(config.AIRFLOW_VARIABLE_KEY)
        health["airflow"] = "ok"
    except Exception as e:
        health["airflow"] = f"error: {e}"

    return health


# -------------------------------------------------------------------
# API: Sensor Polling (triggered by systemd timer every 30 min)
# -------------------------------------------------------------------

@app.post("/api/poll-sensors")
async def api_poll_sensors():
    """Read LCD pages for air temperature and salt level, store in metrics DB."""
    try:
        pages = lcd_read_all_pages()
        metrics_db.insert_sensor(
            air_temp_f=pages.get("air_temp_f"),
            salt_ppm=pages.get("salt_ppm"),
        )
        return {"status": "ok", "pages": pages["pages"],
                "air_temp_f": pages["air_temp_f"], "salt_ppm": pages["salt_ppm"]}
    except Exception as e:
        logger.exception("Sensor poll failed")
        raise HTTPException(status_code=502, detail=f"Sensor poll error: {e}")


# -------------------------------------------------------------------
# API: Metrics / Analytics
# -------------------------------------------------------------------

@app.get("/api/metrics/daily-stats")
async def api_daily_stats(days: int = 30):
    """Daily air temperature averages for the last N days."""
    return {
        "air_temp": metrics_db.daily_air_temp(days),
        "salt_level": metrics_db.daily_salt_level(days),
    }


@app.get("/api/metrics/equipment-runtime")
async def api_equipment_runtime(days: int = 7):
    """Total hours-ON per equipment over the last N days."""
    return {
        "summary_7d": metrics_db.last_7d_runtime_summary(),
    }


@app.get("/api/metrics/trend")
async def api_trend(metric: str = "air_temp", days: int = 30):
    """Time-series data for charting."""
    if metric == "air_temp":
        return {"dates": metrics_db.daily_air_temp(days)}
    elif metric == "salt_level":
        return {"dates": metrics_db.daily_salt_level(days)}
    elif metric.startswith("runtime_"):
        eq_name = metric.replace("runtime_", "")
        data = metrics_db.equipment_daily_runtime(eq_name, days)
        return {"equipment": eq_name, "daily_minutes": data}
    else:
        raise HTTPException(status_code=400,
                           detail=f"Unknown metric '{metric}'. Use: air_temp, salt_level, runtime_<equipment>")


@app.get("/api/metrics/latest")
async def api_latest_sensor():
    """Most recent sensor reading."""
    return metrics_db.latest_sensor()
