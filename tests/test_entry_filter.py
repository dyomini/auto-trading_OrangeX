"""engine/entry_filter.py 유닛 테스트."""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.entry_filter import compute_atr_tick_multiplier, passes_rsi_filter


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


def test_compute_atr_tick_multiplier_no_spike_returns_one():
    multiplier = compute_atr_tick_multiplier(atr_today=Decimal("100"), atr_yesterday=Decimal("100"))
    assert multiplier == Decimal("1")

    # 임계값(1.3배) 이하 상승은 급등으로 안 침
    multiplier = compute_atr_tick_multiplier(atr_today=Decimal("129"), atr_yesterday=Decimal("100"))
    assert multiplier == Decimal("1")


def test_compute_atr_tick_multiplier_spike_scales_by_ratio():
    # 100 -> 150은 1.5배 상승, 임계값(1.3) 초과 -> 그 비율 그대로 적용
    multiplier = compute_atr_tick_multiplier(atr_today=Decimal("150"), atr_yesterday=Decimal("100"))
    assert multiplier == Decimal("1.5")


def test_compute_atr_tick_multiplier_caps_at_two():
    # 100 -> 500은 5배지만 상한 2배로 캡
    multiplier = compute_atr_tick_multiplier(atr_today=Decimal("500"), atr_yesterday=Decimal("100"))
    assert multiplier == Decimal("2.0")


def test_compute_atr_tick_multiplier_handles_zero_baseline():
    multiplier = compute_atr_tick_multiplier(atr_today=Decimal("50"), atr_yesterday=Decimal("0"))
    assert multiplier == Decimal("1")
