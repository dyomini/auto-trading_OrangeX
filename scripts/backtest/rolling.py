"""시작 시점 민감도 — 2주 간격으로 시작일을 옮겨가며 생존기간/최종자산 분포를 본다.

한 번의 백테스트 결과("2025-10-03에 청산됐다")가 시작일 운에 좌우된 것인지 아닌지를
가리기 위한 것이다. 각 시작일마다 데이터 끝까지 돌린다(청산되면 거기서 중단).
"""
from __future__ import annotations

import csv
import datetime as dt
import statistics
from decimal import Decimal
from pathlib import Path

import backtest as B


def main() -> None:
    bars = B.load_bars()
    rsi_ms, rsi_vals = B.load_rsi_series()
    funding = B.load_funding()
    max_stage, leverage = B.resolve_preset("3k", B.GRID_TICK)
    start = Decimal("2000")

    step = 14 * 24 * 60          # 2주(분)
    starts = list(range(0, len(bars) - 30 * 24 * 60, step))   # 최소 30일은 남는 시작점만

    rows = []
    for mode, fn, kwargs in [("auto", B.run_auto, {"sl_enabled": False}),
                             ("both", B.run_both, {})]:
        for s0 in starts:
            sub = bars[s0:]
            sim = B.Simulator(sub, rsi_ms, rsi_vals, funding, start, leverage, max_stage,
                              sl_enabled=kwargs.get("sl_enabled", False), compound=False)
            r = fn(sim, start, **kwargs)
            days_avail = (sub[-1][0] - sub[0][0]) / 86_400_000
            if r.liquidated_at:
                survived = (r.liquidated_at - sub[0][0]) / 86_400_000
            else:
                survived = days_avail
            rows.append({
                "mode": mode,
                "start": dt.datetime.fromtimestamp(sub[0][0] / 1000, dt.UTC).strftime("%Y-%m-%d"),
                "start_px": f"{sub[0][1]:,.0f}",
                "days_avail": round(days_avail, 1),
                "survived_days": round(survived, 1),
                "liquidated": bool(r.liquidated_at),
                "peak": r.peak_balance,
                "final": r.final_balance,
                "cycles": r.cycles,
            })
            print(f"{mode:<5} 시작 {rows[-1]['start']} (BTC {rows[-1]['start_px']:>8}) "
                  f"기간 {days_avail:>5.0f}일 | 생존 {survived:>5.0f}일 | "
                  f"{'청산' if r.liquidated_at else '생존'} | "
                  f"고점 {r.peak_balance:>9,.0f} 최종 {r.final_balance:>9,.0f} "
                  f"({(r.final_balance/start-1)*100:>+7.1f}%) 사이클 {r.cycles}", flush=True)

    with (Path(__file__).parent / "rolling.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print("\n=== 요약 ===")
    for mode in ("auto", "both"):
        rs = [r for r in rows if r["mode"] == mode]
        liq = [r for r in rs if r["liquidated"]]
        surv = [float(r["survived_days"]) for r in liq]
        print(f"{mode}: 시작 시점 {len(rs)}개 중 {len(liq)}개 청산 ({len(liq)/len(rs)*100:.0f}%)")
        if surv:
            print(f"   청산까지 생존일수 — 중앙값 {statistics.median(surv):.0f}일 / "
                  f"최소 {min(surv):.0f}일 / 최대 {max(surv):.0f}일")
        finals = [float(r["final"]) for r in rs]
        print(f"   최종자산 중앙값 {statistics.median(finals):,.0f} USDT "
              f"(최소 {min(finals):,.0f} / 최대 {max(finals):,.0f})")
        peaks = [float(r["peak"]) for r in rs]
        print(f"   고점 중앙값 {statistics.median(peaks):,.0f} USDT")


if __name__ == "__main__":
    main()
