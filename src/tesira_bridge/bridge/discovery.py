"""Generate Home Assistant MQTT Discovery payloads.

HA Discovery docs: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery

All entities are grouped under a single HA Device for a clean device page.
"""

from __future__ import annotations

import json

from ..config import BridgeConfig, ZoneConfig, RoutingConfig


def _device_payload(cfg: BridgeConfig) -> dict:
    return {
        "identifiers": [cfg.mqtt.device_id],
        "name": cfg.mqtt.device_name,
        "model": "Tesira Server I/O",
        "manufacturer": "Biamp",
    }


def _base_topic(cfg: BridgeConfig, entity_type: str, unique_id: str) -> str:
    return f"{cfg.mqtt.discovery_prefix}/{entity_type}/{unique_id}"


# ── Zone entities ─────────────────────────────────────────────────────────────

def zone_level_discovery(cfg: BridgeConfig, zone: ZoneConfig) -> tuple[str, str]:
    """Return (topic, payload_json) for a zone level number entity."""
    unique_id = f"{cfg.mqtt.device_id}_zone_{zone.id}_level"
    state_topic = f"tesira/zone/{zone.id}/level/state"
    command_topic = f"tesira/zone/{zone.id}/level/set"

    payload = {
        "unique_id": unique_id,
        "name": f"{zone.name} Volume",
        "state_topic": state_topic,
        "command_topic": command_topic,
        "min": zone.min_db,
        "max": zone.max_db,
        "step": 0.5,
        "unit_of_measurement": "dB",
        "device": _device_payload(cfg),
        "availability_topic": "tesira/bridge/status",
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    topic = f"{_base_topic(cfg, 'number', unique_id)}/config"
    return topic, json.dumps(payload)


def zone_mute_discovery(cfg: BridgeConfig, zone: ZoneConfig) -> tuple[str, str]:
    """Return (topic, payload_json) for a zone mute switch entity."""
    unique_id = f"{cfg.mqtt.device_id}_zone_{zone.id}_mute"
    state_topic = f"tesira/zone/{zone.id}/mute/state"
    command_topic = f"tesira/zone/{zone.id}/mute/set"

    payload = {
        "unique_id": unique_id,
        "name": f"{zone.name} Mute",
        "state_topic": state_topic,
        "command_topic": command_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "device": _device_payload(cfg),
        "availability_topic": "tesira/bridge/status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "icon": "mdi:volume-mute",
    }
    topic = f"{_base_topic(cfg, 'switch', unique_id)}/config"
    return topic, json.dumps(payload)


# ── Routing entities ──────────────────────────────────────────────────────────

def routing_select_discovery(
    cfg: BridgeConfig,
    route: RoutingConfig,
) -> tuple[str, str]:
    """Return (topic, payload_json) for a source-selector select entity."""
    unique_id = f"{cfg.mqtt.device_id}_routing_{route.id}"
    state_topic = f"tesira/routing/{route.id}/state"
    command_topic = f"tesira/routing/{route.id}/set"

    # Resolve human-readable source names from config
    source_map = {s.id: s.name for s in cfg.sources}
    options = [source_map.get(e.source_id, e.source_id) for e in route.sources]

    payload = {
        "unique_id": unique_id,
        "name": route.name,
        "state_topic": state_topic,
        "command_topic": command_topic,
        "options": options,
        "device": _device_payload(cfg),
        "availability_topic": "tesira/bridge/status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "icon": "mdi:audio-input-stereo-minijack",
    }
    topic = f"{_base_topic(cfg, 'select', unique_id)}/config"
    return topic, json.dumps(payload)


# ── Convenience: all payloads ─────────────────────────────────────────────────

def all_discovery_payloads(cfg: BridgeConfig) -> list[tuple[str, str]]:
    """Return all (topic, payload_json) tuples to publish on startup."""
    payloads: list[tuple[str, str]] = []
    for zone in cfg.zones:
        payloads.append(zone_level_discovery(cfg, zone))
        payloads.append(zone_mute_discovery(cfg, zone))
    for route in cfg.routing:
        payloads.append(routing_select_discovery(cfg, route))
    return payloads
