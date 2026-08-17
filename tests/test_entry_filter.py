"""engine/entry_filter.py 유닛 테스트."""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.entry_filter import direction_from_rsi, passes_rsi_filter


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


# DIRECTION=auto 전용 규칙 (2026-08-17): RSI >= 50 -> 숏, < 50 -> 롱.
# 위 30/70 게이트와는 목적이 다르다 — 이건 방향 선택이지 진입 허용 여부가 아니다.


def test_direction_from_rsi_boundary_at_50():
    assert direction_from_rsi(Decimal("49.9")) == "long"
    assert direction_from_rsi(Decimal("50")) == "short"   # 경계값은 short 쪽
    assert direction_from_rsi(Decimal("50.1")) == "short"


def test_direction_from_rsi_extremes():
    assert direction_from_rsi(Decimal("0")) == "long"
    assert direction_from_rsi(Decimal("100")) == "short"


def test_direction_from_rsi_custom_threshold():
    assert direction_from_rsi(Decimal("55"), threshold=Decimal("60")) == "long"
    assert direction_from_rsi(Decimal("65"), threshold=Decimal("60")) == "short"
