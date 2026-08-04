"""
골든 테스트: strategy 엔진의 출력이 엑셀(제까깟_마틴게이 REALFINALBOSS.xlsx)의
캐시된 계산값과 오차범위 내에서 일치하는지 검증한다.

SPEC.md 61~64줄: "엑셀의 4개 시트, 각 100행의 컬럼(진입가/증거금/수량/누적수량/평단가/
청산가/익절가/SL)을 CSV로 추출해 계산 엔진 출력이 소수점 오차 범위 내에서 전부 일치하는지
검증하라. 하나라도 안 맞으면 다음 Phase로 넘어가지 마라."

허용 오차: 엑셀은 float64로 계산되어 골든 CSV에 float 오차(예: 1.8125000000000001E-3)가
섞여 있다. 우리 엔진은 Decimal로 정확히 계산하므로 완전히 같은 비트값은 아니다.
따라서 절대오차/상대오차 중 큰 쪽을 기준(rel=1e-6)으로 비교한다.
"""
from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

from strategy.grid import compute_grid
from strategy.weights import load_weights

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT / "tests" / "golden"

EQUITY = Decimal("10000")
LEVERAGE = Decimal("20")
MAINT_MARGIN_RATE = Decimal("0.005")
SL_PCT = Decimal("0.03")

SHEETS = {
    "btc_long": dict(direction="long", base_price=Decimal("64000"), tick=Decimal("50")),
    "btc_short": dict(direction="short", base_price=Decimal("64000"), tick=Decimal("50")),
    "eth_long": dict(direction="long", base_price=Decimal("3000"), tick=Decimal("2.5")),
    "eth_short": dict(direction="short", base_price=Decimal("3000"), tick=Decimal("2.5")),
}

REL_TOL = Decimal("1e-6")
ABS_TOL = Decimal("1e-6")


def load_golden(slug: str) -> list[dict]:
    with open(GOLDEN_DIR / f"{slug}.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def assert_close(actual: Decimal, expected_str: str, label: str) -> None:
    expected = Decimal(expected_str)
    diff = abs(actual - expected)
    tol = max(ABS_TOL, REL_TOL * max(abs(actual), abs(expected)))
    assert diff <= tol, f"{label}: actual={actual} expected={expected} diff={diff} tol={tol}"


@pytest.mark.parametrize("slug", list(SHEETS.keys()))
def test_golden_sheet(slug: str) -> None:
    params = SHEETS[slug]
    weights = load_weights()
    rows = load_golden(slug)

    result = compute_grid(
        direction=params["direction"],
        base_price=params["base_price"],
        tick=params["tick"],
        weights=weights,
        equity=EQUITY,
        leverage=LEVERAGE,
        maint_margin_rate=MAINT_MARGIN_RATE,
        sl_pct=SL_PCT,
    )

    assert len(result) == 100 == len(rows)

    for i, (step, golden_row) in enumerate(zip(result, rows)):
        prefix = f"[{slug}] row={i} (major_tier={step.major_tier}, sub_step={step.sub_step})"
        assert step.major_tier == (i // 20) + 1, f"{prefix}: major_tier mismatch"
        assert step.sub_step == (i % 20) + 1, f"{prefix}: sub_step mismatch"

        assert_close(step.entry_price, golden_row["entry_price"], f"{prefix} entry_price")
        assert_close(step.step_qty, golden_row["step_qty"], f"{prefix} step_qty")
        assert_close(step.step_margin, golden_row["step_margin"], f"{prefix} step_margin")
        assert_close(step.cum_qty, golden_row["cum_qty"], f"{prefix} cum_qty")
        assert_close(step.cum_margin, golden_row["cum_margin"], f"{prefix} cum_margin")
        assert_close(step.avg_price, golden_row["avg_price"], f"{prefix} avg_price")
        assert_close(step.available_balance, golden_row["available_balance"], f"{prefix} available_balance")
        assert_close(step.liq_price, golden_row["liq_price"], f"{prefix} liq_price")
        assert_close(step.target_tp_price, golden_row["target_tp_price"], f"{prefix} target_tp_price")
        assert_close(step.sl_price, golden_row["sl_price"], f"{prefix} sl_price")


def test_weights_sum_is_17130() -> None:
    weights = load_weights()
    assert len(weights) == 100
    assert sum(weights) == Decimal("17130")
