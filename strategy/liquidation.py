"""예상 청산가 계산.

SPEC.md 57줄은 롱 포지션의 공식만 명시한다:
  예상 청산가(롱) = (평단*누적수량 - equity) / (누적수량 * (1 - maint_margin_rate))

숏 공식은 SPEC에 없어 엑셀(비트_숏계산기/이더_숏계산기)의 캐시된 값을 역산해 도출했다:
  예상 청산가(숏) = (평단*누적수량 + equity) / (누적수량 * (1 + maint_margin_rate))
이 역산 결과는 tests/test_golden.py의 golden test로 4개 시트 100행 전부와 대조해
정확히 일치함을 확인했다 (가정이 아니라 실측 데이터로 검증된 값).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

Direction = Literal["long", "short"]


def expected_liquidation_price(
    avg_price: Decimal,
    cum_qty: Decimal,
    equity: Decimal,
    maint_margin_rate: Decimal,
    direction: Direction,
) -> Decimal:
    if direction == "long":
        return (avg_price * cum_qty - equity) / (cum_qty * (Decimal("1") - maint_margin_rate))
    if direction == "short":
        return (avg_price * cum_qty + equity) / (cum_qty * (Decimal("1") + maint_margin_rate))
    raise ValueError(f"unknown direction: {direction}")
