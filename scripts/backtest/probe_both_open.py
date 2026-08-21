"""both 모드의 마지막 상태 상세 — 미청산 보유(OPEN_AT_END)면 무엇을 얼마나 들고 끝났는지."""
from __future__ import annotations
import argparse, datetime as dt
from decimal import Decimal
from pathlib import Path
import backtest as B

ap = argparse.ArgumentParser()
ap.add_argument("--preset", default="5k")
ap.add_argument("--equity", default="12000")
a = ap.parse_args()

B.DATA = Path(__file__).parent / "data"
bars = B.load_bars(); rsi_ms, rsi_vals = B.load_rsi_series(); funding = B.load_funding()
max_stage, lev = B.resolve_preset(a.preset, B.GRID_TICK)
seed = Decimal(a.equity)
sim = B.Simulator(bars, rsi_ms, rsi_vals, funding, seed, lev, max_stage,
                  sl_enabled=False, compound=False, path_mode="standard")
r = B.run_both(sim, seed)
ts = lambda ms: dt.datetime.fromtimestamp(ms/1000, dt.UTC).strftime("%Y-%m-%d %H:%M") if ms else "—"
print(B.report(r) if hasattr(B, "report") else "")
last = r.trades[-5:]
for t in last:
    print(f"  {ts(t[0])} {t[1]:<6} base {t[2]:>10,.1f} {t[3]:<12} pnl {t[4]:>12,.2f} "
          f"단계 {t[5]:>3} 잔고/평가 {t[6]:>12,.2f}")
print(f"종가 {bars[-1][4]:,.1f} @ {ts(bars[-1][0])}")
