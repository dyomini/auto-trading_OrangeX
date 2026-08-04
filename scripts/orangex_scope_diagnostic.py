"""get_positions 인증 실패 원인 진단용 스크립트 (읽기전용 — /public/auth, /private/get_positions만 호출).

1차 가설(토큰 scope 부족)은 API 키 권한을 Trading 읽기로 켠 뒤 scope에 trade:read가
포함됐는데도 여전히 Authentication Failure가 나서 기각됐다 (2026-07-29). 2차 가설:
get_positions가 빈 params({})를 거부하고, 파라미터 검증 실패를 (문서화되지 않은 채)
"Authentication Failure"로 잘못 보고하고 있을 가능성 — 다른 private 엔드포인트
(get_open_order_by_instrument 등)는 전부 instrument_name/currency 등을 필수로 받는다.
추측 대신 실제로 여러 파라미터 조합을 서버에 시도해 확인한다.
"""
from __future__ import annotations

import asyncio

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError


async def try_params(client: OrangeXClient, label: str, params: dict) -> None:
    try:
        result = await client.call("/private/get_positions", params)
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
        print(f"[INFO] 발급된 토큰 scope: {client.token_scope!r}")

        await try_params(client, "no params", {})
        await try_params(client, "instrument_name=BTC-USDT-PERPETUAL", {"instrument_name": "BTC-USDT-PERPETUAL"})
        await try_params(client, "currency=BTC", {"currency": "BTC"})
        await try_params(client, "currency=USDT", {"currency": "USDT"})
        await try_params(client, "kind=future", {"kind": "future"})
        await try_params(client, "currency=BTC & kind=future", {"currency": "BTC", "kind": "future"})
        await try_params(client, "currency=USDT & kind=future", {"currency": "USDT", "kind": "future"})
        await try_params(client, "currency=BTC & kind=perpetual", {"currency": "BTC", "kind": "perpetual"})
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
