"""Coordinator — glues the TTP client and MQTT bridge together.

Responsibilities:
- Subscribe to Tesira attributes on connect (and re-subscribe after reconnect)
- Translate TTP callbacks → MQTT state publishes
- Translate MQTT commands → TTP commands
- Sync routing state from hardware on startup by querying crosspoints
- Track current ZoneState and RoutingState
"""

from __future__ import annotations

import logging

from ..config import BridgeConfig, RoutingConfig, ZoneConfig
from ..tesira.client import TesiraClient
from ..tesira.models import RoutingState, ZoneState
from ..tesira.protocol import (
    ErrorResponse,
    ValueResponse,
    cmd_get_crosspoint_level,
    cmd_get_level,
    cmd_set_crosspoint_level,
    cmd_set_level,
    cmd_set_mute,
    cmd_subscribe_level,
    cmd_subscribe_mute,
    parse_bool,
    parse_float,
    parse_response,
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

        # Register as a connect hook so we re-subscribe after every reconnect
        tesira.add_connect_hook(self._on_connect)

    # ── Connect hook (runs after every successful TTP auth) ───────────────────

    async def _on_connect(self) -> None:
        """Called by TesiraClient after each successful authentication.

        Re-subscribes to all attributes and syncs routing state from hardware.
        TTP subscriptions are lost on Tesira reboot/reconnect so this must
        run every time, not just on first connect.
        """
        logger.info("Running coordinator setup after (re)connect")
        for zone in self._cfg.zones:
            await self._subscribe_zone(zone)
        await self._register_mqtt_handlers()
        await self._sync_routing_state()

    # ── Subscription setup ────────────────────────────────────────────────────

    async def _subscribe_zone(self, zone: ZoneConfig) -> None:
        """Subscribe to level and mute for a zone.

        The Tesira immediately pushes the current value as the first notification,
        so HA entities will reflect real hardware state within one round-trip.

        A `get level` probe is sent first so that instance-tag resolution errors
        produce a clear diagnostic rather than a confusing subscribe failure.
        Channel 0 (ganged) is used for set/get; channel 1 is used for subscribe
        because some Tesira firmware versions won't resolve channel 0 for subscribe.
        """
        rate = self._cfg.tesira.subscription_min_rate_ms
        sub_ch = zone.effective_subscribe_channel
        level_token = f"{zone.id}_level"
        mute_token = f"{zone.id}_mute"

        self._tesira.register_callback(level_token, self._on_level_notification)
        self._tesira.register_callback(mute_token, self._on_mute_notification)

        # ── Probe: verify instance tag resolves before subscribing ────────────
        try:
            probe_raw = await self._tesira.send(
                cmd_get_level(zone.level_instance, sub_ch)
            )
            probe_resp = parse_response(probe_raw)
            if isinstance(probe_resp, ErrorResponse):
                logger.error(
                    "Zone %s: instance tag '%s' not found on Tesira "
                    "(get level %d → %s). "
                    "Check the tag name in Tesira Designer — it is case-sensitive.",
                    zone.id, zone.level_instance, sub_ch, probe_resp.reason,
                )
                return   # nothing more we can do for this zone
            logger.debug(
                "Zone %s: probe OK, instance '%s' ch%d = %s",
                zone.id, zone.level_instance, sub_ch, probe_raw.strip(),
            )
        except RuntimeError as exc:
            logger.error(
                "Zone %s: probe failed for instance '%s': %s",
                zone.id, zone.level_instance, exc,
            )
            return

        # ── Level subscription ────────────────────────────────────────────────
        try:
            await self._tesira.send(
                cmd_subscribe_level(zone.level_instance, sub_ch, level_token, rate)
            )
        except RuntimeError as exc:
            logger.warning(
                "Level subscription failed for zone %s (%s ch%d); "
                "level feedback disabled: %s",
                zone.id, zone.level_instance, sub_ch, exc,
            )

        # ── Mute subscription ─────────────────────────────────────────────────
        try:
            await self._tesira.send(
                cmd_subscribe_mute(
                    zone.effective_mute_instance, sub_ch, mute_token, rate
                )
            )
        except RuntimeError as exc:
            # Some block types don't expose a subscribable 'mute' attribute.
            # Log a warning but don't abort — level and routing still work.
            logger.warning(
                "Mute subscription failed for zone %s (%s ch%d); "
                "mute feedback disabled: %s",
                zone.id, zone.effective_mute_instance, sub_ch, exc,
            )
        logger.debug("Subscribed to zone %s", zone.id)

    async def _register_mqtt_handlers(self) -> None:
        """Register MQTT command topic handlers (idempotent — safe to call on reconnect)."""
        for zone in self._cfg.zones:
            self._mqtt.register_command_handler(
                f"tesira/zone/{zone.id}/level/set", self._handle_level_command
            )
            self._mqtt.register_command_handler(
                f"tesira/zone/{zone.id}/mute/set", self._handle_mute_command
            )
        for route in self._cfg.routing:
            self._mqtt.register_command_handler(
                f"tesira/routing/{route.id}/set", self._handle_routing_command
            )

    # ── Startup state sync ────────────────────────────────────────────────────

    async def _sync_routing_state(self) -> None:
        """Query crosspoint mute state from Tesira to determine the active source
        for each routing block and publish to MQTT.

        For each routing block, the active source is the one whose input channels
        are all unmuted on their paired output channels. If zero or multiple sources
        are fully unmuted the state is published as 'Unknown'.
        """
        for route in self._cfg.routing:
            active_id = await self._detect_active_source(route)
            self._routing_states[route.id].active_source_id = active_id
            if active_id:
                name = self._source_id_to_name.get(active_id, active_id)
                logger.info("Route '%s': active source is '%s'", route.id, name)
            else:
                name = "Unknown"
                logger.info("Route '%s': active source could not be determined", route.id)
            await self._mqtt.publish(f"tesira/routing/{route.id}/state", name)

    # Crosspoint level threshold: crosspoints above this dB value are considered
    # "active/on". Matrix Mixer crosspoints are set to 0.0 dB when on and
    # -100.0 dB when off; -60 dB gives plenty of headroom for both states.
    _CROSSPOINT_ON_THRESHOLD_DB = -60.0
    _CROSSPOINT_ON_DB = 0.0
    _CROSSPOINT_OFF_DB = -100.0

    async def _detect_active_source(self, route: RoutingConfig) -> str | None:
        """Return the source_id whose crosspoints are all active (≥ threshold dB), or None.

        Matrix Mixer crosspoints are level-based (not mute-based): 0 dB = on,
        -100 dB = off.  Any level above _CROSSPOINT_ON_THRESHOLD_DB counts as on.
        """
        active_ids: list[str] = []
        for entry in route.sources:
            all_on = True
            for in_ch, out_ch in zip(entry.input_channels, route.output_channels):
                raw = await self._tesira.send(
                    cmd_get_crosspoint_level(route.matrix_instance, in_ch, out_ch)
                )
                resp = parse_response(raw)
                if isinstance(resp, ValueResponse):
                    try:
                        db = parse_float(resp.value)
                        if db < self._CROSSPOINT_ON_THRESHOLD_DB:
                            all_on = False
                            break
                    except ValueError:
                        all_on = False
                        break
                else:
                    all_on = False
                    break
            if all_on:
                active_ids.append(entry.source_id)

        if len(active_ids) == 1:
            return active_ids[0]
        return None   # 0 = nothing active; >1 = ambiguous (multiple sources on)

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
        await self._mqtt.publish(
            f"tesira/zone/{zone_id}/mute/state", "ON" if muted else "OFF"
        )

    # ── MQTT → TTP handlers ───────────────────────────────────────────────────

    async def _handle_level_command(self, topic: str, payload: str) -> None:
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
        await self._tesira.send(cmd_set_mute(zone.effective_mute_instance, zone.effective_mute_channel, muted))

    async def _handle_routing_command(self, topic: str, payload: str) -> None:
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

        # Disable all input → output crosspoints for this route
        for entry in route.sources:
            for in_ch, out_ch in zip(entry.input_channels, route.output_channels):
                await self._tesira.send(
                    cmd_set_crosspoint_level(
                        route.matrix_instance, in_ch, out_ch, self._CROSSPOINT_OFF_DB
                    )
                )

        # Enable only the target source's crosspoints at unity gain
        for in_ch, out_ch in zip(target_entry.input_channels, route.output_channels):
            await self._tesira.send(
                cmd_set_crosspoint_level(
                    route.matrix_instance, in_ch, out_ch, self._CROSSPOINT_ON_DB
                )
            )

        self._routing_states[route.id].active_source_id = target_source_id
        source_name = self._source_id_to_name.get(target_source_id, target_source_id)
        await self._mqtt.publish(f"tesira/routing/{route.id}/state", source_name)
        logger.info("Route '%s' switched to '%s'", route.id, target_source_id)
