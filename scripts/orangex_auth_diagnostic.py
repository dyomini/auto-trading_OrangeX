"""인증 방식 진단용 스크립트 (읽기전용 — /public/auth만 호출, 주문 없음).

client_signature(서명 방식)가 실패했을 때, 더 단순한 client_credentials 방식으로도
시도해봐서 "서명 로직 버그"인지 "키 자체 문제"인지 구분한다.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError


async def try_auth(label: str, grant_type: str) -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type=grant_type,
    )
    if grant_type == "client_signature":
        ts_ms = int(time.time() * 1000)
        iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        print(
            f"[INFO] {label} 시도 시각(참고용): timestamp≈{ts_ms}, UTC≈{iso} "
            "(실제 서명에 쓰인 값과 수 ms 오차 있을 수 있음 — 지원팀 로그 검색 시 시간창 참고용)"
        )
    try:
        await client._ensure_token()  # noqa: SLF001 - 진단 목적으로만 내부 메서드 직접 호출
        print(f"[OK] {label}: 인증 성공")
    except OrangeXError as e:
        print(f"[FAIL] {label}: OrangeX error {e.code}: {e.message}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {label}: {e!r}")
    finally:
        await client.aclose()


async def main() -> None:
    await try_auth("client_signature (서명 방식)", "client_signature")
    await try_auth("client_credentials (평문 방식)", "client_credentials")


if __name__ == "__main__":
    asyncio.run(main())
