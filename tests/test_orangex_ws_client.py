"""exchange/orangex/ws_client.py 유닛 테스트. 실제 웹소켓 대신 최소 프로토콜(send/
__aiter__/close)을 만족하는 FakeTransport를 주입한다 — httpx.MockTransport를 쓰는
tests/test_orangex_client.py와 같은 목적, websockets 라이브러리는 그런 테스트 유틸을
자체 제공하지 않아 직접 구현했다.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from exchange.orangex.ws_client import OrangeXWsClient, OrangeXWsConnectionClosedError, OrangeXWsError

_DISCONNECT = object()


class FakeTransport:
    def __init__(self, responder=None) -> None:
        self._responder = responder
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._disconnect_error: BaseException | None = None

    async def send(self, message: str) -> None:
        request = json.loads(message)
        self.sent.append(request)
        if self._responder is not None:
            response = self._responder(request)
            if response is not None:
                await self._incoming.put(json.dumps(response))

    def push(self, msg: dict) -> None:
        self._incoming.put_nowait(json.dumps(msg))

    def simulate_disconnect(self, error: BaseException | None = None) -> None:
        """이후 반복(`__anext__`)에서 연결 종료를 흉내낸다. error가 None이면 서버가
        정상적으로 닫은 것(EOF)을, 아니면 그 예외로 비정상 종료(네트워크 순단 등)를
        흉내낸다."""
        self._disconnect_error = error
        self._incoming.put_nowait(_DISCONNECT)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        raw = await self._incoming.get()
        if raw is _DISCONNECT:
            if self._disconnect_error is not None:
                raise self._disconnect_error
            raise StopAsyncIteration
        return raw

    async def close(self) -> None:
        pass


def _auth_ok_response(request: dict) -> dict:
    # 라이브 관찰대로 id를 문자열로 echo한다(모듈 docstring 참고).
    return {"id": str(request["id"]), "jsonrpc": "2.0", "result": {"access_token": "tok-123", "expires_in": 3600}}


def make_responder(extra=None):
    """/public/auth는 항상 성공 처리하고, 그 외 메서드는 extra(request)에 위임한다.
    connect()가 내부적으로 /public/auth를 먼저 호출하므로 모든 테스트가 이걸 거친다."""
    def responder(request: dict):
        if request["method"] == "/public/auth":
            return _auth_ok_response(request)
        if extra is not None:
            return extra(request)
        return None
    return responder


@pytest.mark.asyncio
async def test_connect_authenticates_with_client_credentials():
    transport = FakeTransport(responder=make_responder())
    client = OrangeXWsClient(client_id="cid", client_secret="csecret", transport=transport)

    await client.connect()

    auth_request = transport.sent[0]
    assert auth_request["method"] == "/public/auth"
    assert auth_request["params"] == {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "csecret"}
    await client.close()


@pytest.mark.asyncio
async def test_call_normalizes_string_id_from_response():
    """라이브 관찰: 요청은 정수 id로 보내지만 응답은 문자열로 echo된다 — 이게 처리 안 되면
    응답을 영영 못 받은 것처럼 타임아웃난다."""
    def extra(request: dict) -> dict:
        return {"id": str(request["id"]), "jsonrpc": "2.0", "result": {"ok": True}}

    transport = FakeTransport(responder=make_responder(extra))
    client = OrangeXWsClient(client_id="cid", client_secret="csecret", transport=transport)
    await client.connect()

    result = await client.call("/public/some_method", {}, timeout=2)

    assert result == {"ok": True}
    await client.close()


@pytest.mark.asyncio
async def test_call_raises_ws_error_on_error_response():
    def extra(request: dict) -> dict:
        return {"id": str(request["id"]), "jsonrpc": "2.0", "error": {"code": 10000, "message": "Authentication Failure"}}

    transport = FakeTransport(responder=make_responder(extra))
    client = OrangeXWsClient(client_id="cid", client_secret="csecret", transport=transport)
    await client.connect()

    with pytest.raises(OrangeXWsError):
        await client.call("/public/some_failing_method", {}, timeout=2)
    await client.close()


@pytest.mark.asyncio
async def test_subscribe_sends_access_token_in_params_and_returns_channels():
    def responder(request: dict) -> dict:
        if request["method"] == "/public/auth":
            return {"id": str(request["id"]), "jsonrpc": "2.0", "result": {"access_token": "tok-123", "expires_in": 3600}}
        if request["method"] == "/private/subscribe":
            return {"id": str(request["id"]), "jsonrpc": "2.0", "result": request["params"]["channels"]}
        return None

    transport = FakeTransport(responder=responder)
    client = OrangeXWsClient(client_id="cid", client_secret="csecret", transport=transport)
    await client.connect()

    result = await client.subscribe(["user.trades.BTC-USDT-PERPETUAL.raw"])

    assert result == ["user.trades.BTC-USDT-PERPETUAL.raw"]
    subscribe_request = next(r for r in transport.sent if r["method"] == "/private/subscribe")
    assert subscribe_request["params"]["access_token"] == "tok-123"
    assert subscribe_request["params"]["channels"] == ["user.trades.BTC-USDT-PERPETUAL.raw"]
    await client.close()


@pytest.mark.asyncio
async def test_notifications_yields_unsolicited_messages():
    transport = FakeTransport(responder=make_responder())
    client = OrangeXWsClient(client_id="cid", client_secret="csecret", transport=transport)
    await client.connect()

    notif_iter = client.notifications()
    transport.push({"jsonrpc": "2.0", "method": "subscription", "params": {"channel": "user.trades.X.raw", "data": {"foo": "bar"}}})

    msg = await asyncio.wait_for(notif_iter.__anext__(), timeout=2)

    assert msg["params"]["data"] == {"foo": "bar"}
    await client.close()


@pytest.mark.asyncio
async def test_notifications_raises_on_clean_disconnect_instead_of_hanging_forever():
    """2026-08-04 코드 리뷰로 발견한 버그의 회귀 테스트: 예전에는 연결이 끊기면
    `_read_loop`가 조용히 죽고 `notifications()` 소비자(FillRouter 등)는 예외 없이
    영원히 멈췄다 — main.py의 FIRST_EXCEPTION 감지가 이 상황을 절대 못 잡았다.
    이제는 명확한 예외를 던져야 한다."""
    transport = FakeTransport(responder=make_responder())
    client = OrangeXWsClient(client_id="cid", client_secret="csecret", transport=transport)
    await client.connect()
    notif_iter = client.notifications()

    transport.simulate_disconnect()  # 서버가 정상적으로 닫음(EOF)

    with pytest.raises(OrangeXWsConnectionClosedError):
        await asyncio.wait_for(notif_iter.__anext__(), timeout=2)
    await client.close()


@pytest.mark.asyncio
async def test_notifications_raises_original_error_on_abnormal_disconnect():
    transport = FakeTransport(responder=make_responder())
    client = OrangeXWsClient(client_id="cid", client_secret="csecret", transport=transport)
    await client.connect()
    notif_iter = client.notifications()

    transport.simulate_disconnect(ConnectionResetError("네트워크 순단 시뮬레이션"))

    with pytest.raises(ConnectionResetError):
        await asyncio.wait_for(notif_iter.__anext__(), timeout=2)
    await client.close()


@pytest.mark.asyncio
async def test_pending_call_raises_on_disconnect_instead_of_hanging_forever():
    """연결이 끊겼을 때 notifications() 소비자뿐 아니라 응답을 기다리던 call()도
    같이 깨어나야 한다."""
    transport = FakeTransport(responder=make_responder())  # /public/auth 이후로는 응답 없음
    client = OrangeXWsClient(client_id="cid", client_secret="csecret", transport=transport)
    await client.connect()

    call_task = asyncio.create_task(client.call("/private/never_responds", {}, timeout=30))
    await asyncio.sleep(0)  # call()이 실제로 전송하고 pending에 등록될 시간을 준다

    transport.simulate_disconnect(ConnectionResetError("네트워크 순단 시뮬레이션"))

    with pytest.raises(ConnectionResetError):
        await asyncio.wait_for(call_task, timeout=2)
    await client.close()
