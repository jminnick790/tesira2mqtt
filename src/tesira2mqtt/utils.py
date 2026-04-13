"""Utility functions for tesira2mqtt."""

from __future__ import annotations

import math


def db_to_position(db: float, min_db: float, max_db: float) -> float:
    """Convert a dB value to a 0-100 position using a square root curve.

    The square root curve gives more resolution at lower volumes where the
    ear is most sensitive, while still feeling natural across the full range.

    Returns a value clamped to [0.0, 100.0].
    """
    if max_db <= min_db:
        return 0.0
    ratio = (db - min_db) / (max_db - min_db)
    ratio = max(0.0, min(1.0, ratio))
    return round(ratio * 100, 1)


def position_to_db(position: float, min_db: float, max_db: float) -> float:
    """Convert a 0-100 position to a dB value using an inverse square root curve.

    Returns a value clamped to [min_db, max_db].
    """
    if max_db <= min_db:
        return min_db
    ratio = max(0.0, min(100.0, position)) / 100
    db = min_db + ratio * (max_db - min_db)
    return round(db, 4)
