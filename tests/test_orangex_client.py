"""OrangeXClient 유닛 테스트 — httpx.MockTransport로 실제 네트워크 없이 검증.

이 테스트는 절대 실제 OrangeX 서버에 접속하지 않는다 (SPEC 3번 원칙).
"""
from __future__ import annotations

import json

import httpx
import pytest

from exchange.orangex.auth import sign
from exchange.orangex.client import MAX_REQUESTS_PER_SECOND, OrangeXClient, OrangeXError

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"


def make_client(handler) -> OrangeXClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return OrangeXClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, http_client=http_client)


@pytest.mark.asyncio
async def test_authenticates_before_first_call_with_valid_signature():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["method"] == "/public/auth":
            params = payload["params"]
            assert params["grant_type"] == "client_signature"
            assert params["client_id"] == CLIENT_ID
            expected_sig = sign(CLIENT_SECRET, CLIENT_ID, params["timestamp"], params["nonce"])
            assert params["signature"] == expected_sig
            return httpx.Response(
                200,
                json={
                    "id": payload["id"],
                    "jsonrpc": "2.0",
                    "result": {"access_token": "tok-123", "expires_in": 43199},
                },
            )
        assert payload["method"] == "/private/get_positions"
        assert "access_token" not in payload["params"]
        assert request.headers["Authorization"] == "bearer tok-123"
        return httpx.Response(
            200,
            json={"id": payload["id"], "jsonrpc": "2.0", "result": {"positions": []}},
        )

    client = make_client(handler)
    result = await client.call("/private/get_positions", {"instrument_name": "BTC-USDT-PERPETUAL"})

    assert result == {"positions": []}
    assert [c["method"] for c in calls] == ["/public/auth", "/private/get_positions"]


@pytest.mark.asyncio
async def test_token_is_cached_across_calls():
    auth_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        payload = json.loads(request.content)
        if payload["method"] == "/public/auth":
            auth_calls += 1
            return httpx.Response(
                200,
                json={
                    "id": payload["id"],
                    "jsonrpc": "2.0",
                    "result": {"access_token": "tok-abc", "expires_in": 43199},
                },
            )
        return httpx.Response(
            200, json={"id": payload["id"], "jsonrpc": "2.0", "result": {"ok": True}}
        )

    client = make_client(handler)
    await client.call("/private/get_current_account_information")
    await client.call("/private/get_current_account_information")

    assert auth_calls == 1


@pytest.mark.asyncio
async def test_error_response_raises_orangex_error():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": payload["id"],
                "jsonrpc": "2.0",
                "error": {"code": 10001, "message": "invalid params"},
            },
        )

    client = make_client(handler)
    with pytest.raises(OrangeXError) as exc_info:
        await client.call("/public/get_instruments", authed=False)

    assert exc_info.value.code == 10001


@pytest.mark.asyncio
async def test_public_call_does_not_authenticate():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["method"])
        assert "access_token" not in payload["params"]
        assert "Authorization" not in request.headers
        return httpx.Response(
            200, json={"id": payload["id"], "jsonrpc": "2.0", "result": {"instruments": []}}
        )

    client = make_client(handler)
    result = await client.call("/public/get_instruments", authed=False)

    assert result == {"instruments": []}
    assert calls == ["/public/get_instruments"]


@pytest.mark.asyncio
async def test_records_token_scope_from_auth_response():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "/public/auth":
            return httpx.Response(
                200,
                json={
                    "id": payload["id"],
                    "jsonrpc": "2.0",
                    "result": {
                        "access_token": "tok-1",
                        "expires_in": 43199,
                        "scope": "account:read_write trade:read_write",
                    },
                },
            )
        return httpx.Response(200, json={"id": payload["id"], "jsonrpc": "2.0", "result": {}})

    client = make_client(handler)
    await client.call("/private/get_positions")

    assert client.token_scope == "account:read_write trade:read_write"


@pytest.mark.asyncio
async def test_rate_limiter_sleeps_after_max_requests_per_second(monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("exchange.orangex.client.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("exchange.orangex.client.time.monotonic", lambda: 1000.0)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json={"id": payload["id"], "jsonrpc": "2.0", "result": {"ok": True}})

    client = make_client(handler)
    for _ in range(MAX_REQUESTS_PER_SECOND + 1):
        await client.call("/public/get_instruments", authed=False)

    assert len(sleep_calls) == 1
