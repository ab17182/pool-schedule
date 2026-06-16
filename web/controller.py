"""
controller.py — Standalone Hayward AquaConnect controller client.

Extracted from /root/airflow/dags/pool_controller.py with Airflow-specific
imports removed. Reads controller URL from config.py instead of Airflow Variable.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Literal

import requests

import config

logger = logging.getLogger(__name__)

# Controller slot layout (from live controller at http://172.16.0.26)
# Each row: {equipment_name: {slot: controller_key_index, label: display_name, hex_code: KeyId}}
SLOT_DEFINITIONS: dict[str, dict] = {
    "filter":      {"slot": 4,  "label": "FILTER",      "hex_code": "08"},
    "pool":        {"slot": 1,  "label": "POOL",        "hex_code": "07"},
    "spa":         {"slot": 2,  "label": "SPA",         "hex_code": "07"},
    "heater":      {"slot": 6,  "label": "HEATER",      "hex_code": "13"},
    "cleaner":     {"slot": 9,  "label": "CLEANER",     "hex_code": "0A"},
    "waterfall":   {"slot": 12, "label": "WATERFALL",   "hex_code": "0C"},
    "lights":      {"slot": 13, "label": "POOL LIGHT",  "hex_code": "0D"},
    "spa_light":   {"slot": 5,  "label": "SPA LIGHT",   "hex_code": "09"},
    "blower":      {"slot": 10, "label": "BLOWER",      "hex_code": "0B"},
}

# All schedulable equipment — includes SLOT_DEFINITIONS plus items that use
# menu navigation (super_chlorinator) and have no controller slot.
# Slot -1 marks items without a physical toggle key on the controller.
SCHEDULABLE_ITEMS: dict[str, dict] = {**SLOT_DEFINITIONS}
SCHEDULABLE_ITEMS["super_chlorinator"] = {"slot": -1, "label": "SUPER CHLORINATOR", "hex_code": ""}

# Keypad hex codes (shared across all controller pages)
KP_MENU = "02"
KP_RIGHT = "01"
KP_UP = "06"
KP_DOWN = "05"

# LED nibble → state mapping
NIBBLE_STATE: dict[str, str] = {
    "3": "WEBS_NOKEY",
    "4": "WEBS_OFF",
    "5": "WEBS_ON",
    "6": "WEBS_BLINK",
}

State = Literal["WEBS_NOKEY", "WEBS_OFF", "WEBS_ON", "WEBS_BLINK"]


def _controller_url() -> str:
    return config.HAYWARD_CONTROLLER_URL.rstrip("/")


_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"Content-Type": "application/x-www-form-urlencoded"})
    return _SESSION


def send_keypress(hex_code: str, timeout: int = 10) -> None:
    """POST a single keypress to the controller."""
    url = _controller_url() + "/WNewSt.htm"
    body = f"KeyId={hex_code}&"
    logger.info("Sending KeyId=%s to %s", hex_code, url)
    try:
        resp = _get_session().post(url, data=body, timeout=timeout)
        resp.raise_for_status()
        logger.debug("Response status=%d, length=%d", resp.status_code, len(resp.text))
    except requests.RequestException:
        logger.exception("Failed to send KeyId=%s", hex_code)
        raise


def send_key_sequence(hex_codes: list[str], delay_ms: int = 750) -> None:
    """Send multiple keypresses in sequence with a delay between each."""
    for code in hex_codes:
        send_keypress(code)
        time.sleep(delay_ms / 1000.0)


@dataclass
class ControllerStatus:
    line1: str
    line2: str
    key_states: list[State]  # 24 elements, index 0..23
    check_system: bool


def get_status(timeout: int = 10) -> ControllerStatus:
    """Fetch and parse the controller status page."""
    url = _controller_url() + "/WNewSt.htm"
    bodies = [
        b"Update Local Server&",
        b"Update Local Server&",
        b"Update Local Server",
    ]
    last_text: str | None = None
    last_err: Exception | None = None

    for body in bodies:
        try:
            resp = _get_session().post(url, data=body, timeout=timeout)
            resp.raise_for_status()
            text = resp.text
            if "xxx" in text:
                return _parse_response(text)
            last_text = text
        except Exception as exc:
            last_err = exc

    if last_text:
        return _parse_response(last_text)

    raise RuntimeError(f"All status requests failed. Last error: {last_err}")


def _parse_response(html: str) -> ControllerStatus:
    """Parse the controller HTML response into structured data."""
    body_start = html.lower().find("<body")
    if body_start >= 0:
        gt_pos = html.find(">", body_start)
        if gt_pos >= 0:
            body_start = gt_pos + 1
        else:
            body_start += 5
        body_end = html.lower().find("</body>", body_start)
        if body_end < 0:
            body_end = len(html)
        encoded_body = html[body_start:body_end]
    else:
        encoded_body = html

    import html as html_module
    body = html_module.unescape(encoded_body)
    clean = body.replace("\r", "")
    parts = clean.split("xxx")

    def _last_nonempty(s: str) -> str:
        lines = [l.strip() for l in s.split("\n")]
        lines = [l for l in lines if l]
        return lines[-1] if lines else ""

    def _first_nonempty(s: str) -> str:
        lines = [l.strip() for l in s.split("\n")]
        lines = [l for l in lines if l]
        return lines[0] if lines else ""

    line1 = _last_nonempty(parts[0]) if parts else ""
    line2 = _first_nonempty(parts[1]) if len(parts) >= 2 else ""
    led_bytes_raw = _first_nonempty(parts[2]) if len(parts) >= 3 else ""
    key_states, check_system = _parse_led_bytes(led_bytes_raw)

    return ControllerStatus(
        line1=line1,
        line2=line2,
        key_states=key_states,
        check_system=check_system,
    )


def _parse_led_bytes(led_text: str) -> tuple[list[State], bool]:
    """Parse LED hex string into 24 key states."""
    states: list[State] = []
    check_system = False

    if not led_text:
        return (["WEBS_NOKEY"] * 24, False)

    for idx, ch in enumerate(led_text):
        ascii_val = ord(ch)
        hi_nibble = (ascii_val >> 4) & 0xF
        lo_nibble = ascii_val & 0xF

        hi_state = NIBBLE_STATE.get(str(hi_nibble), "WEBS_NOKEY")
        states.append(hi_state)

        if idx == len(led_text) - 1:
            if lo_nibble in (5, 6):
                check_system = True
            states.append("WEBS_NOKEY")
        else:
            lo_state = NIBBLE_STATE.get(str(lo_nibble), "WEBS_NOKEY")
            states.append(lo_state)

    while len(states) < 24:
        states.append("WEBS_NOKEY")

    return (states[:24], check_system)


def ensure_slot_state(equipment: str, desired_on: bool) -> str:
    """Ensure a slot is in the desired ON/OFF state (idempotent)."""
    if equipment not in SLOT_DEFINITIONS:
        raise ValueError(f"Unknown equipment: {equipment}. Available: {list(SLOT_DEFINITIONS.keys())}")

    slot_info = SLOT_DEFINITIONS[equipment]
    slot_index = slot_info["slot"]
    hex_code = slot_info["hex_code"]

    status = get_status()
    current_state = status.key_states[slot_index]
    is_currently_on = current_state in ("WEBS_ON", "WEBS_BLINK")

    if is_currently_on == desired_on:
        return f"{equipment} (slot {slot_index}) already {current_state}. No action needed."

    send_keypress(hex_code)
    return (
        f"{equipment} (slot {slot_index}) was {current_state}, "
        f"desired {'ON' if desired_on else 'OFF'}. Sent toggle KeyId={hex_code}."
    )


def toggle_super_chlorinator(enable: bool) -> str:
    """Toggle the super chlorinator via menu navigation."""
    if enable:
        sequence = [KP_MENU, KP_RIGHT, KP_RIGHT, KP_RIGHT, KP_UP]
        action = "enable"
    else:
        sequence = [KP_MENU, KP_RIGHT, KP_RIGHT, KP_RIGHT, KP_DOWN]
        action = "disable"

    logger.info("Toggling super chlorinator (%s) with sequence: %s", action, sequence)
    send_key_sequence(sequence, delay_ms=750)
    return f"Super chlorinator {action} sequence sent: {sequence}"


def format_status_report(status: ControllerStatus) -> str:
    """Format a human-readable status report."""
    lines = [
        f"LCD Line 1: {status.line1}",
        f"LCD Line 2: {status.line2}",
        f"Check System: {'ON ⚠' if status.check_system else 'OFF'}",
        "Key States:",
    ]
    for eq_name, eq_info in SLOT_DEFINITIONS.items():
        idx = eq_info["slot"]
        state = status.key_states[idx] if idx < len(status.key_states) else "UNKNOWN"
        lines.append(f"  {eq_name} ({eq_info['label']}, slot {idx}): {state}")
    return "\n".join(lines)


def get_equipment_status_dict(status: ControllerStatus) -> dict:
    """Return {equipment_name: 'ON'/'OFF'/'UNKNOWN'} for all defined equipment."""
    result = {}
    for eq_name, eq_info in SLOT_DEFINITIONS.items():
        slot_idx = eq_info["slot"]
        state = status.key_states[slot_idx] if slot_idx < len(status.key_states) else "WEBS_NOKEY"
        if state in ("WEBS_ON", "WEBS_BLINK"):
            result[eq_name] = "ON"
        elif state == "WEBS_OFF":
            result[eq_name] = "OFF"
        else:
            result[eq_name] = "UNKNOWN"
    return result


# ---------------------------------------------------------------------------
# LCD page reader — navigates Hayward display to read sensor pages
# ---------------------------------------------------------------------------

def _read_current_page(timeout: int = 10) -> str | None:
    """HTTP POST to the controller and return the raw response text (no keypress)."""
    url = _controller_url() + "/WNewSt.htm"
    bodies = [b"Update Local Server&", b"Update Local Server"]
    for body in bodies:
        try:
            resp = _get_session().post(url, data=body, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            logger.debug("_read_current_page attempt failed: %s", exc)
    return None


def _lcd_lines(html: str) -> tuple[str, str]:
    """Return (line1, line2) from controller HTML."""
    if not html or "xxx" not in html:
        return ("", "")
    cs = _parse_response(html)
    return cs.line1, cs.line2


def _parse_temp(text: str) -> float | None:
    """Extract a Fahrenheit temperature (e.g. '84' or '84F' or '84.5') from text."""
    m = re.search(r"([+-]?\d+\.?\d*)\s*°?F?", text)
    return float(m.group(1)) if m else None


def _parse_ppm(text: str) -> float | None:
    """Extract a PPM value (e.g. '2800' or '2800ppm') from text."""
    m = re.search(r"(\d+)\s*(?:ppm|PPM)?", text)
    if m:
        val = float(m.group(1))
        # Salt levels are typically 2000-4000 ppm; ignore small numbers that happen
        # to match (like hour values, slot numbers, etc.)
        if val >= 100:
            return val
    return None


def lcd_read_all_pages() -> dict:
    """Take several snapshots of the Hayward LCD display without pressing MENU.

    The controller's LCD auto-cycles through information pages (day/time,
    temperatures, salt level, etc.) roughly every 5-10 seconds each.  By taking
    multiple reads with short delays we increase the chance of catching sensor
    pages that aren't showing right now.

    Returns a dict with best-effort parsed values:
        air_temp_f   – float or None  (air/supply temperature)
        pool_temp_f  – float or None  (pool water temperature)
        spa_temp_f   – float or None  (spa water temperature)
        salt_ppm     – float or None  (salt/chlorine level in ppm)
        pages        – list of (snapshot_num, line1, line2) raw for debugging
    """
    result: dict = {"air_temp_f": None, "pool_temp_f": None,
                    "spa_temp_f": None, "salt_ppm": None, "pages": []}

    # Take SNAPSHOTS reads with a delay between them so the auto-cycling LCD
    # gives us multiple pages.  The Hayward AquaConnect cycles each page for
    # ~5-10 s, so 5 snapshots × 4 s gap = ~20 s of display coverage.
    SNAPSHOTS = 5
    GAP_S = 4

    for idx in range(SNAPSHOTS):
        if idx > 0:
            time.sleep(GAP_S)

        raw = _read_current_page()
        l1, l2 = _lcd_lines(raw) if raw else ("", "")
        result["pages"].append((idx, l1, l2))
        combined = f"{l1} {l2}".upper()

        # --- Temperature detection ---
        if any(kw in combined for kw in ("AIR TEMP", "SUPPLY", "AIR")):
            t = _parse_temp(l2) or _parse_temp(l1)
            if t is not None and 30 < t < 130:
                result["air_temp_f"] = t

        if any(kw in combined for kw in ("POOL TEMP", "WATER TEMP", "POOL")):
            t = _parse_temp(l2) or _parse_temp(l1)
            if t is not None and 40 < t < 110:
                result["pool_temp_f"] = t

        if any(kw in combined for kw in ("SPA TEMP",)):
            t = _parse_temp(l2) or _parse_temp(l1)
            if t is not None and 40 < t < 120:
                result["spa_temp_f"] = t

        # --- Salt / chlorine PPM detection ---
        if any(kw in combined for kw in ("SALT", "PPM", "CHLORINE", "SWG")):
            p = _parse_ppm(l2) or _parse_ppm(l1)
            if p is not None:
                result["salt_ppm"] = p

        # Fallback: try to parse any temperature-looking value on any page
        if result["air_temp_f"] is None or result["pool_temp_f"] is None:
            for line in (l1, l2):
                t = _parse_temp(line)
                if t is not None and 30 < t < 130:
                    # Heuristic: first temp found → pool water; second (different) → air
                    if result["pool_temp_f"] is None:
                        result["pool_temp_f"] = t
                    elif result["air_temp_f"] is None and abs(t - result["pool_temp_f"]) > 2:
                        result["air_temp_f"] = t

    logger.info("LCD scan complete — air_temp=%.1f, pool_temp=%.1f, spa_temp=%.1f, salt=%s ppm",
                result["air_temp_f"] or 0,
                result["pool_temp_f"] or 0,
                result["spa_temp_f"] or 0,
                result["salt_ppm"],)
    return result
