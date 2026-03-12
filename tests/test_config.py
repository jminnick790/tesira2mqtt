"""Unit tests for config loading and validation."""

import pytest
from pydantic import ValidationError

from tesira_bridge.config import BridgeConfig


MINIMAL_VALID = {
    "tesira": {"host": "192.168.1.100"},
    "mqtt": {"host": "192.168.1.50"},
    "sources": [
        {"id": "sonos_port", "name": "Sonos Port"},
    ],
    "zones": [
        {
            "id": "zone_01",
            "name": "Zone 01",
            "level_instance": "Zone01Fader",
            "mute_instance": "Zone01Mute",
        }
    ],
    "routing": [
        {
            "id": "zone_01_source",
            "name": "Zone 01 Source",
            "zone_id": "zone_01",
            "matrix_instance": "MainRouter",
            "output_channels": [1, 2],
            "sources": [
                {"source_id": "sonos_port", "input_channels": [1, 2]},
            ],
        }
    ],
}


def test_valid_config_loads():
    cfg = BridgeConfig.model_validate(MINIMAL_VALID)
    assert cfg.tesira.host == "192.168.1.100"
    assert len(cfg.zones) == 1
    assert len(cfg.routing) == 1


def test_unknown_zone_id_raises():
    bad = {**MINIMAL_VALID, "routing": [
        {**MINIMAL_VALID["routing"][0], "zone_id": "does_not_exist"}
    ]}
    with pytest.raises(ValidationError, match="unknown zone_id"):
        BridgeConfig.model_validate(bad)


def test_unknown_source_id_raises():
    bad = {**MINIMAL_VALID, "routing": [
        {
            **MINIMAL_VALID["routing"][0],
            "sources": [{"source_id": "nonexistent", "input_channels": [1, 2]}],
        }
    ]}
    with pytest.raises(ValidationError, match="unknown source_id"):
        BridgeConfig.model_validate(bad)


def test_mismatched_channel_lengths_raise():
    bad = {**MINIMAL_VALID, "routing": [
        {
            **MINIMAL_VALID["routing"][0],
            "output_channels": [1, 2],
            "sources": [{"source_id": "sonos_port", "input_channels": [1]}],  # length mismatch
        }
    ]}
    with pytest.raises(ValidationError):
        BridgeConfig.model_validate(bad)
