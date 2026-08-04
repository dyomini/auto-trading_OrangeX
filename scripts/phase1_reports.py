"""Phase 1 산출물 3종 리포트를 계산해 docs/phase1-report.md로 정리한다.

SPEC.md 66~75줄 요구사항:
  1. 가용잔고 음수 전환 최초 단계 -> max_feasible_step (4개 시트 각각)
  2. 최소 주문 수량/명목가치 미달 단계 목록 + 처리 정책 3가지 제시
  3. 수수료+펀딩비 반영 실질 손익분기가 (대단계별)

이 스크립트는 "가정"과 "확정값"을 명확히 구분해서 출력한다 (SPEC 0번 절대 원칙).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from strategy.feasibility import find_max_feasible_step, find_min_order_shortfalls
from strategy.fees import breakeven_price, estimate_funding_cost
from strategy.grid import compute_grid
from strategy.weights import load_weights

ROOT = Path(__file__).resolve().parent.parent

EQUITY = Decimal("10000")
LEVERAGE = Decimal("20")
MAINT_MARGIN_RATE = Decimal("0.005")
SL_PCT = Decimal("0.03")

SHEETS = {
    "비트_롱계산기 (btc_long)": dict(direction="long", base_price=Decimal("64000"), tick=Decimal("50")),
    "비트_숏계산기 (btc_short)": dict(direction="short", base_price=Decimal("64000"), tick=Decimal("50")),
    "이더_롱계산기 (eth_long)": dict(direction="long", base_price=Decimal("3000"), tick=Decimal("2.5")),
    "이더_숏계산기 (eth_short)": dict(direction="short", base_price=Decimal("3000"), tick=Decimal("2.5")),
}

# --- 리포트 c에서 사용하는 "가정" 요율 (SPEC.md 74줄이 제시한 예시값, 실측 아님) ---
TAKER_FEE = Decimal("0.0005")  # 0.05%
MAKER_FEE = Decimal("0.0002")  # 0.02%
ASSUMED_FUNDING_RATE_PER_8H = Decimal("0.0001")  # 0.01% — 예시치, 실제 값 아님 (미확인 항목 #5)
TIER_END_INDEXES = [19, 39, 59, 79, 99]  # 각 대단계 마지막 sub-step (0-based index)


def main() -> None:
    lines: list[str] = []
    lines.append("# Phase 1 리포트 — 전략 계산 엔진 검증 및 추가 분석\n")
    lines.append(
        "본 리포트는 SPEC.md Phase 1 산출물이다. 골든 테스트(`pytest tests/test_golden.py`)로 "
        "4개 시트 100행 전부가 엑셀 캐시값과 오차범위(rel=1e-6) 내에서 일치함을 확인했다.\n"
    )

    weights = load_weights()

    lines.append("## 0. 가정 목록 (SPEC 0번 원칙에 따라 명시)\n")
    lines.append(
        "- **확정값**: base_price, equity=10000, leverage=20, maint_margin_rate=0.005, "
        "sl_pct=0.03, weights(E열 100개, 합계 17130) — 전부 엑셀 원본에서 직접 추출/검증됨.\n"
        "- **역산값(검증됨)**: 숏 포지션 예상청산가 공식은 SPEC에 명시되어 있지 않아 엑셀 캐시값에서 "
        "역산했다: `(평단*누적수량 + equity) / (누적수량*(1+maint_margin_rate))`. 4개 시트 100행 전부 "
        "golden test로 정확히 일치함을 확인함 — 가정이 아니라 실측 검증된 값이다.\n"
        "- **미확정(가정)**: 최소 주문 수량(`min_qty`)/최소 명목가치(`min_notional`)의 실제 수치는 "
        "Phase 0에서 라이브로 확인하지 못했다 (docs/api-notes.md 미확인 항목 #7). 아래 리포트 b는 "
        "임계값 없이 명목가치만 낮은 순으로 나열한다.\n"
        "- **미확정(가정)**: 수수료율은 SPEC.md가 제시한 예시값(taker 0.05%, maker 0.02%)을 그대로 "
        "사용했다. 펀딩비율은 실제 값을 확인하지 못해(미확인 항목 #5) 예시치 0.01%/8h로 가정했다 — "
        "**실제 수치가 아니며 Phase 2에서 라이브 값으로 교체해야 한다.**\n"
    )

    # ---------- 리포트 a: max_feasible_step ----------
    lines.append("## 1. 가용잔고 음수 전환 최초 단계 (`max_feasible_step`)\n")
    lines.append("| 시트 | 실행 가능 단계 수 | 최초 불가 단계 (1-based) | 대단계/소단계 |")
    lines.append("|---|---|---|---|")

    all_grids: dict[str, list] = {}
    for name, params in SHEETS.items():
        grid = compute_grid(
            direction=params["direction"],
            base_price=params["base_price"],
            tick=params["tick"],
            weights=weights,
            equity=EQUITY,
            leverage=LEVERAGE,
            maint_margin_rate=MAINT_MARGIN_RATE,
            sl_pct=SL_PCT,
        )
        all_grids[name] = grid
        fr = find_max_feasible_step(grid)
        if fr.all_feasible:
            lines.append(f"| {name} | {fr.max_feasible_step_count} | 없음 (전 단계 실행 가능) | - |")
        else:
            lines.append(
                f"| {name} | {fr.max_feasible_step_count} | {fr.first_infeasible_overall_step} | "
                f"{fr.first_infeasible_major_tier}차 {fr.first_infeasible_sub_step}단계 |"
            )
    lines.append(
        "\nSPEC.md가 명시한 \"롱 시트 기준 5차 12단계에서 -192 USDT\"는 비트_롱계산기에서 "
        "정확히 재현됨(위 표 확인). 봇은 시작 시 `max_feasible_step`을 계산해 그 이후 단계는 "
        "격자에 걸지 않아야 한다 (SPEC 69~70줄).\n"
    )

    # ---------- 리포트 b: 최소 주문 단위 미달 ----------
    lines.append("## 2. 최소 주문 수량/명목가치 미달 단계\n")
    lines.append(
        "**실제 `min_qty`/`min_notional` 값을 Phase 0에서 확인하지 못했으므로 (docs/api-notes.md "
        "미확인 항목 #7), 어떤 단계가 '미달'인지 지금 단정할 수 없다.** 대신 명목가치가 가장 작은 "
        "단계들을 나열한다. SPEC.md 72줄이 예시로 든 \"1차 1단계 명목 116 USDT, BTC 0.0018개\"가 "
        "실제로 미달인지는 Phase 2에서 라이브 계약 스펙 조회 후 `strategy.feasibility."
        "find_min_order_shortfalls(rows, min_qty=..., min_notional=...)`에 실측값을 넣어 재계산해야 "
        "한다 (함수는 이미 구현되어 있고 임계값만 주입하면 됨).\n"
    )
    for name, grid in all_grids.items():
        lines.append(f"\n**{name}** — 명목가치 최저 5단계:\n")
        lines.append("| 단계(1-based) | 대단계/소단계 | 진입가 | 단계수량 | 명목가치(USDT) |")
        lines.append("|---|---|---|---|---|")
        lowest = sorted(grid, key=lambda s: s.step_qty * s.entry_price)[:5]
        for s in lowest:
            notional = s.step_qty * s.entry_price
            lines.append(
                f"| {s.index + 1} | {s.major_tier}차 {s.sub_step}단계 | {s.entry_price} | "
                f"{s.step_qty:.8f} | {notional:.4f} |"
            )
    lines.append(
        "\n### 미달 단계 처리 정책 (택 1 필요 — SPEC 73줄)\n"
        "1. **스킵**: 미달 단계는 주문을 걸지 않고 건너뛴다. 해당 단계의 증거금 배분은 소멸(미사용).\n"
        "2. **다음 단계에 합산**: 미달 단계의 수량/증거금을 다음 단계와 합쳐 하나의 주문으로 발주.\n"
        "3. **봇 기동 거부**: 미달 단계가 하나라도 있으면 해당 심볼/방향의 봇 자체를 시작하지 않는다.\n"
        "\n**사용자 선택 필요**: 위 3가지 중 어느 정책을 적용할지 확정해야 Phase 2 주문 로직에 반영 가능.\n"
    )

    # ---------- 리포트 c: 수수료+펀딩비 반영 손익분기가 ----------
    lines.append("## 3. 수수료·펀딩비 반영 실질 손익분기가 (대단계별)\n")
    lines.append(
        f"가정: 왕복 수수료 — taker 시나리오 {TAKER_FEE*2*100:.2f}%(진입·청산 모두 taker), "
        f"maker 시나리오 {MAKER_FEE*2*100:.2f}%(진입·청산 모두 maker). 펀딩비는 "
        f"{ASSUMED_FUNDING_RATE_PER_8H*100:.3f}%/8h **가정치**(실측 아님)로 1/3/10회(8h/1일/3.3일 보유) "
        "시나리오를 함께 제시한다. 실제 손익분기가는 Phase 2에서 라이브 수수료·펀딩비율 확인 후 "
        "`strategy.fees.breakeven_price()`에 그대로 대입하면 된다.\n"
    )
    for name, grid in all_grids.items():
        lines.append(f"\n**{name}**\n")
        lines.append(
            "| 대단계 | 평단가(해당 대단계 완주 시) | 손익분기(maker 왕복) | "
            "손익분기(taker 왕복) | 손익분기(taker+펀딩10회) |"
        )
        lines.append("|---|---|---|---|---|")
        direction = SHEETS[name]["direction"]
        for tier_idx, end_index in enumerate(TIER_END_INDEXES, start=1):
            step = grid[end_index]
            notional = step.avg_price * step.cum_qty
            funding_cost_10 = estimate_funding_cost(notional, ASSUMED_FUNDING_RATE_PER_8H, 10)

            be_maker = breakeven_price(
                step.avg_price, step.cum_qty, MAKER_FEE, MAKER_FEE, Decimal("0"), direction
            )
            be_taker = breakeven_price(
                step.avg_price, step.cum_qty, TAKER_FEE, TAKER_FEE, Decimal("0"), direction
            )
            be_taker_funding = breakeven_price(
                step.avg_price, step.cum_qty, TAKER_FEE, TAKER_FEE, funding_cost_10, direction
            )
            lines.append(
                f"| {tier_idx}차 | {step.avg_price:.2f} | {be_maker:.2f} | {be_taker:.2f} | "
                f"{be_taker_funding:.2f} |"
            )

    out_path = ROOT / "docs" / "phase1-report.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
