"""Unit tests for volume conversion utilities."""

import pytest
from tesira2mqtt.utils import db_to_position, position_to_db


MIN_DB = -100.0
MAX_DB = 0.0


class TestDbToPosition:
    def test_min_db_maps_to_zero(self):
        assert db_to_position(MIN_DB, MIN_DB, MAX_DB) == 0.0

    def test_max_db_maps_to_100(self):
        assert db_to_position(MAX_DB, MIN_DB, MAX_DB) == 100.0

    def test_midpoint_is_not_linear(self):
        # Square root curve: midpoint dB (-50) maps to ~70.7, not 50
        result = db_to_position(-50.0, MIN_DB, MAX_DB)
        assert result == pytest.approx(70.7, abs=0.1)

    def test_clamped_below_min(self):
        assert db_to_position(-200.0, MIN_DB, MAX_DB) == 0.0

    def test_clamped_above_max(self):
        assert db_to_position(10.0, MIN_DB, MAX_DB) == 100.0

    def test_custom_range(self):
        # -12 dB range (e.g. zone_01 hardware max)
        result = db_to_position(-12.0, -100.0, -12.0)
        assert result == 100.0


class TestPositionToDb:
    def test_zero_maps_to_min_db(self):
        assert position_to_db(0.0, MIN_DB, MAX_DB) == MIN_DB

    def test_100_maps_to_max_db(self):
        assert position_to_db(100.0, MIN_DB, MAX_DB) == MAX_DB

    def test_midpoint_is_not_linear(self):
        # Inverse square root: position 50 maps to -75 dB, not -50
        result = position_to_db(50.0, MIN_DB, MAX_DB)
        assert result == pytest.approx(-75.0, abs=0.1)

    def test_clamped_below_zero(self):
        assert position_to_db(-10.0, MIN_DB, MAX_DB) == MIN_DB

    def test_clamped_above_100(self):
        assert position_to_db(110.0, MIN_DB, MAX_DB) == MAX_DB

    def test_roundtrip(self):
        # Converting dB → position → dB should return the original value
        for db in [-100.0, -75.0, -50.0, -25.0, -12.0, 0.0]:
            pos = db_to_position(db, MIN_DB, MAX_DB)
            result = position_to_db(pos, MIN_DB, MAX_DB)
            assert result == pytest.approx(db, abs=0.01)
