"""백테스트 데이터 수집.

 - 1분봉(체결 경로용): Binance USDT-M 선물 BTCUSDT (봇이 거래하는 것과 같은 무기한 선물 성격)
 - 15분봉(RSI 방향판정용): Binance 현물 BTCUSDT — engine/direction_selector.py가
   strategy/market_data.py를 통해 실제로 쓰는 소스와 동일(api.binance.com)
 - 펀딩비: Binance USDT-M 선물 실제 정산 이력(8시간 주기)

OrangeX에는 캔들 엔드포인트가 없어서 바이낸스를 쓴다(CLAUDE.md, 사용자 승인됨).

    python fetch_data.py                                   # 기본: 최근 1년 -> data/
    python fetch_data.py --start 2026-08-17 --end 2026-08-20 --out data-yesterday
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time
from pathlib import Path

import httpx

DAY_MS = 24 * 60 * 60 * 1000


def to_ms(date_str: str) -> int:
    """'YYYY-MM-DD' 또는 'YYYY-MM-DDTHH:MM' (UTC 기준)을 epoch ms로."""
    fmt = "%Y-%m-%dT%H:%M" if "T" in date_str else "%Y-%m-%d"
    return int(dt.datetime.strptime(date_str, fmt).replace(tzinfo=dt.UTC).timestamp() * 1000)


def fetch_klines(base, path, symbol, interval, start_ms, end_ms, out_file):
    client = httpx.Client(timeout=60)
    rows = []
    cur = start_ms
    step_ms = {"1m": 60_000, "15m": 900_000}[interval]
    while cur < end_ms:
        for attempt in range(5):
            try:
                r = client.get(f"{base}{path}", params={
                    "symbol": symbol, "interval": interval,
                    "startTime": cur, "endTime": end_ms, "limit": 1500 if "fapi" in base else 1000,
                })
                r.raise_for_status()
                batch = r.json()
                break
            except Exception as e:
                print(f"  retry {attempt}: {e!r}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError("반복 실패")
        if not batch:
            break
        rows.extend(batch)
        cur = int(batch[-1][0]) + step_ms
        if len(rows) % 100000 < 1500:
            print(f"  {out_file.name}: {len(rows)}행", flush=True)
    client.close()

    with out_file.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time_ms", "open", "high", "low", "close"])
        seen = set()
        for row in rows:
            t = int(row[0])
            if t in seen or t >= end_ms:
                continue
            seen.add(t)
            w.writerow([t, row[1], row[2], row[3], row[4]])
    print(f"{out_file.name}: {len(seen)}행 저장", flush=True)


def fetch_funding(start_ms, end_ms, out_file):
    client = httpx.Client(timeout=60)
    rows, cur = [], start_ms
    while cur < end_ms:
        r = client.get("https://fapi.binance.com/fapi/v1/fundingRate",
                       params={"symbol": "BTCUSDT", "startTime": cur, "endTime": end_ms, "limit": 1000})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        cur = int(batch[-1]["fundingTime"]) + 1
    client.close()
    with out_file.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["funding_time_ms", "funding_rate"])
        for row in rows:
            w.writerow([row["fundingTime"], row["fundingRate"]])
    print(f"funding: {len(rows)}행 저장", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="UTC 시작 (기본: end - 365일)")
    ap.add_argument("--end", help="UTC 끝 (기본: 오늘 00:00 UTC)")
    ap.add_argument("--out", default="data", help="저장 디렉터리 (scripts/backtest/ 기준 상대경로)")
    a = ap.parse_args()

    end_ms = to_ms(a.end) if a.end else int(
        dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    start_ms = to_ms(a.start) if a.start else end_ms - 365 * DAY_MS

    out = Path(__file__).parent / a.out
    out.mkdir(exist_ok=True)
    print(f"수집 구간 {dt.datetime.fromtimestamp(start_ms/1000, dt.UTC)} ~ "
          f"{dt.datetime.fromtimestamp(end_ms/1000, dt.UTC)} UTC -> {out}")

    fetch_funding(start_ms, end_ms, out / "funding.csv")
    # RSI(14)는 마감된 15분봉 15개가 필요하다 — 워밍업으로 30봉 앞에서부터 받는다.
    fetch_klines("https://api.binance.com", "/api/v3/klines", "BTCUSDT", "15m",
                 start_ms - 30 * 900_000, end_ms, out / "spot_15m.csv")
    fetch_klines("https://fapi.binance.com", "/fapi/v1/klines", "BTCUSDT", "1m",
                 start_ms, end_ms, out / "fut_1m.csv")


if __name__ == "__main__":
    main()
