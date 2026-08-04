"""특정 order_id를 취소하고 재조회로 검증한다 (긴급, orangex_test_cancel_order.py 후속).

이전 스크립트가 주문 접수(order_id=828623293479342080)까지는 성공했으나
후속 get_order_state 호출에서 원인불명 KeyError('result')가 발생해 중단됐다.
raw httpx 직접 호출로는 정상적으로 order_state=open이 확인됐으므로, 여기서는
client.call()을 다시 시도하되 실패하면 raw httpx로 폴백해서 취소를 완료한다.
미체결 주문을 방치하지 않는 것이 최우선이다.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

ORDER_ID = sys.argv[1] if len(sys.argv) > 1 else "828623293479342080"


async def raw_call(client: OrangeXClient, method: str, params: dict) -> dict:
    await client._ensure_token()  # noqa: SLF001
    headers = {"Authorization": f"bearer {client._access_token}"}  # noqa: SLF001
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with httpx.AsyncClient() as http:
        resp = await http.post(f"https://api.orangex.com/api/v1{method}", json=payload, headers=headers)
    data = resp.json()
    print(f"[RAW] {method} status={resp.status_code} body={data}")
    return data


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        print(f"[INFO] cancelling order_id={ORDER_ID}")
        try:
            result = await client.call("/private/cancel", {"order_id": ORDER_ID})
            print("[OK] cancel (via client.call):", result)
        except (OrangeXError, KeyError) as e:
            print(f"[WARN] client.call 실패({e!r}), raw httpx로 재시도")
            await raw_call(client, "/private/cancel", {"order_id": ORDER_ID})

        print("[INFO] 취소 후 상태 재조회")
        try:
            state = await client.call("/private/get_order_state", {"order_id": ORDER_ID})
            print("[OK] get_order_state (via client.call):", state)
        except (OrangeXError, KeyError) as e:
            print(f"[WARN] client.call 실패({e!r}), raw httpx로 재시도")
            await raw_call(client, "/private/get_order_state", {"order_id": ORDER_ID})
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
