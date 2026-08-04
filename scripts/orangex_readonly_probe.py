"""OrangeX 읽기전용 라이브 확인 스크립트 (Phase 2 §7).

이 스크립트는 **조회성(GET류) 호출만** 수행한다. 아래 목록에 없는 호출(주문 전송,
주문 취소, 레버리지 변경, 포지션 청산 등 상태를 변경하는 모든 호출)은 이 파일에
포함하지 않는다 — SPEC.md 3번 규칙("라이브 주문을 실제로 전송하는 코드는 사용자가
명시적으로 요청하기 전까지 실행하지 마라")을 지키기 위함이다.

호출 목록:
  - /public/get_instruments (BTC-USDT-PERPETUAL, ETH-USDT-PERPETUAL)
  - /public/get_perpetual_instrument_config (위 두 심볼) — OrangeX 지원팀 확인(2026-07-28):
    존재하지 않는 메서드라 "No service found"가 정상 응답임. 참고용으로 계속 호출한다.
  - /private/get_assets_info (asset_type=PERPETUAL) — 지원팀이 확인해준 올바른 계좌조회 메서드
    (get_current_account_information은 존재하지 않는 메서드였음)
  - /private/get_positions
  - /private/get_open_order_by_instrument (위 두 심볼)

실행 결과는 콘솔에 출력하고 docs/orangex-live-samples.json에 저장한다. 이 파일은
사용자 실계좌의 잔고/포지션 데이터를 포함할 수 있으므로 .gitignore에 등록되어 있다.
API_SECRET/access_token 등 민감정보는 절대 출력/저장하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "docs" / "orangex-live-samples.json"
INSTRUMENTS = ["BTC-USDT-PERPETUAL", "ETH-USDT-PERPETUAL"]


async def safe_call(client: OrangeXClient, label: str, method: str, params: dict, authed: bool) -> dict:
    try:
        result = await client.call(method, params, authed=authed)
        print(f"[OK] {label}")
        return {"ok": True, "result": result}
    except OrangeXError as e:
        print(f"[ERROR] {label}: OrangeX error {e.code}: {e.message}")
        return {"ok": False, "error": {"code": e.code, "message": e.message}}
    except Exception as e:  # noqa: BLE001 - 조사 스크립트이므로 전부 잡아 기록하고 계속 진행
        print(f"[ERROR] {label}: {e!r}")
        return {"ok": False, "error": repr(e)}


async def main() -> None:
    settings = Settings()
    api_key = settings.api_key.get_secret_value()
    api_secret = settings.api_secret.get_secret_value()
    if not api_key or not api_secret:
        raise SystemExit(".env에 API_KEY/API_SECRET이 비어있다. 읽기 전용 키를 채운 뒤 다시 실행할 것.")

    # client_signature 방식은 이 키로 6가지 변형을 시도했으나 전부 실패했다
    # (docs/api-notes.md §6에 미해결 항목으로 기록). client_credentials는 검증됨.
    client = OrangeXClient(client_id=api_key, client_secret=api_secret, auth_grant_type="client_credentials")
    output: dict[str, dict] = {}

    try:
        for instrument in INSTRUMENTS:
            output[f"get_instruments:{instrument}"] = await safe_call(
                client, f"get_instruments({instrument})",
                "/public/get_instruments", {"instrument_name": instrument}, authed=False,
            )
            output[f"get_perpetual_instrument_config:{instrument}"] = await safe_call(
                client, f"get_perpetual_instrument_config({instrument})",
                "/public/get_perpetual_instrument_config", {"instrument_name": instrument}, authed=False,
            )

        await client._ensure_token()  # noqa: SLF001 - scope 확인을 위해 인증 결과를 직접 들여다봄
        print(f"[INFO] 발급된 토큰 scope: {client.token_scope!r}")

        output["get_assets_info:PERPETUAL"] = await safe_call(
            client, "get_assets_info(PERPETUAL)",
            "/private/get_assets_info", {"asset_type": ["PERPETUAL"]}, authed=True,
        )
        output["get_positions"] = await safe_call(
            client, "get_positions", "/private/get_positions", {}, authed=True,
        )
        for instrument in INSTRUMENTS:
            output[f"get_open_order_by_instrument:{instrument}"] = await safe_call(
                client, f"get_open_order_by_instrument({instrument})",
                "/private/get_open_order_by_instrument", {"instrument_name": instrument}, authed=True,
            )
    finally:
        await client.aclose()

    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n저장 완료: {OUTPUT_PATH} (이 파일은 .gitignore에 등록되어 있어 커밋되지 않음)")


if __name__ == "__main__":
    asyncio.run(main())
