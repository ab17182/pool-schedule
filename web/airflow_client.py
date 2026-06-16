"""
airflow_client.py — Airflow REST API client with JWT token management.

Handles authentication, variable read/write, and DAG run queries.
"""

import base64
import json
import logging
import time

import config
import requests

logger = logging.getLogger(__name__)


class AirflowClient:
    """Thin client for Airflow REST API v2 with JWT token lifecycle."""

    def __init__(
        self,
        base_url: str = config.AIRFLOW_API_BASE_URL,
        username: str = config.AIRFLOW_USERNAME,
        password: str = config.AIRFLOW_PASSWORD,
    ):
        self.base_url = base_url
        self.username = username
        self.password = password
        self._session = requests.Session()
        self._token: str | None = None
        self._token_expires_at: float = 0
        self._token_refresh_buffer = 300  # Refresh 5 min before expiry

    def _ensure_authenticated(self) -> None:
        """Get a fresh JWT token if needed. Caches and refreshes as needed."""
        now = time.time()
        if self._token and now < self._token_expires_at - self._token_refresh_buffer:
            return

        logger.info("Authenticating with Airflow API...")
        try:
            resp = self._session.post(
                f"{self.base_url}/auth/token",
                json={"username": self.username, "password": self.password},
                timeout=10,
            )
            resp.raise_for_status()
            token_data = resp.json()
            self._token = token_data["access_token"]

            # Decode JWT payload to get exp claim (stdlib only, no jwt library needed)
            payload = self._token.split(".")[1]
            padded = payload + "=" * (4 - len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(padded))
            self._token_expires_at = decoded.get("exp", now + 86400)
            self._session.headers["Authorization"] = f"Bearer {self._token}"
            logger.info("Authenticated successfully. Token expires at %s", self._token_expires_at)
        except Exception as e:
            logger.error("Failed to authenticate with Airflow: %s", e)
            raise

    def get_variable(self, key: str) -> str:
        """GET /api/v2/variables/{key} — returns decrypted variable value."""
        self._ensure_authenticated()
        resp = self._session.get(
            f"{self.base_url}/api/v2/variables/{key}", timeout=10
        )
        resp.raise_for_status()
        return resp.json()["value"]

    def set_variable(self, key: str, value: str) -> bool:
        """PATCH /api/v2/variables/{key} — set a variable value (encrypted by Airflow)."""
        self._ensure_authenticated()
        resp = self._session.patch(
            f"{self.base_url}/api/v2/variables/{key}",
            json={"key": key, "value": value},
            timeout=10,
        )
        resp.raise_for_status()
        return True

    def get_dag_runs(self, dag_id: str, limit: int = 20) -> list:
        """GET /api/v2/dags/{dag_id}/dagRuns — returns recent DAG runs."""
        self._ensure_authenticated()
        resp = self._session.get(
            f"{self.base_url}/api/v2/dags/{dag_id}/dagRuns",
            params={"limit": limit, "order_by": "-start_date"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("dag_runs", [])
