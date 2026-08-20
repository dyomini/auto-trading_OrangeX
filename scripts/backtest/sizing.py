"""잔고는 2000 USDT 그대로 두고 EQUITY_USDT(격자 사이징)만 낮췄을 때의 생존 분석.

봇은 `settings.equity_usdt`로만 수량을 정하고 실제 잔고는 보지 않는다 — 즉 잔고를
사이징보다 크게 두면 그 차액이 그대로 청산까지의 완충이 된다.
"""
import datetime as dt
from decimal import Decimal
import backtest as B

bars = B.load_bars(); rsi_ms, rsi_vals = B.load_rsi_series(); funding = B.load_funding()
max_stage, lev = B.resolve_preset("3k", B.GRID_TICK)
BAL = Decimal("2000")

print(f"{'모드':<6} {'EQUITY_USDT':>11} {'배수':>5} {'최종':>10} {'수익률':>9} {'고점':>9} {'MDD':>7} {'사이클':>6} {'청산일':>12}")
for mode, fn, kw in [("auto", B.run_auto, {"sl_enabled": False}), ("both", B.run_both, {})]:
    for sz in ["2000", "1000", "500", "250", "125"]:
        sim = B.Simulator(bars, rsi_ms, rsi_vals, funding, Decimal(sz), lev, max_stage,
                          sl_enabled=kw.get("sl_enabled", False), compound=False)
        r = fn(sim, BAL, **kw)
        liq = dt.datetime.fromtimestamp(r.liquidated_at/1000, dt.UTC).strftime("%Y-%m-%d") if r.liquidated_at else "청산없음"
        print(f"{mode:<6} {sz:>11} {float(BAL)/float(sz):>4.0f}x {r.final_balance:>10,.0f} "
              f"{(r.final_balance/BAL-1)*100:>+8.1f}% {r.peak_balance:>9,.0f} {r.max_drawdown_pct:>6.1f}% "
              f"{r.cycles:>6} {liq:>12}")
