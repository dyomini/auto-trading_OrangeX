"""RSI 진입 판단용 캔들 데이터 조달 (일봉 / 15분봉).

OrangeX에는 캔들(OHLC)/kline류 공개 엔드포인트가 존재하지 않는다 — 9개 후보
메서드명(get_tradingview_chart_data/get_candles/get_kline/... 등)을 전부 시도했으나
"No service found"였다(2026-07-30, scripts/orangex_probe_candles.py). 사용자 승인
(2026-07-30)에 따라 캔들 데이터만 바이낸스 공개 API(인증 불필요)로 보조 조달한다.
**실제 주문/체결/포지션은 여전히 전부 OrangeX에서만 발생** — 이 모듈은 RSI/ATR 계산을
위한 순수 시세 참고용이며 SPEC 3번 규칙(라이브 주문 실행 제한)과 무관하다.

바이낸스와 OrangeX의 가격이 완전히 일치하지는 않지만(거래소 간 스프레드), RSI/ATR은
추세/변동성을 보는 지표라 이 정도 오차는 SPEC의 진입 필터 목적에 허용 가능하다고
가정한다 — 이 가정 자체는 SPEC 0번 원칙에 따라 여기 명시한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx

BINANCE_BASE_URL = "https://api.binance.com"
_ONE_DAY_MS = 24 * 60 * 60 * 1000

# 지원하는 interval만 명시한다. 문자열을 파싱해서 ms를 유추하면("15m" -> 15*60*1000)
# 오타나 바이낸스가 안 받는 값도 그럴듯하게 통과해버린다 — 모르는 값은 추측하지 말고
# 막는다(SPEC 0번). 필요해지면 여기에 명시적으로 추가한다.
INTERVAL_TO_MS: dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": _ONE_DAY_MS,
}


class UnsupportedIntervalError(Exception):
    pass


def interval_to_ms(interval: str) -> int:
    try:
        return INTERVAL_TO_MS[interval]
    except KeyError as e:
        raise UnsupportedIntervalError(
            f"지원하지 않는 캔들 interval: {interval!r} — 가능한 값: {sorted(INTERVAL_TO_MS)}"
        ) from e

_INSTRUMENT_TO_BINANCE_SYMBOL: dict[str, str] = {
    "BTC-USDT-PERPETUAL": "BTCUSDT",
    "ETH-USDT-PERPETUAL": "ETHUSDT",
}


class UnsupportedInstrumentError(Exception):
    pass


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def to_binance_symbol(instrument: str) -> str:
    try:
        return _INSTRUMENT_TO_BINANCE_SYMBOL[instrument]
    except KeyError as e:
        raise UnsupportedInstrumentError(f"바이낸스 심볼 매핑이 없는 instrument: {instrument!r}") from e


async def fetch_candles(
    instrument: str,
    interval: str = "1d",
    limit: int = 30,
    http_client: Optional[httpx.AsyncClient] = None,
) -> list[Candle]:
    """오래된 캔들이 먼저 오는 순서(바이낸스 기본 순서)로 반환한다.

    15분봉과 일봉의 응답 행 구조는 동일하다(12칼럼, 여기서는 row[0..4]만 사용) —
    2026-08-17 실제 응답으로 확인했으므로 파싱 코드는 interval과 무관하게 같다."""
    interval_to_ms(interval)  # 지원 여부를 요청 전에 검증
    symbol = to_binance_symbol(instrument)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        response = await client.get(
            f"{BINANCE_BASE_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": str(limit)},
        )
        response.raise_for_status()
        raw = response.json()
    finally:
        if owns_client:
            await client.aclose()

    return [
        Candle(
            open_time_ms=int(row[0]),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
        )
        for row in raw
    ]


async def fetch_daily_candles(
    instrument: str,
    limit: int = 30,
    http_client: Optional[httpx.AsyncClient] = None,
) -> list[Candle]:
    """`fetch_candles(..., interval="1d")`의 얇은 래퍼 — 기존 호출부 하위호환용."""
    return await fetch_candles(instrument, interval="1d", limit=limit, http_client=http_client)


def closed_candles(candles: list[Candle], interval_ms: int = _ONE_DAY_MS) -> list[Candle]:
    """아직 마감 안 된(진행 중인) 마지막 봉을 제외한, 완결된 봉만 반환한다.
    바이낸스 klines가 마지막 행으로 진행 중인 봉을 같이 주기 때문에 필요하다.

    **15분봉에서는 이 필터가 일봉보다 훨씬 중요하다** — 진행 중 봉을 포함하면 RSI가
    15분 안에도 여러 번 뒤집혀 방향 판정이 요동친다(`engine/direction_selector.py`).
    `interval_ms`는 `interval_to_ms()`로 얻어서 넘긴다. 기본값이 일봉인 이유는 기존
    호출부(`engine/entry_scheduler.py`) 하위호환 때문이다."""
    if not candles:
        return candles
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if candles[-1].open_time_ms + interval_ms > now_ms:
        return candles[:-1]
    return candles
