"""
config.py — Central configuration for the Pool Equipment Scheduler.

Copy this file to config.py and edit values for your environment:
    cp config.example.py config.py

Do NOT commit config.py with real credentials to version control.
"""

# ---------------------------------------------------------------------------
# Airflow API  — connection to the Airflow webserver that runs the DAG
# ---------------------------------------------------------------------------
AIRFLOW_API_BASE_URL = "http://127.0.0.1:8080"
AIRFLOW_USERNAME = "admin"
AIRFLOW_PASSWORD = "<your-airflow-password>"
AIRFLOW_DAG_ID = "pool_equipment_scheduler"
AIRFLOW_VARIABLE_KEY = "pool_schedules"

# ---------------------------------------------------------------------------
# Local data files  — paths for schedules and action history JSON
# ---------------------------------------------------------------------------
LOCAL_SCHEDULES_FILE = "/path/to/poolschedule/schedules.json"
LOCAL_HISTORY_FILE =  "/path/to/poolschedule/action_history.json"

# ---------------------------------------------------------------------------
# App server  — FastAPI / uvicorn bind address and port
# ---------------------------------------------------------------------------
APP_PORT = 8700
APP_HOST = "0.0.0.0"

# ---------------------------------------------------------------------------
# Hayward controller  — AquaConnect WebsR2 IP or hostname
# ---------------------------------------------------------------------------
HAYWARD_CONTROLLER_URL = "http://192.168.x.x"

# ---------------------------------------------------------------------------
# Timezone  — used by the Airflow DAG for schedule windows
# ---------------------------------------------------------------------------
TIMEZONE = "America/New_York"

# ---------------------------------------------------------------------------
# Dashboard auto-refresh interval in seconds (browser polls /api/status)
# ---------------------------------------------------------------------------
DASHBOARD_REFRESH_INTERVAL = 15
