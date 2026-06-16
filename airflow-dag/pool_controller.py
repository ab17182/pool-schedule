"""
pool_controller.py — Hayward Aqua Connect controller client for Airflow.

Translated from the PowerShell AquaConnect Local v10.3 GUI app.
Communicates with the Hayward WebsR2 controller via HTTP POST to /WNewSt.htm.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

import requests
from airflow.sdk import Variable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Controller slot layout (discovered from live controller at http://172.16.0.26)
# ---------------------------------------------------------------------------
# Each slot: (index, label, toggle_hex_code)
# toggle_hex_code is the KeyId value sent to WebsProcessKey / POST body.
SLOT_DEFINITIONS: dict[str, dict] = {
    "filter":      {"slot": 4,  "label": "FILTER",      "hex_code": "08"},
    "pool":        {"slot": 1,  "label": "POOL",        "hex_code": "07"},
    "spa":         {"slot": 2,  "label": "SPA",         "hex_code": "07"},
    "heater":      {"slot": 6,  "label": "HEATER",     "hex_code": "13"},
    "cleaner":     {"slot": 9,  "label": "CLEANER",     "hex_code": "0A"},
    "waterfall":   {"slot": 12, "label": "WATERFALL",   "hex_code": "0C"},
    "lights":      {"slot": 13, "label": "POOL LIGHT",  "hex_code": "0D"},
    "spa_light":   {"slot": 5,  "label": "SPA LIGHT",   "hex_code": "09"},
    "blower":      {"slot": 10, "label": "BLOWER",      "hex_code": "0B"},
}

# Keypad hex codes (shared across all controller pages)
KP_MENU = "02"
KP_RIGHT = "01"
KP_UP = "06"
KP_DOWN = "05"

# LED nibble → state mapping (from PowerShell Convert-NibbleToClass)
NIBBLE_STATE: dict[str, str] = {
    "3": "WEBS_NOKEY",
    "4": "WEBS_OFF",
    "5": "WEBS_ON",
    "6": "WEBS_BLINK",
}

State = Literal["WEBS_NOKEY", "WEBS_OFF", "WEBS_ON", "WEBS_BLINK"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _controller_url() -> str:
    url = Variable.get("pool_controller_url", default="http://172.16.0.26")
    return url.rstrip("/")


_SESSION: requests.Session | None = None


def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"Content-Type": "application/x-www-form-urlencoded"})
    return _SESSION


# ---------------------------------------------------------------------------
# Core communication
# ---------------------------------------------------------------------------
def send_keypress(hex_code: str, timeout: int = 10) -> None:
    """POST a single keypress to the controller.

    Equivalent to PowerShell Send-KeyPress.
    """
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
    """Send multiple keypresses in sequence with a delay between each.

    Equivalent to PowerShell Toggle-Keys.
    Used for multi-step menu navigation (e.g., super chlorinator).
    """
    for code in hex_codes:
        send_keypress(code)
        time.sleep(delay_ms / 1000.0)


# ---------------------------------------------------------------------------
# Status parsing
# ---------------------------------------------------------------------------
@dataclass
class ControllerStatus:
    line1: str
    line2: str
    key_states: list[State]  # 24 elements, index 0..23
    check_system: bool


def get_status(timeout: int = 10) -> ControllerStatus:
    """Fetch and parse the controller status page.

    Equivalent to PowerShell Get-PanelStatus + Parse-ControllerResponse.

    Tries three POST body variants (the controller is picky about trailing &).
    """
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
            # The controller response contains 'xxx' delimiters
            if "xxx" in text:
                return _parse_response(text)
            last_text = text
        except Exception as exc:
            last_err = exc

    # Fallback: try parsing the last response even without 'xxx'
    if last_text:
        return _parse_response(last_text)

    raise RuntimeError(
        f"All status requests failed. Last error: {last_err}"
    )


def _parse_response(html: str) -> ControllerStatus:
    """Parse the controller HTML response into structured data.

    Body format: <text>xxx<text>xxx<led_bytes>xxx
    """
    # Extract <body> content
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

    # HTML-decode the body content
    import html as html_module

    body = html_module.unescape(encoded_body)

    # Remove carriage returns and split on 'xxx' delimiter
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

    # Parse LED bytes into 24 key states
    key_states, check_system = _parse_led_bytes(led_bytes_raw)

    return ControllerStatus(
        line1=line1,
        line2=line2,
        key_states=key_states,
        check_system=check_system,
    )


def _parse_led_bytes(led_text: str) -> tuple[list[State], bool]:
    """Parse LED hex string into 24 key states.

    Each ASCII character encodes two 4-bit nibbles.
    Nibble values: 3=NOKEY, 4=OFF, 5=ON, 6=BLINK.
    The last character's low nibble indicates system check (5/6=warning).

    Equivalent to PowerShell Get-NibblesFromAsciiChar + Convert-NibbleToClass.
    """
    states: list[State] = []
    check_system = False

    if not led_text:
        # If no LED data, fill with NOKEY
        return (["WEBS_NOKEY"] * 24, False)

    for idx, ch in enumerate(led_text):
        ascii_val = ord(ch)
        hi_nibble = (ascii_val >> 4) & 0xF
        lo_nibble = ascii_val & 0xF

        hi_state = NIBBLE_STATE.get(str(hi_nibble), "WEBS_NOKEY")
        states.append(hi_state)

        if idx == len(led_text) - 1:
            # Last character: low nibble is system check indicator
            if lo_nibble in (5, 6):
                check_system = True
            states.append("WEBS_NOKEY")
        else:
            lo_state = NIBBLE_STATE.get(str(lo_nibble), "WEBS_NOKEY")
            states.append(lo_state)

    # Pad to 24 if shorter
    while len(states) < 24:
        states.append("WEBS_NOKEY")

    # Truncate to 24 if longer
    return (states[:24], check_system)


# ---------------------------------------------------------------------------
# High-level equipment control
# ---------------------------------------------------------------------------
def ensure_slot_state(equipment: str, desired_on: bool) -> str:
    """Ensure a slot is in the desired ON/OFF state (idempotent).

    Reads current status. If the slot's state differs from desired,
    sends a toggle keypress. Returns a description of what happened.
    """
    if equipment not in SLOT_DEFINITIONS:
        raise ValueError(f"Unknown equipment: {equipment}. Available: {list(SLOT_DEFINITIONS.keys())}")

    slot_info = SLOT_DEFINITIONS[equipment]
    slot_index = slot_info["slot"]
    hex_code = slot_info["hex_code"]

    status = get_status()
    current_state = status.key_states[slot_index]

    # Determine if current state matches desired
    is_currently_on = current_state in ("WEBS_ON", "WEBS_BLINK")

    if is_currently_on == desired_on:
        return (
            f"{equipment} (slot {slot_index}) already {current_state}. "
            f"No action needed."
        )

    # Send toggle
    send_keypress(hex_code)
    return (
        f"{equipment} (slot {slot_index}) was {current_state}, desired {'ON' if desired_on else 'OFF'}. "
        f"Sent toggle KeyId={hex_code}."
    )


def toggle_super_chlorinator(enable: bool) -> str:
    """Toggle the super chlorinator via menu navigation.

    The super chlorinator is not a simple slot toggle. It requires:
    1. Press MENU to enter settings menu
    2. Press RIGHT 3 times to navigate to Super Chlorinate
    3. Press UP (enable) or DOWN (disable)

    Note: This is a best-effort translation. The exact sequence may need
    adjustment based on what page the controller is currently on.
    """
    if enable:
        sequence = [KP_MENU, KP_RIGHT, KP_RIGHT, KP_RIGHT, KP_UP]
        action = "enable"
    else:
        sequence = [KP_MENU, KP_RIGHT, KP_RIGHT, KP_RIGHT, KP_DOWN]
        action = "disable"

    logger.info(
        "Toggling super chlorinator (%s) with sequence: %s",
        action,
        sequence,
    )
    send_key_sequence(sequence, delay_ms=750)
    return f"Super chlorinator {action} sequence sent: {sequence}"


def format_status_report(status: ControllerStatus) -> str:
    """Format a human-readable status report for logging."""
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
