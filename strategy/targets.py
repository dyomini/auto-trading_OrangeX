"""목표 익절가(TP)·스톱로스가(SL) 계산.

SPEC.md 58~59줄:
  목표 익절: ROE 기준 — 1~2차 10%, 3~4차 5%, 5차 2% -> 익절가 = 평단 * (1 ± ROE/leverage)
  스톱로스 = 평단 * (1 ∓ 0.03)

2026-07-30 갱신: `condition_type=STOP` 조건부 주문이 실제 거래소 등록형 SL로 동작함을
라이브로 확인했다 (docs/api-notes.md §6 항목18, docs/phase3-plan.md). sl_price()는
이제 참고용이 아니라 Phase 3 실행 엔진이 `place_stop_order`(exchange/base.py)에
넘길 실제 트리거가 계산에 그대로 쓰인다.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from strategy.liquidation import Direction

TIER_ROE = {
    1: Decimal("0.10"),
    2: Decimal("0.10"),
    3: Decimal("0.05"),
    4: Decimal("0.05"),
    5: Decimal("0.02"),
}


def roe_for_tier(major_tier: int) -> Decimal:
    try:
        return TIER_ROE[major_tier]
    except KeyError as e:
        raise ValueError(f"unknown major_tier: {major_tier}") from e


def target_tp_price(avg_price: Decimal, roe: Decimal, leverage: Decimal, direction: Direction) -> Decimal:
    factor = roe / leverage
    if direction == "long":
        return avg_price * (Decimal("1") + factor)
    if direction == "short":
        return avg_price * (Decimal("1") - factor)
    raise ValueError(f"unknown direction: {direction}")


def sl_price(avg_price: Decimal, sl_pct: Decimal, direction: Direction) -> Decimal:
    if direction == "long":
        return avg_price * (Decimal("1") - sl_pct)
    if direction == "short":
        return avg_price * (Decimal("1") + sl_pct)
    raise ValueError(f"unknown direction: {direction}")
