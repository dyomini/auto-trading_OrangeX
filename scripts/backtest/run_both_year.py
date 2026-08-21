"""DIRECTION=both 전용 1년 드라이버 — 최소 시드 검증용.

`run_year.py`는 auto 4종을 같이 돌려서 both만 보고 싶을 때 낭비가 크다. 이건 both만
경로 3종 + 시작일 민감도로 돌리고, 미청산 보유(OPEN_AT_END)와 증거금부족 거부 횟수를
함께 보고한다(둘 다 "생존"을 잘못 읽게 만드는 항목이라 표에 반드시 남긴다).

    python run_both_year.py --preset 5k --equity 10108
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
    ap.add_argument("--preset", default="5k")
    ap.add_argument("--equity", default="10108")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    B.DATA = Path(__file__).parent / a.data
    bars = B.load_bars()
    rsi_ms, rsi_vals = B.load_rsi_series()
    funding = B.load_funding()
    max_stage, leverage = B.resolve_preset(a.preset, B.GRID_TICK)
    seed = Decimal(a.equity)

    print(f"1분봉 {len(bars):,}개 {day(bars[0][0])} ~ {day(bars[-1][0])} UTC | "
          f"BTC {bars[0][1]:,.0f} -> {bars[-1][4]:,.0f}")
    print(f"{a.preset} 프리셋 both: {max_stage*20}단계 / {leverage}배 / 시드 {seed} "
          f"(한쪽당 {seed/2}) USDT\n")

    rows = []
    print("=== 전체 구간 (both) ===")
    for path in ("standard", "lowfirst", "highfirst"):
        sim = B.Simulator(bars, rsi_ms, rsi_vals, funding, seed, leverage, max_stage,
                          sl_enabled=False, compound=False, path_mode=path)
        r = B.run_both(sim, seed)
        rows.append((path, r))
        liq = f"청산 {day(r.liquidated_at)} @{r.liquidation_price:,.0f}" if r.liquidated_at else "청산없음"
        open_end = any(t[3] == "OPEN_AT_END" for t in r.trades)
        print(f"  {path:<10} 최종 {r.final_balance:>10,.2f} ({(r.final_balance/seed-1)*100:>+8.2f}%) | "
              f"고점 {r.peak_balance:>10,.2f} | 저점 {r.min_balance:>10,.2f} | MDD {r.max_drawdown_pct:>5.1f}% | "
              f"사이클 {r.cycles:>4} (익절 {r.tp_closes}) | 최대단계 {r.max_steps_filled:>3} | "
              f"수수료 {r.fees:>9,.0f} | 펀딩 {r.funding:>8,.0f} | 증거금거부 {r.margin_rejects:>3} | "
              f"첫기동 {day(r.first_start_ms)} | {'미청산보유(OPEN_AT_END)' if open_end else '전부 청산완료'} | {liq}",
              flush=True)

    print("\n=== 시작일 민감도 (2주 간격, standard) ===")
    step = 14 * 24 * 60
    starts = list(range(0, len(bars) - 30 * 24 * 60, step))
    roll = []
    for s0 in starts:
        sub = bars[s0:]
        sim = B.Simulator(sub, rsi_ms, rsi_vals, funding, seed, leverage, max_stage,
                          sl_enabled=False, compound=False, path_mode="standard")
        r = B.run_both(sim, seed)
        avail = (sub[-1][0] - sub[0][0]) / 86_400_000
        surv = (r.liquidated_at - sub[0][0]) / 86_400_000 if r.liquidated_at else avail
        open_end = any(t[3] == "OPEN_AT_END" for t in r.trades)
        roll.append(("both", day(sub[0][0]), avail, surv, bool(r.liquidated_at),
                     r.peak_balance, r.final_balance, r.cycles, day(r.first_start_ms), open_end,
                     r.margin_rejects))
        print(f"  시작 {day(sub[0][0])} 기간 {avail:>5.0f}일 | 첫기동 {day(r.first_start_ms)} | "
              f"생존 {surv:>5.0f}일 | {'청산' if r.liquidated_at else '생존'} | "
              f"고점 {r.peak_balance:>10,.0f} 최종 {r.final_balance:>10,.0f} "
              f"({(r.final_balance/seed-1)*100:>+7.1f}%) 사이클 {r.cycles:>4}"
              f"{' | 미청산보유' if open_end else ''}", flush=True)

    print("\n=== 요약 ===")
    liq = [x for x in roll if x[4]]
    print(f"both: {len(roll)}개 시작점 중 {len(liq)}개 청산 ({len(liq)/len(roll)*100:.0f}%)")
    if liq:
        s = [x[3] for x in liq]
        print(f"   청산까지 생존 — 중앙값 {statistics.median(s):.0f}일 / 최소 {min(s):.0f} / 최대 {max(s):.0f}")
    f = [float(x[6]) for x in roll]
    print(f"   최종 중앙값 {statistics.median(f):,.0f} USDT (최소 {min(f):,.0f} / 최대 {max(f):,.0f})")
    print(f"   미청산 보유로 끝난 시작점 {sum(1 for x in roll if x[9])}개")

    tag = a.out or f"both-{a.preset}-{a.equity}"
    with (Path(__file__).parent / f"results-{tag}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "final", "peak", "min", "mdd_pct", "cycles", "tp", "fees", "funding",
                    "margin_rejects", "first_start_ms", "liq_ms", "liq_px", "max_steps", "open_at_end"])
        for path, r in rows:
            w.writerow([path, r.final_balance, r.peak_balance, r.min_balance, r.max_drawdown_pct,
                        r.cycles, r.tp_closes, r.fees, r.funding, r.margin_rejects,
                        r.first_start_ms or "", r.liquidated_at or "", r.liquidation_price or "",
                        r.max_steps_filled, any(t[3] == "OPEN_AT_END" for t in r.trades)])
    with (Path(__file__).parent / f"rolling-{tag}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["mode", "start", "days_avail", "survived_days", "liquidated", "peak", "final",
                    "cycles", "first_start", "open_at_end", "margin_rejects"])
        w.writerows(roll)
    print(f"\n저장: results-{tag}.csv / rolling-{tag}.csv")


if __name__ == "__main__":
    main()
