"""짧은 구간(하루~며칠) 백테스트 드라이버 — "그 날 살아남았나"를 보는 용도.

1년 백테스트(`backtest.py` main)와 **같은 시뮬레이터**를 쓰고 데이터 구간과 프리셋/시드만
바꾼다. 하루 단위 결과는 시작 시각 운에 크게 좌우되므로 시작 시각을 30분 간격으로
옮겨가며 분포도 함께 낸다.

    python run_window.py --data data-aug19 --preset 1k --equity 296 \
        --from 2026-08-18T15:00 --to 2026-08-19T15:00
"""
from __future__ import annotations

import argparse
import datetime as dt
import statistics
from decimal import Decimal
from pathlib import Path

import backtest as B

KST = dt.timezone(dt.timedelta(hours=9))


def kst(ms: int | None) -> str:
    if not ms:
        return "—"
    return dt.datetime.fromtimestamp(ms / 1000, KST).strftime("%m-%d %H:%M")


def slice_bars(bars, start_ms, end_ms):
    return [b for b in bars if start_ms <= b[0] < end_ms]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data-aug19")
    ap.add_argument("--preset", default="1k")
    ap.add_argument("--equity", default="296")
    ap.add_argument("--from", dest="t0", required=True, help="UTC 'YYYY-MM-DDTHH:MM'")
    ap.add_argument("--to", dest="t1", required=True)
    ap.add_argument("--roll-minutes", type=int, default=30)
    a = ap.parse_args()

    B.DATA = Path(__file__).parent / a.data
    bars_all = B.load_bars()
    rsi_ms, rsi_vals = B.load_rsi_series()
    funding = B.load_funding()

    def ms(s):
        return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=dt.UTC).timestamp() * 1000)

    t0, t1 = ms(a.t0), ms(a.t1)
    bars = slice_bars(bars_all, t0, t1)
    if not bars:
        raise SystemExit("구간에 1분봉이 없다")

    max_stage, leverage = B.resolve_preset(a.preset, B.GRID_TICK)
    seed = Decimal(a.equity)
    o, hi, lo, c = bars[0][1], max(b[2] for b in bars), min(b[3] for b in bars), bars[-1][4]
    print(f"구간 {kst(bars[0][0])} ~ {kst(bars[-1][0])} KST  ({len(bars):,}개 1분봉)")
    print(f"BTC 시가 {o:,.1f} / 고가 {hi:,.1f} / 저가 {lo:,.1f} / 종가 {c:,.1f}"
          f"  → 시가대비 {c-o:+,.1f} ({(c/o-1)*100:+.2f}%), 저가→고가 {hi-lo:+,.1f}")
    print(f"{a.preset} 프리셋: {max_stage*20}단계 / {leverage}배 / 시드 {seed} USDT\n")

    # --- 1) 구간 시작에 그대로 기동 ---
    print("=== 구간 시작에 기동 (봉내부 경로 가정 3종) ===")
    for path in ("standard", "lowfirst", "highfirst"):
        for mode in ("auto", "both"):
            sim = B.Simulator(bars, rsi_ms, rsi_vals, funding, seed, leverage, max_stage,
                              sl_enabled=False, compound=False, path_mode=path)
            r = B.run_auto(sim, seed, sl_enabled=False) if mode == "auto" else B.run_both(sim, seed)
            print(f"  {mode:<5} {path:<10} 최종 {r.final_balance:>9,.2f} "
                  f"({(r.final_balance/seed-1)*100:>+7.2f}%) | 고점 {r.peak_balance:>8,.2f} | "
                  f"MDD {r.max_drawdown_pct:>5.1f}% | 사이클 {r.cycles:>3} | "
                  f"최대단계 {r.max_steps_filled:>2} | "
                  f"{'청산 ' + kst(r.liquidated_at) + f' @{r.liquidation_price:,.0f}' if r.liquidated_at else '청산없음'}")
        print()

    # --- 2) 사이클 상세 (standard 경로) ---
    for mode in ("auto", "both"):
        sim = B.Simulator(bars, rsi_ms, rsi_vals, funding, seed, leverage, max_stage,
                          sl_enabled=False, compound=False)
        r = B.run_auto(sim, seed, sl_enabled=False) if mode == "auto" else B.run_both(sim, seed)
        print(f"=== {mode} 사이클 상세 (standard) — 총 {len(r.trades)}건 ===")
        for t in r.trades[:40]:
            print(f"  {kst(t[0])} {t[1]:<5} base={t[2]:>9,.1f} {t[3]:<11} "
                  f"손익 {t[4]:>+9,.2f}  단계 {t[5]:>2}  잔고 {t[6]:>9,.2f}")
        if len(r.trades) > 40:
            print(f"  ... 이하 {len(r.trades)-40}건 생략")
        print()

    # --- 3) 시작 시각 민감도 ---
    step = a.roll_minutes
    starts = list(range(0, max(1, len(bars) // 2), step))
    print(f"=== 시작 시각 민감도 ({step}분 간격 {len(starts)}개, 각각 구간 끝까지) ===")
    for mode in ("auto", "both"):
        rows = []
        for s0 in starts:
            sub = bars[s0:]
            sim = B.Simulator(sub, rsi_ms, rsi_vals, funding, seed, leverage, max_stage,
                              sl_enabled=False, compound=False)
            r = B.run_auto(sim, seed, sl_enabled=False) if mode == "auto" else B.run_both(sim, seed)
            rows.append((sub[0][0], r))
        liq = [r for _, r in rows if r.liquidated_at]
        finals = [float(r.final_balance) for _, r in rows]
        print(f"  {mode}: {len(rows)}개 중 청산 {len(liq)}개 ({len(liq)/len(rows)*100:.0f}%) | "
              f"최종 중앙값 {statistics.median(finals):,.1f} "
              f"(최소 {min(finals):,.1f} / 최대 {max(finals):,.1f})")
        for ms0, r in rows:
            flag = f"청산 {kst(r.liquidated_at)} @{r.liquidation_price:,.0f}" if r.liquidated_at else "생존"
            print(f"    시작 {kst(ms0)}  최종 {r.final_balance:>9,.2f} "
                  f"({(r.final_balance/seed-1)*100:>+7.2f}%)  사이클 {r.cycles:>3}  최대단계 {r.max_steps_filled:>2}  {flag}")
        print()


if __name__ == "__main__":
    main()
