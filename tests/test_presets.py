"""config/presets.py + Settings의 프리셋 해석 유닛 테스트 (2026-08-17)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from config.presets import GRID_PRESETS, GridPresetError, resolve_preset
from config.settings import Settings


def test_3k_preset_resolves_to_three_tiers_at_40x():
    max_stage, leverage = resolve_preset("3k", Decimal("50"))
    assert (max_stage, leverage) == (3, Decimal("40"))


def test_5k_preset_resolves_to_five_tiers_at_40x():
    max_stage, leverage = resolve_preset("5k", Decimal("50"))
    assert (max_stage, leverage) == (5, Decimal("40"))


def test_both_presets_fix_leverage_at_40():
    assert {p.leverage for p in GRID_PRESETS.values()} == {Decimal("40")}


def test_tick_that_does_not_divide_the_range_evenly_raises():
    # 3000 / 70 = 42.857... — 단계 수가 정수가 아니다. 반올림해서 넘기지 않는다.
    with pytest.raises(GridPresetError, match="정수"):
        resolve_preset("3k", Decimal("70"))


def test_tick_that_is_not_a_tier_boundary_raises():
    # 3000 / 100 = 30단계 — 정수지만 20(STEPS_PER_TIER)의 배수가 아니라 tier가 안 맞는다.
    with pytest.raises(GridPresetError, match="20"):
        resolve_preset("3k", Decimal("100"))


def test_tick_producing_more_than_100_steps_raises():
    # 5000 / 10 = 500단계 — config/weights.csv가 100개뿐이라 감당 못 한다.
    with pytest.raises(GridPresetError, match="100"):
        resolve_preset("5k", Decimal("10"))


def test_unknown_preset_name_raises():
    with pytest.raises(GridPresetError):
        resolve_preset("10k", Decimal("50"))


def test_settings_with_preset_overrides_max_stage_and_leverage():
    """프리셋을 고르면 .env의 LEVERAGE/MAX_STAGE는 무시되고 프리셋 값이 이긴다 —
    그래야 read site(grid_setup/main/quick_entry)를 하나도 안 고쳐도 일관된다."""
    settings = Settings(
        grid_preset="3k", grid_tick=Decimal("50"),
        leverage=Decimal("2"), max_stage=5,  # 프리셋이 덮어써야 하는 값들
    )

    assert settings.max_stage == 3
    assert settings.leverage == Decimal("40")


def test_settings_with_5k_preset():
    settings = Settings(grid_preset="5k", grid_tick=Decimal("50"), leverage=Decimal("2"), max_stage=1)

    assert settings.max_stage == 5
    assert settings.leverage == Decimal("40")


def test_settings_without_preset_keeps_explicit_values():
    """프리셋 미지정이면 기존 동작 그대로 — 기존 테스트/운용이 영향받지 않아야 한다."""
    settings = Settings(leverage=Decimal("20"), max_stage=4, grid_tick=Decimal("50"))

    assert settings.grid_preset is None
    assert settings.max_stage == 4
    assert settings.leverage == Decimal("20")


def test_settings_with_incompatible_tick_raises():
    with pytest.raises(ValueError):
        Settings(grid_preset="3k", grid_tick=Decimal("70"))


def test_empty_grid_preset_env_value_is_treated_as_unset():
    """.env.example이 `GRID_PRESET=` (빈 값)으로 배포되므로 빈 문자열이 None이어야 한다."""
    settings = Settings(grid_preset="", leverage=Decimal("20"), max_stage=4, grid_tick=Decimal("50"))

    assert settings.grid_preset is None
    assert settings.leverage == Decimal("20")
    assert settings.max_stage == 4
