"""engine/fill_router.py 유닛 테스트.

route()는 개별 Fill 하나를 직접 넘겨 매칭/디스패치 로직만 검증하고,
run()은 PaperAdapter.watch_fills()를 백그라운드 태스크로 돌려 실제 체결이
큐를 타고 엔진까지 도달하는 전체 경로를 검증한다.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from engine.fill_router import FillRouter
from engine.grid_engine import EngineState, GridEngine
from exchange.base import ContractSpec, Fill, StopOrderRequest
from exchange.paper import PaperAdapter
from strategy.grid import GridStepResult

INSTRUMENT = "BTC-USDT-PERP"


def make_row(index: int, major_tier: int, entry_price: Decimal, avg_price: Decimal, tp: Decimal, sl: Decimal) -> GridStepResult:
    return GridStepResult(
        index=index,
        major_tier=major_tier,
        sub_step=1,
        entry_price=entry_price,
        weight=Decimal("1"),
        step_qty=Decimal("0.01"),
        step_margin=Decimal("100"),
        cum_qty=Decimal("0.01") * (index + 1),
        cum_margin=Decimal("100") * (index + 1),
        avg_price=avg_price,
        available_balance=Decimal("1000"),
        liq_price=Decimal("50000"),
        target_roe=Decimal("0.1"),
        target_tp_price=tp,
        sl_price=sl,
    )


def make_grid_rows() -> list[GridStepResult]:
    return [
        make_row(0, 1, Decimal("64000"), Decimal("64000"), Decimal("64640"), Decimal("62080")),
        make_row(1, 2, Decimal("63900"), Decimal("63950"), Decimal("64590"), Decimal("62031")),
        make_row(2, 3, Decimal("63800"), Decimal("63900"), Decimal("64220"), Decimal("61983")),
        make_row(3, 4, Decimal("63700"), Decimal("63850"), Decimal("64170"), Decimal("61934")),
        make_row(4, 5, Decimal("63600"), Decimal("63800"), Decimal("64076"), Decimal("61886")),
    ]


def make_spec() -> ContractSpec:
    return ContractSpec(
        instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.0001"),
        min_notional=Decimal("10"), contract_size=Decimal("1"),
    )


def make_adapter() -> PaperAdapter:
    return PaperAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )


def make_engine(adapter: PaperAdapter, rows: list[GridStepResult], max_open: int = 5) -> GridEngine:
    return GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=max_open)


@pytest.mark.asyncio
async def test_route_grid_entry_fill_calls_on_fill():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = make_engine(adapter, rows)
    router = FillRouter(adapter=adapter, engine=engine, instrument=INSTRUMENT)
    await engine.start_laddering()

    order_id = engine.resting_grid_order_ids[0]
    fill = Fill(order_id=order_id, client_order_id="irrelevant", side="buy", price=rows[0].entry_price, qty=rows[0].step_qty, fee=Decimal("0"))

    await router.route(fill)

    assert engine.filled_step_count == 1
    assert engine.open_qty == Decimal("0.01")
    assert 0 not in engine.resting_grid_order_ids


@pytest.mark.asyncio
async def test_route_tp_fill_calls_on_tp_filled():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = make_engine(adapter, rows)
    router = FillRouter(adapter=adapter, engine=engine, instrument=INSTRUMENT)
    await engine.start_laddering()
    await engine.on_fill(0)  # TP 등록됨

    tp_order_id = engine.tp_order_id
    fill = Fill(order_id=tp_order_id, client_order_id="irrelevant", side="sell", price=rows[0].target_tp_price, qty=engine.open_qty, fee=Decimal("0"))

    await router.route(fill)

    assert engine.state == EngineState.COOLDOWN
    assert engine.tp_order_id is None
    assert engine.open_qty == Decimal("0")


@pytest.mark.asyncio
async def test_route_sl_fill_calls_on_sl_filled():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = make_engine(adapter, rows)
    router = FillRouter(adapter=adapter, engine=engine, instrument=INSTRUMENT)
    await engine.start_laddering()
    for idx in (0, 1, 2, 3):  # idx3 = major_tier 4 -> SL 등록됨
        await engine.on_fill(idx)

    sl_order_id = engine.sl_order_id
    tp_order_id = engine.tp_order_id
    assert sl_order_id is not None
    fill = Fill(order_id=sl_order_id, client_order_id="irrelevant", side="sell", price=rows[3].sl_price, qty=engine.open_qty, fee=Decimal("0"))

    await router.route(fill)

    assert engine.state == EngineState.COOLDOWN
    assert engine.sl_order_id is None
    assert engine.open_qty == Decimal("0")
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert not any(o.order_id == tp_order_id for o in open_orders)  # TP도 같이 취소됨


@pytest.mark.asyncio
async def test_route_ignores_unmatched_fill():
    """hybrid reset/강제청산 시장가 체결처럼 엔진이 이미 동기적으로 처리한 Fill은 무시한다."""
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = make_engine(adapter, rows)
    router = FillRouter(adapter=adapter, engine=engine, instrument=INSTRUMENT)
    await engine.start_laddering()
    await engine.on_fill(0)
    state_before = engine.state
    qty_before = engine.open_qty

    fill = Fill(order_id="unrelated-market-fill", client_order_id="hybrid-0-xxxx", side="sell", price=Decimal("64000"), qty=Decimal("0.005"), fee=Decimal("0"))
    await router.route(fill)

    assert engine.state == state_before
    assert engine.open_qty == qty_before


@pytest.mark.asyncio
async def test_run_consumes_real_paper_adapter_fills_end_to_end():
    """PaperAdapter.watch_fills()를 백그라운드에서 실제로 돌려 grid -> TP까지 라우팅되는지 검증."""
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = make_engine(adapter, rows, max_open=2)
    router = FillRouter(adapter=adapter, engine=engine, instrument=INSTRUMENT)
    await engine.start_laddering()

    task = asyncio.create_task(router.run())
    try:
        order_id = engine.resting_grid_order_ids[0]
        await adapter.fill_order(order_id, qty=rows[0].step_qty, price=rows[0].entry_price)

        for _ in range(100):
            if engine.filled_step_count >= 1 and engine.tp_order_id is not None:
                break
            await asyncio.sleep(0)
        assert engine.filled_step_count == 1
        assert engine.tp_order_id is not None

        tp_order_id = engine.tp_order_id
        await adapter.fill_order(tp_order_id, qty=engine.open_qty, price=rows[0].target_tp_price)

        for _ in range(100):
            if engine.state == EngineState.COOLDOWN:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.COOLDOWN
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_consumes_sl_trigger_fill_end_to_end():
    """PaperAdapter의 crossing-trigger STOP 체결이 watch_fills를 거쳐 on_sl_filled까지 도달하는지 검증.

    격자 행을 4개(인덱스0~3)로 제한한다 — 5개 전부 쓰면 tier4(idx3) 체결 후 마지막 남은
    idx4가 새로 롤링돼 걸리는데, 그 진입가(63600)가 SL 트리거가(61934)보다 높아서 SL을
    발동시키려 가격을 내리는 동안 idx4 진입 주문까지 같은 틱에서 동시에 크로싱돼버린다
    (같은 tick에 진입 체결과 SL 체결이 동시에 큐잉되는 것 자체는 실제로도 일어날 수 있는
    상황이지만, 그 동시성 처리는 이 라우터의 검증 범위가 아니다 — 여기서는 SL 단독 체결
    라우팅만 본다).
    """
    adapter = make_adapter()
    rows = make_grid_rows()[:4]
    engine = make_engine(adapter, rows, max_open=5)
    router = FillRouter(adapter=adapter, engine=engine, instrument=INSTRUMENT)
    await engine.start_laddering()

    task = asyncio.create_task(router.run())
    try:
        for idx in (0, 1, 2, 3):  # idx3 = major_tier 4 -> SL 등록
            order_id = engine.resting_grid_order_ids[idx]
            await adapter.fill_order(order_id, qty=rows[idx].step_qty, price=rows[idx].entry_price)
            await adapter.on_price_tick(rows[idx].entry_price)
            for _ in range(100):
                if engine.filled_step_count >= idx + 1:
                    break
                await asyncio.sleep(0)

        assert engine.sl_order_id is not None
        sl_trigger_price = rows[3].sl_price
        await adapter.on_price_tick(sl_trigger_price)  # crossing 발동 -> Fill 큐잉

        for _ in range(100):
            if engine.state == EngineState.COOLDOWN:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.COOLDOWN
        assert engine.sl_order_id is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
