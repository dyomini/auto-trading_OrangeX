"""strategy/market_data.py 유닛 테스트 — httpx.MockTransport로 실제 네트워크 없이 검증
(tests/test_orangex_client.py와 동일한 패턴)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from strategy.market_data import Candle, UnsupportedInstrumentError, closed_candles, fetch_daily_candles, to_binance_symbol

ONE_DAY_MS = 24 * 60 * 60 * 1000

RAW_KLINE_ROW = [
    1499040000000,
    "42000.10",
    "42500.50",
    "41800.00",
    "42300.25",
    "148976.11427815",
    1499644799999,
    "2434.19055334",
    308,
    "1756.87402397",
    "28.55912154",
    "17928899.62484339",
]


def make_mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_to_binance_symbol_maps_known_instruments():
    assert to_binance_symbol("BTC-USDT-PERPETUAL") == "BTCUSDT"
    assert to_binance_symbol("ETH-USDT-PERPETUAL") == "ETHUSDT"


def test_to_binance_symbol_raises_for_unknown_instrument():
    with pytest.raises(UnsupportedInstrumentError):
        to_binance_symbol("DOGE-USDT-PERPETUAL")


@pytest.mark.asyncio
async def test_fetch_daily_candles_parses_binance_klines():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/klines"
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["interval"] == "1d"
        assert request.url.params["limit"] == "30"
        return httpx.Response(200, json=[RAW_KLINE_ROW])

    client = make_mock_client(handler)
    candles = await fetch_daily_candles("BTC-USDT-PERPETUAL", limit=30, http_client=client)
    await client.aclose()

    assert len(candles) == 1
    candle = candles[0]
    assert candle.open_time_ms == 1499040000000
    assert candle.open == Decimal("42000.10")
    assert candle.high == Decimal("42500.50")
    assert candle.low == Decimal("41800.00")
    assert candle.close == Decimal("42300.25")


@pytest.mark.asyncio
async def test_fetch_daily_candles_raises_for_unsupported_instrument():
    client = make_mock_client(lambda request: httpx.Response(200, json=[]))
    with pytest.raises(UnsupportedInstrumentError):
        await fetch_daily_candles("DOGE-USDT-PERPETUAL", http_client=client)
    await client.aclose()


def test_closed_candles_drops_still_forming_last_row():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    closed = Candle(open_time_ms=now_ms - 2 * ONE_DAY_MS, open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"))
    forming = Candle(open_time_ms=now_ms - 3_600_000, open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"))

    result = closed_candles([closed, forming])

    assert result == [closed]


def test_closed_candles_keeps_all_when_last_is_fully_elapsed():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    c1 = Candle(open_time_ms=now_ms - 3 * ONE_DAY_MS, open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"))
    c2 = Candle(open_time_ms=now_ms - 2 * ONE_DAY_MS, open=Decimal("1"), high=Decimal("1"), low=Decimal("1"), close=Decimal("1"))

    result = closed_candles([c1, c2])

    assert result == [c1, c2]


def test_closed_candles_handles_empty_list():
    assert closed_candles([]) == []
