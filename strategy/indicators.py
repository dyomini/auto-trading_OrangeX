"""RSI(14)/ATR(14) 진입 필터 지표 — Wilder 평활화, Decimal 전용 계산.

SPEC.md 90줄은 pandas-ta 사용을 제안했지만, pandas/pandas-ta는 내부적으로 float를
사용해 이 프로젝트 전체의 "모든 계산은 Decimal, float 미사용" 원칙(strategy/ 다른
모듈 전부가 따르는 컨벤션)과 어긋난다. 표준 Wilder 평활화 공식을 Decimal로 직접
구현해 무거운 의존성(pandas/numpy/pandas-ta) 추가 없이 동일한 결과를 얻는다.
"""
from __future__ import annotations

from decimal import Decimal

from strategy.market_data import Candle


def compute_rsi(closes: list[Decimal], period: int = 14) -> Decimal:
    if len(closes) < period + 1:
        raise ValueError(
            f"RSI({period}) 계산에는 최소 {period + 1}개의 종가가 필요함, 받은 개수: {len(closes)}"
        )

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0 else Decimal("0"))
        losses.append(-diff if diff < 0 else Decimal("0"))

    avg_gain = sum(gains[:period], Decimal("0")) / period
    avg_loss = sum(losses[:period], Decimal("0")) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return Decimal("100")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def _true_range(candle: Candle, prev_close: Decimal) -> Decimal:
    return max(
        candle.high - candle.low,
        abs(candle.high - prev_close),
        abs(candle.low - prev_close),
    )


def compute_atr(candles: list[Candle], period: int = 14) -> Decimal:
    if len(candles) < period + 1:
        raise ValueError(
            f"ATR({period}) 계산에는 최소 {period + 1}개의 캔들이 필요함, 받은 개수: {len(candles)}"
        )

    true_ranges = [_true_range(candles[i], candles[i - 1].close) for i in range(1, len(candles))]

    atr = sum(true_ranges[:period], Decimal("0")) / period
    for i in range(period, len(true_ranges)):
        atr = (atr * (period - 1) + true_ranges[i]) / period
    return atr
