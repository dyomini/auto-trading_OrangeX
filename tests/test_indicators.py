"""RSI(14)/ATR(14) Decimal 계산 유닛 테스트 — 손으로 검증 가능한 소형(period=2) 케이스 사용."""
from __future__ import annotations

from decimal import Decimal

import pytest

from strategy.indicators import compute_atr, compute_rsi
from strategy.market_data import Candle


def test_compute_rsi_matches_hand_calculation():
    # closes=[10,12,11], period=2 -> diffs=[+2,-1] -> gains=[2,0], losses=[0,1]
    # avg_gain=(2+0)/2=1, avg_loss=(0+1)/2=0.5, rs=2, RSI=100-100/3
    closes = [Decimal("10"), Decimal("12"), Decimal("11")]

    rsi = compute_rsi(closes, period=2)

    expected = Decimal("100") - (Decimal("100") / Decimal("3"))
    assert rsi == expected


def test_compute_rsi_all_gains_returns_100():
    closes = [Decimal("10"), Decimal("11"), Decimal("12")]
    assert compute_rsi(closes, period=2) == Decimal("100")


def test_compute_rsi_raises_when_not_enough_closes():
    with pytest.raises(ValueError):
        compute_rsi([Decimal("10"), Decimal("11")], period=14)


def test_compute_atr_matches_hand_calculation():
    # c0(close=9) -> c1(high=11,low=9,close=10): TR1=max(2,2,0)=2
    # c1 -> c2(high=9,low=7,close=8): TR2=max(2,|9-10|=1,|7-10|=3)=3
    # period=2 -> atr=(2+3)/2=2.5
    candles = [
        Candle(open_time_ms=0, open=Decimal("8.5"), high=Decimal("10"), low=Decimal("8"), close=Decimal("9")),
        Candle(open_time_ms=1, open=Decimal("9"), high=Decimal("11"), low=Decimal("9"), close=Decimal("10")),
        Candle(open_time_ms=2, open=Decimal("10"), high=Decimal("9"), low=Decimal("7"), close=Decimal("8")),
    ]

    atr = compute_atr(candles, period=2)

    assert atr == Decimal("2.5")


def test_compute_atr_raises_when_not_enough_candles():
    candles = [
        Candle(open_time_ms=0, open=Decimal("8"), high=Decimal("9"), low=Decimal("7"), close=Decimal("8")),
    ]
    with pytest.raises(ValueError):
        compute_atr(candles, period=14)
