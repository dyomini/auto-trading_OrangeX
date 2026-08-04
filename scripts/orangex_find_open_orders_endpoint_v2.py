"""get_open_orders() 대체 엔드포인트 재탐색 (읽기전용). scripts/orangex_find_open_orders_endpoint.py
가 시도한 후보들은 전부 실패했는데, 그 목록에 Deribit 실제 API의 정확한 이름
(`get_open_orders_by_instrument` — "orders"가 복수형, 기존에 시도한
`get_open_order_by_instrument`는 단수형)이 빠져있었다. cancel_by_id -> cancel
사례처럼 단/복수 표기 차이가 원인이었을 가능성을 확인한다. 전부 읽기전용.
"""
from __future__ import annotations

import asyncio

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"

CANDIDATES: list[tuple[str, dict]] = [
    ("/private/get_open_orders_by_instrument", {"instrument_name": INSTRUMENT}),
    ("/private/get_open_orders_by_currency", {"currency": "USDT"}),
    ("/private/get_active_orders", {"instrument_name": INSTRUMENT}),
    ("/private/get_active_orders_by_instrument", {"instrument_name": INSTRUMENT}),
    ("/private/get_current_orders", {"instrument_name": INSTRUMENT}),
    ("/private/get_pending_orders", {"instrument_name": INSTRUMENT}),
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
                result = await client.call(method, params)
                print(f"[OK] {method}({params}) -> {result}")
            except OrangeXError as e:
                print(f"[FAIL] {method}({params}): {e.code} {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
