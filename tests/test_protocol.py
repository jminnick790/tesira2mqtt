"""Unit tests for TTP command builders and response/notification parsers."""

import pytest
from tesira_bridge.tesira.protocol import (
    OkResponse,
    ValueResponse,
    ErrorResponse,
    Notification,
    cmd_set_level,
    cmd_set_mute,
    cmd_set_crosspoint_mute,
    cmd_subscribe_level,
    parse_response,
    parse_notification,
    parse_float,
    parse_bool,
)


class TestCommandBuilders:
    def test_set_level(self):
        assert cmd_set_level("Zone01Fader", 0, -20.0) == "Zone01Fader set level 0 -20.0000"

    def test_set_mute_true(self):
        assert cmd_set_mute("Zone01Mute", 0, True) == "Zone01Mute set mute 0 true"

    def test_set_mute_false(self):
        assert cmd_set_mute("Zone01Mute", 0, False) == "Zone01Mute set mute 0 false"

    def test_set_crosspoint_mute(self):
        assert cmd_set_crosspoint_mute("Router", 1, 2, True) == "Router set crosspointMute 1 2 true"

    def test_subscribe_level(self):
        assert cmd_subscribe_level("Zone01Fader", 0, "z01_level", 100) == \
            "Zone01Fader subscribe level 0 z01_level 100"


class TestResponseParser:
    def test_ok_bare(self):
        assert isinstance(parse_response("+OK"), OkResponse)

    def test_ok_with_value(self):
        r = parse_response('+OK "value":-20.500000')
        assert isinstance(r, ValueResponse)
        assert parse_float(r.value) == pytest.approx(-20.5)

    def test_error(self):
        r = parse_response("-ERR invalid instance tag")
        assert isinstance(r, ErrorResponse)
        assert "invalid instance tag" in r.reason

    def test_ok_bool_value(self):
        r = parse_response('+OK "value":true')
        assert isinstance(r, ValueResponse)
        assert parse_bool(r.value) is True


class TestNotificationParser:
    def test_scalar_notification(self):
        line = '! "publishToken":"zone_01_level" "value":-18.250000'
        n = parse_notification(line)
        assert n is not None
        assert n.publish_token == "zone_01_level"
        assert parse_float(n.value) == pytest.approx(-18.25)

    def test_mute_notification(self):
        line = '! "publishToken":"zone_01_mute" "value":true'
        n = parse_notification(line)
        assert n is not None
        assert parse_bool(n.value) is True

    def test_non_notification_returns_none(self):
        assert parse_notification('+OK "value":0') is None

    def test_malformed_returns_none(self):
        assert parse_notification("! garbage line") is None
