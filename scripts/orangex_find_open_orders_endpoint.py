"""get_open_orders() 대체 엔드포인트 탐색 (읽기전용, 2026-07-30).

문서상 `/private/get_open_order_by_instrument`는 라이브에서 "No service found"를
반환한다(docs/api-notes.md §6 항목10). cancel_by_id -> cancel, get_positions ->
get_user_position과 같은 패턴으로 실제 메서드명이 다를 가능성이 높다. 전부
읽기전용(GET류) 호출이라 자금/주문 상태에 부작용이 없다.
"""
from __future__ import annotations

import asyncio

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"

CANDIDATES: list[tuple[str, dict]] = [
    ("/private/get_open_order_by_instrument", {"instrument_name": INSTRUMENT}),
    ("/private/get_open_order_by_currency", {"currency": "USDT"}),
    ("/private/get_open_orders", {}),
    ("/private/get_open_orders", {"instrument_name": INSTRUMENT}),
    ("/private/get_user_open_order", {"instrument_name": INSTRUMENT}),
    ("/private/get_user_open_orders", {"instrument_name": INSTRUMENT}),
    ("/private/get_order_history_by_instrument", {"instrument_name": INSTRUMENT}),
    ("/private/open_orders", {"instrument_name": INSTRUMENT}),
    ("/private/get_orders", {"instrument_name": INSTRUMENT}),
    ("/private/get_orders_by_instrument", {"instrument_name": INSTRUMENT}),
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
