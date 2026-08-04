"""engine/grid_engine.py 유닛 테스트 — PaperAdapter를 실제로 채우면서(fill_order) 검증한다.

GridStepResult 행은 strategy.grid.compute_grid()(100단계 강제)를 거치지 않고 테스트용으로
직접 구성한다 — 엔진은 major_tier/가격 필드를 그대로 읽어 쓸 뿐 재계산하지 않으므로
값 자체의 격자 공식 정합성은 이 테스트의 관심사가 아니다(그건 tests/test_golden.py 담당).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.grid_engine import EngineHaltedError, EngineState, GridEngine
from exchange.base import ContractSpec, StopOrderRequest
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


def make_adapter(adapter_cls=PaperAdapter) -> PaperAdapter:
    return adapter_cls(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )


async def fill_grid_index(adapter: PaperAdapter, engine: GridEngine, rows: list[GridStepResult], index: int) -> None:
    """엔진이 인덱스 idx에 건 격자 주문을 실제로 PaperAdapter에서 체결시킨 뒤 on_fill을 호출한다
    — 실제 운용에서는 watch_fills()가 체결을 알려주면 호출부가 이 순서로 처리하게 된다."""
    order_id = engine.resting_grid_order_ids[index]
    row = rows[index]
    await adapter.fill_order(order_id, qty=row.step_qty, price=row.entry_price)
    # 시장가 청산(hybrid reset/강제청산)에 필요한 "현재가"를 PaperAdapter가 알도록 갱신.
    # 남은 격자/TP/SL 주문의 가격대와 겹치지 않아 의도치 않은 교차체결은 없음(테스트 데이터 설계상).
    await adapter.on_price_tick(row.entry_price)
    await engine.on_fill(index)


@pytest.mark.asyncio
async def test_start_laddering_places_front_n_orders():
    adapter = make_adapter()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=make_grid_rows(), max_open_grid_orders=2)

    await engine.start_laddering()

    assert engine.state == EngineState.LADDERING
    assert len(engine.resting_grid_order_ids) == 2
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert len(open_orders) == 2


@pytest.mark.asyncio
async def test_on_fill_reregisters_tp_and_rolls_next_order():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await engine.start_laddering()

    await fill_grid_index(adapter, engine, rows, 0)

    assert engine.filled_step_count == 1
    assert engine.open_qty == Decimal("0.01")
    assert engine.tp_order_id is not None
    assert engine.sl_order_id is None  # tier1이라 SL 미등록

    # index0 체결, index1은 원래 걸려 있었고, 슬롯 여유로 index2가 새로 걸림
    assert set(engine.resting_grid_order_ids.keys()) == {1, 2}
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert len(open_orders) == 3  # 격자 주문 2개 + TP 1개

    position = await adapter.get_position(INSTRUMENT)
    assert position.qty == Decimal("0.01")
    assert position.direction == "long"


@pytest.mark.asyncio
async def test_on_fill_tier4_registers_sl():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()

    for idx in (0, 1, 2, 3):  # idx3 = major_tier 4
        await fill_grid_index(adapter, engine, rows, idx)

    assert engine.sl_order_id is not None
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    stop_order = next(o for o in open_orders if o.order_id == engine.sl_order_id)
    assert stop_order.status == "open"


@pytest.mark.asyncio
async def test_reregister_cancels_previous_tp_before_placing_new_one():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()

    await fill_grid_index(adapter, engine, rows, 0)
    first_tp_id = engine.tp_order_id
    await fill_grid_index(adapter, engine, rows, 1)
    second_tp_id = engine.tp_order_id

    assert first_tp_id != second_tp_id
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    order_ids = {o.order_id for o in open_orders}
    assert first_tp_id not in order_ids  # 취소됨
    assert second_tp_id in order_ids


@pytest.mark.asyncio
async def test_maybe_hybrid_reset_closes_half_at_breakeven():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()

    for idx in (0, 1, 2):  # idx2 = major_tier 3, avg_price=63900
        await fill_grid_index(adapter, engine, rows, idx)

    qty_before = engine.open_qty
    triggered = await engine.maybe_hybrid_reset(Decimal("63900"))

    assert triggered is True
    assert engine.hybrid_reset_done is True
    assert engine.open_qty == qty_before / 2

    position = await adapter.get_position(INSTRUMENT)
    assert position.qty == engine.open_qty

    triggered_again = await engine.maybe_hybrid_reset(Decimal("63900"))
    assert triggered_again is False


@pytest.mark.asyncio
async def test_maybe_hybrid_reset_does_not_fire_below_tier3():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()

    await fill_grid_index(adapter, engine, rows, 0)  # major_tier=1

    triggered = await engine.maybe_hybrid_reset(Decimal("64000"))
    assert triggered is False
    assert engine.hybrid_reset_done is False


@pytest.mark.asyncio
async def test_on_tp_filled_transitions_to_cooldown_and_cleans_up():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()
    await fill_grid_index(adapter, engine, rows, 0)
    tp_qty = engine.open_qty
    await adapter.fill_order(engine.tp_order_id, qty=tp_qty, price=rows[0].target_tp_price)

    await engine.on_tp_filled()

    assert engine.state == EngineState.COOLDOWN
    assert engine.tp_order_id is None
    assert engine.sl_order_id is None
    assert engine.resting_grid_order_ids == {}
    assert engine.open_qty == Decimal("0")
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert open_orders == []


@pytest.mark.asyncio
async def test_reset_for_new_cycle_after_cooldown_reinitializes_state():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()
    await fill_grid_index(adapter, engine, rows, 0)
    await adapter.fill_order(engine.tp_order_id, qty=engine.open_qty, price=rows[0].target_tp_price)
    await engine.on_tp_filled()
    assert engine.state == EngineState.COOLDOWN

    new_rows = make_grid_rows()  # 새 사이클용으로 다시 계산됐다고 가정(실제로는 다른 base_price)
    engine.reset_for_new_cycle(new_rows)

    assert engine.state == EngineState.IDLE
    assert engine.grid_rows is new_rows
    assert engine.filled_step_count == 0
    assert engine.open_qty == Decimal("0")
    assert engine.resting_grid_order_ids == {}
    assert engine.tp_order_id is None
    assert engine.sl_order_id is None
    assert engine.hybrid_reset_done is False


def test_reset_for_new_cycle_rejects_non_cooldown_state():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    assert engine.state == EngineState.IDLE

    with pytest.raises(ValueError, match="COOLDOWN"):
        engine.reset_for_new_cycle(rows)


@pytest.mark.asyncio
async def test_reset_for_new_cycle_rejects_when_halted():
    adapter = make_adapter(_FailingCancelAndMarketAdapter)
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await engine.start_laddering()
    await fill_grid_index(adapter, engine, rows, 0)
    with pytest.raises(RuntimeError):
        await fill_grid_index(adapter, engine, rows, 1)
    assert engine.halted is True

    with pytest.raises(EngineHaltedError):
        engine.reset_for_new_cycle(rows)


class _FailingStopAdapter(PaperAdapter):
    async def place_stop_order(self, order: StopOrderRequest):
        raise RuntimeError("시뮬레이션: SL 등록 실패")


@pytest.mark.asyncio
async def test_sl_registration_failure_forces_close_and_halts():
    adapter = make_adapter(_FailingStopAdapter)
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()

    for idx in (0, 1, 2):
        await fill_grid_index(adapter, engine, rows, idx)

    with pytest.raises(EngineHaltedError):
        await fill_grid_index(adapter, engine, rows, 3)  # major_tier=4 -> SL 등록 시도 -> 실패

    assert engine.halted is True
    assert engine.state == EngineState.CLOSING
    assert engine.open_qty == Decimal("0")
    position = await adapter.get_position(INSTRUMENT)
    assert position.direction is None  # 전량 강제청산됨


@pytest.mark.asyncio
async def test_halted_engine_rejects_further_calls():
    adapter = make_adapter(_FailingStopAdapter)
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()
    for idx in (0, 1, 2):
        await fill_grid_index(adapter, engine, rows, idx)
    with pytest.raises(EngineHaltedError):
        await fill_grid_index(adapter, engine, rows, 3)

    with pytest.raises(EngineHaltedError):
        await engine.maybe_hybrid_reset(Decimal("63900"))


class _FailingTpCancelAdapter(PaperAdapter):
    """TP 주문(client_order_id가 'tp-'로 시작)만 취소 실패시킨다 — 격자 진입 주문
    취소는 정상 동작해야 다른 롤링 로직에 영향을 안 준다."""

    async def cancel_order(self, order_id: str) -> None:
        internal = self._open_orders.get(order_id)
        if internal is not None and internal.request.client_order_id.startswith("tp-"):
            raise RuntimeError("시뮬레이션: TP 주문이 이미 사라짐")
        await super().cancel_order(order_id)


class _FailingSlCancelAdapter(PaperAdapter):
    """SL(스톱) 주문만 취소 실패시킨다."""

    async def cancel_order(self, order_id: str) -> None:
        if order_id in self._stop_orders:
            raise RuntimeError("시뮬레이션: SL 주문이 이미 사라짐")
        await super().cancel_order(order_id)


@pytest.mark.asyncio
async def test_tp_cancel_failure_forces_close_and_halts():
    """기존 TP를 취소하지 못하면(이미 거래소에서 사라진 경합 등) 새 TP를 추측해서
    걸지 않고 SL 등록 실패와 동일하게 강제청산 후 정지해야 한다."""
    adapter = make_adapter(_FailingTpCancelAdapter)
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await engine.start_laddering()
    await fill_grid_index(adapter, engine, rows, 0)  # 첫 TP 등록 성공(취소할 기존 TP가 없음)

    with pytest.raises(EngineHaltedError):
        await fill_grid_index(adapter, engine, rows, 1)  # 기존 TP 취소 시도 -> 실패

    assert engine.halted is True
    assert engine.state == EngineState.CLOSING
    assert engine.open_qty == Decimal("0")
    position = await adapter.get_position(INSTRUMENT)
    assert position.direction is None  # 전량 강제청산됨


@pytest.mark.asyncio
async def test_sl_cancel_failure_forces_close_and_halts():
    """기존 SL을 취소하지 못하는 경우도 SL 등록 실패와 동일하게 취급한다."""
    adapter = make_adapter(_FailingSlCancelAdapter)
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await engine.start_laddering()
    for idx in (0, 1, 2, 3):  # idx3 = major_tier4 -> 첫 SL 등록 성공(취소할 기존 SL 없음)
        await fill_grid_index(adapter, engine, rows, idx)

    with pytest.raises(EngineHaltedError):
        await fill_grid_index(adapter, engine, rows, 4)  # 기존 SL 취소 시도 -> 실패

    assert engine.halted is True
    assert engine.state == EngineState.CLOSING
    assert engine.open_qty == Decimal("0")
    position = await adapter.get_position(INSTRUMENT)
    assert position.direction is None


class _FailingCancelAndMarketAdapter(_FailingTpCancelAdapter):
    """취소도, 강제청산 시장가 주문도 둘 다 실패하는 최악의 경우."""

    async def place_market_order(self, order):
        raise RuntimeError("시뮬레이션: 강제청산 시장가 주문마저 실패")


@pytest.mark.asyncio
async def test_halted_flag_set_even_if_force_close_market_order_also_fails():
    """강제청산 시장가 주문 자체가 실패해도 halted/state는 반드시 먼저 확정돼 있어야
    한다 — 그래야 엔진이 '멀쩡한 척' 계속 동작하는 최악의 상황을 막는다."""
    adapter = make_adapter(_FailingCancelAndMarketAdapter)
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await engine.start_laddering()
    await fill_grid_index(adapter, engine, rows, 0)

    with pytest.raises(RuntimeError, match="강제청산 시장가 주문마저 실패"):
        await fill_grid_index(adapter, engine, rows, 1)

    assert engine.halted is True
    assert engine.state == EngineState.CLOSING
    with pytest.raises(EngineHaltedError):
        await engine.maybe_hybrid_reset(Decimal("63900"))


@pytest.mark.asyncio
async def test_manual_mode_on_fill_skips_tp_and_sl_registration():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, manual_mode=True,
    )
    await engine.start_laddering()

    for idx in (0, 1, 2, 3):  # idx3 = major_tier 4, 일반 모드라면 SL도 등록됐어야 함
        await fill_grid_index(adapter, engine, rows, idx)

    assert engine.tp_order_id is None
    assert engine.sl_order_id is None
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    # 남은 격자 진입 주문(idx4, 롤링 윈도우로 걸림) 말고는 TP/SL 등 다른 주문이 전혀 없어야 함
    assert [o.order_id for o in open_orders] == [engine.resting_grid_order_ids[4]]


@pytest.mark.asyncio
async def test_manual_mode_stays_laddering_after_all_rows_filled():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, manual_mode=True,
    )
    await engine.start_laddering()

    for idx in range(len(rows)):
        await fill_grid_index(adapter, engine, rows, idx)

    assert engine.state == EngineState.LADDERING  # TP_PENDING으로 안 넘어감(TP 자체가 없음)
    assert engine.open_qty == sum((r.step_qty for r in rows), Decimal("0"))


@pytest.mark.asyncio
async def test_manual_mode_hybrid_reset_never_fires():
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, manual_mode=True,
    )
    await engine.start_laddering()
    for idx in (0, 1, 2):  # major_tier 3 -> 일반 모드라면 hybrid reset 대상
        await fill_grid_index(adapter, engine, rows, idx)

    triggered = await engine.maybe_hybrid_reset(Decimal("63900"))

    assert triggered is False
    assert engine.hybrid_reset_done is False
