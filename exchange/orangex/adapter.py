"""OrangeXAdapter — docs/api-notes.md §3/§5에 문서화된 엔드포인트로 ExchangeAdapter를 구현한다.

일부 응답 필드 매핑은 Phase 0 문서 조사만으로 확정할 수 없었다
(docs/api-notes.md §6 항목7 계약스펙 실측값, 항목8 계좌 응답 스키마).
추측으로 잘못된 숫자를 조용히 반환하는 대신, 그 지점에서 명시적으로
`OrangeXResponseSchemaError`를 발생시킨다 — SPEC 0번 절대 원칙(추측 금지) 준수.

`get_balance()`는 OrangeX 고객지원 답변(2026-07-28)으로 정확한 메서드명(`/private/get_assets_info`)과
필드명(`available_funds`/`total_margin_balance`)을 확인해 구현했다. 다만 응답 envelope이
`asset_type` 파라미터로 지정한 데이터가 최상위에 바로 오는지, `{"PERPETUAL": {...}}`처럼
한 번 더 감싸져 오는지는 지원팀 답변에 명시되지 않아 라이브 재검증이 필요하다
(코드는 두 형태를 모두 시도하되, 필드가 없으면 추측하지 않고 명시적으로 실패한다).

buy/sell(`place_limit_order`)는 2026-07-30 사용자 요청으로 최초 실주문을 라이브 검증했다.
확인된 사실 세 가지:
1. **응답 envelope은 `{"order": {"order_id": ..., "custom_order_id": ...}}`뿐이고
   `order_state`/`filled_amount` 등 상태 필드가 아예 없다.** 실제 상태는 `get_order_state`를
   별도로 호출해야 알 수 있다 — `place_limit_order()`가 내부적으로 두 번 호출하도록 구현.
2. **헤지 모드 계좌(`dual_side_position=true`)에서는 `position_side`(`LONG`/`SHORT`)를
   명시하지 않으면 서버가 기본값 `BOTH`(원웨이 모드용)로 처리해 기존 포지션과 충돌 →
   주문이 즉시 자동 취소된다(`order_state=canceled`, `error_code=5998`).** `leverage`
   파라미터를 함께 보내는 시도는 효과가 없어 기각했다 — 원인은 leverage가 아니라
   position_side였다. SPEC상 봇 인스턴스는 항상 단일 방향(`config/settings.py`의
   `direction`)만 다루므로, `OrangeXAdapter`가 생성 시점에 `position_side`를 받아
   모든 주문에 동일하게 태깅한다.
3. **`order_state`의 실제 값은 `cancelled`(영국식)가 아니라 `canceled`(미국식, L 1개)다.**
   기존 매핑 테이블에 없어 방치하면 `.get(..., "open")` 기본값 폴백으로 조용히 "열려있음"
   취급될 뻔했다 — 매핑에 없는 상태는 추측 대신 `OrangeXResponseSchemaError`로 막는다.

`watch_fills()`는 2026-07-30 `exchange/orangex/ws_client.py`(`OrangeXWsClient`)로 구현했다.
연결/인증(client_credentials)/구독/**실제 체결 알림의 필드 스키마까지 전부 라이브로
검증 완료**(`scripts/orangex_observe_live_fill_ws.py`, 사용자 명시적 요청 — 0.001 BTC
진입 후 즉시 청산, docs/api-notes.md §6 항목19). `fee` 필드명도 확정됨(그대로 "fee").

**주문 접수 직후 `get_order_state` 재조회 재시도**: docs/api-notes.md §6 항목16이
"주문/취소 직후 즉시 조회 시 지연·오류 발생(2초 대기 후 성공, 5초 대기 후 반영)"을
관찰만 하고 재시도 정책은 "추측성 하드코딩 대신 사용자와 상의 후 반영"으로 보류해뒀었다.
2026-07-30 `place_market_order` 라이브 검증(`scripts/orangex_test_market_order.py`,
사용자 명시적 요청) 도중 이 문제가 실제로 재현돼(`get_order_state`가 `KeyError: 'result'`로
죽음 — 주문 자체는 체결됐는데 상태 조회만 실패) 라이브 포지션이 미청산 상태로 남는
사고가 났다(수동으로 정리함). 세 번째 관찰 데이터가 생긴 김에 `_get_order_state_with_retry`
로 반영했다 — 즉시 1회 시도 후 실패하면 2초/3초/5초 간격으로 최대 3회 재시도(관찰된
"2초 성공"/"5초 반영" 사례를 그대로 반영한 값, 총 대기 10초). 그래도 실패하면 그대로
예외를 올린다(무한 재시도하지 않음).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, Optional

import httpx

from exchange.base import (
    Balance,
    ContractSpec,
    Direction,
    ExchangeAdapter,
    Fill,
    MarketOrderRequest,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    StopOrderRequest,
    Ticker,
)
from exchange.orangex.client import OrangeXClient
from exchange.orangex.ws_client import OrangeXWsClient


class OrangeXResponseSchemaError(Exception):
    """문서로 확정되지 않은 응답 필드에 의존해야 할 때, 추측 대신 명시적으로 실패시키기 위한 예외."""


_ORDER_STATE_TO_STATUS: dict[str, OrderStatus] = {
    "open": "open",
    "filled": "filled",
    "cancelled": "cancelled",
    "canceled": "cancelled",  # OrangeX 라이브 응답 철자(L 1개) — 2026-07-30 실주문 테스트로 확인
    "rejected": "rejected",
}

_DIRECTION_TO_POSITION_SIDE: dict[Direction, str] = {"long": "LONG", "short": "SHORT"}

# docs/api-notes.md §5: trigger_price_type 1=mark price(미검증), 2=last price(라이브 검증됨)
_TRIGGER_PRICE_TYPE_TO_CODE: dict[str, int] = {"mark": 1, "last": 2}

_ACCOUNT_ASSET_TYPE = "PERPETUAL"

# docs/api-notes.md §6 항목16 관찰값(2초 대기 후 성공, 5초 대기 후 반영) 그대로 반영 —
# 위 모듈 docstring "주문 접수 직후 get_order_state 재조회 재시도" 참고.
_ORDER_STATE_RETRY_DELAYS_SECONDS = (2, 3, 5)


class OrangeXAdapter(ExchangeAdapter):
    def __init__(
        self,
        client: OrangeXClient,
        position_side: Optional[Direction] = None,
        ws_client: Optional[OrangeXWsClient] = None,
    ) -> None:
        self._client = client
        # 헤지 모드 계좌에서 주문에 태깅할 position_side. None이면 파라미터를 아예 안 보낸다
        # (원웨이 모드 계좌 대비 — 2026-07-30 발견, 위 모듈 docstring 참고).
        self._position_side = position_side
        # watch_fills() 전용 — REST용 OrangeXClient와 별도 연결이 필요하다(exchange/orangex/
        # ws_client.py 참고). None이면 watch_fills() 호출 시점에 명시적으로 실패한다.
        self._ws_client = ws_client

    async def _get_order_state_with_retry(self, order_id: str) -> dict[str, Any]:
        """주문 접수 직후 곧바로 get_order_state를 부르면 서버 인덱싱 지연으로 실패할 수
        있다(위 모듈 docstring 참고) — 즉시 1회 시도 후 실패 시에만 재시도한다(정상
        케이스의 지연을 늘리지 않기 위해 즉시 시도를 먼저 함).

        재시도 대상: (1) 관찰된 원인 그대로인 `KeyError`/`TypeError`(응답에 result/error가
        아예 없는 기형 응답), (2) 네트워크 순단류(`httpx.TransportError` — 연결/타임아웃/
        프로토콜 오류), (3) 서버측 5xx(`httpx.HTTPStatusError`, status>=500) — 전부
        "일시적일 가능성이 있는" 실패라 재시도 없이 바로 포기하면 주문이 실제로는
        접수됐는데 상태 확인만 실패해 미청산 포지션이 남는 사고(2026-07-30 실제 발생,
        위 모듈 docstring 참고)가 다른 경로로 재발할 수 있다. 4xx(예: 인증/파라미터
        오류)와 `OrangeXError`(코드별 의미가 명확하지 않은 게 많아 추측 금지)는 재시도
        없이 즉시 올린다 — 이런 실패는 시간이 지난다고 저절로 해결되지 않는다."""
        delays = (0, *_ORDER_STATE_RETRY_DELAYS_SECONDS)
        last_error: Exception | None = None
        for i, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self._client.call("/private/get_order_state", {"order_id": order_id})
            except (KeyError, TypeError) as e:
                last_error = e
                continue
            except httpx.TransportError as e:
                last_error = e
                continue
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise
                last_error = e
                continue
        raise OrangeXResponseSchemaError(
            f"get_order_state(order_id={order_id})가 {len(delays)}회 재시도(총 대기 "
            f"{sum(_ORDER_STATE_RETRY_DELAYS_SECONDS)}초) 후에도 계속 실패함 — "
            f"주문 자체는 접수됐을 수 있으니 수동으로 포지션/미체결 주문을 확인할 것: {last_error!r}"
        ) from last_error

    async def get_balance(self) -> Balance:
        # OrangeX 고객지원 답변(2026-07-29) 확정: asset_type은 일반 문자열이 아니라
        # JSON 배열([...])이어야 한다. 문자열/정수로 보내면 1001 Bad requested.
        result = await self._client.call(
            "/private/get_assets_info", {"asset_type": [_ACCOUNT_ASSET_TYPE]}
        )
        data = result
        if isinstance(result, dict) and _ACCOUNT_ASSET_TYPE in result:
            data = result[_ACCOUNT_ASSET_TYPE]

        try:
            available = Decimal(str(data["available_funds"]))
            equity = Decimal(str(data["total_margin_balance"]))
        except (KeyError, TypeError) as e:
            raise OrangeXResponseSchemaError(
                "get_assets_info(asset_type=PERPETUAL) 응답에 available_funds/"
                "total_margin_balance 필드가 없음 (OrangeX 지원팀 답변 2026-07-28 기준 "
                f"필드명인데 응답 envelope이 다름) — 라이브 재검증 필요: {e!r}"
            ) from e
        return Balance(equity=equity, available=available)

    async def get_position(self, instrument: str) -> Position:
        # 2026-07-30: 문서(§3)에 명시된 `/private/get_positions`는 실제 포지션이 있는
        # 계좌에서도 20가지 이상의 파라미터 조합(빈 값/instrument_name/currency×kind
        # 전 조합/kind=swap·linear·option·spot/margin_type/position_side/subaccount_id)
        # 전부 빈 배열만 반환했다 — 원인 불명(서버 버그로 추정), docs/api-notes.md §6
        # 항목13 참고. 실제로 포지션을 반환하는 메서드는 시행착오로 찾은
        # `/private/get_user_position`이며, 라이브 샘플로 필드 매핑을 확정했다:
        # position_side(LONG/SHORT), size(부호 있는 수량), average_price.
        result = await self._client.call(
            "/private/get_user_position", {"instrument_name": instrument}
        )
        positions = result if isinstance(result, list) else result.get("positions", [])
        # 헤지 모드 계좌는 같은 instrument에 LONG/SHORT 포지션이 동시에 존재할 수 있다
        # (direction="both" 지원, 2026-08-04) — 이 어댑터가 생성 시 받은 position_side로
        # 반드시 내 몫만 골라야 한다. 안 그러면 롱 담당 어댑터가 숏 포지션을 자기 것으로
        # 착각하는 사고가 날 수 있다. position_side가 없으면(one-way 모드 등) 기존처럼
        # 첫 매치를 그대로 쓴다.
        expected_side = _DIRECTION_TO_POSITION_SIDE.get(self._position_side) if self._position_side else None
        for pos in positions:
            if pos.get("instrument_name") != instrument:
                continue
            if expected_side is not None and pos.get("position_side") != expected_side:
                continue
            try:
                side_raw = pos["position_side"]
                qty = abs(Decimal(str(pos["size"])))
                avg_price = Decimal(str(pos["average_price"]))
            except (KeyError, TypeError) as e:
                raise OrangeXResponseSchemaError(
                    f"get_user_position 응답에 예상 필드가 없음: {e!r} — 원본: {pos!r}"
                ) from e

            direction: Optional[Direction]
            if side_raw == "LONG":
                direction = "long"
            elif side_raw == "SHORT":
                direction = "short"
            else:
                raise OrangeXResponseSchemaError(
                    f"position_side 예상치 못한 값: {side_raw!r} — 원본: {pos!r}"
                )
            return Position(instrument=instrument, direction=direction, qty=qty, avg_price=avg_price)

        # 빈 배열 = 무포지션(flat)으로 간주. 이 계좌가 현재 포지션을 보유 중이라
        # get_user_position의 flat 케이스 자체는 라이브로 재검증하지 못했다 —
        # 이전에 /private/get_positions로 검증했던 flat 동작과 동일하다고 가정한 것.
        return Position(instrument=instrument, direction=None, qty=Decimal("0"), avg_price=Decimal("0"))

    async def get_contract_spec(self, instrument: str) -> ContractSpec:
        result = await self._client.call(
            "/public/get_instruments", {"instrument_name": instrument}, authed=False
        )
        instruments = result.get("instruments") if isinstance(result, dict) else result
        if instruments is None:
            instruments = [result]

        for item in instruments:
            if item.get("instrument_name") == instrument:
                return ContractSpec(
                    instrument=instrument,
                    tick_size=Decimal(str(item["tick_size"])),
                    min_qty=Decimal(str(item["min_qty"])),
                    min_notional=Decimal(str(item["min_notional"])),
                    contract_size=Decimal(str(item.get("contract_size", "1"))),
                )
        raise OrangeXResponseSchemaError(f"get_instruments 응답에서 instrument_name={instrument}을 찾지 못함")

    async def get_ticker(self, instrument: str) -> Ticker:
        # 2026-07-30 라이브 검증(docs/api-notes.md §6 항목15): 문서에는 없던 공개
        # 엔드포인트지만 /public/ticker(instrument_name)가 last_price/mark_price/
        # best_bid_price/best_ask_price/stats를 반환함을 확인했다. 인증 불필요.
        result = await self._client.call(
            "/public/ticker", {"instrument_name": instrument}, authed=False
        )
        try:
            last_price = Decimal(str(result["last_price"]))
        except (KeyError, TypeError) as e:
            raise OrangeXResponseSchemaError(
                f"/public/ticker 응답에 last_price가 없음: {e!r} — 원본: {result!r}"
            ) from e
        return Ticker(instrument=instrument, last_price=last_price)

    async def set_leverage(self, instrument: str, leverage: Decimal) -> None:
        await self._client.call(
            "/private/modify_perpetual_instrument_leverage",
            {"instrument_name": instrument, "leverage": str(leverage)},
        )

    async def place_limit_order(self, order: OrderRequest) -> OrderResult:
        method = "/private/buy" if order.side == "buy" else "/private/sell"
        params = {
            "instrument_name": order.instrument,
            "amount": str(order.qty),
            "type": "limit",
            "price": str(order.price),
            "time_in_force": "good_til_cancelled",
            "post_only": order.post_only,
            "reduce_only": order.reduce_only,
            "custom_order_id": order.client_order_id,
        }
        if self._position_side is not None:
            params["position_side"] = _DIRECTION_TO_POSITION_SIDE[self._position_side]
        result = await self._client.call(method, params)

        order_envelope = result.get("order", result) if isinstance(result, dict) else result
        try:
            order_id = str(order_envelope["order_id"])
        except (KeyError, TypeError) as e:
            raise OrangeXResponseSchemaError(
                f"{method} 응답에 order_id가 없음: {e!r} — 원본: {result!r}"
            ) from e

        # 2026-07-30 라이브 확인: buy/sell 응답에는 order_id/custom_order_id만 오고
        # order_state/filled_amount 등 상태 필드가 없다 — get_order_state로 재조회해야 한다.
        state = await self._get_order_state_with_retry(order_id)
        return self._parse_order_result(state, order.client_order_id)

    async def place_stop_order(self, order: StopOrderRequest) -> OrderResult:
        # 2026-07-30 라이브 검증(docs/api-notes.md §6 항목18, scripts/orangex_test_stop_order.py):
        # condition_type=STOP + trigger_price + trigger_price_type로 실제 거래소 등록형
        # SL이 동작함을 확인했다. 트리거는 crossing-trigger다 — 주문 시점에 이미 조건이
        # 참이어도 발동하지 않고, 이후 실제 가격이 trigger_price를 가로질러야 발동한다.
        # 호출하는 쪽(engine)이 trigger_price를 "현재가 기준 아직 미도달 방향"으로 넘겨야 한다.
        method = "/private/buy" if order.side == "buy" else "/private/sell"
        params = {
            "instrument_name": order.instrument,
            "amount": str(order.qty),
            "type": "limit",
            "price": str(order.trigger_price),
            "time_in_force": "good_til_cancelled",
            "post_only": False,
            "reduce_only": order.reduce_only,
            "custom_order_id": order.client_order_id,
            "condition_type": "STOP",
            "trigger_price": str(order.trigger_price),
            "trigger_price_type": _TRIGGER_PRICE_TYPE_TO_CODE[order.trigger_price_type],
        }
        if self._position_side is not None:
            params["position_side"] = _DIRECTION_TO_POSITION_SIDE[self._position_side]
        result = await self._client.call(method, params)

        order_envelope = result.get("order", result) if isinstance(result, dict) else result
        try:
            order_id = str(order_envelope["order_id"])
        except (KeyError, TypeError) as e:
            raise OrangeXResponseSchemaError(
                f"{method}(STOP) 응답에 order_id가 없음: {e!r} — 원본: {result!r}"
            ) from e

        state = await self._get_order_state_with_retry(order_id)
        return self._parse_order_result(state, order.client_order_id)

    async def place_market_order(self, order: MarketOrderRequest) -> OrderResult:
        # 2026-07-30 라이브 검증 완료(사용자 명시적 요청, scripts/orangex_test_market_order.py):
        # 0.001 BTC 시장가 진입/청산 둘 다 즉시 체결됨을 get_order_state와 get_user_position
        # 교차 확인으로 검증했다 — STOP 주문 같은 crossing-trigger 등 예상 밖 특성 없음.
        # (검증 도중 주문 접수 직후 get_order_state 조회가 실패해 미청산 포지션이 잠깐
        # 남는 사고가 있었다 — 원인과 수정은 위 모듈 docstring "get_order_state 재조회
        # 재시도" 참고, 수동으로 정리 완료함.)
        method = "/private/buy" if order.side == "buy" else "/private/sell"
        params = {
            "instrument_name": order.instrument,
            "amount": str(order.qty),
            "type": "market",
            "reduce_only": order.reduce_only,
            "custom_order_id": order.client_order_id,
        }
        if self._position_side is not None:
            params["position_side"] = _DIRECTION_TO_POSITION_SIDE[self._position_side]
        result = await self._client.call(method, params)

        order_envelope = result.get("order", result) if isinstance(result, dict) else result
        try:
            order_id = str(order_envelope["order_id"])
        except (KeyError, TypeError) as e:
            raise OrangeXResponseSchemaError(
                f"{method}(market) 응답에 order_id가 없음: {e!r} — 원본: {result!r}"
            ) from e

        state = await self._get_order_state_with_retry(order_id)
        return self._parse_order_result(state, order.client_order_id)

    async def cancel_order(self, order_id: str) -> None:
        # 2026-07-30 라이브 검증: 문서상 메서드명 `/private/cancel_by_id`는 실제로
        # "No service found"(code 1000)를 반환한다 — get_positions/get_open_order_by_instrument와
        # 같은 패턴(문서 메서드명이 실제 서버에 없음). 시행착오로 찾은 정확한 메서드명은
        # `/private/cancel`이며, order_id로 취소 성공(order_state: open -> canceled,
        # filled_amount=0, error_code=0)까지 라이브로 확인했다 (docs/api-notes.md §6 항목15).
        await self._client.call("/private/cancel", {"order_id": order_id})

    async def get_open_orders(self, instrument: str) -> list[OrderResult]:
        # 2026-07-30 라이브 확인(scripts/orangex_find_open_orders_endpoint_v2.py,
        # scripts/orangex_verify_get_open_orders.py): 문서/기존 코드가 쓰던
        # `/private/get_open_order_by_instrument`(단수 "order")는 "No service found"였고,
        # 실제로 동작하는 이름은 `/private/get_open_orders_by_instrument`(복수 "orders")다
        # — cancel_by_id -> cancel과 같은 패턴의 단/복수 표기 문제였다. 실제로 미체결
        # 주문을 하나 걸고 조회해서 나타나는지, 취소 후 사라지는지까지 교차 검증했다
        # (get_positions처럼 "성공은 하지만 있어도 항상 빈 배열"인 함정이 아님을 확인).
        # 이걸로 engine/restart_recovery.py의 라이브 블로커가 해소됨.
        result = await self._client.call(
            "/private/get_open_orders_by_instrument", {"instrument_name": instrument}
        )
        orders = result if isinstance(result, list) else result.get("orders", [])
        # get_position()과 동일한 이유(2026-08-04, direction="both" 지원) — 헤지 모드
        # 계좌에서는 이 instrument에 롱/숏 양쪽 주문이 섞여서 온다. 이 어댑터가 담당하는
        # position_side가 아닌 주문은 애초에 내 것이 아니므로 걸러낸다. **주의**: 다른
        # 엔드포인트(get_order_state 등)에서 position_side 필드가 확인된 것과 달리, 이
        # get_open_orders_by_instrument 응답에도 동일한 필드가 있는지는 아직 라이브로
        # 별도 검증하지 못했다 — 같은 주문 객체 스키마를 공유할 것으로 추정한 것(합리적
        # 추정이지만 SPEC 0번 원칙상 라이브 재검증 필요, docs/phase3-plan.md 참고).
        expected_side = _DIRECTION_TO_POSITION_SIDE.get(self._position_side) if self._position_side else None
        if expected_side is not None:
            orders = [o for o in orders if o.get("position_side") == expected_side]
        return [self._parse_order_result(o, o.get("custom_order_id", "")) for o in orders]

    def watch_fills(self, instrument: str) -> AsyncIterator[Fill]:
        if self._ws_client is None:
            raise RuntimeError(
                "watch_fills()를 쓰려면 OrangeXAdapter 생성 시 ws_client(OrangeXWsClient)를 "
                "넘겨야 함 — exchange/orangex/ws_client.py 참고"
            )
        return self._watch_fills_gen(instrument)

    async def _watch_fills_gen(self, instrument: str) -> AsyncIterator[Fill]:
        # 2026-07-30 라이브 완전 검증(scripts/orangex_observe_live_fill_ws.py, 사용자
        # 명시적 요청으로 0.001 BTC 진입+즉시청산 실행): 연결/인증/구독뿐 아니라 실제
        # 체결 알림의 필드 스키마까지 확인했다. 실제 payload 예시(요약):
        #   {"direction":"buy","amount":"0.001","price":"64606.4","fee":"0.02713469",
        #    "trade_id":"...","order_id":"...","instrument_name":"BTC-USDT-PERPETUAL", ...}
        # (tests/test_orangex_adapter.py의 test_watch_fills_parses_real_live_trade_payload에
        # 원본 그대로 고정해둠). `custom_order_id`는 이 실제 payload에 아예 없었다 — 아래
        # `_parse_trade_to_fill`이 빈 문자열로 방어적으로 처리한다(FillRouter는 order_id로만
        # 매칭하므로 라우팅에 영향 없음).
        channel = f"user.trades.{instrument}.raw"
        if not self._ws_client.is_connected:
            await self._ws_client.connect()
        await self._ws_client.subscribe([channel])

        async for msg in self._ws_client.notifications():
            if msg.get("method") != "subscription":
                continue
            params = msg.get("params", {})
            if params.get("channel") != channel:
                continue
            trades = params.get("data")
            if not isinstance(trades, list):
                raise OrangeXResponseSchemaError(
                    f"{channel} 알림의 data가 리스트가 아님 — 원본: {msg!r}"
                )
            for trade in trades:
                yield self._parse_trade_to_fill(trade)

    def _parse_trade_to_fill(self, trade: dict[str, Any]) -> Fill:
        try:
            order_id = str(trade["order_id"])
            side = trade["direction"]
            price = Decimal(str(trade["price"]))
            qty = Decimal(str(trade["amount"]))
            fee = Decimal(str(trade["fee"]))
        except (KeyError, TypeError) as e:
            # 2026-07-30 라이브로 스키마 확정됐지만(위 _watch_fills_gen 참고), 혹시라도
            # 다른 상황(다른 order_type 등)에서 필드가 빠지면 추측 대신 여기서 막는다.
            raise OrangeXResponseSchemaError(
                f"user.trades 알림에 예상 필드가 없음: {e!r} — 원본: {trade!r}"
            ) from e
        if side not in ("buy", "sell"):
            raise OrangeXResponseSchemaError(f"trade.direction 예상치 못한 값: {side!r} — 원본: {trade!r}")

        # custom_order_id는 place_limit_order 응답에서 빈 문자열로 돌아온 전례가 있다
        # (docs/api-notes.md §6 항목14) — 여기서는 필수로 요구하지 않고 없으면 빈 문자열로
        # 둔다. FillRouter는 order_id로만 매칭하므로 이 필드가 비어도 라우팅엔 영향 없음.
        client_order_id = str(trade.get("custom_order_id", ""))

        return Fill(order_id=order_id, client_order_id=client_order_id, side=side, price=price, qty=qty, fee=fee)

    def _parse_order_result(self, result: dict[str, Any], client_order_id: str) -> OrderResult:
        order = result.get("order", result) if isinstance(result, dict) else result
        try:
            order_id = str(order["order_id"])
            order_state = order["order_state"]
            filled_qty = Decimal(str(order.get("filled_amount", "0")))
            average_price_raw = order.get("average_price")
            average_price = Decimal(str(average_price_raw)) if average_price_raw else None
        except (KeyError, TypeError) as e:
            raise OrangeXResponseSchemaError(
                f"주문 응답에 예상 필드가 없음: {e!r} — 원본: {order!r}"
            ) from e

        status = _ORDER_STATE_TO_STATUS.get(order_state)
        if status is None:
            raise OrangeXResponseSchemaError(
                f"알 수 없는 order_state: {order_state!r} — 원본: {order!r}"
            )
        if status == "open" and filled_qty > 0:
            status = "partially_filled"

        return OrderResult(
            order_id=order_id,
            client_order_id=client_order_id,
            status=status,
            filled_qty=filled_qty,
            avg_fill_price=average_price,
        )
