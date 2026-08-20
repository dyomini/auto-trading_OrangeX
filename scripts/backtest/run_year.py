"""프리셋·시드를 인자로 받아 1년 구간을 돌리는 드라이버 (auto/both + 시작일 민감도).

`backtest.py`의 main()은 3k/2000 USDT로 고정돼 있고 `run5k.py`는 5k 전용이다. 이건
"내 실제 시드로는 어떻게 되나"를 보기 위한 일반화 버전이다.

    python run_year.py --preset 1k --equity 296
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import statistics
from decimal import Decimal
from pathlib import Path

import backtest as B


def day(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d") if ms else "—"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="1k")
    ap.add_argument("--equity", default="296")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default=None, help="결과 CSV 접미사 (기본: preset+equity)")
    a = ap.parse_args()

    B.DATA = Path(__file__).parent / a.data
    bars = B.load_bars()
    rsi_ms, rsi_vals = B.load_rsi_series()
    funding = B.load_funding()
    max_stage, leverage = B.resolve_preset(a.preset, B.GRID_TICK)
    seed = Decimal(a.equity)

    print(f"1분봉 {len(bars):,}개 {day(bars[0][0])} ~ {day(bars[-1][0])} UTC | "
          f"BTC {bars[0][1]:,.0f} -> {bars[-1][4]:,.0f}")
    print(f"{a.preset} 프리셋: {max_stage*20}단계 / {leverage}배 / 시드 {seed} USDT\n")

    def sim_for(path="standard", sl=False, sizing=None, sub=None):
        return B.Simulator(sub if sub is not None else bars, rsi_ms, rsi_vals, funding,
                           sizing or seed, leverage, max_stage, sl_enabled=sl,
                           compound=False, path_mode=path)

    rows = []
    print("=== 전체 구간 ===")
    for label, mode, kw in [("auto (SL off)", "auto", dict(sl=False)),
                            ("auto (SL on)", "auto", dict(sl=True)),
                            ("both", "both", {}),
                            ("auto lowfirst", "auto", dict(sl=False, path="lowfirst")),
                            ("auto highfirst", "auto", dict(sl=False, path="highfirst")),
                            ("both lowfirst", "both", dict(path="lowfirst")),
                            ("both highfirst", "both", dict(path="highfirst"))]:
        sim = sim_for(path=kw.get("path", "standard"), sl=kw.get("sl", False))
        r = B.run_auto(sim, seed, sl_enabled=kw.get("sl", False)) if mode == "auto" else B.run_both(sim, seed)
        rows.append((label, r))
        liq = f"청산 {day(r.liquidated_at)} @{r.liquidation_price:,.0f}" if r.liquidated_at else "청산없음"
        print(f"  {label:<16} 최종 {r.final_balance:>9,.2f} ({(r.final_balance/seed-1)*100:>+8.2f}%) | "
              f"고점 {r.peak_balance:>9,.2f} | MDD {r.max_drawdown_pct:>5.1f}% | "
              f"사이클 {r.cycles:>5} | 최대단계 {r.max_steps_filled:>3} | {liq}", flush=True)
        if r.liquidated_at and r.first_start_ms:
            print(f"      생존 {(r.liquidated_at - r.first_start_ms)/86_400_000:.1f}일 "
                  f"(첫 기동 {day(r.first_start_ms)})")

    print("\n=== 시작일 민감도 (2주 간격) ===")
    step = 14 * 24 * 60
    starts = list(range(0, len(bars) - 30 * 24 * 60, step))
    roll = []
    for mode in ("auto", "both"):
        for s0 in starts:
            sub = bars[s0:]
            sim = sim_for(sub=sub)
            r = B.run_auto(sim, seed, sl_enabled=False) if mode == "auto" else B.run_both(sim, seed)
            avail = (sub[-1][0] - sub[0][0]) / 86_400_000
            surv = (r.liquidated_at - sub[0][0]) / 86_400_000 if r.liquidated_at else avail
            roll.append((mode, day(sub[0][0]), avail, surv, bool(r.liquidated_at),
                         r.peak_balance, r.final_balance, r.cycles))
            print(f"  {mode:<5} 시작 {day(sub[0][0])} 기간 {avail:>5.0f}일 | 생존 {surv:>5.0f}일 | "
                  f"{'청산' if r.liquidated_at else '생존'} | 고점 {r.peak_balance:>9,.0f} "
                  f"최종 {r.final_balance:>9,.0f} ({(r.final_balance/seed-1)*100:>+7.1f}%) "
                  f"사이클 {r.cycles}", flush=True)

    print("\n=== 요약 ===")
    for mode in ("auto", "both"):
        rs = [x for x in roll if x[0] == mode]
        liq = [x for x in rs if x[4]]
        print(f"{mode}: {len(rs)}개 시작점 중 {len(liq)}개 청산 ({len(liq)/len(rs)*100:.0f}%)")
        if liq:
            s = [x[3] for x in liq]
            print(f"   청산까지 생존 — 중앙값 {statistics.median(s):.0f}일 / 최소 {min(s):.0f} / 최대 {max(s):.0f}")
        f = [float(x[6]) for x in rs]
        print(f"   최종 중앙값 {statistics.median(f):,.0f} USDT (최소 {min(f):,.0f} / 최대 {max(f):,.0f})")

    tag = a.out or f"{a.preset}-{a.equity}"
    with (Path(__file__).parent / f"results-{tag}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "final", "peak", "min", "mdd_pct", "cycles", "tp", "sl", "hybrid",
                    "fees", "funding", "first_start_ms", "liq_ms", "liq_px", "max_steps"])
        for label, r in rows:
            w.writerow([label, r.final_balance, r.peak_balance, r.min_balance, r.max_drawdown_pct,
                        r.cycles, r.tp_closes, r.sl_closes, r.hybrid_resets, r.fees, r.funding,
                        r.first_start_ms or "", r.liquidated_at or "", r.liquidation_price or "",
                        r.max_steps_filled])
    with (Path(__file__).parent / f"rolling-{tag}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["mode", "start", "days_avail", "survived_days", "liquidated", "peak", "final", "cycles"])
        w.writerows(roll)
    print(f"\n저장: results-{tag}.csv / rolling-{tag}.csv")


if __name__ == "__main__":
    main()
