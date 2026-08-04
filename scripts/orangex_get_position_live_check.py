"""OrangeXAdapter.get_position()이 실제 계좌의 실 포지션을 올바르게 파싱하는지 확인하는
1회성 라이브 검증 스크립트 (읽기전용). scripts/orangex_position_diagnostic.py에서 찾은
`/private/get_user_position` 기반 구현(exchange/orangex/adapter.py)을 어댑터 계층까지
통째로 검증한다.
"""
from __future__ import annotations

import asyncio

from config.settings import Settings
from exchange.orangex.adapter import OrangeXAdapter
from exchange.orangex.client import OrangeXClient


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    adapter = OrangeXAdapter(client)
    try:
        position = await adapter.get_position("BTC-USDT-PERPETUAL")
        print(position)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
