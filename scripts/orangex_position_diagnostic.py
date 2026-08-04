"""get_positions vs get_user_position 진단 스크립트 (읽기전용).

2026-07-30: 사용자 계좌에 실제 포지션(BTC-USDT-PERPETUAL, cross, short)이 있는 상태에서
`/private/get_positions`(docs/api-notes.md §3에 문서화된 메서드)를 20가지 이상의 파라미터
조합(빈 값/instrument_name/currency×kind 전 조합/kind=swap·linear·option·spot/margin_type/
position_side/subaccount_id 등)으로 호출했지만 전부 빈 배열을 반환했다. 반면 `get_assets_info`
에는 initial_margin/floating_pl이 0이 아니어서 서버에는 분명히 포지션이 있었다.

**결론: 문서의 `/private/get_positions`는 이 계정에서 항상 빈 배열만 반환하고(원인 불명,
서버 버그일 가능성), 실제로 포지션을 반환하는 메서드는 `/private/get_user_position`
(문서에 없는, 시행착오로 발견한 이름)이다.** exchange/orangex/adapter.py의 get_position()을
이 메서드로 변경했다 — docs/api-notes.md §6 항목13 참고.
"""
from __future__ import annotations

import asyncio

from config.settings import Settings
from exchange.orangex.client import OrangeXClient


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        result = await client.call("/private/get_user_position", {"instrument_name": "BTC-USDT-PERPETUAL"})
        print(result)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
