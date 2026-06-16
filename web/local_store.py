"""
local_store.py — Local JSON file persistence for schedules and action history.

Provides atomic write operations and bounded history to prevent unbounded growth.
"""

import json
import logging
import os

import config

logger = logging.getLogger(__name__)

MAX_HISTORY_ENTRIES = 200


class LocalStore:
    """Manage local JSON files for schedule storage and action history."""

    def __init__(
        self,
        schedules_path: str = config.LOCAL_SCHEDULES_FILE,
        history_path: str = config.LOCAL_HISTORY_FILE,
    ):
        self.schedules_path = schedules_path
        self.history_path = history_path

    def load_schedules(self) -> dict:
        """Load schedules from local JSON. Returns {} if file missing or corrupt."""
        try:
            with open(self.schedules_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning("Schedules file not found: %s", self.schedules_path)
            return {}
        except json.JSONDecodeError as e:
            logger.error("Failed to parse schedules file: %s", e)
            return {}

    def save_schedules(self, schedules: dict) -> bool:
        """Write schedules to local JSON file atomically (write to tmp, then rename)."""
        try:
            tmp_path = self.schedules_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(schedules, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, self.schedules_path)
            logger.info("Schedules saved to %s", self.schedules_path)
            return True
        except Exception as e:
            logger.error("Failed to save schedules: %s", e)
            return False

    def append_history(self, entry: dict) -> None:
        """Append a timestamped action entry to history. Keeps last MAX_HISTORY_ENTRIES."""
        try:
            entries = []
            try:
                with open(self.history_path, "r") as f:
                    entries = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                pass

            entries.append(entry)
            entries = entries[-MAX_HISTORY_ENTRIES:]

            tmp_path = self.history_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(entries, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, self.history_path)
        except Exception as e:
            logger.error("Failed to append history entry: %s", e)

    def get_history(self, limit: int = 50) -> list:
        """Return the last N history entries."""
        try:
            with open(self.history_path, "r") as f:
                entries = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return entries[-limit:]
