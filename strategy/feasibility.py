"""SPEC.md 66~73줄이 요구하는, 엑셀이 반영하지 않은 2가지 실행가능성 분석.

1. 가용잔고가 음수로 전환되는 최초 단계 -> max_feasible_step
2. 최소 주문 수량/명목가치 미달 단계 목록

(2)는 실제 min_qty/min_notional 값을 Phase 0에서 확인하지 못했으므로(docs/api-notes.md
미확인 항목 #7), 이 모듈은 임계값을 인자로만 받는 순수 함수로 제공한다. 값을 하드코딩하지
않는다 — Phase 2에서 라이브 계약 스펙을 조회한 뒤 그 값을 그대로 넣어 호출하면 된다.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from strategy.grid import GridStepResult


@dataclass(frozen=True)
class FeasibilityResult:
    all_feasible: bool
    first_infeasible_index: int | None  # 0-based
    first_infeasible_overall_step: int | None  # 1-based
    first_infeasible_major_tier: int | None
    first_infeasible_sub_step: int | None
    max_feasible_step_count: int  # 실행 가능한 단계 수 (0-based first_infeasible_index와 동일)


def find_max_feasible_step(rows: list[GridStepResult]) -> FeasibilityResult:
    for step in rows:
        if step.available_balance < 0:
            return FeasibilityResult(
                all_feasible=False,
                first_infeasible_index=step.index,
                first_infeasible_overall_step=step.index + 1,
                first_infeasible_major_tier=step.major_tier,
                first_infeasible_sub_step=step.sub_step,
                max_feasible_step_count=step.index,
            )
    return FeasibilityResult(
        all_feasible=True,
        first_infeasible_index=None,
        first_infeasible_overall_step=None,
        first_infeasible_major_tier=None,
        first_infeasible_sub_step=None,
        max_feasible_step_count=len(rows),
    )


@dataclass(frozen=True)
class MinOrderShortfall:
    index: int
    major_tier: int
    sub_step: int
    step_qty: Decimal
    notional: Decimal
    below_min_qty: bool
    below_min_notional: bool


def find_min_order_shortfalls(
    rows: list[GridStepResult],
    min_qty: Decimal | None,
    min_notional: Decimal | None,
) -> list[MinOrderShortfall]:
    """min_qty/min_notional 중 확인되지 않은 값은 None으로 넘기면 해당 항목은 판정하지 않는다."""
    shortfalls = []
    for step in rows:
        notional = step.step_qty * step.entry_price
        below_qty = min_qty is not None and step.step_qty < min_qty
        below_notional = min_notional is not None and notional < min_notional
        if below_qty or below_notional:
            shortfalls.append(
                MinOrderShortfall(
                    index=step.index,
                    major_tier=step.major_tier,
                    sub_step=step.sub_step,
                    step_qty=step.step_qty,
                    notional=notional,
                    below_min_qty=below_qty,
                    below_min_notional=below_notional,
                )
            )
    return shortfalls
