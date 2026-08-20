"""5k 프리셋(100단계 / 40배) 백테스트 — auto / both.

3k 때와 동일한 데이터·동일한 시뮬레이터를 쓰고 max_stage만 5로 바꾼다.
5k는 최소 시드가 커서 2,000 USDT로는 1년 내내 격자가 구성되지 않으므로,
"실제로 돌아가는 시드"를 함께 돌려 비교한다.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
from decimal import Decimal
from pathlib import Path

import backtest as B

BARS = B.load_bars()
RSI_MS, RSI_VALS = B.load_rsi_series()
FUNDING = B.load_funding()
MAX_STAGE, LEVERAGE = B.resolve_preset("5k", B.GRID_TICK)


def run(mode: str, seed: str, sl: bool = False, path: str = "standard", bars=None):
    bars = bars if bars is not None else BARS
    s = Decimal(seed)
    sim = B.Simulator(bars, RSI_MS, RSI_VALS, FUNDING, s, LEVERAGE, MAX_STAGE,
                      sl_enabled=sl, compound=False, path_mode=path)
    r = B.run_auto(sim, s, sl_enabled=sl) if mode == "auto" else B.run_both(sim, s)
    return r, s


def line(tag: str, r, s: Decimal) -> str:
    liq = dt.datetime.fromtimestamp(r.liquidated_at / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M") if r.liquidated_at else "—"
    first = dt.datetime.fromtimestamp(r.first_start_ms / 1000, dt.UTC).strftime("%Y-%m-%d") if r.first_start_ms else "기동 못함"
    return (f"{tag:<26} 최종 {r.final_balance:>10,.2f} ({(r.final_balance/s-1)*100:>+7.2f}%) | "
            f"고점 {r.peak_balance:>10,.2f} | MDD {r.max_drawdown_pct:>5.1f}% | "
            f"사이클 {r.cycles:>4} | 첫기동 {first:>10} | 청산 {liq:>16}")


def main() -> None:
    print(f"5k 프리셋: max_stage={MAX_STAGE} ({MAX_STAGE*20}단계), leverage={LEVERAGE}배")
    print(f"기간 {dt.datetime.fromtimestamp(BARS[0][0]/1000, dt.UTC):%Y-%m-%d} ~ "
          f"{dt.datetime.fromtimestamp(BARS[-1][0]/1000, dt.UTC):%Y-%m-%d}, "
          f"BTC {BARS[0][1]:,.0f} -> {BARS[-1][4]:,.0f}\n")

    results = {}
    print("--- 시드별 (표준 경로) ---")
    for seed in ["2000", "6000", "12000"]:
        for mode in ["auto", "both"]:
            r, s = run(mode, seed)
            results[f"{mode}@{seed}"] = r
            print(line(f"{mode}  시드 {seed}", r, s))
    print()

    print("--- SL 켠 auto (참고) ---")
    for seed in ["6000", "12000"]:
        r, s = run("auto", seed, sl=True)
        results[f"auto+SL@{seed}"] = r
        print(line(f"auto+SL  시드 {seed}", r, s))
    print()

    print("--- 봉 내부 경로 민감도 (시드 12000) ---")
    for mode in ["auto", "both"]:
        for path in ["lowfirst", "highfirst"]:
            r, s = run(mode, "12000", path=path)
            results[f"{mode}@12000/{path}"] = r
            print(line(f"{mode}  {path}", r, s))
    print()

    print("--- 마지막 사이클 (시드 12000) ---")
    for mode in ["auto", "both"]:
        r = results[f"{mode}@12000"]
        for t in r.trades[-4:]:
            print(f"  {mode:<5} {dt.datetime.fromtimestamp(t[0]/1000, dt.UTC):%Y-%m-%d %H:%M} "
                  f"{t[1]:<5} base={t[2]:>10,.1f} {t[3]:<11} 손익 {t[4]:>+11,.2f} 단계 {t[5]:>3} 잔고 {t[6]:>11,.2f}")
    print()

    # ---- 시작일 민감도 (시드 12000) ----
    print("--- 시작일 민감도: 2주 간격 (시드 12000) ---")
    step = 14 * 24 * 60
    starts = list(range(0, len(BARS) - 30 * 24 * 60, step))
    roll = []
    for mode in ["auto", "both"]:
        for s0 in starts:
            sub = BARS[s0:]
            r, s = run(mode, "12000", bars=sub)
            avail = (sub[-1][0] - sub[0][0]) / 86_400_000
            surv = (r.liquidated_at - sub[0][0]) / 86_400_000 if r.liquidated_at else avail
            roll.append({"mode": mode, "start": dt.datetime.fromtimestamp(sub[0][0]/1000, dt.UTC).strftime("%Y-%m-%d"),
                         "px": f"{sub[0][1]:,.0f}", "avail": round(avail, 1), "surv": round(surv, 1),
                         "liq": bool(r.liquidated_at), "peak": round(float(r.peak_balance)),
                         "final": round(float(r.final_balance))})
    for mode in ["auto", "both"]:
        rs = [x for x in roll if x["mode"] == mode]
        liq = [x for x in rs if x["liq"]]
        print(f"  {mode}: {len(rs)}개 중 {len(liq)}개 청산 ({len(liq)/len(rs)*100:.0f}%)")
        if liq:
            sv = [x["surv"] for x in liq]
            print(f"     청산까지 — 중앙값 {statistics.median(sv):.0f}일 / 최소 {min(sv):.0f} / 최대 {max(sv):.0f}")
        print(f"     최종자산 중앙값 {statistics.median([x['final'] for x in rs]):,.0f} "
              f"(시드 12,000 대비 {statistics.median([x['final'] for x in rs])/12000*100:.1f}%)")

    # ---- 저장 ----
    out = Path(__file__).parent
    with (out / "results5k.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "final", "peak", "min", "mdd_pct", "cycles", "tp", "sl", "hybrid",
                    "fees", "funding", "first_start_ms", "liq_ms", "liq_px", "max_steps"])
        for k, r in results.items():
            w.writerow([k, r.final_balance, r.peak_balance, r.min_balance, r.max_drawdown_pct,
                        r.cycles, r.tp_closes, r.sl_closes, r.hybrid_resets, r.fees, r.funding,
                        r.first_start_ms or "", r.liquidated_at or "", r.liquidation_price or "",
                        r.max_steps_filled])
    curves = {}
    for key in ("auto@12000", "both@12000"):
        curves["auto" if key.startswith("auto") else "both"] = [
            [dt.datetime.fromtimestamp(ms/1000, dt.UTC).strftime("%Y-%m-%d"), round(float(eq), 1)]
            for ms, eq in results[key].equity_curve]
    json.dump({"curves": curves, "rolling": roll}, (out / "viz5k.json").open("w"), ensure_ascii=False, separators=(",", ":"))
    print("\n저장: results5k.csv / viz5k.json")


if __name__ == "__main__":
    main()
