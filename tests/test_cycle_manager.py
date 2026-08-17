"""engine/cycle_manager.py 유닛 테스트.

CycleManager가 새 사이클을 위해 계산하는 grid_rows는 engine/grid_setup.py의
build_grid_rows()를 그대로 거치므로(실제 100단계 격자, tests/test_grid_setup.py가
그 자체는 검증함) 여기서는 "COOLDOWN을 감지해서 기다렸다가 reset_for_new_cycle을
실제로 호출하는지"라는 CycleManager 고유의 오케스트레이션만 검증한다.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from config.settings import Settings
from engine.cycle_manager import CycleManager, CycleOutcome, CycleRestartPolicy
from engine.grid_engine import EngineState, GridEngine
from exchange.base import ContractSpec
from exchange.orangex.adapter import OrangeXAdapter
from exchange.paper import PaperAdapter
from strategy.grid import GridStepResult

INSTRUMENT = "BTC-USDT-PERPETUAL"


class FakeOrangeXClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses

    async def call(self, method, params=None, authed=True):
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return response


def make_market_data_adapter(last_price: str = "64000") -> OrangeXAdapter:
    client = FakeOrangeXClient(
        {
            "/public/get_instruments": {
                "instruments": [
                    {
                        "instrument_name": INSTRUMENT,
                        "tick_size": "50",
                        "min_qty": "0.001",
                        "min_notional": "10",
                        "contract_size": "1",
                    }
                ]
            },
            "/public/ticker": {"last_price": last_price},
        }
    )
    return OrangeXAdapter(client)


def make_row(index: int, major_tier: int, entry_price: Decimal, avg_price: Decimal, tp: Decimal, sl: Decimal) -> GridStepResult:
    return GridStepResult(
        index=index, major_tier=major_tier, sub_step=1, entry_price=entry_price, weight=Decimal("1"),
        step_qty=Decimal("0.01"), step_margin=Decimal("100"), cum_qty=Decimal("0.01") * (index + 1),
        cum_margin=Decimal("100") * (index + 1), avg_price=avg_price, available_balance=Decimal("1000"),
        liq_price=Decimal("50000"), target_roe=Decimal("0.1"), target_tp_price=tp, sl_price=sl,
    )


def make_grid_rows() -> list[GridStepResult]:
    return [make_row(0, 1, Decimal("64000"), Decimal("64000"), Decimal("64640"), Decimal("62080"))]


def make_paper_adapter() -> PaperAdapter:
    spec = ContractSpec(instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.0001"), min_notional=Decimal("10"), contract_size=Decimal("1"))
    return PaperAdapter(instrument=INSTRUMENT, contract_spec=spec, initial_equity=Decimal("10000"), leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"))


def make_settings(**overrides) -> Settings:
    defaults = dict(
        symbol=INSTRUMENT, direction="long", equity_usdt=Decimal("10000"), leverage=Decimal("20"),
        grid_tick=Decimal("50"), cooldown_minutes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def drive_engine_to_cooldown(engine: GridEngine, adapter: PaperAdapter, rows: list[GridStepResult]) -> None:
    """5-row 픽스처가 아니라 1-row로 충분 — tier1이라 SL 없이 바로 TP_PENDING까지 간다."""
    await engine.start_laddering()
    order_id = engine.resting_grid_order_ids[0]
    await adapter.fill_order(order_id, qty=rows[0].step_qty, price=rows[0].entry_price)
    await adapter.on_price_tick(rows[0].entry_price)
    await engine.on_fill(0)
    assert engine.state == EngineState.TP_PENDING

    await adapter.fill_order(engine.tp_order_id, qty=engine.open_qty, price=rows[0].target_tp_price)
    await engine.on_tp_filled()
    assert engine.state == EngineState.COOLDOWN


@pytest.mark.asyncio
async def test_start_next_cycle_resets_engine_with_freshly_computed_rows():
    adapter = make_paper_adapter()
    old_rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=old_rows, max_open_grid_orders=1)
    await drive_engine_to_cooldown(engine, adapter, old_rows)

    market_data_adapter = make_market_data_adapter(last_price="65000")
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    settings = make_settings()
    manager = CycleManager(
        engine=engine, market_data_adapter=market_data_adapter, contract_spec=contract_spec,
        settings=settings,
    )

    await manager.start_next_cycle()

    assert engine.state == EngineState.IDLE
    assert engine.grid_rows is not old_rows
    assert engine.grid_rows[0].entry_price == Decimal("65000")  # 새 현재가 기준 base_price
    assert len(engine.grid_rows) > 1  # 실제 100단계(또는 절삭된) 격자로 교체됨
    assert engine.filled_step_count == 0
    assert engine.open_qty == Decimal("0")


@pytest.mark.asyncio
async def test_run_detects_cooldown_and_starts_next_cycle_automatically():
    adapter = make_paper_adapter()
    old_rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=old_rows, max_open_grid_orders=1)
    await engine.start_laddering()  # 아직 COOLDOWN 전 — CycleManager가 대기 상태를 유지해야 함

    market_data_adapter = make_market_data_adapter()
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    settings = make_settings(cooldown_minutes=0)
    manager = CycleManager(
        engine=engine, market_data_adapter=market_data_adapter, contract_spec=contract_spec,
        settings=settings, poll_interval_seconds=0,
    )

    task = asyncio.create_task(manager.run())
    try:
        await asyncio.sleep(0)
        assert engine.state == EngineState.LADDERING  # 아직 대기 중 — 잘못 리셋되지 않았음

        order_id = engine.resting_grid_order_ids[0]
        await adapter.fill_order(order_id, qty=old_rows[0].step_qty, price=old_rows[0].entry_price)
        await adapter.on_price_tick(old_rows[0].entry_price)
        await engine.on_fill(0)
        await adapter.fill_order(engine.tp_order_id, qty=engine.open_qty, price=old_rows[0].target_tp_price)
        await engine.on_tp_filled()
        assert engine.state == EngineState.COOLDOWN

        for _ in range(500):
            if engine.state == EngineState.IDLE:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.IDLE
        assert engine.grid_rows is not old_rows
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_returns_rebuild_requested_under_rebuild_policy():
    """REBUILD_STACK이면 제자리 리셋을 하지 않고 호출부에 재조립을 요청만 해야 한다.
    build_grid_rows를 아예 호출하지 않는다는 걸 확인하려고, 호출되면 터지는
    market_data_adapter를 준다."""
    adapter = make_paper_adapter()
    rows = make_grid_rows()
    engine = GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=rows, max_open_grid_orders=1)
    await drive_engine_to_cooldown(engine, adapter, rows)
    assert engine.state == EngineState.COOLDOWN

    class _ExplodingMarketData:
        async def get_ticker(self, instrument):  # pragma: no cover
            raise AssertionError("REBUILD_STACK에서는 build_grid_rows가 호출되면 안 됨")

    manager = CycleManager(
        engine=engine, market_data_adapter=_ExplodingMarketData(),
        contract_spec=ContractSpec(instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.0001"), min_notional=Decimal("10"), contract_size=Decimal("1")), settings=make_settings(cooldown_minutes=0),
        poll_interval_seconds=0, restart_policy=CycleRestartPolicy.REBUILD_STACK,
    )

    outcome = await asyncio.wait_for(manager.run(), timeout=5)

    assert outcome is CycleOutcome.REBUILD_REQUESTED
    assert engine.state == EngineState.COOLDOWN  # 제자리 리셋되지 않았다
    assert engine.grid_rows is rows


@pytest.mark.asyncio
async def test_default_policy_is_reset_in_place():
    """회귀 방어: 고정 방향 운용이 실수로 재조립 모드로 바뀌지 않았는지."""
    manager = CycleManager(
        engine=GridEngine(adapter=make_paper_adapter(), instrument=INSTRUMENT, direction="long",
                          grid_rows=make_grid_rows(), max_open_grid_orders=1),
        market_data_adapter=make_market_data_adapter(), contract_spec=ContractSpec(instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.0001"), min_notional=Decimal("10"), contract_size=Decimal("1")),
        settings=make_settings(),
    )
    assert manager.restart_policy is CycleRestartPolicy.RESET_IN_PLACE
