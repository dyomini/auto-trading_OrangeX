"""orangex_test_market_order.py가 진입 직후 알려진 지연 이슈(docs/api-notes.md §6
항목16 — 주문 접수 직후 즉시 조회 시 KeyError)로 청산 단계 전에 죽어서 생긴 미청산
LONG 0.001 BTC 포지션을 정리한다. 이번엔 첫 조회 전 5초를 대기해 그 지연 이슈를 피한다.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"
POSITION_SIDE = "LONG"


async def get_position(client: OrangeXClient) -> dict | None:
    result = await client.call("/private/get_user_position", {"instrument_name": INSTRUMENT})
    positions = result if isinstance(result, list) else result.get("positions", [])
    return next((p for p in positions if p.get("instrument_name") == INSTRUMENT), None)


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        position = await get_position(client)
        print(f"[INFO] 현재 포지션: {position}")
        if position is None:
            print("[OK] 이미 flat — 할 일 없음")
            return
        qty = abs(Decimal(str(position["size"])))

        coid = f"cleanup-{uuid.uuid4().hex[:12]}"
        params = {
            "instrument_name": INSTRUMENT, "amount": str(qty), "type": "market",
            "reduce_only": True, "position_side": POSITION_SIDE, "custom_order_id": coid,
        }
        print(f"[INFO] 청산 주문: {params}")
        result = await client.call("/private/sell", params)
        order_id = str(result.get("order", result)["order_id"])
        print(f"[OK] order_id={order_id}")

        print("[INFO] 5초 대기 (접수 직후 즉시 조회 지연 이슈 회피, §6 항목16)")
        await asyncio.sleep(5)

        for attempt in range(10):
            state = await client.call("/private/get_order_state", {"order_id": order_id})
            print(f"[POLL {attempt}] order_state={state.get('order_state')}, filled_amount={state.get('filled_amount')}")
            if state.get("order_state") == "filled":
                break
            await asyncio.sleep(2)

        final_position = await get_position(client)
        print(f"[RESULT] 최종 포지션(flat이어야 함): {final_position}")

    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
