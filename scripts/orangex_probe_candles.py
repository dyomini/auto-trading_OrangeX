"""RSI(14)/ATR 계산에 쓸 캔들(OHLC) 공개 엔드포인트가 존재하는지 탐색 (읽기전용).

docs/api-notes.md에는 캔들/kline류 엔드포인트가 전혀 언급되지 않았다 — ticker/
order_book/last_trades만 확인된 상태. Phase 3의 RSI/ATR 진입 필터에 필요한 데이터
소스를 찾기 위해 후보 메서드명을 순차 시도한다. 전부 읽기전용 GET류라 SPEC 3번
규칙과 무관하게 자유롭게 실행 가능.
"""
from __future__ import annotations

import asyncio

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"

CANDIDATES = [
    ("/public/get_tradingview_chart_data", {"instrument_name": INSTRUMENT, "resolution": "1D", "start_timestamp": 0, "end_timestamp": 9999999999}),
    ("/public/get_candles", {"instrument_name": INSTRUMENT, "resolution": "1D"}),
    ("/public/get_kline", {"instrument_name": INSTRUMENT, "interval": "1D"}),
    ("/public/get_klines", {"instrument_name": INSTRUMENT, "interval": "1D"}),
    ("/public/get_chart_data", {"instrument_name": INSTRUMENT}),
    ("/public/candles", {"instrument_name": INSTRUMENT}),
    ("/public/get_ohlc", {"instrument_name": INSTRUMENT}),
    ("/public/get_index_price_history", {"index_name": "BTC_USDT"}),
    ("/public/get_historical_volatility", {"currency": "BTC"}),
]


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        for method, params in CANDIDATES:
            try:
                result = await client.call(method, params, authed=False)
                print(f"[OK] {method}: {str(result)[:300]}")
            except OrangeXError as e:
                print(f"[ERROR] {method}: {e.code} {e.message}")
            except Exception as e:  # noqa: BLE001 - 조사 스크립트, 전부 기록하고 계속
                print(f"[ERROR] {method}: {e!r}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
