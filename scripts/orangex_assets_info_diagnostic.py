"""get_assets_info가 "Bad requested"(1001)를 반환하는 원인을 찾기 위한 파라미터 진단
(읽기전용 — /public/auth, /private/get_assets_info만 호출).

OrangeX 지원팀 답변(2026-07-28)은 asset_type 파라미터로 ALL/SPOT/PERPETUAL/WALLET을
쓸 수 있다고 했으나, 정확한 대소문자·파라미터명·요청 형태는 명시하지 않았다.
추측 대신 여러 변형을 실제 서버에 시도해 확인한다.
"""
from __future__ import annotations

import asyncio

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError


async def try_params(client: OrangeXClient, label: str, params: dict) -> None:
    try:
        result = await client.call("/private/get_assets_info", params)
        print(f"[OK]   {label} params={params}: {result}")
    except OrangeXError as e:
        print(f"[FAIL] {label} params={params}: OrangeX error {e.code}: {e.message}")


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        await client._ensure_token()  # noqa: SLF001 - 진단 목적으로만 내부 메서드 직접 호출

        await try_params(client, "asset_type=PERPETUAL", {"asset_type": "PERPETUAL"})
        await try_params(client, "asset_type=ALL", {"asset_type": "ALL"})
        await try_params(client, "asset_type=perpetual (소문자)", {"asset_type": "perpetual"})
        await try_params(client, "no params", {})
        await try_params(client, "currency=USDT", {"currency": "USDT"})
        await try_params(client, "asset_type=PERPETUAL & currency=USDT", {"asset_type": "PERPETUAL", "currency": "USDT"})
        await try_params(client, "type=PERPETUAL", {"type": "PERPETUAL"})
        await try_params(client, "asset_type=[PERPETUAL] (배열)", {"asset_type": ["PERPETUAL"]})
        await try_params(client, "asset_types=PERPETUAL (복수형)", {"asset_types": "PERPETUAL"})
        for alt in ["FUTURES", "SWAP", "CONTRACT", "DERIVATIVES", "0", "1", "2", "3"]:
            await try_params(client, f"asset_type={alt}", {"asset_type": alt})
        for n in [0, 1, 2, 3, 4]:
            await try_params(client, f"asset_type={n} (JSON 정수)", {"asset_type": n})
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
