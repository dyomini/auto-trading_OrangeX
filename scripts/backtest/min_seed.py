"""프리셋별 최소 시드 실측 — `main.py`가 StartupError 없이 기동되는 가장 작은 equity.

`build_side()`가 None을 돌려주는 조건이 곧 `main.py`의 기동 거부 조건이다
(가용잔고 절삭 후에도 최소 주문 수량/금액 미달 단계가 남는 경우).
이분탐색은 단조성을 가정하므로, 여기서는 **선형 스캔으로 첫 통과점을 찾고
그 위쪽이 계속 통과하는지까지 확인**한다.

    python min_seed.py --preset 5k --price 64683.5
"""
from __future__ import annotations

import argparse
import decimal
from decimal import Decimal

import backtest as B


def feasible(preset: str, direction: str, price: Decimal, equity: Decimal) -> bool:
    max_stage, lev = B.resolve_preset(preset, B.GRID_TICK)
    try:
        return B.build_side(direction, price, equity, lev, B.load_weights(), max_stage) is not None
    except (decimal.InvalidOperation, ZeroDivisionError):
        # equity가 너무 작아 1단계 수량조차 0으로 절삭되면 compute_grid가 0으로 나눈다.
        # (봇도 같은 예외를 낼 것이다 — 여기서는 "기동 불가"로만 센다.)
        return False


def min_seed(preset: str, direction: str, price: Decimal, hi: int = 40000, step: int = 1) -> int | None:
    """coarse -> fine 2단 스캔으로 첫 통과 equity를 찾는다."""
    coarse = None
    for e in range(step, hi + 1, 50):
        if feasible(preset, direction, price, Decimal(e)):
            coarse = e
            break
    if coarse is None:
        return None
    for e in range(max(step, coarse - 50), coarse + 1, step):
        if feasible(preset, direction, price, Decimal(e)):
            return e
    return coarse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="5k")
    ap.add_argument("--price", required=True, type=str)
    ap.add_argument("--check-above", type=int, default=8, help="통과점 위쪽 몇 개 지점을 검증할지")
    a = ap.parse_args()

    price = Decimal(a.price)
    max_stage, lev = B.resolve_preset(a.preset, B.GRID_TICK)
    print(f"{a.preset} 프리셋: {max_stage*20}단계 / {lev}배 / 기준가 {price:,.1f}")

    for direction in ("short", "long"):
        m = min_seed(a.preset, direction, price)
        if m is None:
            print(f"  {direction:<5} 최소 시드: 찾지 못함(40,000까지 스캔)")
            continue
        # 단조성 확인
        bad = [e for e in range(m, m + a.check_above * 250 + 1, 250)
               if not feasible(a.preset, direction, price, Decimal(e))]
        side = B.build_side(direction, price, Decimal(m), lev,
                            B.load_weights(), max_stage)
        note = "단조 OK" if not bad else f"**비단조** 미통과 지점 {bad}"
        print(f"  {direction:<5} 최소 시드 {m:>6,} USDT | 실사용 {len(side.entries):>3}/{max_stage*20}단계 | "
              f"최심 {side.entries[-1]:,.1f} ({(side.entries[-1]/price-1)*100:+.2f}%) | {note}")
    print(f"  → DIRECTION=both 는 한쪽당 절반이므로 위 값의 2배가 필요하다.")


if __name__ == "__main__":
    main()
