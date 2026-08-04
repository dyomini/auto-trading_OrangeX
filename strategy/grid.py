"""100단계 격자 계산 엔진 (거래소와 완전 분리된 순수 함수).

SPEC.md 44~59줄 전체 격자 정의를 구현한다. 모든 계산은 Decimal로만 수행하며
float은 전혀 사용하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from strategy.liquidation import Direction, expected_liquidation_price
from strategy.targets import roe_for_tier, sl_price, target_tp_price

STEPS_PER_TIER = 20
TOTAL_STEPS = 100
MARGIN_QUANT = Decimal("0.1")


@dataclass(frozen=True)
class GridStepResult:
    index: int
    major_tier: int
    sub_step: int
    entry_price: Decimal
    weight: Decimal
    step_qty: Decimal
    step_margin: Decimal
    cum_qty: Decimal
    cum_margin: Decimal
    avg_price: Decimal
    available_balance: Decimal
    liq_price: Decimal
    target_roe: Decimal
    target_tp_price: Decimal
    sl_price: Decimal


def _entry_price(base_price: Decimal, tick: Decimal, i: int, direction: Direction) -> Decimal:
    if direction == "long":
        return base_price - tick * i
    if direction == "short":
        return base_price + tick * i
    raise ValueError(f"unknown direction: {direction}")


def _available_balance(
    equity: Decimal,
    cum_margin: Decimal,
    cum_qty: Decimal,
    entry_price: Decimal,
    avg_price: Decimal,
    direction: Direction,
) -> Decimal:
    if direction == "long":
        return equity - cum_margin + cum_qty * (entry_price - avg_price)
    if direction == "short":
        return equity - cum_margin + cum_qty * (avg_price - entry_price)
    raise ValueError(f"unknown direction: {direction}")


def compute_grid(
    direction: Direction,
    base_price: Decimal,
    tick: Decimal,
    weights: list[Decimal],
    equity: Decimal,
    leverage: Decimal,
    maint_margin_rate: Decimal,
    sl_pct: Decimal = Decimal("0.03"),
) -> list[GridStepResult]:
    # max_stage로 앞쪽 N개 tier만 쓰는 압축 구조(2026-08-04, "3k" 참고 설계)를 지원하려고
    # 정확히 TOTAL_STEPS개가 아니라 1..TOTAL_STEPS개까지 허용한다. weight_sum이 넘겨받은
    # weights 리스트 전체를 기준으로 계산되므로(아래), 앞쪽 N개만 잘라 넘기면 그 N개
    # 안에서 비중이 재정규화된다 — engine/grid_setup.py가 max_stage 절삭을 여기 넘기기
    # *전에* 적용해서 이 효과를 낸다.
    if not (1 <= len(weights) <= TOTAL_STEPS):
        raise ValueError(f"weights must have 1..{TOTAL_STEPS} entries, got {len(weights)}")

    weight_sum = sum(weights)
    results: list[GridStepResult] = []
    cum_qty = Decimal("0")
    cum_margin = Decimal("0")

    for i, weight in enumerate(weights):
        major_tier = (i // STEPS_PER_TIER) + 1
        sub_step = (i % STEPS_PER_TIER) + 1

        entry_price = _entry_price(base_price, tick, i, direction)
        step_margin = (equity * weight / weight_sum).quantize(MARGIN_QUANT, rounding=ROUND_HALF_UP)
        step_qty = step_margin * leverage / entry_price

        cum_qty += step_qty
        cum_margin += step_margin
        avg_price = cum_margin * leverage / cum_qty

        available_balance = _available_balance(equity, cum_margin, cum_qty, entry_price, avg_price, direction)
        liq_price = expected_liquidation_price(avg_price, cum_qty, equity, maint_margin_rate, direction)

        roe = roe_for_tier(major_tier)
        tp_price = target_tp_price(avg_price, roe, leverage, direction)
        stop_loss_price = sl_price(avg_price, sl_pct, direction)

        results.append(
            GridStepResult(
                index=i,
                major_tier=major_tier,
                sub_step=sub_step,
                entry_price=entry_price,
                weight=weight,
                step_qty=step_qty,
                step_margin=step_margin,
                cum_qty=cum_qty,
                cum_margin=cum_margin,
                avg_price=avg_price,
                available_balance=available_balance,
                liq_price=liq_price,
                target_roe=roe,
                target_tp_price=tp_price,
                sl_price=stop_loss_price,
            )
        )

    return results
