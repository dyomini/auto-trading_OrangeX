"""cancel_by_id가 'No service found'를 반환해 실제 취소 엔드포인트명을 탐색한다 (긴급).

order_id=828623293479342080 주문이 아직 오더북에 열려있는 상태다(0.001 BTC SHORT,
67304.5). get_positions -> get_user_position, get_open_order_by_instrument -> 대체
필요였던 것과 같은 패턴으로, cancel_by_id도 실제 서버 메서드명이 다를 가능성이 높다.
후보를 순서대로 시도하고, 성공(order_state != open)하면 즉시 중단한다.
"""
from __future__ import annotations

import asyncio
import sys

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

ORDER_ID = sys.argv[1] if len(sys.argv) > 1 else "828623293479342080"
INSTRUMENT = "BTC-USDT-PERPETUAL"

CANDIDATES: list[tuple[str, dict]] = [
    ("/private/cancel", {"order_id": ORDER_ID}),
    ("/private/cancel_order", {"order_id": ORDER_ID}),
    ("/private/cancel_by_order_id", {"order_id": ORDER_ID}),
    ("/private/order_cancel", {"order_id": ORDER_ID}),
    ("/private/cancel_order_by_id", {"order_id": ORDER_ID}),
    ("/private/user_cancel_order", {"order_id": ORDER_ID}),
    ("/private/cancel_all_by_instrument", {"instrument_name": INSTRUMENT}),
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
                print(f"[FAIL] {method}: {e.code} {e.message}")
                continue

            state = await client.call("/private/get_order_state", {"order_id": ORDER_ID})
            print(f"[STATE] order_state={state.get('order_state')} filled={state.get('filled_amount')}")
            if state.get("order_state") != "open":
                print(f"[SUCCESS] 취소 성공 확인 — 실제 메서드명: {method}")
                return
    finally:
        await client.aclose()

    print("[FATAL] 모든 후보 실패 — 주문이 여전히 open 상태일 수 있음. 수동 확인 필요.")


if __name__ == "__main__":
    asyncio.run(main())
