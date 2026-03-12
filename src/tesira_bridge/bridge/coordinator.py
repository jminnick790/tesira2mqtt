"""Coordinator — glues the TTP client and MQTT bridge together.

Responsibilities:
- Subscribe to Tesira attributes on startup; re-subscribe after reconnect
- Translate TTP callbacks → MQTT state publishes
- Translate MQTT commands → TTP commands
- Track current state (ZoneState, RoutingState) for routing logic
"""

from __future__ import annotations

import logging

from ..config import BridgeConfig, RoutingConfig, ZoneConfig
from ..tesira.client import TesiraClient
from ..tesira.models import RoutingState, ZoneState
from ..tesira.protocol import (
    cmd_get_crosspoint_mute,
    cmd_get_level,
    cmd_get_mute,
    cmd_set_crosspoint_mute,
    cmd_set_level,
    cmd_set_mute,
    cmd_subscribe_level,
    cmd_subscribe_mute,
    parse_bool,
    parse_float,
)
from .mqtt import MqttBridge

logger = logging.getLogger(__name__)


class Coordinator:
    def __init__(self, cfg: BridgeConfig, tesira: TesiraClient, mqtt: MqttBridge) -> None:
        self._cfg = cfg
        self._tesira = tesira
        self._mqtt = mqtt

        self._zone_states: dict[str, ZoneState] = {
            z.id: ZoneState(zone_id=z.id) for z in cfg.zones
        }
        self._routing_states: dict[str, RoutingState] = {
            r.id: RoutingState(routing_id=r.id) for r in cfg.routing
        }

        # Source name ↔ id maps for routing select entities
        self._source_name_to_id = {s.name: s.id for s in cfg.sources}
        self._source_id_to_name = {s.id: s.name for s in cfg.sources}

    # ── Setup ─────────────────────────────────────────────────────────────────

    async def setup(self) -> None:
        """Subscribe to all Tesira attributes and register MQTT command handlers."""
        for zone in self._cfg.zones:
            await self._subscribe_zone(zone)
        await self._register_mqtt_handlers()

    async def _subscribe_zone(self, zone: ZoneConfig) -> None:
        rate = self._cfg.tesira.subscription_min_rate_ms
        level_token = f"{zone.id}_level"
        mute_token = f"{zone.id}_mute"

        self._tesira.register_callback(level_token, self._on_level_notification)
        self._tesira.register_callback(mute_token, self._on_mute_notification)

        await self._tesira.send(
            cmd_subscribe_level(zone.level_instance, zone.level_channel, level_token, rate)
        )
        await self._tesira.send(
            cmd_subscribe_mute(zone.mute_instance, zone.mute_channel, mute_token, rate)
        )
        logger.info("Subscribed to zone %s", zone.id)

    async def _register_mqtt_handlers(self) -> None:
        for zone in self._cfg.zones:
            level_topic = f"tesira/zone/{zone.id}/level/set"
            mute_topic = f"tesira/zone/{zone.id}/mute/set"
            self._mqtt.register_command_handler(level_topic, self._handle_level_command)
            self._mqtt.register_command_handler(mute_topic, self._handle_mute_command)

        for route in self._cfg.routing:
            source_topic = f"tesira/routing/{route.id}/set"
            self._mqtt.register_command_handler(source_topic, self._handle_routing_command)

    # ── TTP → MQTT callbacks ──────────────────────────────────────────────────

    async def _on_level_notification(self, token: str, value: str) -> None:
        zone_id = token.removesuffix("_level")
        try:
            db = parse_float(value)
        except ValueError:
            logger.warning("Could not parse level value '%s' for zone %s", value, zone_id)
            return
        self._zone_states[zone_id].level_db = db
        await self._mqtt.publish(f"tesira/zone/{zone_id}/level/state", f"{db:.1f}")

    async def _on_mute_notification(self, token: str, value: str) -> None:
        zone_id = token.removesuffix("_mute")
        muted = parse_bool(value)
        self._zone_states[zone_id].muted = muted
        await self._mqtt.publish(f"tesira/zone/{zone_id}/mute/state", "ON" if muted else "OFF")

    # ── MQTT → TTP handlers ───────────────────────────────────────────────────

    async def _handle_level_command(self, topic: str, payload: str) -> None:
        # topic: tesira/zone/<zone_id>/level/set
        zone_id = topic.split("/")[2]
        zone = next((z for z in self._cfg.zones if z.id == zone_id), None)
        if not zone:
            logger.warning("Level command for unknown zone '%s'", zone_id)
            return
        try:
            db = float(payload)
        except ValueError:
            logger.warning("Invalid level value '%s' for zone %s", payload, zone_id)
            return
        await self._tesira.send(cmd_set_level(zone.level_instance, zone.level_channel, db))

    async def _handle_mute_command(self, topic: str, payload: str) -> None:
        zone_id = topic.split("/")[2]
        zone = next((z for z in self._cfg.zones if z.id == zone_id), None)
        if not zone:
            logger.warning("Mute command for unknown zone '%s'", zone_id)
            return
        muted = payload.upper() == "ON"
        await self._tesira.send(cmd_set_mute(zone.mute_instance, zone.mute_channel, muted))

    async def _handle_routing_command(self, topic: str, payload: str) -> None:
        # topic: tesira/routing/<routing_id>/set
        # payload: source name (human-readable, as shown in HA select)
        routing_id = topic.split("/")[2]
        route = next((r for r in self._cfg.routing if r.id == routing_id), None)
        if not route:
            logger.warning("Routing command for unknown route '%s'", routing_id)
            return

        source_id = self._source_name_to_id.get(payload)
        if not source_id:
            logger.warning("Unknown source name '%s' for route %s", payload, routing_id)
            return

        await self._switch_source(route, source_id)

    async def _switch_source(self, route: RoutingConfig, target_source_id: str) -> None:
        """Mute all crosspoints for this route's outputs, then unmute the target source."""
        target_entry = next(
            (e for e in route.sources if e.source_id == target_source_id), None
        )
        if not target_entry:
            logger.warning("Source '%s' not in route '%s'", target_source_id, route.id)
            return

        # Mute all input → output crosspoints for this route
        for entry in route.sources:
            for in_ch, out_ch in zip(entry.input_channels, route.output_channels):
                await self._tesira.send(
                    cmd_set_crosspoint_mute(route.matrix_instance, in_ch, out_ch, muted=True)
                )

        # Unmute only the target source's crosspoints
        for in_ch, out_ch in zip(target_entry.input_channels, route.output_channels):
            await self._tesira.send(
                cmd_set_crosspoint_mute(route.matrix_instance, in_ch, out_ch, muted=False)
            )

        self._routing_states[route.id].active_source_id = target_source_id
        source_name = self._source_id_to_name.get(target_source_id, target_source_id)
        await self._mqtt.publish(f"tesira/routing/{route.id}/state", source_name)
        logger.info("Route '%s' switched to source '%s'", route.id, target_source_id)
