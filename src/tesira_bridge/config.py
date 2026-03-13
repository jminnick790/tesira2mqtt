"""Configuration models and loader for tesira-bridge.

Environment variables override the corresponding config.yaml values:

  TESIRA_HOST        Tesira IP address
  TESIRA_PORT        Tesira Telnet port        (default: 23)
  TESIRA_USERNAME    Tesira login username     (default: default)
  TESIRA_PASSWORD    Tesira login password     (default: default)

  MQTT_HOST          MQTT broker IP address
  MQTT_PORT          MQTT broker port          (default: 1883)
  MQTT_USERNAME      MQTT broker username      (default: empty)
  MQTT_PASSWORD      MQTT broker password      (default: empty)

  CONFIG_PATH        Path to config.yaml       (default: config/config.yaml)
  LOG_LEVEL          Logging level             (default: INFO)
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


# ── Low-level connection config ───────────────────────────────────────────────

class TesiraConfig(BaseModel):
    host: str
    port: int = 23
    username: str = "default"
    password: str = "default"
    reconnect_interval_s: float = 5.0
    subscription_min_rate_ms: int = 100


class MqttConfig(BaseModel):
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    discovery_prefix: str = "homeassistant"
    device_name: str = "Biamp Tesira"
    device_id: str = "tesira_main"


# ── Domain objects ────────────────────────────────────────────────────────────

class SourceConfig(BaseModel):
    id: str
    name: str


class ZoneConfig(BaseModel):
    id: str
    name: str
    stereo: bool = True
    level_instance: str
    level_channel: int = 0
    # mute_instance is optional — defaults to level_instance when not set.
    # Tesira Level control blocks expose both 'level' and 'mute' attributes,
    # so a separate mute block is only needed if your design uses one.
    mute_instance: str | None = None
    mute_channel: int | None = None
    min_db: float = -100.0
    max_db: float = 12.0

    @property
    def effective_mute_instance(self) -> str:
        return self.mute_instance or self.level_instance

    @property
    def effective_mute_channel(self) -> int:
        return self.mute_channel if self.mute_channel is not None else self.level_channel

    @property
    def effective_subscribe_channel(self) -> int:
        """Channel index to use for TTP `subscribe` commands.

        Ganged channel 0 works for `get`/`set` (moves L+R together) but
        some Tesira firmware versions do not resolve channel 0 for subscribe.
        Fall back to channel 1 so subscriptions land on the Left channel,
        which is sufficient for ganged stereo zones.
        """
        return self.level_channel if self.level_channel != 0 else 1


class RoutingSourceEntry(BaseModel):
    source_id: str
    input_channels: list[int]

    @model_validator(mode="after")
    def channels_must_be_nonempty(self) -> "RoutingSourceEntry":
        if not self.input_channels:
            raise ValueError("input_channels must not be empty")
        return self


class RoutingConfig(BaseModel):
    id: str
    name: str
    zone_id: str
    matrix_instance: str
    output_channels: list[int]
    sources: list[RoutingSourceEntry]

    @model_validator(mode="after")
    def channel_lengths_must_match(self) -> "RoutingConfig":
        n = len(self.output_channels)
        for entry in self.sources:
            if len(entry.input_channels) != n:
                raise ValueError(
                    f"source '{entry.source_id}' has {len(entry.input_channels)} "
                    f"input_channels but output_channels has {n}"
                )
        return self


# ── Root config ───────────────────────────────────────────────────────────────

class BridgeConfig(BaseModel):
    tesira: TesiraConfig
    mqtt: MqttConfig
    sources: list[SourceConfig] = Field(default_factory=list)
    zones: list[ZoneConfig] = Field(default_factory=list)
    routing: list[RoutingConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "BridgeConfig":
        source_ids = {s.id for s in self.sources}
        zone_ids = {z.id for z in self.zones}

        for route in self.routing:
            if route.zone_id not in zone_ids:
                raise ValueError(
                    f"routing '{route.id}' references unknown zone_id '{route.zone_id}'"
                )
            for entry in route.sources:
                if entry.source_id not in source_ids:
                    raise ValueError(
                        f"routing '{route.id}' references unknown source_id "
                        f"'{entry.source_id}'"
                    )
        return self


def _apply_env_overrides(raw: dict) -> dict:
    """Override connection settings with environment variables when set.

    Structural config (zones, sources, routing) always comes from the YAML file.
    Connection details can be set or overridden via env vars, which makes it
    easy to inject secrets into Docker containers without editing the config file.
    """
    _str_override(raw, ["tesira", "host"],     "TESIRA_HOST")
    _int_override(raw, ["tesira", "port"],     "TESIRA_PORT")
    _str_override(raw, ["tesira", "username"], "TESIRA_USERNAME")
    _str_override(raw, ["tesira", "password"], "TESIRA_PASSWORD")

    _str_override(raw, ["mqtt", "host"],     "MQTT_HOST")
    _int_override(raw, ["mqtt", "port"],     "MQTT_PORT")
    _str_override(raw, ["mqtt", "username"], "MQTT_USERNAME")
    _str_override(raw, ["mqtt", "password"], "MQTT_PASSWORD")

    return raw


def _str_override(data: dict, path: list[str], env_var: str) -> None:
    value = os.environ.get(env_var)
    if value is not None:
        node = data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value


def _int_override(data: dict, path: list[str], env_var: str) -> None:
    value = os.environ.get(env_var)
    if value is not None:
        node = data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = int(value)


def load_config(path: Path | str | None = None) -> BridgeConfig:
    """Load and validate config, applying env var overrides.

    Path defaults to CONFIG_PATH env var, then 'config/config.yaml'.
    """
    config_path = Path(path or os.environ.get("CONFIG_PATH", "config/config.yaml"))
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Copy config/config.yaml.example to config/config.yaml and fill in your values.\n"
            "Or set CONFIG_PATH to point to your config file."
        )
    with config_path.open() as f:
        raw = yaml.safe_load(f) or {}
    raw = _apply_env_overrides(raw)
    return BridgeConfig.model_validate(raw)
