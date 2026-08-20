"""격자가 감당할 수 있는 폭 vs BTC가 실제로 움직인 폭.

봇이 청산되는 조건은 단순하다: **사이클 기준가에서 역방향으로 X% 움직이면 끝**이다
(X는 프리셋/레버리지가 정하는 값 — 1k/40배는 약 3.6%). 이 스크립트는 지난 1년 1분봉에서
"임의의 시점에 들어갔을 때 H시간 안에 역방향 X%가 나올 확률"을 센다.

    python move_stats.py --thresholds 2.4 3.6 5.0
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import backtest as B


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--thresholds", nargs="+", type=float, default=[2.4, 3.6, 5.0])
    ap.add_argument("--horizons", nargs="+", type=int, default=[1, 6, 24, 72, 168])
    a = ap.parse_args()

    B.DATA = Path(__file__).parent / a.data
    bars = B.load_bars()
    n = len(bars)
    px = [float(b[1]) for b in bars]
    hi = [float(b[2]) for b in bars]
    lo = [float(b[3]) for b in bars]

    print(f"1분봉 {n:,}개 / 임의 시점 진입 가정\n")
    head = f"{'구간':>6} | " + " | ".join(f"{t:>4.1f}% 상승  {t:>4.1f}% 하락" for t in a.thresholds)
    print(head)
    print("-" * len(head))
    for H in a.horizons:
        w = H * 60
        cells = []
        for t in a.thresholds:
            up_hits = dn_hits = total = 0
            # 슬라이딩 최대/최소 대신 단순 스텝 스캔 — 1분봉 1년이면 충분히 빠르다.
            step = 5    # 5분마다 표본
            for i in range(0, n - w, step):
                total += 1
                base = px[i]
                mx = max(hi[i:i + w])
                mn = min(lo[i:i + w])
                if (mx / base - 1) * 100 >= t:
                    up_hits += 1
                if (1 - mn / base) * 100 >= t:
                    dn_hits += 1
            cells.append(f"{up_hits/total*100:>10.1f}% {dn_hits/total*100:>11.1f}%")
        print(f"{H:>4}시간 | " + " | ".join(cells), flush=True)


if __name__ == "__main__":
    main()
