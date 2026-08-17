"""engine/entry_filter.py 유닛 테스트."""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.entry_filter import passes_rsi_filter


def test_passes_rsi_filter_long_at_or_below_threshold():
    assert passes_rsi_filter("long", Decimal("30")) is True
    assert passes_rsi_filter("long", Decimal("29")) is True
    assert passes_rsi_filter("long", Decimal("31")) is False


def test_passes_rsi_filter_short_at_or_above_threshold():
    assert passes_rsi_filter("short", Decimal("70")) is True
    assert passes_rsi_filter("short", Decimal("71")) is True
    assert passes_rsi_filter("short", Decimal("69")) is False


def test_passes_rsi_filter_raises_for_unknown_direction():
    with pytest.raises(ValueError):
        passes_rsi_filter("sideways", Decimal("50"))
