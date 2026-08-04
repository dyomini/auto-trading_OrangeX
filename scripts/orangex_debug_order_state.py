"""get_order_state 응답이 result 키 없이 온 원인을 raw로 확인 (긴급 디버그, 2026-07-30).

scripts/orangex_test_cancel_order.py 실행 중 order_id=828623293479342080 주문 접수
직후 get_order_state 호출에서 KeyError: 'result'가 발생했다. OrangeXClient._raw_call은
data["error"]가 없으면 무조건 data["result"]를 반환하는데, 응답에 error도 result도
없는 예상 밖 스키마였을 가능성이 있다 — client.call()을 거치지 않고 httpx로 직접
호출해 원본 JSON을 그대로 찍어본다.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from config.settings import Settings
from exchange.orangex.client import OrangeXClient

ORDER_ID = sys.argv[1] if len(sys.argv) > 1 else "828623293479342080"


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        await client._ensure_token()  # noqa: SLF001
        headers = {"Authorization": f"bearer {client._access_token}"}  # noqa: SLF001
        payload = {
            "jsonrpc": "2.0", "id": 999, "method": "/private/get_order_state",
            "params": {"order_id": ORDER_ID},
        }
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                "https://api.orangex.com/api/v1/private/get_order_state",
                json=payload, headers=headers,
            )
        print("[STATUS]", resp.status_code)
        print("[RAW BODY]", resp.text)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
