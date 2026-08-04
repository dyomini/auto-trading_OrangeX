"""OrangeX JSON-RPC 2.0 저수준 클라이언트 (docs/api-notes.md §1, §2).

실제 네트워크 호출은 `httpx.AsyncClient`를 통해서만 발생한다. 테스트에서는
`httpx.MockTransport`를 주입해 네트워크 없이 요청 payload 형태를 검증한다
(SPEC 3번 "라이브 주문을 실제로 전송하는 코드는 사용자가 명시적으로 요청하기 전까지
실행하지 마라"를 지키기 위해, 이 모듈 자체의 테스트는 절대 실제 서버에 접속하지 않는다).
"""
from __future__ import annotations

import asyncio
import collections
import itertools
import time
import uuid
from typing import Any, Literal, Optional

import httpx

from exchange.orangex.auth import sign

BASE_URL = "https://api.orangex.com/api/v1"
TOKEN_EXPIRY_BUFFER_SECONDS = 30
# OrangeX 고객지원 답변(2026-07-28) 확정값: 10 req/s. 응답 헤더에 실측 근거가 없어
# 서버측 정확한 버스트 허용치는 모르지만, 지원팀이 알려준 수치를 그대로 클라이언트
# 스로틀 기준으로 사용한다 (추측이 아니라 지원팀 확인값).
MAX_REQUESTS_PER_SECOND = 10
_RATE_LIMIT_WINDOW_SECONDS = 1.0


class OrangeXError(Exception):
    def __init__(self, code: Any, message: str) -> None:
        super().__init__(f"OrangeX error {code}: {message}")
        self.code = code
        self.message = message


class OrangeXClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = BASE_URL,
        http_client: Optional[httpx.AsyncClient] = None,
        auth_grant_type: Literal["client_signature", "client_credentials"] = "client_signature",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url
        self._auth_grant_type = auth_grant_type
        self._http = http_client or httpx.AsyncClient()
        self._owns_http_client = http_client is None

        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[float] = None
        self._token_scope: Optional[str] = None
        self._request_id = itertools.count(1)

        self._rate_limit_lock = asyncio.Lock()
        self._recent_request_times: collections.deque[float] = collections.deque()

    @property
    def token_scope(self) -> Optional[str]:
        """마지막 인증에서 서버가 실제로 부여한 scope (get_positions 인증 실패 원인 진단용)."""
        return self._token_scope

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()

    async def call(self, method: str, params: Optional[dict] = None, authed: bool = True) -> dict:
        params = dict(params or {})
        headers = None
        if authed:
            await self._ensure_token()
            # OrangeX 고객지원 답변(2026-07-29) 확정: access_token은 params가 아니라
            # `Authorization: bearer {access_token}` HTTP 헤더로 전달해야 한다. 기존에
            # params에 넣던 방식은 문서화되지 않은 방식이었고, get_positions/get_assets_info가
            # 항상 Authentication Failure(10000)를 반환한 근본 원인으로 의심된다.
            headers = {"Authorization": f"bearer {self._access_token}"}
        return await self._raw_call(method, params, headers=headers)

    async def _ensure_token(self) -> None:
        if self._access_token is None or (
            self._token_expires_at is not None and time.time() >= self._token_expires_at
        ):
            await self._authenticate()

    async def _authenticate(self) -> None:
        if self._auth_grant_type == "client_credentials":
            result = await self._raw_call(
                "/public/auth",
                {
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        else:
            timestamp = str(int(time.time() * 1000))
            nonce = uuid.uuid4().hex
            signature = sign(self._client_secret, self._client_id, timestamp, nonce)

            result = await self._raw_call(
                "/public/auth",
                {
                    "grant_type": "client_signature",
                    "client_id": self._client_id,
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "signature": signature,
                },
            )
        self._access_token = result["access_token"]
        self._token_expires_at = time.time() + result["expires_in"] - TOKEN_EXPIRY_BUFFER_SECONDS
        self._token_scope = result.get("scope")

    async def _throttle(self) -> None:
        async with self._rate_limit_lock:
            now = time.monotonic()
            while (
                self._recent_request_times
                and now - self._recent_request_times[0] > _RATE_LIMIT_WINDOW_SECONDS
            ):
                self._recent_request_times.popleft()
            if len(self._recent_request_times) >= MAX_REQUESTS_PER_SECOND:
                sleep_for = _RATE_LIMIT_WINDOW_SECONDS - (now - self._recent_request_times[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = time.monotonic()
            self._recent_request_times.append(now)

    async def _raw_call(self, method: str, params: dict, headers: Optional[dict] = None) -> dict:
        await self._throttle()
        request_id = next(self._request_id)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}

        # Deribit 계열 관례: URL 경로 자체에 메서드명을 붙인다 (예: base_url + "/public/auth").
        # docs/api-notes.md의 예시는 body만 보여주고 실제 URL은 명시하지 않았는데,
        # base_url 하나에만 계속 POST했더니 302(https->http 다운그레이드 리다이렉트)가
        # 발생해 이 방식으로 변경 — 라이브 확인으로 실제 검증 필요.
        url = f"{self._base_url}{method}"
        response = await self._http.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            err = data["error"]
            raise OrangeXError(err.get("code"), err.get("message"))
        return data["result"]
