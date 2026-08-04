"""수수료·펀딩비 반영 실질 손익분기가 계산 (엑셀에는 없는, SPEC.md 74~75줄이 요구하는 신규 계산).

주의(가정 명시): 실제 taker/maker 수수료율과 펀딩비율은 Phase 0에서 라이브로 확인하지
못했다 (docs/api-notes.md 미확인 항목 #5, #7). 아래 함수들은 요율을 전부 인자로 받는
순수 함수이며, 이 모듈 자체는 어떤 수치도 하드코딩하지 않는다. 리포트에서 사용하는
예시 요율(taker 0.05%, maker 0.02%, 펀딩비 예시치)은 SPEC.md가 제시한 참고값이며
"가정"으로 표시해야 한다.

손익분기가 유도:
  평단가 정의상 avg_price * cum_qty == cum_margin * leverage 이므로,
  롱: X*(1-fee_exit) = avg_price*(1+fee_entry) + funding_cost/cum_qty
      -> X = (avg_price*(1+fee_entry) + funding_cost/cum_qty) / (1-fee_exit)
  숏: X*(1+fee_exit) = avg_price*(1-fee_entry) - funding_cost/cum_qty
      -> X = (avg_price*(1-fee_entry) - funding_cost/cum_qty) / (1+fee_exit)
  (funding_cost는 보유 기간 동안 누적된 펀딩비 총액(USDT), 방향에 따라 부호는 호출부에서
   조정해 넘긴다 — 이 함수는 이미 "비용"으로 확정된 금액만 받는다.)
"""
from __future__ import annotations

from decimal import Decimal

from strategy.liquidation import Direction


def estimate_funding_cost(notional: Decimal, funding_rate_per_period: Decimal, num_periods: int) -> Decimal:
    """단순 추정치: notional * 요율 * 횟수. 실제 펀딩비는 매 정산시점의 시가/수량에 따라 변하므로
    이 값은 근사치이며 반드시 "가정"으로 표시해야 한다."""
    return notional * funding_rate_per_period * num_periods


def breakeven_price(
    avg_price: Decimal,
    cum_qty: Decimal,
    fee_entry_rate: Decimal,
    fee_exit_rate: Decimal,
    funding_cost: Decimal,
    direction: Direction,
) -> Decimal:
    funding_per_qty = funding_cost / cum_qty
    if direction == "long":
        numerator = avg_price * (Decimal("1") + fee_entry_rate) + funding_per_qty
        return numerator / (Decimal("1") - fee_exit_rate)
    if direction == "short":
        numerator = avg_price * (Decimal("1") - fee_entry_rate) - funding_per_qty
        return numerator / (Decimal("1") + fee_exit_rate)
    raise ValueError(f"unknown direction: {direction}")
