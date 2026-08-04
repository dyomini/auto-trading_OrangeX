"""SPEC.md 90줄 진입 필터: 일봉 RSI(14) ≤30(롱 진입)/≥70(숏 진입), ATR 급등 시 격자
간격(tick) 확대.

SPEC은 ATR 급등 시 "격자 간격 확대"만 요구하고 구체적인 배율/판정 기준을 주지 않는다.
2026-07-30 사용자가 "직접 정하지 말고 알아서 판단해서 진행"으로 결정 권한을 넘겨줘서,
아래 기본값으로 구현했다 — SPEC/엑셀에서 도출된 값이 아니라 이 구현이 자체적으로
정한 가정이라는 걸 분명히 남겨둔다:

  - "급등" 판정: 오늘자 ATR(14)이 어제자 ATR(14)(하루 앞선 창)보다 30% 이상 높을 때.
    하루 단위 변화율을 보는 이유는 이미 RSI와 같은 일봉 데이터로 계산하므로 별도
    데이터 소스가 필요 없어서다.
  - 확대 배율: 그 상승 비율을 그대로 tick에 곱하되, 최대 2배로 상한을 둔다(격자
    간격이 통제 불능으로 벌어지는 것을 막기 위한 보수적 안전장치).
  - 데이터가 부족하면(완결 일봉이 16개 미만) 확대하지 않는다(배율 1) — 값을 추측해서
    만들어내는 대신 보수적으로 미확대를 기본값으로 삼음.
"""
from __future__ import annotations

from decimal import Decimal

RSI_LONG_ENTRY_THRESHOLD = Decimal("30")
RSI_SHORT_ENTRY_THRESHOLD = Decimal("70")

# 이 구현이 정한 기본값(SPEC/엑셀 근거 없음) — 아래 모듈 docstring 참고.
ATR_SPIKE_THRESHOLD_RATIO = Decimal("1.3")
ATR_TICK_MULTIPLIER_CAP = Decimal("2.0")


def passes_rsi_filter(direction: str, rsi: Decimal) -> bool:
    if direction == "long":
        return rsi <= RSI_LONG_ENTRY_THRESHOLD
    if direction == "short":
        return rsi >= RSI_SHORT_ENTRY_THRESHOLD
    raise ValueError(f"unknown direction: {direction}")


def compute_atr_tick_multiplier(atr_today: Decimal, atr_yesterday: Decimal) -> Decimal:
    """ATR이 전일 대비 급등했으면 격자 간격(tick)에 곱할 배율을 반환한다(급등 아니면 1).
    `atr_yesterday`가 0이면(변동성이 전혀 없던 극단적 경우) 비율 계산이 무의미해 1을
    반환한다."""
    if atr_yesterday == 0:
        return Decimal("1")
    ratio = atr_today / atr_yesterday
    if ratio <= ATR_SPIKE_THRESHOLD_RATIO:
        return Decimal("1")
    return min(ratio, ATR_TICK_MULTIPLIER_CAP)
