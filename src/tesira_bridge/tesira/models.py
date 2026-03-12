"""Runtime state models for Tesira entities."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ZoneState:
    zone_id: str
    level_db: float | None = None   # None = unknown (not yet received from Tesira)
    muted: bool | None = None


@dataclass
class RoutingState:
    routing_id: str
    active_source_id: str | None = None  # None = unknown / no source selected
