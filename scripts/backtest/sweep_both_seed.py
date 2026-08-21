"""both 모드 시드 스윕 — "최소 시드 vs 여유 시드"의 결과 차이가 진짜인지 확인한다.

시드는 포지션 크기에 비례하므로 이론상 결과는 시드에 거의 무관해야 하는데,
qty_step(0.001 BTC) 내림과 최소 주문금액(10 USDT) 때문에 격자 모양이 불연속으로 바뀐다.
그 불연속이 생존/청산을 가르는지 보려고 시드만 바꿔가며 1년 구간을 돌린다.

    python sweep_both_seed.py --preset 5k --seeds 10108,11000,12000
"""
from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal
from pathlib import Path

import backtest as B


def day(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d") if ms else "—"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="5k")
    ap.add_argument("--seeds", default="10108,11000,12000,13000,14000,16000,20000")
    ap.add_argument("--data", default="data")
    a = ap.parse_args()

    B.DATA = Path(__file__).parent / a.data
    bars = B.load_bars()
    rsi_ms, rsi_vals = B.load_rsi_series()
    funding = B.load_funding()
    max_stage, leverage = B.resolve_preset(a.preset, B.GRID_TICK)

    print(f"{a.preset} both / {max_stage*20}단계 / {leverage}배 / 1년 standard 경로\n")
    for s in a.seeds.split(","):
        seed = Decimal(s.strip())
        side_eq = seed / 2
        sd = B.build_side("short", Decimal(str(bars[0][1])), side_eq, leverage,
                          B.load_weights(), max_stage)
        steps = f"{len(sd.entries)}/{max_stage*20}" if sd else "기동불가"
        sim = B.Simulator(bars, rsi_ms, rsi_vals, funding, seed, leverage, max_stage,
                          sl_enabled=False, compound=False, path_mode="standard")
        r = B.run_both(sim, seed)
        open_end = any(t[3] == "OPEN_AT_END" for t in r.trades)
        liq = f"청산 {day(r.liquidated_at)} @{r.liquidation_price:,.0f}" if r.liquidated_at else "무청산"
        print(f"  시드 {seed:>7,} (한쪽 {side_eq:>7,.0f}, 초기격자 {steps:>7}) | "
              f"최종 {r.final_balance:>10,.0f} ({(r.final_balance/seed-1)*100:>+7.1f}%) | "
              f"고점 {r.peak_balance:>10,.0f} | MDD {r.max_drawdown_pct:>5.1f}% | "
              f"사이클 {r.cycles:>4} | 첫기동 {day(r.first_start_ms)} | "
              f"{'미청산보유' if open_end else '보유없음  '} | {liq}", flush=True)


if __name__ == "__main__":
    main()
