"""RSI/ATR 진입 필터용 일봉 캔들 데이터 조달.

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


async def fetch_daily_candles(
    instrument: str,
    limit: int = 30,
    http_client: Optional[httpx.AsyncClient] = None,
) -> list[Candle]:
    """오래된 캔들이 먼저 오는 순서(바이낸스 기본 순서)로 반환한다."""
    symbol = to_binance_symbol(instrument)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        response = await client.get(
            f"{BINANCE_BASE_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": str(limit)},
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


def closed_candles(candles: list[Candle]) -> list[Candle]:
    """아직 마감 안 된(진행 중인) 마지막 봉을 제외한, 완결된 일봉만 반환한다.
    바이낸스 klines가 마지막 행으로 당일 진행 중 봉을 같이 주기 때문에 필요하다 —
    안 걸러내면 RSI/ATR 같은 일봉 지표가 하루 중에 계속 값이 뒤집힐 수 있다.
    `engine/entry_scheduler.py`(RSI)와 `engine/grid_setup.py`(ATR)가 공유해서 쓴다."""
    if not candles:
        return candles
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if candles[-1].open_time_ms + _ONE_DAY_MS > now_ms:
        return candles[:-1]
    return candles
