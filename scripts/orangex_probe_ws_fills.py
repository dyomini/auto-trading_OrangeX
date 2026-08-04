"""OrangeX WebSocket 체결 스트림 탐색 (읽기전용 — 이 스크립트는 주문을 걸거나
취소하지 않는다. `/public/auth`로 토큰을 얻고 `/private/subscribe`로 구독만 한다.
SPEC 3번 규칙은 "주문 전송/취소/레버리지 변경/포지션 청산 등 상태를 바꾸는 호출"만
막는다 — 구독은 조회성 동작이라 자유롭게 실행 가능(scripts/orangex_readonly_probe.py와
동일한 근거).

목적: `OrangeXAdapter.watch_fills()`가 현재 NotImplementedError인 이유는 문서
(docs/api-notes.md §3)에 `/private/subscribe` -> `user.orders.{instrument}.raw`,
`user.trades.{instrument}.{interval}` 채널이 있다고만 적혀 있고 한 번도 라이브로
검증한 적이 없기 때문이다. 이 프로젝트에서 지금까지 문서에 있던 메서드명이 실제로는
다르거나 존재하지 않았던 사례가 반복됐다(cancel_by_id, get_positions,
get_open_order_by_instrument 등, docs/api-notes.md 여러 항목) — 그래서 이번에도
추측하지 않고 직접 연결해서 확인한다.

확인할 것:
  1. WS 연결이 되는지, `/public/auth`가 REST와 동일한 그랜트/서명으로 통하는지.
  2. WS에서 인증된 메서드를 호출할 때 access_token을 params에 넣는 문서 방식이
     맞는지(REST는 이 방식이 아니라 Authorization 헤더였다 — §2 정정 참고. WS도
     같은 함정이 있을 가능성을 염두에 둔다).
  3. `/private/subscribe`에 후보 채널 여러 개를 동시에 넣어 어떤 게 성공/실패하는지.
  4. 구독 후 일정 시간 수동으로 메시지가 오는지 관찰(기존 미체결 주문이 있으면
     그 상태 변화가 올 수도 있음 — 새 주문을 걸지는 않는다).

결과는 docs/api-notes.md에 별도로 기록한다.
"""
from __future__ import annotations

import asyncio
import json

import websockets

from config.settings import Settings

WS_URL = "wss://api.orangex.com/ws/api/v1"
INSTRUMENT = "BTC-USDT-PERPETUAL"
LISTEN_SECONDS = 15

# 문서(§3)는 user.orders.{instrument}.raw만 명시하고 user.trades는 {interval}이 뭔지
# 안 알려준다 — Deribit 계열 관례(raw/100ms)를 후보로 같이 시도한다. 이 프로젝트가
# "Deribit 계열 설계"라고 스스로 명시한 문서 근거(docs/api-notes.md §1)에 기반한
# 후보 나열이지, 아무 값이나 찍어보는 게 아니다.
CANDIDATE_CHANNELS = [
    f"user.orders.{INSTRUMENT}.raw",
    f"user.trades.{INSTRUMENT}.raw",
    f"user.trades.{INSTRUMENT}.100ms",
    "user.changes.future.USDT.raw",
]


class WsProbe:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._id_counter = 1
        self._pending: dict[str, asyncio.Future] = {}
        self._unsolicited: list[dict] = []
        self._reader_task: asyncio.Task | None = None

    def start_reader(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[WARN] JSON 파싱 실패: {raw!r}")
                continue
            # 서버가 id를 문자열로 echo한다(요청은 정수로 보냈는데 응답은 "1"처럼 옴) —
            # 문자열로 정규화해서 매칭한다.
            msg_id = msg.get("id")
            msg_id_norm = str(msg_id) if msg_id is not None else None
            if msg_id_norm is not None and msg_id_norm in self._pending:
                self._pending.pop(msg_id_norm).set_result(msg)
            else:
                self._unsolicited.append(msg)
                print(f"[EVENT] {json.dumps(msg, ensure_ascii=False)}")

    async def call(self, method: str, params: dict, timeout: float = 10.0) -> dict:
        request_id = self._id_counter
        self._id_counter += 1
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[str(request_id)] = fut
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        await self.ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise


async def main() -> None:
    settings = Settings()
    client_id = settings.api_key.get_secret_value()
    client_secret = settings.api_secret.get_secret_value()

    print(f"[INFO] connecting to {WS_URL}")
    async with websockets.connect(WS_URL, open_timeout=15) as ws:
        probe = WsProbe(ws)
        probe.start_reader()

        # docs/api-notes.md §6 항목9: client_signature는 REST에서 6가지 변형을 다 시도해도
        # 계속 실패했고, client_credentials로 전환한 뒤에야 모든 /private/* 호출이
        # 성공했다(원인 불명이지만 우선순위를 낮춰둔 상태) — WS도 동일하게 client_credentials로
        # 시작한다.
        print("[INFO] sending /public/auth (client_credentials)")
        auth_resp = await probe.call(
            "/public/auth",
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        print(f"[RESULT] auth response: {json.dumps(auth_resp, ensure_ascii=False)}")

        if "error" in auth_resp:
            print("[FAIL] 인증 실패 — 구독 단계로 진행하지 않음")
            return

        access_token = auth_resp["result"]["access_token"]

        for channel in CANDIDATE_CHANNELS:
            print(f"[INFO] subscribing: {channel}")
            # 문서(§2 WS 인증 절) 방식대로 access_token을 params에 포함 — REST는 이 방식이
            # 아니었으므로(헤더 방식) WS도 같은 함정이 있는지 이 호출 결과로 확인한다.
            resp = await probe.call(
                "/private/subscribe",
                {"channels": [channel], "access_token": access_token},
            )
            print(f"[RESULT] {channel}: {json.dumps(resp, ensure_ascii=False)}")

        print(f"[INFO] {LISTEN_SECONDS}초간 수신 대기 (기존 미체결 주문의 상태 변화 등 관찰)")
        await asyncio.sleep(LISTEN_SECONDS)
        print(f"[INFO] 대기 중 수신된 unsolicited 메시지 수: {len(probe._unsolicited)}")


if __name__ == "__main__":
    asyncio.run(main())
