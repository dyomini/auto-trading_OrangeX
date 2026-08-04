"""engine/restart_recovery.py 유닛 테스트.

실제 GridEngine + PaperAdapter로 시나리오를 만든 뒤(진짜 client_order_id 명명 규칙을
그대로 쓰기 위해) "재시작"을 흉내 내려고 그 GridEngine 인스턴스는 버리고, 같은
adapter/grid_rows만 가지고 reconstruct_state()/build_recovered_engine()을 새로 호출해
복구된 값이 원래 엔진 상태와 일치하는지 비교한다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from engine.grid_engine import EngineState, GridEngine
from engine.restart_recovery import RestartRecoveryError, build_recovered_engine, reconstruct_state
from exchange.base import ContractSpec, MarketOrderRequest, OrderRequest, StopOrderRequest
from exchange.paper import PaperAdapter
from strategy.grid import GridStepResult

INSTRUMENT = "BTC-USDT-PERP"


def make_row(index: int, major_tier: int, entry_price: Decimal, avg_price: Decimal, tp: Decimal, sl: Decimal) -> GridStepResult:
    return GridStepResult(
        index=index, major_tier=major_tier, sub_step=1, entry_price=entry_price, weight=Decimal("1"),
        step_qty=Decimal("0.01"), step_margin=Decimal("100"), cum_qty=Decimal("0.01") * (index + 1),
        cum_margin=Decimal("100") * (index + 1), avg_price=avg_price, available_balance=Decimal("1000"),
        liq_price=Decimal("50000"), target_roe=Decimal("0.1"), target_tp_price=tp, sl_price=sl,
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


async def fill_grid_index(adapter: PaperAdapter, engine: GridEngine, rows: list[GridStepResult], index: int) -> None:
    order_id = engine.resting_grid_order_ids[index]
    row = rows[index]
    await adapter.fill_order(order_id, qty=row.step_qty, price=row.entry_price)
    await adapter.on_price_tick(row.entry_price)
    await engine.on_fill(index)


@pytest.mark.asyncio
async def test_reconstructs_idle_when_flat_and_no_orders():
    adapter = make_adapter()
    rows = make_grid_rows()

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long")

    assert recovered.state == EngineState.IDLE
    assert recovered.filled_step_count == 0
    assert recovered.open_qty == Decimal("0")
    assert recovered.resting_grid_order_ids == {}
    assert recovered.tp_order_id is None
    assert recovered.sl_order_id is None


@pytest.mark.asyncio
async def test_reconstructs_laddering_below_sl_tier():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)  # tier1, TP만 등록

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long")

    assert recovered.state == EngineState.LADDERING
    assert recovered.filled_step_count == old_engine.filled_step_count == 1
    assert recovered.open_qty == old_engine.open_qty == Decimal("0.01")
    assert recovered.resting_grid_order_ids == old_engine.resting_grid_order_ids
    assert recovered.tp_order_id == old_engine.tp_order_id
    assert recovered.sl_order_id is None
    assert recovered.hybrid_reset_done is False


@pytest.mark.asyncio
async def test_reconstructs_laddering_with_sl_at_tier4():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await old_engine.start_laddering()
    for idx in (0, 1, 2, 3):
        await fill_grid_index(adapter, old_engine, rows, idx)

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long")

    assert recovered.state == EngineState.LADDERING
    assert recovered.filled_step_count == 4
    assert recovered.sl_order_id == old_engine.sl_order_id
    assert recovered.sl_order_id is not None
    assert recovered.hybrid_reset_done is False


@pytest.mark.asyncio
async def test_reconstructs_laddering_with_sl_at_tier3_using_custom_mandatory_sl_min_tier():
    # 2026-08-04: mandatory_sl_min_tier=3(3-tier 압축 설계)로 GridEngine을 운용했다면
    # reconstruct_state()에도 같은 값을 넘겨야 tier3에서 SL이 필수라고 올바르게 판단한다.
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, mandatory_sl_min_tier=3,
    )
    await old_engine.start_laddering()
    for idx in (0, 1, 2):  # idx2 = major_tier 3
        await fill_grid_index(adapter, old_engine, rows, idx)
    assert old_engine.sl_order_id is not None

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long", mandatory_sl_min_tier=3)

    assert recovered.state == EngineState.LADDERING
    assert recovered.filled_step_count == 3
    assert recovered.sl_order_id == old_engine.sl_order_id
    assert recovered.sl_order_id is not None


@pytest.mark.asyncio
async def test_reconstruct_state_default_mandatory_sl_min_tier_rejects_tier3_sl():
    # 위 테스트와 반대 방향 확인: mandatory_sl_min_tier를 넘기지 않으면(기본값 4) tier3에
    # SL이 있는 상태를 "예상 밖"으로 거부해야 한다 — 두 설정이 일관되게 맞물려야 함을 보장.
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, mandatory_sl_min_tier=3,
    )
    await old_engine.start_laddering()
    for idx in (0, 1, 2):
        await fill_grid_index(adapter, old_engine, rows, idx)

    with pytest.raises(RestartRecoveryError, match="SL 주문이 존재함"):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")  # mandatory_sl_min_tier 기본값(4)


@pytest.mark.asyncio
async def test_reconstructs_tp_pending_when_all_rows_filled():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await old_engine.start_laddering()
    for idx in range(5):
        await fill_grid_index(adapter, old_engine, rows, idx)

    assert old_engine.state == EngineState.TP_PENDING
    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long")

    assert recovered.state == EngineState.TP_PENDING
    assert recovered.filled_step_count == 5
    assert recovered.resting_grid_order_ids == {}
    assert recovered.tp_order_id == old_engine.tp_order_id


@pytest.mark.asyncio
async def test_reconstructs_hybrid_reset_done():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await old_engine.start_laddering()
    for idx in (0, 1, 2):  # tier3
        await fill_grid_index(adapter, old_engine, rows, idx)
    await old_engine.maybe_hybrid_reset(Decimal("63900"))

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long")

    assert recovered.hybrid_reset_done is True
    assert recovered.open_qty == old_engine.open_qty
    assert recovered.state == EngineState.LADDERING


@pytest.mark.asyncio
async def test_build_recovered_engine_matches_old_engine_fields():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await old_engine.start_laddering()
    for idx in (0, 1):
        await fill_grid_index(adapter, old_engine, rows, idx)

    new_engine = await build_recovered_engine(adapter, INSTRUMENT, "long", rows, max_open_grid_orders=5)

    assert new_engine.state == old_engine.state
    assert new_engine.filled_step_count == old_engine.filled_step_count
    assert new_engine.open_qty == old_engine.open_qty
    assert new_engine.resting_grid_order_ids == old_engine.resting_grid_order_ids
    assert new_engine.tp_order_id == old_engine.tp_order_id
    assert new_engine.sl_order_id == old_engine.sl_order_id

    # 복구된 엔진으로 실제 이어서 체결 처리도 가능해야 한다.
    await fill_grid_index(adapter, new_engine, rows, 2)
    assert new_engine.filled_step_count == 3


@pytest.mark.asyncio
async def test_direction_mismatch_raises():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "short")


@pytest.mark.asyncio
async def test_reconstructs_laddering_with_zero_fills():
    """2026-08-04 코드 리뷰로 발견한 버그의 회귀 테스트: start_laddering() 직후 첫
    체결이 나기 전(포지션 flat, 진입 지정가 주문만 미체결)은 완전히 정상인 LADDERING
    상태인데, 예전엔 이것도 무조건 RestartRecoveryError로 막아서 이 타이밍에 봇이
    재시작되면 복구가 아예 불가능했다."""
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await old_engine.start_laddering()  # 아직 체결 없음

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long")

    assert recovered.state == EngineState.LADDERING
    assert recovered.filled_step_count == 0
    assert recovered.open_qty == Decimal("0")
    assert recovered.resting_grid_order_ids == old_engine.resting_grid_order_ids
    assert recovered.tp_order_id is None
    assert recovered.sl_order_id is None


@pytest.mark.asyncio
async def test_flat_position_with_tp_order_raises():
    adapter = make_adapter()
    rows = make_grid_rows()
    await adapter.place_limit_order(
        OrderRequest(instrument=INSTRUMENT, side="sell", price=rows[0].target_tp_price, qty=rows[0].step_qty, client_order_id="tp-0-deadbeef", reduce_only=True)
    )

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")


@pytest.mark.asyncio
async def test_flat_position_with_non_contiguous_from_zero_grid_orders_raises():
    """체결이 하나도 없다면 미체결 격자 주문은 반드시 index 0부터 시작해야 한다 —
    index0/1은 없이 index2만 미체결로 남은 건 뭔가 앞뒤가 안 맞는 상태다."""
    adapter = make_adapter()
    rows = make_grid_rows()
    await adapter.place_limit_order(
        OrderRequest(instrument=INSTRUMENT, side="buy", price=rows[2].entry_price, qty=rows[2].step_qty, client_order_id="grid-2-deadbeef")
    )

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")


@pytest.mark.asyncio
async def test_position_without_tp_order_raises():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)
    await adapter.cancel_order(old_engine.tp_order_id)

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")


@pytest.mark.asyncio
async def test_missing_required_sl_at_tier4_raises():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await old_engine.start_laddering()
    for idx in (0, 1, 2, 3):
        await fill_grid_index(adapter, old_engine, rows, idx)
    await adapter.cancel_order(old_engine.sl_order_id)

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")


@pytest.mark.asyncio
async def test_unexpected_sl_below_tier4_raises():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)  # tier1
    await adapter.place_stop_order(
        StopOrderRequest(instrument=INSTRUMENT, side="sell", trigger_price=Decimal("62000"), qty=old_engine.open_qty, client_order_id="sl-0-bogus")
    )

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")


@pytest.mark.asyncio
async def test_multiple_tp_orders_raises():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=2)
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)
    await adapter.place_limit_order(
        OrderRequest(instrument=INSTRUMENT, side="sell", price=rows[0].target_tp_price, qty=old_engine.open_qty, client_order_id="tp-99-extra", reduce_only=True)
    )

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")


@pytest.mark.asyncio
async def test_non_contiguous_resting_grid_indices_raises():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)
    # index1 주문만 인위적으로 취소해 index0(이미 체결로 사라짐)/1(취소됨)/2/3/4 중 2,3,4만
    # 남겨 연속성이 깨지게 만든다.
    await adapter.cancel_order(old_engine.resting_grid_order_ids[1])

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")


@pytest.mark.asyncio
async def test_unparseable_client_order_id_raises():
    adapter = make_adapter()
    rows = make_grid_rows()
    await adapter.place_limit_order(
        OrderRequest(instrument=INSTRUMENT, side="buy", price=rows[0].entry_price, qty=rows[0].step_qty, client_order_id="mystery-order-123")
    )

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")


@pytest.mark.asyncio
async def test_manual_mode_reconstructs_laddering_without_tp_order():
    """manual_mode 엔진은 TP를 아예 안 걸므로, 포지션은 있는데 TP 미체결 주문이
    없어도(일반 모드라면 test_position_without_tp_order_raises처럼 에러) 정상으로
    복구돼야 한다."""
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=2, manual_mode=True,
    )
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)
    assert old_engine.tp_order_id is None  # manual_mode라 애초에 안 걸림

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long", manual_mode=True)

    assert recovered.state == EngineState.LADDERING
    assert recovered.filled_step_count == 1
    assert recovered.tp_order_id is None
    assert recovered.open_qty == old_engine.open_qty


@pytest.mark.asyncio
async def test_manual_mode_ignores_user_placed_manual_orders():
    """사용자가 거래소에서 직접 건 TP/SL(봇 명명 규칙과 무관한 client_order_id)이
    미체결로 남아있어도 manual_mode에서는 엔진 추적 대상이 아니므로 조용히 무시해야
    한다 — 일반 모드라면 test_unparseable_client_order_id_raises처럼 막혔을 상황."""
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=2, manual_mode=True,
    )
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)
    await adapter.place_limit_order(
        OrderRequest(
            instrument=INSTRUMENT, side="sell", price=rows[0].target_tp_price,
            qty=old_engine.open_qty, client_order_id="my-manual-tp-order", reduce_only=True,
        )
    )

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long", manual_mode=True)

    assert recovered.state == EngineState.LADDERING
    assert recovered.tp_order_id is None
    assert recovered.sl_order_id is None


@pytest.mark.asyncio
async def test_manual_mode_stays_laddering_when_all_rows_filled():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, manual_mode=True,
    )
    await old_engine.start_laddering()
    for idx in range(len(rows)):
        await fill_grid_index(adapter, old_engine, rows, idx)

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long", manual_mode=True)

    assert recovered.state == EngineState.LADDERING  # TP_PENDING 아님(TP 자체가 없음)
    assert recovered.filled_step_count == len(rows)


@pytest.mark.asyncio
async def test_manual_mode_tolerates_position_qty_mismatch_with_theoretical():
    """사용자가 거래소에서 직접 부분청산했을 수 있어 포지션 수량이 이론치(cum_qty)와
    달라도 manual_mode에서는 추측/검증 없이 실측 수량을 그대로 신뢰해야 한다 — 일반
    모드라면 test_quantity_matching_neither_theoretical_nor_hybrid_raises처럼 막혔을
    상황."""
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, manual_mode=True,
    )
    await old_engine.start_laddering()
    for idx in (0, 1, 2):
        await fill_grid_index(adapter, old_engine, rows, idx)
    await adapter.on_price_tick(rows[2].entry_price)
    await adapter.place_market_order(
        MarketOrderRequest(instrument=INSTRUMENT, side="sell", qty=Decimal("0.001"), client_order_id="user-manual-partial-close")
    )

    recovered = await reconstruct_state(adapter, INSTRUMENT, rows, "long", manual_mode=True)

    assert recovered.open_qty == old_engine.open_qty - Decimal("0.001")


@pytest.mark.asyncio
async def test_build_recovered_engine_manual_mode_sets_flag_and_can_continue():
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(
        adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows,
        max_open_grid_orders=5, manual_mode=True,
    )
    await old_engine.start_laddering()
    await fill_grid_index(adapter, old_engine, rows, 0)

    new_engine = await build_recovered_engine(
        adapter, INSTRUMENT, "long", rows, max_open_grid_orders=5, manual_mode=True
    )

    assert new_engine.manual_mode is True
    assert new_engine.tp_order_id is None
    await fill_grid_index(adapter, new_engine, rows, 1)
    assert new_engine.tp_order_id is None  # 이어서 체결돼도 여전히 TP 안 걸림


@pytest.mark.asyncio
async def test_quantity_matching_neither_theoretical_nor_hybrid_raises():
    """포지션 수량이 이론치(cum_qty)도, hybrid reset 이후 수량(cum_qty*0.5)도 아닌
    제3의 값이면(외부 개입 등으로 추정) 추측하지 않고 막아야 한다."""
    adapter = make_adapter()
    rows = make_grid_rows()
    old_engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=5)
    await old_engine.start_laddering()
    for idx in (0, 1, 2):  # tier3 — hybrid reset 가능 구간(HYBRID_RESET_MIN_TIER)
        await fill_grid_index(adapter, old_engine, rows, idx)
    # 시장가 주문으로 소량을 더 사서 포지션 수량을 이론치(cum_qty)/hybrid 수량(cum_qty*0.5)
    # 둘 다와 다르게 만든다 (외부 개입으로 포지션이 grid 계산과 어긋난 상황을 흉내낸다).
    await adapter.on_price_tick(rows[2].entry_price)
    await adapter.place_market_order(
        MarketOrderRequest(instrument=INSTRUMENT, side="buy", qty=Decimal("0.001"), client_order_id="grid-manual-extra")
    )

    with pytest.raises(RestartRecoveryError):
        await reconstruct_state(adapter, INSTRUMENT, rows, "long")
