"""짧은 구간에서 방어 수단별 생존 여부 비교.

`run_window.py`가 "기본 설정으로 그 날 살아남았나"를 보는 것이고, 이건 "어떻게 하면
살아남았나"를 보는 것이다 — SL 등록 / 사이징 축소(EQUITY_USDT를 잔고보다 작게) /
레버리지 축소를 각각 켜본다.

    python run_window_variants.py --data data-aug19 --from 2026-08-18T15:00 --to 2026-08-20T06:00
"""
from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
from pathlib import Path

import backtest as B

KST = dt.timezone(dt.timedelta(hours=9))


def kst(ms):
    return dt.datetime.fromtimestamp(ms / 1000, KST).strftime("%m-%d %H:%M") if ms else "—"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data-aug19")
    ap.add_argument("--from", dest="t0", required=True)
    ap.add_argument("--to", dest="t1", required=True)
    ap.add_argument("--balance", default="296")
    a = ap.parse_args()

    B.DATA = Path(__file__).parent / a.data
    bars_all = B.load_bars()
    rsi_ms, rsi_vals = B.load_rsi_series()
    funding = B.load_funding()

    def ms(s):
        return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=dt.UTC).timestamp() * 1000)

    bars = [b for b in bars_all if ms(a.t0) <= b[0] < ms(a.t1)]
    bal = Decimal(a.balance)

    def run(mode, preset, sizing, lev=None, sl=False, sl_tier=None):
        max_stage, leverage = B.resolve_preset(preset, B.GRID_TICK)
        if lev is not None:
            leverage = Decimal(lev)
        old_tier = B.MANDATORY_SL_MIN_TIER
        if sl_tier is not None:
            B.MANDATORY_SL_MIN_TIER = sl_tier
        try:
            sim = B.Simulator(bars, rsi_ms, rsi_vals, funding, Decimal(sizing), leverage,
                              max_stage, sl_enabled=sl, compound=False)
            return B.run_auto(sim, bal, sl_enabled=sl) if mode == "auto" else B.run_both(sim, bal)
        finally:
            B.MANDATORY_SL_MIN_TIER = old_tier

    print(f"구간 {kst(bars[0][0])} ~ {kst(bars[-1][0])} KST | 잔고 {bal} USDT\n")
    header = (f"{'변형':<40} {'최종':>9} {'수익률':>9} {'고점':>8} {'사이클':>5} "
              f"{'단계':>4} {'SL':>3} {'결과':>24}")
    print(header)
    print("-" * len(header))

    variants = [
        ("1k auto  기본(SL off)",            dict(mode="auto", preset="1k", sizing="296")),
        ("1k auto  SL on (min_tier=1)",      dict(mode="auto", preset="1k", sizing="296", sl=True, sl_tier=1)),
        ("1k auto  사이징 148 (잔고의 1/2)", dict(mode="auto", preset="1k", sizing="148")),
        ("1k auto  사이징 74 (1/4)",         dict(mode="auto", preset="1k", sizing="74")),
        ("1k auto  사이징 37 (1/8)",         dict(mode="auto", preset="1k", sizing="37")),
        ("1k auto  레버리지 10배",           dict(mode="auto", preset="1k", sizing="296", lev="10")),
        ("1k auto  레버리지 5배",            dict(mode="auto", preset="1k", sizing="296", lev="5")),
        ("1k both  기본",                    dict(mode="both", preset="1k", sizing="296")),
        ("1k both  사이징 148",              dict(mode="both", preset="1k", sizing="148")),
        ("1k both  사이징 74",               dict(mode="both", preset="1k", sizing="74")),
        ("3k auto  기본(기동 가능?)",        dict(mode="auto", preset="3k", sizing="296")),
    ]
    for label, kw in variants:
        r = run(**kw)
        if r.cycles == 0:
            outcome = "기동 못함(최소주문 미달)"
        elif r.liquidated_at:
            outcome = f"청산 {kst(r.liquidated_at)} @{r.liquidation_price:,.0f}"
        else:
            outcome = "생존"
        print(f"{label:<40} {r.final_balance:>9,.2f} {(r.final_balance/bal-1)*100:>+8.2f}% "
              f"{r.peak_balance:>8,.2f} {r.cycles:>5} {r.max_steps_filled:>4} {r.sl_closes:>3} {outcome:>24}")


if __name__ == "__main__":
    main()
