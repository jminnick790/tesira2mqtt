"""Configuration models and loader for tesira-bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

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
    mute_instance: str
    mute_channel: int = 0
    min_db: float = -100.0
    max_db: float = 12.0


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


def load_config(path: Path | str = "config/config.yaml") -> BridgeConfig:
    """Load and validate config.yaml, raising clear errors on misconfiguration."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            f"Copy config/config.yaml.example to config/config.yaml and fill in your values."
        )
    with config_path.open() as f:
        raw = yaml.safe_load(f)
    return BridgeConfig.model_validate(raw)
