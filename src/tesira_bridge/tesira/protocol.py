"""TTP command string builders and response parsers.

All functions are pure — no I/O, easy to unit-test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union


# ── Response types ────────────────────────────────────────────────────────────

@dataclass
class OkResponse:
    """A bare +OK with no value."""


@dataclass
class ValueResponse:
    """A +OK response carrying a single value."""
    value: str  # raw string; callers parse float/bool as needed


@dataclass
class ErrorResponse:
    reason: str


TTPResponse = Union[OkResponse, ValueResponse, ErrorResponse]


# ── Response parser ───────────────────────────────────────────────────────────

_VALUE_RE = re.compile(r'\+OK\s+"value"\s*:\s*(.+)')


def parse_response(line: str) -> TTPResponse:
    """Parse a raw TTP response line into a typed response object."""
    line = line.strip()
    if line.startswith("-ERR"):
        return ErrorResponse(reason=line[4:].strip())
    m = _VALUE_RE.match(line)
    if m:
        return ValueResponse(value=m.group(1).strip())
    if line.startswith("+OK"):
        return OkResponse()
    # Fallback: treat as a value response for unexpected formats
    return ValueResponse(value=line)


# ── Notification parser ───────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r'"publishToken"\s*:\s*"([^"]+)"')
_NOTIF_VALUE_RE = re.compile(r'"value"\s*:\s*(.+)$')


@dataclass
class Notification:
    publish_token: str
    value: str  # raw; may be scalar or bracketed array


def parse_notification(line: str) -> Notification | None:
    """Parse a subscription notification line ('! ...').

    Returns None if the line is not a valid notification.
    """
    if not line.startswith("!"):
        return None
    token_m = _TOKEN_RE.search(line)
    value_m = _NOTIF_VALUE_RE.search(line)
    if not token_m or not value_m:
        return None
    return Notification(
        publish_token=token_m.group(1),
        value=value_m.group(1).strip(),
    )


# ── Command builders ──────────────────────────────────────────────────────────

def cmd_get_level(instance: str, channel: int) -> str:
    return f"{instance} get level {channel}"


def cmd_set_level(instance: str, channel: int, db: float) -> str:
    return f"{instance} set level {channel} {db:.4f}"


def cmd_get_mute(instance: str, channel: int) -> str:
    return f"{instance} get mute {channel}"


def cmd_set_mute(instance: str, channel: int, muted: bool) -> str:
    return f"{instance} set mute {channel} {'true' if muted else 'false'}"


def cmd_subscribe_level(instance: str, channel: int, token: str, min_rate_ms: int) -> str:
    return f"{instance} subscribe level {channel} {token} {min_rate_ms}"


def cmd_subscribe_mute(instance: str, channel: int, token: str, min_rate_ms: int) -> str:
    return f"{instance} subscribe mute {channel} {token} {min_rate_ms}"


def cmd_set_crosspoint_level(instance: str, input_ch: int, output_ch: int, db: float) -> str:
    """Set a matrix crosspoint level. Use 0.0 dB to enable, -100.0 dB to disable."""
    return f"{instance} set crosspointLevel {input_ch} {output_ch} {db:.4f}"


def cmd_get_crosspoint_level(instance: str, input_ch: int, output_ch: int) -> str:
    return f"{instance} get crosspointLevel {input_ch} {output_ch}"


# ── Value coercions ───────────────────────────────────────────────────────────

def parse_float(value: str) -> float:
    return float(value)


def parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1")
