"""OrangeX WebSocket 저수준 클라이언트 (docs/api-notes.md §3, §6 항목19).

REST(`OrangeXClient`)와 별도의 지속 연결이 필요한 스트림(체결 등)에 쓴다. 같은
JSON-RPC 2.0 envelope을 쓰지만 두 가지 라이브로 확인된 차이가 있다
(`scripts/orangex_probe_ws_fills.py`, 2026-07-30):

1. 응답의 `id`는 요청 시 보낸 정수가 아니라 **문자열**로 echo된다 — 매칭 시 문자열로
   정규화해야 한다(안 하면 응답을 영영 못 받은 것처럼 타임아웃난다).
2. 인증된 메서드(`/private/subscribe` 등) 호출 시 `access_token`을 **params에
   포함하는 문서 그대로의 방식이 실제로 통한다** — REST와는 다르다(REST는 문서와
   달리 `Authorization` 헤더가 필요했다, §2 정정 참고). WS는 문서 그대로였다.

인증 그랜트는 `client_credentials`를 쓴다 — `client_signature`는 REST에서도 원인
불명으로 계속 실패해왔고(§6 항목9), WS에서도 동일하게 `Authentication Failure`
(code 10000)로 재현됨을 라이브로 확인했다.

**라이브로 확인된 구독 채널** (2026-07-30): `user.orders.{instrument}.raw`,
`user.trades.{instrument}.raw`, `user.trades.{instrument}.100ms` 전부 구독 성공
응답을 받았다. 다만 이 세션에서는 계좌에 체결이 발생하지 않아 **실제 체결
알림 메시지의 필드 스키마는 아직 관측하지 못했다** — `exchange/orangex/adapter.py`의
`OrangeXAdapter.watch_fills()`가 이 스키마를 어떻게 다루는지는 그쪽 docstring 참고.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, AsyncIterator, Optional, Protocol

import websockets

WS_URL = "wss://api.orangex.com/ws/api/v1"

_CLOSED_SENTINEL = object()  # notifications() 소비자를 깨워서 연결 종료를 알리는 표식


class OrangeXWsError(Exception):
    def __init__(self, code: Any, message: str) -> None:
        super().__init__(f"OrangeX WS error {code}: {message}")
        self.code = code
        self.message = message


class OrangeXWsConnectionClosedError(Exception):
    """WS 연결이 끊겨 더 이상 알림을 받을 수 없을 때 `notifications()`가 던진다.
    자동 재연결은 하지 않는다 — 끊긴 동안 체결을 놓쳤을 수 있어(gap) 추측으로 이어서
    구독하는 대신, 사람이 봇을 재시작해 restart_recovery로 실제 거래소 상태를 다시
    확인하게 하는 게 안전하다(SPEC 0번 원칙). 재연결/하트비트 정책 자체가 여전히
    미확인이라(docs/api-notes.md §6 항목6/19) 자동 복구를 구현할 근거도 없다."""


class _WsTransport(Protocol):
    """websockets.ClientConnection이 만족하는 최소 인터페이스 — 테스트에서 가짜
    연결을 주입할 수 있도록 프로토콜로 뽑아뒀다."""

    async def send(self, message: str) -> None: ...
    def __aiter__(self) -> AsyncIterator[str]: ...
    async def close(self) -> None: ...


class OrangeXWsClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        ws_url: str = WS_URL,
        transport: Optional[_WsTransport] = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._ws_url = ws_url
        self._transport = transport  # 테스트용 사전 주입 — 실제 사용 시 connect()가 채움
        self._owns_transport = transport is None
        self._access_token: Optional[str] = None
        self._id_counter = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._notifications: asyncio.Queue[Any] = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._closed_error: Optional[BaseException] = None

    @property
    def is_connected(self) -> bool:
        return self._access_token is not None

    async def connect(self) -> None:
        if self._transport is None:
            self._transport = await websockets.connect(self._ws_url, open_timeout=15)
        self._reader_task = asyncio.create_task(self._read_loop())
        result = await self.call(
            "/public/auth",
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        self._access_token = result["access_token"]

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._transport is not None and self._owns_transport:
            await self._transport.close()
        # 토큰을 비워 `is_connected`가 닫힌 뒤에도 True로 남지 않게 한다 — 안 그러면
        # 닫힌 클라이언트를 실수로 재사용할 때 connect()를 건너뛰고 조용히 실패한다
        # (사이클마다 어댑터를 새로 만드는 direction="auto"에서 실제로 위험해졌다).
        self._access_token = None

    async def subscribe(self, channels: list[str]) -> list[str]:
        """`/private/subscribe`. 반환값은 실제로 구독된 채널 목록(요청과 다를 수 있음
        — 라이브 관찰상 성공한 채널 그대로 echo됨)."""
        assert self._access_token is not None, "connect()를 먼저 호출해야 함"
        return await self.call(
            "/private/subscribe", {"channels": channels, "access_token": self._access_token}
        )

    async def call(self, method: str, params: dict, timeout: float = 10.0) -> Any:
        assert self._transport is not None, "connect()를 먼저 호출해야 함"
        request_id = next(self._id_counter)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[str(request_id)] = fut
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        await self._transport.send(json.dumps(payload))
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(str(request_id), None)
        if "error" in msg:
            raise OrangeXWsError(msg["error"].get("code"), msg["error"].get("message"))
        return msg["result"]

    async def notifications(self) -> AsyncIterator[dict]:
        """구독한 채널의 알림(unsolicited 메시지)을 무한히 소비한다.

        연결이 끊기면(서버가 닫거나 네트워크 오류) `_read_loop()`가 조용히 죽는 대신
        `_CLOSED_SENTINEL`을 큐에 넣어 이 제너레이터를 깨우고 `OrangeXWsConnectionClosedError`를
        던진다 — 예전에는 소비자(`OrangeXAdapter.watch_fills()` -> `FillRouter.run()`)가
        예외 없이 영원히 멈춰서, `main.py`의 `asyncio.wait(..., FIRST_EXCEPTION)`가 이
        상황을 절대 감지하지 못하는 버그가 있었다(2026-08-04 코드 리뷰로 발견)."""
        while True:
            msg = await self._notifications.get()
            if msg is _CLOSED_SENTINEL:
                raise self._closed_error or OrangeXWsConnectionClosedError("WS 연결이 끊김")
            yield msg

    async def _read_loop(self) -> None:
        assert self._transport is not None
        try:
            async for raw in self._transport:
                msg = json.loads(raw)
                # id는 요청 시 정수로 보내지만 응답엔 문자열로 온다(위 모듈 docstring 참고).
                msg_id = msg.get("id")
                msg_id_norm = str(msg_id) if msg_id is not None else None
                if msg_id_norm is not None and msg_id_norm in self._pending:
                    self._pending.pop(msg_id_norm).set_result(msg)
                else:
                    await self._notifications.put(msg)
        except asyncio.CancelledError:
            # close()에 의한 명시적 종료 — 정상적인 태스크 취소이니 그대로 전파한다.
            # 이 시점엔 소비자(FillRouter 등)도 같은 종료 시퀀스로 취소되는 중이라
            # 아래처럼 깨워줄 필요가 없다.
            raise
        except Exception as e:
            self._closed_error = e
        else:
            # for 루프가 예외 없이 끝났다는 건 서버가 연결을 정상 종료했다는 뜻 — 이것도
            # 소비자 입장에서는 "더 이상 알림을 못 받는" 동일한 실패 상황이다.
            self._closed_error = OrangeXWsConnectionClosedError(
                "WS 연결이 종료됨(서버가 닫았거나 EOF) — 재연결은 자동으로 하지 않으니 "
                "봇을 재시작해 restart_recovery로 실제 거래소 상태를 다시 확인할 것"
            )

        # 여기 도달했다는 건 CancelledError가 아닌 실제 연결 종료 — 응답을 기다리던
        # call()들과 notifications() 소비자를 전부 깨운다(안 그러면 둘 다 영원히 멈춘다).
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(self._closed_error)
        self._pending.clear()
        await self._notifications.put(_CLOSED_SENTINEL)
