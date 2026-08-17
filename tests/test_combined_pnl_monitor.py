"""engine/combined_pnl_monitor.py 유닛 테스트 (2026-08-17).

DIRECTION=both의 합산 손익 판정 — 롱/숏을 동시에 깔아두고 합산 ROE가 목표에
도달하면 양쪽을 전량 청산한다.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from config.settings import Settings
from engine.combined_pnl_monitor import CombinedPnlMonitor
from engine.cycle_manager import CycleOutcome
from engine.grid_engine import EngineState, GridEngine
from exchange.base import ContractSpec, Ticker
from exchange.paper import PaperAdapter
from strategy.grid import GridStepResult

INSTRUMENT = "BTC-USDT-PERPETUAL"


def make_settings(**overrides) -> Settings:
    defaults = dict(
        symbol=INSTRUMENT, direction="both", equity_usdt=Decimal("20000"),
        leverage=Decimal("20"), grid_tick=Decimal("50"), maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0006"), manual_mode=False, sl_enabled=True, grid_preset=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_row(entry_price: Decimal, avg_price: Decimal) -> GridStepResult:
    return GridStepResult(
        index=0, major_tier=1, sub_step=1, entry_price=entry_price, weight=Decimal("1"),
        step_qty=Decimal("0.1"), step_margin=Decimal("320"), cum_qty=Decimal("0.1"),
        cum_margin=Decimal("320"), avg_price=avg_price, available_balance=Decimal("1000"),
        liq_price=Decimal("50000"), target_roe=Decimal("0.1"),
        target_tp_price=avg_price, sl_price=avg_price,
    )


def make_adapter() -> PaperAdapter:
    spec = ContractSpec(
        instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.0001"),
        min_notional=Decimal("10"), contract_size=Decimal("1"),
    )
    return PaperAdapter(
        instrument=INSTRUMENT, contract_spec=spec, initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )


def make_engine(direction: str, qty: Decimal, avg: Decimal) -> GridEngine:
    engine = GridEngine(
        adapter=make_adapter(), instrument=INSTRUMENT, direction=direction,
        grid_rows=[make_row(avg, avg)], max_open_grid_orders=1, manual_mode=True,
    )
    engine.state = EngineState.LADDERING
    engine.filled_step_count = 1
    engine.open_qty = qty
    return engine


class _FakeMarketData:
    def __init__(self, price: Decimal) -> None:
        self.price = price

    async def get_ticker(self, instrument: str) -> Ticker:
        return Ticker(instrument=instrument, last_price=self.price)


def make_monitor(engines: dict, price: Decimal, target: str = "0.10") -> CombinedPnlMonitor:
    return CombinedPnlMonitor(
        engines=engines, market_data_adapter=_FakeMarketData(price),
        settings=make_settings(), target_roe=Decimal(target), poll_interval_seconds=0,
    )


def test_compute_long_profit_when_price_rises():
    engines = {"long": make_engine("long", Decimal("0.1"), Decimal("64000"))}
    snapshot = make_monitor(engines, Decimal("65000")).compute(Decimal("65000"))

    assert snapshot.pnl == Decimal("100.0")            # 0.1 * (65000 - 64000)
    assert snapshot.margin == Decimal("320")           # 0.1 * 64000 / 20
    # 진입 maker 0.1*64000*0.0002=1.28 + 청산 taker 0.1*65000*0.0006=3.90
    assert snapshot.fees == Decimal("5.18")
    assert snapshot.net_pnl == Decimal("94.82")


def test_compute_short_profit_when_price_falls():
    engines = {"short": make_engine("short", Decimal("0.1"), Decimal("64000"))}
    snapshot = make_monitor(engines, Decimal("63000")).compute(Decimal("63000"))

    assert snapshot.pnl == Decimal("100.0")            # 0.1 * (64000 - 63000)
    assert snapshot.margin == Decimal("320")


def test_compute_combines_both_sides_with_correct_signs():
    """같은 평단으로 롱/숏을 같은 크기로 들고 있으면 가격이 어디로 가든 손익 합은 0 —
    부호를 반대로 넣었으면 200이 나온다."""
    engines = {
        "long": make_engine("long", Decimal("0.1"), Decimal("64000")),
        "short": make_engine("short", Decimal("0.1"), Decimal("64000")),
    }
    snapshot = make_monitor(engines, Decimal("65000")).compute(Decimal("65000"))

    assert snapshot.pnl == Decimal("0.0")
    assert snapshot.margin == Decimal("640")
    assert snapshot.roe < 0  # 수수료만큼 마이너스


def test_compute_returns_zero_roe_when_nothing_is_open():
    engines = {
        "long": make_engine("long", Decimal("0"), Decimal("64000")),
        "short": make_engine("short", Decimal("0"), Decimal("64000")),
    }
    snapshot = make_monitor(engines, Decimal("65000")).compute(Decimal("65000"))

    assert snapshot.margin == Decimal("0")
    assert snapshot.roe == Decimal("0")


def test_roe_accounts_for_fees():
    """수수료를 빼지 않으면 '10% 달성'이라고 청산했는데 실제로는 못 미치는 상황이 된다."""
    engines = {"long": make_engine("long", Decimal("0.1"), Decimal("64000"))}
    monitor = make_monitor(engines, Decimal("64200"))
    snapshot = monitor.compute(Decimal("64200"))

    gross_roe = snapshot.pnl / snapshot.margin
    assert snapshot.roe < gross_roe
    assert snapshot.roe == (snapshot.pnl - snapshot.fees) / snapshot.margin


@pytest.mark.asyncio
async def test_run_does_not_trigger_below_target():
    engines = {
        "long": make_engine("long", Decimal("0.1"), Decimal("64000")),
        "short": make_engine("short", Decimal("0.1"), Decimal("64000")),
    }
    monitor = make_monitor(engines, Decimal("64100"))  # 합산 0, 수수료만큼 마이너스

    task = asyncio.create_task(monitor.run())
    for _ in range(200):
        await asyncio.sleep(0)
    assert not task.done(), "목표 미달인데 발동했다"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert all(e.state == EngineState.LADDERING for e in engines.values())


@pytest.mark.asyncio
async def test_run_waits_until_both_stacks_registered():
    """한쪽만 등록된 상태에서 판정하면 안 된다 — 롱만 보고 익절해버리는 사고 방지."""
    engines = {"long": make_engine("long", Decimal("0.1"), Decimal("64000"))}
    monitor = make_monitor(engines, Decimal("70000"))  # 롱 단독이면 목표를 한참 넘김

    task = asyncio.create_task(monitor.run())
    for _ in range(200):
        await asyncio.sleep(0)
    assert not task.done(), "양쪽이 다 등록되기 전에 발동했다"
    assert monitor.last_snapshot is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_closes_both_and_requests_rebuild_at_target():
    long_engine = make_engine("long", Decimal("0.1"), Decimal("64000"))
    short_engine = make_engine("short", Decimal("0.05"), Decimal("64000"))
    engines = {"long": long_engine, "short": short_engine}
    # 가격 상승: 롱 +0.1*2000=200, 숏 -0.05*2000=-100 -> 합산 +100
    # margin = 0.1*64000/20 + 0.05*64000/20 = 320 + 160 = 480 -> ROE 약 20%
    monitor = make_monitor(engines, Decimal("66000"))
    for engine in engines.values():
        await engine.adapter.on_price_tick(Decimal("66000"))

    outcome = await asyncio.wait_for(monitor.run(), timeout=5)

    assert outcome is CycleOutcome.REBUILD_REQUESTED
    assert long_engine.state == EngineState.COOLDOWN
    assert short_engine.state == EngineState.COOLDOWN
    assert long_engine.open_qty == Decimal("0")
    assert short_engine.open_qty == Decimal("0")
    assert monitor.last_snapshot is not None
    assert monitor.last_snapshot.roe >= Decimal("0.10")
