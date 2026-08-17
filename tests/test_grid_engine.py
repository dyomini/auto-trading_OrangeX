"""engine/grid_engine.py 유닛 테스트 — PaperAdapter를 실제로 채우면서(fill_order) 검증한다.

GridStepResult 행은 strategy.grid.compute_grid()(100단계 강제)를 거치지 않고 테스트용으로
직접 구성한다 — 엔진은 major_tier/가격 필드를 그대로 읽어 쓸 뿐 재계산하지 않으므로
값 자체의 격자 공식 정합성은 이 테스트의 관심사가 아니다(그건 tests/test_golden.py 담당).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.grid_engine import EngineHaltedError, EngineState, GridEngine, OrderQtyTooSmallError
from exchange.base import ContractSpec, StopOrderRequest
from exchange.paper import PaperAdapter
from strategy.grid import GridStepResult

INSTRUMENT = "BTC-USDT-PERP"


def make_row(
    index: int,
    major_tier: int,
    entry_price: Decimal,
    avg_price: Decimal,
    tp: Decimal,
    sl: Decimal,
    step_qty: Decimal = Decimal("0.01"),
) -> GridStepResult:
    return GridStepResult(
        index=index,
        major_tier=major_tier,
        sub_step=1,
        entry_price=entry_price,
        weight=Decimal("1"),
        step_qty=step_qty,
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
async def test_on_fill_respects_custom_mandatory_sl_min_tier():
    # 2026-08-04: max_stage=3(3-tier 압축 설계, 제까깟-마틴게이-3k.xlsx 기준)으로 운용하면
    # major_tier가 4에 절대 도달하지 못해 기본값(4)으로는 SL이 영원히 등록되지 않는다 —
    # mandatory_sl_min_tier를 3으로 낮춰서 tier3에서 SL이 등록되는지 확인한다.
    adapter = make_adapter()
    rows = make_grid_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, mandatory_sl_min_tier=3,
    )
    await engine.start_laddering()

    await fill_grid_index(adapter, engine, rows, 0)
    assert engine.sl_order_id is None  # tier1
    await fill_grid_index(adapter, engine, rows, 1)
    assert engine.sl_order_id is None  # tier2
    await fill_grid_index(adapter, engine, rows, 2)
    assert engine.sl_order_id is not None  # tier3 — 기본값(4)이었으면 여기서 등록 안 됐어야 함
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


# --------------------------------------------------------------------------
# 수량 정밀도(qty_step) 반올림 — 2026-08-17.
# quick_entry.py는 2026-08-06에 고쳤지만 메인 엔진은 미가공 수량을 그대로 주문에 넣고
# 있었다(라이브면 거래소가 정밀도 불일치로 전부 거부). docs/phase3-plan.md 참고.
# --------------------------------------------------------------------------

# 실제 BTC-USDT-PERPETUAL 값(min_trade_amount=0.001)을 반영한 스펙.
# 위 make_spec()은 qty_step 미지정(=0, "반올림 안 함")이라 기존 테스트는 영향을 안 받는다.
def make_spec_with_step() -> ContractSpec:
    return ContractSpec(
        instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.001"),
        min_notional=Decimal("10"), contract_size=Decimal("1"), qty_step=Decimal("0.001"),
    )


class _RecordingAdapter(PaperAdapter):
    """엔진이 실제로 어떤 수량을 주문했는지 확인하기 위해 요청 객체를 그대로 기록한다
    (PaperAdapter.get_open_orders()는 OrderResult만 돌려줘서 주문 수량이 안 보인다)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.limit_requests: list = []
        self.stop_requests: list = []
        self.market_requests: list = []

    async def place_limit_order(self, order):
        self.limit_requests.append(order)
        return await super().place_limit_order(order)

    async def place_stop_order(self, order):
        self.stop_requests.append(order)
        return await super().place_stop_order(order)

    async def place_market_order(self, order):
        self.market_requests.append(order)
        return await super().place_market_order(order)


def make_messy_rows() -> list[GridStepResult]:
    """compute_grid()가 실제로 만드는 형태의 "지저분한" 수량 — 나눗셈 결과라 소수점이
    한참 이어진다. 0.001 단위로 내리면 각각 0.003 / 0.002 / 0.002 (합 0.007)."""
    return [
        make_row(0, 1, Decimal("64000"), Decimal("64000"), Decimal("64640"), Decimal("62080"),
                 step_qty=Decimal("0.003912345678901234567890")),
        make_row(1, 2, Decimal("63900"), Decimal("63950"), Decimal("64590"), Decimal("62031"),
                 step_qty=Decimal("0.002987654321098765432109")),
        make_row(2, 3, Decimal("63800"), Decimal("63900"), Decimal("64220"), Decimal("61983"),
                 step_qty=Decimal("0.002111111111111111111111")),
    ]


async def fill_rounded(adapter: PaperAdapter, engine: GridEngine, rows, index: int, qty: Decimal) -> None:
    """엔진이 실제로 건 (반올림된) 수량 그대로 체결시킨다."""
    order_id = engine.resting_grid_order_ids[index]
    await adapter.fill_order(order_id, qty=qty, price=rows[index].entry_price)
    await adapter.on_price_tick(rows[index].entry_price)
    await engine.on_fill(index)


@pytest.mark.asyncio
async def test_grid_entry_qty_is_rounded_down_to_qty_step():
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec_with_step(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = make_messy_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=3, contract_spec=make_spec_with_step(),
    )

    await engine.start_laddering()

    assert [o.qty for o in adapter.limit_requests] == [
        Decimal("0.003"), Decimal("0.002"), Decimal("0.002")
    ]


@pytest.mark.asyncio
async def test_open_qty_accumulates_the_rounded_quantity_not_the_raw_one():
    """엔진이 아는 수량과 거래소 실제 포지션이 어긋나면 그 오차가 TP/청산 주문 수량에
    그대로 전파된다 — 누적도 반드시 반올림된 값이어야 한다."""
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec_with_step(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = make_messy_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=3, contract_spec=make_spec_with_step(), manual_mode=True,
    )
    await engine.start_laddering()

    await fill_rounded(adapter, engine, rows, 0, Decimal("0.003"))
    await fill_rounded(adapter, engine, rows, 1, Decimal("0.002"))

    assert engine.open_qty == Decimal("0.005")  # 0.003912... + 0.002987... 이 아니라


@pytest.mark.asyncio
async def test_tp_order_qty_is_rounded_down_to_qty_step():
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec_with_step(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = make_messy_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=3, contract_spec=make_spec_with_step(),
    )
    await engine.start_laddering()
    entry_count = len(adapter.limit_requests)

    await fill_rounded(adapter, engine, rows, 0, Decimal("0.003"))

    tp_requests = [o for o in adapter.limit_requests[entry_count:] if o.reduce_only]
    assert len(tp_requests) == 1
    assert tp_requests[0].qty == Decimal("0.003")


@pytest.mark.asyncio
async def test_hybrid_reset_close_qty_is_rounded_and_residual_tracked():
    """open_qty=0.007의 절반은 0.0035 — 0.001 배수가 아니라 내림해서 0.003만 청산되고
    잔량 0.004가 남는다. 엔진의 open_qty도 정확히 그 잔량이어야 한다."""
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec_with_step(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = make_messy_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=3, contract_spec=make_spec_with_step(),
    )
    await engine.start_laddering()
    await fill_rounded(adapter, engine, rows, 0, Decimal("0.003"))
    await fill_rounded(adapter, engine, rows, 1, Decimal("0.002"))
    await fill_rounded(adapter, engine, rows, 2, Decimal("0.002"))
    assert engine.open_qty == Decimal("0.007")

    triggered = await engine.maybe_hybrid_reset(rows[2].avg_price)

    assert triggered is True
    assert [o.qty for o in adapter.market_requests] == [Decimal("0.003")]
    assert engine.open_qty == Decimal("0.004")


@pytest.mark.asyncio
async def test_order_below_min_qty_after_rounding_raises_instead_of_placing():
    """반올림 후 최소 주문 수량에 미달하면 애매한 주문을 내보내지 않고 명확히 실패한다
    (거래소가 조용히 거부하게 두지 않는다 — SPEC 0번)."""
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec_with_step(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = [make_row(0, 1, Decimal("64000"), Decimal("64000"), Decimal("64640"), Decimal("62080"),
                     step_qty=Decimal("0.0009"))]  # 내리면 0 -> min_qty(0.001) 미달
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=1, contract_spec=make_spec_with_step(),
    )

    with pytest.raises(OrderQtyTooSmallError):
        await engine.start_laddering()

    assert adapter.limit_requests == []


@pytest.mark.asyncio
async def test_no_contract_spec_keeps_raw_quantities_backward_compatible():
    """contract_spec을 안 넘기면(기존 호출부 전부) 반올림하지 않고 기존 동작 그대로."""
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = make_messy_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=1,
    )

    await engine.start_laddering()

    assert adapter.limit_requests[0].qty == rows[0].step_qty


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


# --------------------------------------------------------------------------
# SL 미등록 (sl_enabled=False) — 2026-08-17 사용자 결정 "sl은 안 걸어도돼".
# SPEC Phase 3의 "4~5차 SL 필수"에서 의도적으로 벗어난다(docs/phase3-plan.md 기록).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sl_disabled_skips_registration_even_at_mandatory_tier():
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = make_grid_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, sl_enabled=False,
    )
    await engine.start_laddering()

    for idx in (0, 1, 2, 3):  # idx3 = major_tier 4 -> 기본값이면 SL이 등록됐어야 함
        await fill_grid_index(adapter, engine, rows, idx)

    assert engine.sl_order_id is None
    assert adapter.stop_requests == []
    assert engine.tp_order_id is not None  # TP는 여전히 정상 재등록된다


@pytest.mark.asyncio
async def test_sl_enabled_by_default_still_registers():
    """회귀 방어: 기본값(True)에서는 기존 동작 그대로여야 한다."""
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = make_grid_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5,
    )
    await engine.start_laddering()
    for idx in (0, 1, 2, 3):
        await fill_grid_index(adapter, engine, rows, idx)

    assert engine.sl_order_id is not None
    assert len(adapter.stop_requests) == 1


@pytest.mark.asyncio
async def test_sl_disabled_hybrid_reset_still_works_without_sl():
    """hybrid reset은 SL과 무관하게 그대로 동작해야 한다(유일하게 남은 리스크 완화 장치)."""
    adapter = _RecordingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    rows = make_grid_rows()
    engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, sl_enabled=False,
    )
    await engine.start_laddering()
    for idx in (0, 1, 2, 3):  # tier4까지 — hybrid reset 대상이면서 SL 대상이기도 한 구간
        await fill_grid_index(adapter, engine, rows, idx)

    triggered = await engine.maybe_hybrid_reset(rows[3].avg_price)

    assert triggered is True
    assert engine.sl_order_id is None
    assert adapter.stop_requests == []
