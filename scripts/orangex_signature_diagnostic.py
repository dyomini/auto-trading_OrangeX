"""client_signature 인증이 실패한 원인을 찾기 위해 서명 계산 방식 후보 여러 개를
실제 서버(/public/auth, 읽기전용)에 시도해보고 어떤 게 통하는지 확인한다.

문서의 공식(StringToSign = clientId+"\\n"+Timestamp+"\\n"+Nonce+"\\n",
Signature = HEX_STRING(HMAC_SHA256(key=ClientSecret, ...)))을 그대로 구현했는데도
서버가 거부했으므로, 문서가 생략했을 수 있는 디테일(대소문자, 초/밀리초, hex/base64 등)을
실제로 하나씩 검증한다 — 추측이 아니라 서버 응답으로 확인하는 것.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import uuid

import httpx

from config.settings import Settings

BASE_URL = "https://api.orangex.com/api/v1"


def hmac_sha256(secret: str, message: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()


async def try_variant(http: httpx.AsyncClient, label: str, client_id: str, client_secret: str, params: dict) -> bool:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "/public/auth", "params": params}
    response = await http.post(f"{BASE_URL}/public/auth", json=payload)
    data = response.json()
    if "error" in data:
        print(f"[FAIL] {label}: {data['error']}")
        return False
    print(f"[OK]   {label}: 인증 성공! result keys = {list(data['result'].keys())}")
    return True


async def main() -> None:
    settings = Settings()
    client_id = settings.api_key.get_secret_value()
    client_secret = settings.api_secret.get_secret_value()

    async with httpx.AsyncClient() as http:
        ts_ms = str(int(time.time() * 1000))
        ts_s = str(int(time.time()))
        nonce = uuid.uuid4().hex

        variants = []

        # A: 문서 그대로 (현재 구현) - ms 타임스탬프, hex lower, nonce 포함
        msg_a = f"{client_id}\n{ts_ms}\n{nonce}\n"
        variants.append(("A: ms/hex-lower/nonce (현재 구현)", {
            "grant_type": "client_signature", "client_id": client_id,
            "timestamp": ts_ms, "nonce": nonce,
            "signature": hmac_sha256(client_secret, msg_a).hex(),
        }))

        # B: hex 대문자
        variants.append(("B: ms/hex-upper/nonce", {
            "grant_type": "client_signature", "client_id": client_id,
            "timestamp": ts_ms, "nonce": nonce,
            "signature": hmac_sha256(client_secret, msg_a).hex().upper(),
        }))

        # C: base64
        variants.append(("C: ms/base64/nonce", {
            "grant_type": "client_signature", "client_id": client_id,
            "timestamp": ts_ms, "nonce": nonce,
            "signature": base64.b64encode(hmac_sha256(client_secret, msg_a)).decode(),
        }))

        # D: 초 단위 타임스탬프
        msg_d = f"{client_id}\n{ts_s}\n{nonce}\n"
        variants.append(("D: seconds/hex-lower/nonce", {
            "grant_type": "client_signature", "client_id": client_id,
            "timestamp": ts_s, "nonce": nonce,
            "signature": hmac_sha256(client_secret, msg_d).hex(),
        }))

        # E: nonce 없이 (빈 문자열)
        msg_e = f"{client_id}\n{ts_ms}\n\n"
        variants.append(("E: ms/hex-lower/nonce=빈문자열, params에 nonce 생략", {
            "grant_type": "client_signature", "client_id": client_id,
            "timestamp": ts_ms,
            "signature": hmac_sha256(client_secret, msg_e).hex(),
        }))

        # F: 끝에 개행 없이 (StringToSign 끝에 \n 없음)
        msg_f = f"{client_id}\n{ts_ms}\n{nonce}"
        variants.append(("F: 끝 개행 없음", {
            "grant_type": "client_signature", "client_id": client_id,
            "timestamp": ts_ms, "nonce": nonce,
            "signature": hmac_sha256(client_secret, msg_f).hex(),
        }))

        any_ok = False
        for label, params in variants:
            ok = await try_variant(http, label, client_id, client_secret, params)
            any_ok = any_ok or ok
            await asyncio.sleep(0.3)

        if not any_ok:
            print("\n어떤 변형도 통하지 않음 — 서명 방식 자체가 아니라 다른 요인(권한, IP 제한 등)일 수 있음")


if __name__ == "__main__":
    asyncio.run(main())
