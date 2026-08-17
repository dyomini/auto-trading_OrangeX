"""OrangeXAdapter 유닛 테스트. 실제 OrangeXClient/네트워크 대신 FakeClient로 응답을 주입한다."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from exchange.base import MarketOrderRequest, OrderRequest, StopOrderRequest
from exchange.orangex.adapter import OrangeXAdapter, OrangeXResponseSchemaError

INSTRUMENT = "BTC-USDT-PERPETUAL"


class QueuedClient:
    """메서드별로 순서대로 소비되는 응답/예외 큐 — get_order_state 재시도(성공하기 전
    N번 실패)처럼 같은 메서드를 여러 번 다르게 응답해야 하는 테스트 전용."""

    def __init__(self, queues: dict[str, list]) -> None:
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls: list[tuple[str, dict, bool]] = []

    async def call(self, method, params=None, authed=True):
        self.calls.append((method, dict(params or {}), authed))
        item = self.queues[method].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict, bool]] = []

    async def call(self, method, params=None, authed=True):
        self.calls.append((method, dict(params or {}), authed))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_get_balance_parses_flat_envelope():
    client = FakeClient(
        {
            "/private/get_assets_info": {
                "available_funds": "9500.5",
                "wallet_balance": "10000",
                "total_margin_balance": "10020.3",
            }
        }
    )
    adapter = OrangeXAdapter(client)

    balance = await adapter.get_balance()

    assert balance.available == Decimal("9500.5")
    assert balance.equity == Decimal("10020.3")
    method, params, authed = client.calls[0]
    assert method == "/private/get_assets_info"
    assert params == {"asset_type": ["PERPETUAL"]}


@pytest.mark.asyncio
async def test_get_balance_parses_nested_envelope():
    client = FakeClient(
        {
            "/private/get_assets_info": {
                "PERPETUAL": {"available_funds": "1", "total_margin_balance": "2"}
            }
        }
    )
    adapter = OrangeXAdapter(client)

    balance = await adapter.get_balance()

    assert balance.available == Decimal("1")
    assert balance.equity == Decimal("2")


@pytest.mark.asyncio
async def test_get_balance_missing_fields_raises_schema_error():
    client = FakeClient({"/private/get_assets_info": {"unexpected": "shape"}})
    adapter = OrangeXAdapter(client)

    with pytest.raises(OrangeXResponseSchemaError):
        await adapter.get_balance()


@pytest.mark.asyncio
async def test_get_position_returns_flat_when_no_matching_position():
    client = FakeClient({"/private/get_user_position": []})
    adapter = OrangeXAdapter(client)

    position = await adapter.get_position(INSTRUMENT)

    assert position.direction is None
    assert position.qty == Decimal("0")
    method, params, authed = client.calls[0]
    assert method == "/private/get_user_position"
    assert params == {"instrument_name": INSTRUMENT}


@pytest.mark.asyncio
async def test_get_position_parses_short_position():
    client = FakeClient(
        {
            "/private/get_user_position": [
                {
                    "instrument_name": INSTRUMENT,
                    "position_side": "SHORT",
                    "size": "-0.029",
                    "average_price": "64000",
                }
            ]
        }
    )
    adapter = OrangeXAdapter(client)

    position = await adapter.get_position(INSTRUMENT)

    assert position.direction == "short"
    assert position.qty == Decimal("0.029")
    assert position.avg_price == Decimal("64000")


@pytest.mark.asyncio
async def test_get_position_parses_long_position():
    client = FakeClient(
        {
            "/private/get_user_position": [
                {
                    "instrument_name": INSTRUMENT,
                    "position_side": "LONG",
                    "size": "0.5",
                    "average_price": "60000",
                }
            ]
        }
    )
    adapter = OrangeXAdapter(client)

    position = await adapter.get_position(INSTRUMENT)

    assert position.direction == "long"
    assert position.qty == Decimal("0.5")
    assert position.avg_price == Decimal("60000")


@pytest.mark.asyncio
async def test_get_position_raises_schema_error_for_unmapped_live_fields():
    client = FakeClient(
        {"/private/get_user_position": [{"instrument_name": INSTRUMENT, "size": "1"}]}
    )
    adapter = OrangeXAdapter(client)

    with pytest.raises(OrangeXResponseSchemaError):
        await adapter.get_position(INSTRUMENT)


@pytest.mark.asyncio
async def test_get_position_raises_schema_error_for_unexpected_position_side():
    client = FakeClient(
        {
            "/private/get_user_position": [
                {
                    "instrument_name": INSTRUMENT,
                    "position_side": "FLAT",
                    "size": "0",
                    "average_price": "0",
                }
            ]
        }
    )
    adapter = OrangeXAdapter(client)

    with pytest.raises(OrangeXResponseSchemaError):
        await adapter.get_position(INSTRUMENT)


@pytest.mark.asyncio
async def test_get_position_filters_by_own_position_side_in_hedge_mode():
    """2026-08-04, direction="both"(롱/숏 동시 운용) 지원 — 헤지 모드 계좌는 같은
    instrument에 LONG/SHORT 포지션이 동시에 존재할 수 있다. 이 어댑터가 담당하는
    position_side가 아닌 포지션은 절대 자기 것으로 착각하면 안 된다."""
    client = FakeClient(
        {
            "/private/get_user_position": [
                {"instrument_name": INSTRUMENT, "position_side": "LONG", "size": "0.5", "average_price": "60000"},
                {"instrument_name": INSTRUMENT, "position_side": "SHORT", "size": "-0.3", "average_price": "65000"},
            ]
        }
    )
    long_adapter = OrangeXAdapter(client, position_side="long")
    short_adapter = OrangeXAdapter(client, position_side="short")

    long_position = await long_adapter.get_position(INSTRUMENT)
    short_position = await short_adapter.get_position(INSTRUMENT)

    assert long_position.direction == "long"
    assert long_position.qty == Decimal("0.5")
    assert short_position.direction == "short"
    assert short_position.qty == Decimal("0.3")


@pytest.mark.asyncio
async def test_get_position_without_position_side_keeps_first_match_behavior():
    """position_side를 안 받은 어댑터(one-way 모드 등 기존 단일방향 사용)는 기존처럼
    첫 매치를 그대로 쓴다 — 하위호환성 확인."""
    client = FakeClient(
        {
            "/private/get_user_position": [
                {"instrument_name": INSTRUMENT, "position_side": "LONG", "size": "0.5", "average_price": "60000"},
            ]
        }
    )
    adapter = OrangeXAdapter(client)

    position = await adapter.get_position(INSTRUMENT)

    assert position.direction == "long"
    assert position.qty == Decimal("0.5")


@pytest.mark.asyncio
async def test_get_contract_spec_parses_matching_instrument():
    client = FakeClient(
        {
            "/public/get_instruments": {
                "instruments": [
                    {"instrument_name": "ETH-USDT-PERPETUAL", "tick_size": "2.5", "min_qty": "0.01", "min_notional": "10"},
                    {"instrument_name": INSTRUMENT, "tick_size": "50", "min_qty": "0.0001", "min_notional": "10", "contract_size": "1"},
                ]
            }
        }
    )
    adapter = OrangeXAdapter(client)

    spec = await adapter.get_contract_spec(INSTRUMENT)

    assert spec.tick_size == Decimal("50")
    assert spec.min_qty == Decimal("0.0001")
    assert spec.min_notional == Decimal("10")
    method, params, authed = client.calls[0]
    assert method == "/public/get_instruments"
    assert authed is False


@pytest.mark.asyncio
async def test_get_contract_spec_missing_instrument_raises():
    client = FakeClient({"/public/get_instruments": {"instruments": []}})
    adapter = OrangeXAdapter(client)

    with pytest.raises(OrangeXResponseSchemaError):
        await adapter.get_contract_spec(INSTRUMENT)


@pytest.mark.asyncio
async def test_get_ticker_parses_last_price():
    client = FakeClient(
        {
            "/public/ticker": {
                "last_price": "64123.5",
                "mark_price": "64120.1",
                "best_bid_price": "64120",
                "best_ask_price": "64125",
            }
        }
    )
    adapter = OrangeXAdapter(client)

    ticker = await adapter.get_ticker(INSTRUMENT)

    assert ticker.instrument == INSTRUMENT
    assert ticker.last_price == Decimal("64123.5")
    method, params, authed = client.calls[0]
    assert method == "/public/ticker"
    assert params == {"instrument_name": INSTRUMENT}
    assert authed is False


@pytest.mark.asyncio
async def test_get_ticker_missing_last_price_raises():
    client = FakeClient({"/public/ticker": {"mark_price": "64120.1"}})
    adapter = OrangeXAdapter(client)

    with pytest.raises(OrangeXResponseSchemaError):
        await adapter.get_ticker(INSTRUMENT)


@pytest.mark.asyncio
async def test_place_limit_order_buy_builds_expected_params_and_parses_result():
    client = FakeClient(
        {
            "/private/buy": {"order": {"order_id": "ord-1", "custom_order_id": "coid-1"}},
            "/private/get_order_state": {
                "order_id": "ord-1", "order_state": "open", "filled_amount": "0", "average_price": None,
            },
        }
    )
    adapter = OrangeXAdapter(client)
    order = OrderRequest(
        instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"),
        client_order_id="coid-1", reduce_only=False, post_only=True,
    )

    result = await adapter.place_limit_order(order)

    assert result.order_id == "ord-1"
    assert result.client_order_id == "coid-1"
    assert result.status == "open"
    assert result.filled_qty == Decimal("0")

    method, params, authed = client.calls[0]
    assert method == "/private/buy"
    assert authed is True
    assert params["instrument_name"] == INSTRUMENT
    assert params["amount"] == "0.001"
    assert params["price"] == "64000"
    assert params["type"] == "limit"
    assert params["custom_order_id"] == "coid-1"
    assert params["post_only"] is True
    assert params["reduce_only"] is False
    assert "position_side" not in params

    method2, params2, _ = client.calls[1]
    assert method2 == "/private/get_order_state"
    assert params2 == {"order_id": "ord-1"}


@pytest.mark.asyncio
async def test_place_limit_order_tags_position_side_when_configured():
    client = FakeClient(
        {
            "/private/sell": {"order": {"order_id": "ord-hedge", "custom_order_id": "coid-hedge"}},
            "/private/get_order_state": {
                "order_id": "ord-hedge", "order_state": "open", "filled_amount": "0",
            },
        }
    )
    adapter = OrangeXAdapter(client, position_side="short")
    order = OrderRequest(
        instrument=INSTRUMENT, side="sell", price=Decimal("64660"), qty=Decimal("0.002"),
        client_order_id="coid-hedge",
    )

    await adapter.place_limit_order(order)

    method, params, _ = client.calls[0]
    assert params["position_side"] == "SHORT"


@pytest.mark.asyncio
async def test_place_limit_order_sell_uses_sell_method():
    client = FakeClient(
        {
            "/private/sell": {"order": {"order_id": "ord-2"}},
            "/private/get_order_state": {"order_id": "ord-2", "order_state": "open", "filled_amount": "0"},
        }
    )
    adapter = OrangeXAdapter(client)
    order = OrderRequest(instrument=INSTRUMENT, side="sell", price=Decimal("65000"), qty=Decimal("0.001"), client_order_id="coid-2")

    result = await adapter.place_limit_order(order)

    assert result.order_id == "ord-2"
    assert client.calls[0][0] == "/private/sell"


@pytest.mark.asyncio
async def test_place_limit_order_partially_filled_when_open_with_fill():
    client = FakeClient(
        {
            "/private/buy": {"order": {"order_id": "ord-3"}},
            "/private/get_order_state": {
                "order_id": "ord-3", "order_state": "open", "filled_amount": "0.0005", "average_price": "64000",
            },
        }
    )
    adapter = OrangeXAdapter(client)
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id="coid-3")

    result = await adapter.place_limit_order(order)

    assert result.status == "partially_filled"
    assert result.filled_qty == Decimal("0.0005")
    assert result.avg_fill_price == Decimal("64000")


@pytest.mark.asyncio
async def test_place_limit_order_canceled_hedge_mode_conflict():
    # 2026-07-30: position_side 없이 헤지 모드 계좌에 주문을 넣으면 실제로 이 상태가 나온다.
    client = FakeClient(
        {
            "/private/buy": {"order": {"order_id": "ord-canceled"}},
            "/private/get_order_state": {
                "order_id": "ord-canceled", "order_state": "canceled", "filled_amount": "0", "error_code": 5998,
            },
        }
    )
    adapter = OrangeXAdapter(client)
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id="coid-5")

    result = await adapter.place_limit_order(order)

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_place_limit_order_unknown_order_state_raises_schema_error():
    client = FakeClient(
        {
            "/private/buy": {"order": {"order_id": "ord-6"}},
            "/private/get_order_state": {"order_id": "ord-6", "order_state": "mystery_state", "filled_amount": "0"},
        }
    )
    adapter = OrangeXAdapter(client)
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id="coid-6")

    with pytest.raises(OrangeXResponseSchemaError):
        await adapter.place_limit_order(order)


@pytest.mark.asyncio
async def test_place_limit_order_missing_order_id_raises_schema_error():
    client = FakeClient({"/private/buy": {"unexpected": "shape"}})
    adapter = OrangeXAdapter(client)
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id="coid-4")

    with pytest.raises(OrangeXResponseSchemaError):
        await adapter.place_limit_order(order)


@pytest.mark.asyncio
async def test_place_stop_order_builds_expected_params_and_parses_result():
    # 2026-07-30 라이브 검증(docs/api-notes.md §6 항목18): condition_type=STOP +
    # trigger_price + trigger_price_type=2(last)로 실제 거래소 등록형 SL이 동작함.
    client = FakeClient(
        {
            "/private/sell": {"order": {"order_id": "stop-1", "custom_order_id": "coid-stop-1"}},
            "/private/get_order_state": {
                "order_id": "stop-1", "order_state": "open", "filled_amount": "0",
            },
        }
    )
    adapter = OrangeXAdapter(client, position_side="long")
    order = StopOrderRequest(
        instrument=INSTRUMENT, side="sell", trigger_price=Decimal("62000"),
        qty=Decimal("0.001"), client_order_id="coid-stop-1", reduce_only=True,
    )

    result = await adapter.place_stop_order(order)

    assert result.order_id == "stop-1"
    assert result.status == "open"

    method, params, authed = client.calls[0]
    assert method == "/private/sell"
    assert params["instrument_name"] == INSTRUMENT
    assert params["amount"] == "0.001"
    assert params["price"] == "62000"
    assert params["reduce_only"] is True
    assert params["condition_type"] == "STOP"
    assert params["trigger_price"] == "62000"
    assert params["trigger_price_type"] == 2
    assert params["position_side"] == "LONG"


@pytest.mark.asyncio
async def test_place_stop_order_mark_price_type_maps_to_code_1():
    client = FakeClient(
        {
            "/private/buy": {"order": {"order_id": "stop-2"}},
            "/private/get_order_state": {"order_id": "stop-2", "order_state": "open", "filled_amount": "0"},
        }
    )
    adapter = OrangeXAdapter(client)
    order = StopOrderRequest(
        instrument=INSTRUMENT, side="buy", trigger_price=Decimal("70000"),
        qty=Decimal("0.001"), client_order_id="coid-stop-2", trigger_price_type="mark",
    )

    await adapter.place_stop_order(order)

    method, params, _ = client.calls[0]
    assert method == "/private/buy"
    assert params["trigger_price_type"] == 1


@pytest.mark.asyncio
async def test_place_stop_order_missing_order_id_raises_schema_error():
    client = FakeClient({"/private/sell": {"unexpected": "shape"}})
    adapter = OrangeXAdapter(client)
    order = StopOrderRequest(
        instrument=INSTRUMENT, side="sell", trigger_price=Decimal("62000"),
        qty=Decimal("0.001"), client_order_id="coid-stop-3",
    )

    with pytest.raises(OrangeXResponseSchemaError):
        await adapter.place_stop_order(order)


@pytest.mark.asyncio
async def test_place_market_order_returns_filled_result():
    client = FakeClient({
        "/private/buy": {"order": {"order_id": "ord-mkt-1", "custom_order_id": "coid-mkt-1"}},
        "/private/get_order_state": {"order_id": "ord-mkt-1", "order_state": "filled", "filled_amount": "0.001", "average_price": "64000"},
    })
    adapter = OrangeXAdapter(client)
    order = MarketOrderRequest(instrument=INSTRUMENT, side="buy", qty=Decimal("0.001"), client_order_id="coid-mkt-1")

    result = await adapter.place_market_order(order)

    assert result.status == "filled"
    assert result.order_id == "ord-mkt-1"
    method, params, _ = client.calls[0]
    assert method == "/private/buy"
    assert params["type"] == "market"
    assert "price" not in params  # 시장가는 price 파라미터 자체가 없어야 함


@pytest.mark.asyncio
async def test_place_market_order_retries_get_order_state_on_transient_failure():
    """2026-07-30 라이브로 재현된 문제(docs/api-notes.md §6 항목16) — 주문 접수 직후
    get_order_state가 KeyError로 실패할 수 있어 재시도해야 한다."""
    client = QueuedClient({
        "/private/buy": [{"order": {"order_id": "ord-mkt-2"}}],
        "/private/get_order_state": [
            KeyError("result"), KeyError("result"),
            {"order_id": "ord-mkt-2", "order_state": "filled", "filled_amount": "0.001", "average_price": "64000"},
        ],
    })
    adapter = OrangeXAdapter(client)
    order = MarketOrderRequest(instrument=INSTRUMENT, side="buy", qty=Decimal("0.001"), client_order_id="coid-mkt-2")

    with patch("exchange.orangex.adapter.asyncio.sleep", new=AsyncMock()):
        result = await adapter.place_market_order(order)

    assert result.status == "filled"
    get_order_state_calls = [c for c in client.calls if c[0] == "/private/get_order_state"]
    assert len(get_order_state_calls) == 3


@pytest.mark.asyncio
async def test_place_market_order_raises_after_exhausting_retries():
    client = QueuedClient({
        "/private/buy": [{"order": {"order_id": "ord-mkt-3"}}],
        "/private/get_order_state": [KeyError("result")] * 4,  # 즉시 1회 + 재시도 3회
    })
    adapter = OrangeXAdapter(client)
    order = MarketOrderRequest(instrument=INSTRUMENT, side="buy", qty=Decimal("0.001"), client_order_id="coid-mkt-3")

    with patch("exchange.orangex.adapter.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(OrangeXResponseSchemaError):
            await adapter.place_market_order(order)

    get_order_state_calls = [c for c in client.calls if c[0] == "/private/get_order_state"]
    assert len(get_order_state_calls) == 4


def _make_http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.orangex.com/api/v1/private/get_order_state")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"{status_code} error", request=request, response=response)


@pytest.mark.asyncio
async def test_place_market_order_retries_get_order_state_on_network_error():
    """2026-08-04 코드 리뷰로 확장한 재시도 범위 — KeyError뿐 아니라 네트워크 순단
    (httpx.TransportError)도 일시적 실패로 보고 재시도해야 한다."""
    client = QueuedClient({
        "/private/buy": [{"order": {"order_id": "ord-mkt-4"}}],
        "/private/get_order_state": [
            httpx.ConnectError("connection reset"),
            {"order_id": "ord-mkt-4", "order_state": "filled", "filled_amount": "0.001", "average_price": "64000"},
        ],
    })
    adapter = OrangeXAdapter(client)
    order = MarketOrderRequest(instrument=INSTRUMENT, side="buy", qty=Decimal("0.001"), client_order_id="coid-mkt-4")

    with patch("exchange.orangex.adapter.asyncio.sleep", new=AsyncMock()):
        result = await adapter.place_market_order(order)

    assert result.status == "filled"
    get_order_state_calls = [c for c in client.calls if c[0] == "/private/get_order_state"]
    assert len(get_order_state_calls) == 2


@pytest.mark.asyncio
async def test_place_market_order_retries_get_order_state_on_server_5xx():
    client = QueuedClient({
        "/private/buy": [{"order": {"order_id": "ord-mkt-5"}}],
        "/private/get_order_state": [
            _make_http_status_error(503),
            {"order_id": "ord-mkt-5", "order_state": "filled", "filled_amount": "0.001", "average_price": "64000"},
        ],
    })
    adapter = OrangeXAdapter(client)
    order = MarketOrderRequest(instrument=INSTRUMENT, side="buy", qty=Decimal("0.001"), client_order_id="coid-mkt-5")

    with patch("exchange.orangex.adapter.asyncio.sleep", new=AsyncMock()):
        result = await adapter.place_market_order(order)

    assert result.status == "filled"
    get_order_state_calls = [c for c in client.calls if c[0] == "/private/get_order_state"]
    assert len(get_order_state_calls) == 2


@pytest.mark.asyncio
async def test_place_market_order_does_not_retry_get_order_state_on_client_4xx():
    """4xx는 시간이 지난다고 저절로 해결되지 않는 오류(인증/파라미터 문제 등)라 재시도
    없이 즉시 올려야 한다 — 재시도로 실패를 감추면 실제 버그를 놓칠 수 있다."""
    client = QueuedClient({
        "/private/buy": [{"order": {"order_id": "ord-mkt-6"}}],
        "/private/get_order_state": [_make_http_status_error(401)],
    })
    adapter = OrangeXAdapter(client)
    order = MarketOrderRequest(instrument=INSTRUMENT, side="buy", qty=Decimal("0.001"), client_order_id="coid-mkt-6")

    with patch("exchange.orangex.adapter.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.place_market_order(order)

    get_order_state_calls = [c for c in client.calls if c[0] == "/private/get_order_state"]
    assert len(get_order_state_calls) == 1  # 재시도 없이 즉시 실패


@pytest.mark.asyncio
async def test_cancel_order_calls_cancel():
    # 2026-07-30 라이브 검증: 문서상 메서드명 cancel_by_id는 "No service found"를
    # 반환하며, 실제로 동작하는 메서드명은 /private/cancel이다 (docs/api-notes.md §6 항목15).
    client = FakeClient({"/private/cancel": {"order_id": "ord-1"}})
    adapter = OrangeXAdapter(client)

    await adapter.cancel_order("ord-1")

    method, params, authed = client.calls[0]
    assert method == "/private/cancel"
    assert params == {"order_id": "ord-1"}


@pytest.mark.asyncio
async def test_get_open_orders_parses_list():
    client = FakeClient(
        {
            "/private/get_open_orders_by_instrument": [
                {"order_id": "a", "order_state": "open", "filled_amount": "0", "custom_order_id": "c1"},
                {"order_id": "b", "order_state": "filled", "filled_amount": "0.002", "custom_order_id": "c2"},
            ]
        }
    )
    adapter = OrangeXAdapter(client)

    orders = await adapter.get_open_orders(INSTRUMENT)

    assert [o.order_id for o in orders] == ["a", "b"]
    assert orders[0].client_order_id == "c1"
    assert orders[1].status == "filled"

    method, params, authed = client.calls[0]
    assert method == "/private/get_open_orders_by_instrument"
    assert params == {"instrument_name": INSTRUMENT}


@pytest.mark.asyncio
async def test_get_open_orders_filters_by_own_position_side_in_hedge_mode():
    """2026-08-04, direction="both" 지원 — 헤지 모드 계좌에서 롱/숏 주문이 섞여서
    와도 이 어댑터가 담당하는 position_side의 주문만 반환해야 한다."""
    client = FakeClient(
        {
            "/private/get_open_orders_by_instrument": [
                {"order_id": "long-1", "order_state": "open", "filled_amount": "0", "custom_order_id": "grid-0-aaa", "position_side": "LONG"},
                {"order_id": "short-1", "order_state": "open", "filled_amount": "0", "custom_order_id": "grid-0-bbb", "position_side": "SHORT"},
            ]
        }
    )
    long_adapter = OrangeXAdapter(client, position_side="long")
    short_adapter = OrangeXAdapter(client, position_side="short")

    long_orders = await long_adapter.get_open_orders(INSTRUMENT)
    short_orders = await short_adapter.get_open_orders(INSTRUMENT)

    assert [o.order_id for o in long_orders] == ["long-1"]
    assert [o.order_id for o in short_orders] == ["short-1"]


@pytest.mark.asyncio
async def test_watch_fills_requires_ws_client():
    adapter = OrangeXAdapter(FakeClient({}))
    with pytest.raises(RuntimeError, match="ws_client"):
        adapter.watch_fills(INSTRUMENT)


class FakeWsClient:
    """exchange/orangex/ws_client.py의 OrangeXWsClient가 만족하는 최소 인터페이스."""

    def __init__(self, queued_notifications: list[dict]) -> None:
        self._queued = queued_notifications
        self.is_connected = True
        self.connect_called = False
        self.subscribed_channels: Optional[list[str]] = None

    async def connect(self) -> None:
        self.connect_called = True
        self.is_connected = True

    async def subscribe(self, channels: list[str]) -> list[str]:
        self.subscribed_channels = channels
        return channels

    async def notifications(self):
        for msg in self._queued:
            yield msg


def make_trade_notification(channel: str, trades: list[dict]) -> dict:
    return {"jsonrpc": "2.0", "method": "subscription", "params": {"channel": channel, "data": trades}}


@pytest.mark.asyncio
async def test_watch_fills_parses_valid_trade_notification():
    channel = f"user.trades.{INSTRUMENT}.raw"
    trade = {
        "order_id": "ord-1", "trade_id": "t-1", "instrument_name": INSTRUMENT,
        "direction": "buy", "amount": "0.01", "price": "64000", "fee": "0.0002",
        "custom_order_id": "grid-0-abcd1234",
    }
    ws_client = FakeWsClient([make_trade_notification(channel, [trade])])
    adapter = OrangeXAdapter(FakeClient({}), ws_client=ws_client)

    fills = [fill async for fill in adapter.watch_fills(INSTRUMENT)]

    assert len(fills) == 1
    fill = fills[0]
    assert fill.order_id == "ord-1"
    assert fill.side == "buy"
    assert fill.price == Decimal("64000")
    assert fill.qty == Decimal("0.01")
    assert fill.fee == Decimal("0.0002")
    assert fill.client_order_id == "grid-0-abcd1234"
    assert ws_client.subscribed_channels == [channel]
    assert ws_client.connect_called is False  # 이미 is_connected=True였으므로 재연결 안 함


@pytest.mark.asyncio
async def test_watch_fills_parses_real_live_trade_payload():
    """2026-07-30 실제 라이브 체결로 캡처한 원본 payload를 그대로 고정한 회귀 테스트
    (scripts/orangex_observe_live_fill_ws.py, 사용자 명시적 요청 — 0.001 BTC 진입 후
    즉시 청산). fee 필드명이 정확히 "fee"임이 이걸로 확정됐고, custom_order_id는 이
    실제 payload에 아예 없었다(그래도 파서가 죽지 않고 빈 문자열로 처리하는지 확인)."""
    channel = f"user.trades.{INSTRUMENT}.raw"
    live_notification = {
        "jsonrpc": "2.0", "method": "subscription",
        "params": {
            "channel": channel,
            "data": [{
                "instrId": 28, "direction": "buy", "amount": "0.001", "price": "64606.4",
                "timestamp": 1785407209150, "role": "taker", "rpl": "0", "posId": 1,
                "positionSide": "LONG", "leverage": 50, "marginType": "cross",
                "fee": "0.02713469", "feeCoupon": "0", "feeActual": "0.02713469",
                "feeReal": "0.02713469", "source": "api", "trade_id": "38754630328",
                "order_id": "828697200877981696", "instrument_name": INSTRUMENT,
                "show_name": "BTCUSDT", "order_type": "limit", "fee_use_coupon": False,
                "fee_coin_type": "USDT", "index_price": "", "mark_price": "64606.3",
                "self_trade": False,
            }],
        },
    }
    ws_client = FakeWsClient([live_notification])
    adapter = OrangeXAdapter(FakeClient({}), ws_client=ws_client)

    fills = [fill async for fill in adapter.watch_fills(INSTRUMENT)]

    assert len(fills) == 1
    fill = fills[0]
    assert fill.order_id == "828697200877981696"
    assert fill.side == "buy"
    assert fill.price == Decimal("64606.4")
    assert fill.qty == Decimal("0.001")
    assert fill.fee == Decimal("0.02713469")
    assert fill.client_order_id == ""  # 이 실제 payload엔 custom_order_id가 아예 없었음


@pytest.mark.asyncio
async def test_watch_fills_connects_if_not_already_connected():
    channel = f"user.trades.{INSTRUMENT}.raw"
    ws_client = FakeWsClient([])
    ws_client.is_connected = False
    adapter = OrangeXAdapter(FakeClient({}), ws_client=ws_client)

    [fill async for fill in adapter.watch_fills(INSTRUMENT)]

    assert ws_client.connect_called is True


@pytest.mark.asyncio
async def test_watch_fills_ignores_other_channels_and_non_subscription_messages():
    channel = f"user.trades.{INSTRUMENT}.raw"
    other_channel_msg = make_trade_notification(f"user.orders.{INSTRUMENT}.raw", [{"anything": "x"}])
    non_subscription_msg = {"jsonrpc": "2.0", "id": "5", "result": {"ignored": True}}
    ws_client = FakeWsClient([other_channel_msg, non_subscription_msg])
    adapter = OrangeXAdapter(FakeClient({}), ws_client=ws_client)

    fills = [fill async for fill in adapter.watch_fills(INSTRUMENT)]

    assert fills == []


@pytest.mark.asyncio
async def test_watch_fills_raises_on_missing_fee_field():
    """fee는 이 프로젝트 어디에서도 라이브로 확인된 적 없는 필드 — 없으면 추측하지 않고
    명시적으로 실패해야 한다(SPEC 0번)."""
    channel = f"user.trades.{INSTRUMENT}.raw"
    trade = {"order_id": "ord-1", "direction": "buy", "amount": "0.01", "price": "64000"}
    ws_client = FakeWsClient([make_trade_notification(channel, [trade])])
    adapter = OrangeXAdapter(FakeClient({}), ws_client=ws_client)

    with pytest.raises(OrangeXResponseSchemaError):
        [fill async for fill in adapter.watch_fills(INSTRUMENT)]


@pytest.mark.asyncio
async def test_watch_fills_raises_on_unexpected_direction_value():
    channel = f"user.trades.{INSTRUMENT}.raw"
    trade = {"order_id": "ord-1", "direction": "long", "amount": "0.01", "price": "64000", "fee": "0"}
    ws_client = FakeWsClient([make_trade_notification(channel, [trade])])
    adapter = OrangeXAdapter(FakeClient({}), ws_client=ws_client)

    with pytest.raises(OrangeXResponseSchemaError):
        [fill async for fill in adapter.watch_fills(INSTRUMENT)]


@pytest.mark.asyncio
async def test_watch_fills_raises_when_data_is_not_a_list():
    channel = f"user.trades.{INSTRUMENT}.raw"
    malformed_msg = {"jsonrpc": "2.0", "method": "subscription", "params": {"channel": channel, "data": {"not": "a list"}}}
    ws_client = FakeWsClient([malformed_msg])
    adapter = OrangeXAdapter(FakeClient({}), ws_client=ws_client)

    with pytest.raises(OrangeXResponseSchemaError):
        [fill async for fill in adapter.watch_fills(INSTRUMENT)]


@pytest.mark.asyncio
async def test_aclose_closes_ws_client_but_not_shared_rest_client():
    """REST 클라이언트는 방향/사이클 간 공유되므로 어댑터가 닫으면 안 된다
    (2026-08-17, direction="auto"에서 사이클마다 어댑터를 새로 만들면서 필요해짐)."""

    class _SpyWs:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class _SpyRest:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

        async def call(self, method, params=None, authed=True):  # pragma: no cover
            raise AssertionError("이 테스트에서는 호출되지 않는다")

    ws, rest = _SpyWs(), _SpyRest()
    adapter = OrangeXAdapter(rest, ws_client=ws)

    await adapter.aclose()

    assert ws.closed is True
    assert rest.closed is False


@pytest.mark.asyncio
async def test_aclose_without_ws_client_is_noop():
    adapter = OrangeXAdapter(FakeClient({}))
    await adapter.aclose()  # 예외 없이 통과해야 함
