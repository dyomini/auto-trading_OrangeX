"""격자 프리셋 — 사용자가 "3k"/"5k" 중 하나를 고르면 단계 수와 레버리지가 함께 정해진다
(2026-08-17 사용자 결정: "5k와 3k는 내가 설정에서 미리 설정할 수 있도록 해.
레버리지는 40배 고정").

프리셋은 **현재가 기준 ± 가격 범위(USDT)** 를 뜻한다 — quick_entry의 3k/5k 선택지와
같은 의미다. 범위를 `grid_tick`으로 나눠 단계 수를 얻고, 그걸 20단계(1 tier)로 나눠
`max_stage`를 정한다. 기본값(`grid_tick=50`)에서 3k -> 60단계(3 tier), 5k -> 100단계(5 tier)로
정확히 떨어진다.

**레버리지 40배를 고른 근거**(라이브 BTC 63,663 기준으로 계산해 사용자와 확인함):
가용잔고 절삭 후 실제로 걸리는 격자의 최심 주문가와, 그 상태의 청산가 사이 완충이
3k는 2.08%p / 5k는 2.01%p다. 66배면 완충이 1.00%p까지 줄어든다. SL을 걸지 않는
운용이라(`sl_enabled`) 이 완충이 유일한 방어선이다.

**주의**: `set_leverage()`는 봇 경로에서 호출되지 않는다 — 여기 값은 수량 계산에만
쓰이는 지역값이고, 거래소 계좌의 실제 레버리지는 사용자가 직접 맞춰야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from strategy.grid import STEPS_PER_TIER, TOTAL_STEPS

GridPresetName = Literal["3k", "5k"]


class GridPresetError(ValueError):
    """프리셋과 grid_tick 조합이 격자 구조에 맞지 않을 때. 반올림해서 억지로 맞추지
    않고 명시적으로 막는다(SPEC 0번)."""


@dataclass(frozen=True)
class GridPreset:
    price_range_usdt: Decimal
    leverage: Decimal


GRID_PRESETS: dict[str, GridPreset] = {
    "3k": GridPreset(price_range_usdt=Decimal("3000"), leverage=Decimal("40")),
    "5k": GridPreset(price_range_usdt=Decimal("5000"), leverage=Decimal("40")),
}


def resolve_preset(name: str, grid_tick: Decimal) -> tuple[int, Decimal]:
    """프리셋 이름과 격자 간격으로 `(max_stage, leverage)`를 계산한다."""
    try:
        preset = GRID_PRESETS[name]
    except KeyError as e:
        raise GridPresetError(
            f"알 수 없는 프리셋: {name!r} — 가능한 값: {sorted(GRID_PRESETS)}"
        ) from e

    if grid_tick <= 0:
        raise GridPresetError(f"grid_tick은 양수여야 함: {grid_tick}")

    steps = preset.price_range_usdt / grid_tick
    if steps != steps.to_integral_value():
        raise GridPresetError(
            f"프리셋 {name}(범위 {preset.price_range_usdt} USDT)를 grid_tick={grid_tick}으로 "
            f"나누면 {steps}단계로 정수가 아니다 — grid_tick을 조정해라"
        )
    steps = int(steps)

    if steps % STEPS_PER_TIER != 0:
        raise GridPresetError(
            f"프리셋 {name} + grid_tick={grid_tick} -> {steps}단계는 {STEPS_PER_TIER}단계"
            f"(1 tier)의 배수가 아니라 대단계 경계에 안 맞는다"
        )
    if not (0 < steps <= TOTAL_STEPS):
        raise GridPresetError(
            f"프리셋 {name} + grid_tick={grid_tick} -> {steps}단계는 지원 범위"
            f"(1..{TOTAL_STEPS})를 벗어난다 — config/weights.csv가 {TOTAL_STEPS}개뿐이다"
        )

    return steps // STEPS_PER_TIER, preset.leverage
