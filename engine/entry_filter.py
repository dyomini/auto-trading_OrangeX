"""SPEC.md 90줄 진입 필터: 일봉 RSI(14) ≤30(롱 진입)/≥70(숏 진입).

**2026-08-17 변경**: SPEC 90줄의 "ATR 급등 시 격자 간격 확대"는 사용자 결정으로
완전히 제거했다("진입 근거에서 atr은 배제해"). 기존 `compute_atr_tick_multiplier()`와
그 임계값/상한 상수(1.3 / 2.0 — SPEC 근거 없이 이 구현이 정했던 값)도 같이 지웠다.
`strategy/indicators.py`의 `compute_atr()` 자체는 순수 함수라 남겨뒀지만 이제 봇
경로에서는 쓰이지 않는다.

`passes_rsi_filter()`는 `direction`을 `long`/`short`로 **고정**해 운용할 때의 진입
게이트다(SPEC 원안). `DIRECTION=auto`에서는 이 게이트 대신 `direction_from_rsi()`가
15분봉 RSI로 방향 자체를 정한다 — 둘은 목적이 다르므로 임계값도 별개다.
"""
from __future__ import annotations

from decimal import Decimal

from strategy.liquidation import Direction

RSI_LONG_ENTRY_THRESHOLD = Decimal("30")
RSI_SHORT_ENTRY_THRESHOLD = Decimal("70")


def passes_rsi_filter(direction: str, rsi: Decimal) -> bool:
    if direction == "long":
        return rsi <= RSI_LONG_ENTRY_THRESHOLD
    if direction == "short":
        return rsi >= RSI_SHORT_ENTRY_THRESHOLD
    raise ValueError(f"unknown direction: {direction}")
