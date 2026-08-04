"""PaperAdapter(인메모리 체결 시뮬레이터) 유닛 테스트.

SPEC.md 82줄: "PaperAdapter 를 먼저 구현 — 인메모리 체결 시뮬레이터.
수수료·슬리피지·부분체결 반영." / 84줄: "모든 주문에 client_order_id(UUID) 부여
→ 재시도 시 중복 주문 방지."
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from exchange.base import ContractSpec, OrderRequest, StopOrderRequest
from exchange.paper import DuplicateClientOrderId, NoKnownPriceError, OrderNotFoundError, PaperAdapter

INSTRUMENT = "BTC-USDT-PERP"


def make_spec() -> ContractSpec:
    return ContractSpec(
        instrument=INSTRUMENT,
        tick_size=Decimal("50"),
        min_qty=Decimal("0.0001"),
        min_notional=Decimal("10"),
        contract_size=Decimal("1"),
    )


def make_adapter(**kwargs) -> PaperAdapter:
    defaults = dict(
        instrument=INSTRUMENT,
        contract_spec=make_spec(),
        initial_equity=Decimal("10000"),
        leverage=Decimal("20"),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0005"),
    )
    defaults.update(kwargs)
    return PaperAdapter(**defaults)


@pytest.mark.asyncio
async def test_get_ticker_raises_before_any_price_tick():
    adapter = make_adapter()

    with pytest.raises(NoKnownPriceError):
        await adapter.get_ticker(INSTRUMENT)


@pytest.mark.asyncio
async def test_get_ticker_returns_last_ticked_price():
    adapter = make_adapter()
    await adapter.on_price_tick(Decimal("64500"))

    ticker = await adapter.get_ticker(INSTRUMENT)

    assert ticker.instrument == INSTRUMENT
    assert ticker.last_price == Decimal("64500")


@pytest.mark.asyncio
async def test_place_limit_order_returns_open_status():
    adapter = make_adapter()
    coid = str(uuid.uuid4())
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id=coid)

    result = await adapter.place_limit_order(order)

    assert result.status == "open"
    assert result.filled_qty == Decimal("0")
    assert result.client_order_id == coid


@pytest.mark.asyncio
async def test_duplicate_client_order_id_is_rejected():
    adapter = make_adapter()
    coid = str(uuid.uuid4())
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id=coid)

    first = await adapter.place_limit_order(order)
    with pytest.raises(DuplicateClientOrderId):
        await adapter.place_limit_order(order)

    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert len(open_orders) == 1
    assert open_orders[0].order_id == first.order_id


@pytest.mark.asyncio
async def test_buy_order_fills_when_price_drops_to_limit():
    adapter = make_adapter()
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id=str(uuid.uuid4()))
    await adapter.place_limit_order(order)

    fills = await adapter.on_price_tick(Decimal("64100"))
    assert fills == []

    fills = await adapter.on_price_tick(Decimal("63950"))
    assert len(fills) == 1
    fill = fills[0]
    assert fill.price == Decimal("64000")
    assert fill.qty == Decimal("0.001")
    assert fill.fee == Decimal("0.001") * Decimal("64000") * Decimal("0.0002")

    position = await adapter.get_position(INSTRUMENT)
    assert position.direction == "long"
    assert position.qty == Decimal("0.001")
    assert position.avg_price == Decimal("64000")

    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert open_orders == []


@pytest.mark.asyncio
async def test_sell_order_realizes_pnl_against_long_position():
    adapter = make_adapter()
    buy = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id=str(uuid.uuid4()))
    await adapter.place_limit_order(buy)
    await adapter.on_price_tick(Decimal("64000"))

    balance_before = await adapter.get_balance()

    sell = OrderRequest(instrument=INSTRUMENT, side="sell", price=Decimal("64500"), qty=Decimal("0.001"), client_order_id=str(uuid.uuid4()), reduce_only=True)
    await adapter.place_limit_order(sell)
    fills = await adapter.on_price_tick(Decimal("64600"))

    assert len(fills) == 1
    assert fills[0].price == Decimal("64500")

    position = await adapter.get_position(INSTRUMENT)
    assert position.qty == Decimal("0")
    assert position.direction is None

    balance_after = await adapter.get_balance()
    gross_pnl = Decimal("0.001") * (Decimal("64500") - Decimal("64000"))
    fee = Decimal("0.001") * Decimal("64500") * Decimal("0.0002")
    assert balance_after.equity == balance_before.equity + gross_pnl - fee


@pytest.mark.asyncio
async def test_partial_fill_leaves_remainder_open():
    adapter = make_adapter()
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.002"), client_order_id=str(uuid.uuid4()))
    placed = await adapter.place_limit_order(order)

    result = await adapter.fill_order(placed.order_id, qty=Decimal("0.0012"), price=Decimal("64000"))
    assert result.status == "partially_filled"
    assert result.filled_qty == Decimal("0.0012")

    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert len(open_orders) == 1
    assert open_orders[0].filled_qty == Decimal("0.0012")

    result2 = await adapter.fill_order(placed.order_id, qty=Decimal("0.0008"), price=Decimal("64000"))
    assert result2.status == "filled"
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert open_orders == []


@pytest.mark.asyncio
async def test_cancel_order_removes_from_open_orders():
    adapter = make_adapter()
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.001"), client_order_id=str(uuid.uuid4()))
    placed = await adapter.place_limit_order(order)

    await adapter.cancel_order(placed.order_id)

    assert await adapter.get_open_orders(INSTRUMENT) == []
    with pytest.raises(OrderNotFoundError):
        await adapter.cancel_order(placed.order_id)


@pytest.mark.asyncio
async def test_stop_order_does_not_fire_when_condition_already_met_at_placement():
    """OrangeX 라이브 검증(docs/api-notes.md §6 항목18)으로 확인된 crossing-trigger 재현:
    등록 시점에 이미 트리거 조건이 참이면(가격이 이미 trigger_price를 지나 있으면)
    이후 같은 방향으로 틱이 와도 절대 발동하지 않는다."""
    adapter = make_adapter()
    buy = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.01"), client_order_id=str(uuid.uuid4()))
    await adapter.place_limit_order(buy)
    await adapter.on_price_tick(Decimal("64000"))  # long 포지션 보유 상태로 만듦, _last_price=64000

    # SELL stop: 조건은 price <= trigger_price. trigger를 현재가(64000)보다 낮게 걸면
    # "이미 조건 충족"(64000 <= trigger는 거짓이지만, 여기선 반대로 이미 만족되는 방향으로 구성) —
    # 실제로 이미 참인 상태를 만들려면 trigger_price를 현재가보다 높게 둔다: 64000 <= 64500 참.
    stop = StopOrderRequest(
        instrument=INSTRUMENT, side="sell", trigger_price=Decimal("64500"),
        qty=Decimal("0.01"), client_order_id=str(uuid.uuid4()), reduce_only=True,
    )
    result = await adapter.place_stop_order(stop)
    assert result.status == "open"

    # 이미 조건이 참인 상태(64000 <= 64500)에서 계속 그 조건을 유지하는 틱만 준다 —
    # crossing(거짓->참)이 없으므로 발동하면 안 된다.
    fills = await adapter.on_price_tick(Decimal("64100"))
    assert fills == []
    fills = await adapter.on_price_tick(Decimal("64200"))
    assert fills == []

    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert len(open_orders) == 1
    assert open_orders[0].status == "open"


@pytest.mark.asyncio
async def test_stop_order_fires_when_price_crosses_trigger_after_placement():
    adapter = make_adapter()
    buy = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.01"), client_order_id=str(uuid.uuid4()))
    await adapter.place_limit_order(buy)
    await adapter.on_price_tick(Decimal("64000"))  # _last_price=64000, long 포지션 보유

    # SELL stop(평단 대비 SL): trigger=63500. 등록 시점엔 조건(price<=63500)이 거짓(64000<=63500 아님).
    stop = StopOrderRequest(
        instrument=INSTRUMENT, side="sell", trigger_price=Decimal("63500"),
        qty=Decimal("0.01"), client_order_id=str(uuid.uuid4()), reduce_only=True,
    )
    await adapter.place_stop_order(stop)

    fills = await adapter.on_price_tick(Decimal("63800"))
    assert fills == []  # 아직 조건 미충족(63800 <= 63500 아님)

    fills = await adapter.on_price_tick(Decimal("63400"))  # 63400 <= 63500 -> 크로싱 발생
    assert len(fills) == 1
    fill = fills[0]
    assert fill.price == Decimal("63500")  # trigger_price에 체결
    assert fill.qty == Decimal("0.01")
    assert fill.fee == Decimal("0.01") * Decimal("63500") * adapter.taker_fee  # 트리거 체결은 taker

    position = await adapter.get_position(INSTRUMENT)
    assert position.direction is None  # 전량 청산됨

    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert open_orders == []


@pytest.mark.asyncio
async def test_stop_order_can_be_cancelled_before_trigger():
    adapter = make_adapter()
    stop = StopOrderRequest(
        instrument=INSTRUMENT, side="sell", trigger_price=Decimal("60000"),
        qty=Decimal("0.001"), client_order_id=str(uuid.uuid4()),
    )
    placed = await adapter.place_stop_order(stop)

    await adapter.cancel_order(placed.order_id)

    assert await adapter.get_open_orders(INSTRUMENT) == []
    with pytest.raises(OrderNotFoundError):
        await adapter.cancel_order(placed.order_id)


@pytest.mark.asyncio
async def test_get_balance_reflects_used_margin():
    adapter = make_adapter()
    order = OrderRequest(instrument=INSTRUMENT, side="buy", price=Decimal("64000"), qty=Decimal("0.002"), client_order_id=str(uuid.uuid4()))
    await adapter.place_limit_order(order)
    await adapter.on_price_tick(Decimal("64000"))

    balance = await adapter.get_balance()
    used_margin = Decimal("0.002") * Decimal("64000") / Decimal("20")
    fee = Decimal("0.002") * Decimal("64000") * Decimal("0.0002")
    expected_equity = Decimal("10000") - fee
    assert balance.equity == expected_equity
    assert balance.available == expected_equity - used_margin
