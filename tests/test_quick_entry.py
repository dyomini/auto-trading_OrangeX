"""quick_entry.py 유닛 테스트 — PaperAdapter로 실제 주문 접수까지 검증한다."""
from __future__ import annotations

from decimal import Decimal

import pytest

from config.settings import Settings
from exchange.base import ContractSpec
from exchange.paper import PaperAdapter
from quick_entry import QuickEntryError, run_quick_entry

INSTRUMENT = "BTC-USDT-PERPETUAL"


def make_settings(**overrides) -> Settings:
    defaults = dict(
        trading_mode="paper",
        symbol=INSTRUMENT,
        equity_usdt=Decimal("10000"),
        leverage=Decimal("20"),
        grid_tick=Decimal("50"),
        quick_entry_chunk_usdt=Decimal("50"),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_spec() -> ContractSpec:
    return ContractSpec(
        instrument=INSTRUMENT, tick_size=Decimal("0.1"), min_qty=Decimal("0.0001"),
        min_notional=Decimal("10"), contract_size=Decimal("1"),
    )


async def make_adapter(last_price: str = "64000") -> PaperAdapter:
    adapter = PaperAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    await adapter.on_price_tick(Decimal(last_price))
    return adapter


@pytest.mark.asyncio
async def test_short_places_orders_at_increasing_prices():
    settings = make_settings()
    adapter = await make_adapter("64000")

    order_ids = await run_quick_entry(settings, "short", Decimal("250"), adapter)

    assert len(order_ids) == 5
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert len(open_orders) == 5
    prices = sorted(o.request.price for o in adapter._open_orders.values())
    assert prices == [Decimal("64000"), Decimal("64050"), Decimal("64100"), Decimal("64150"), Decimal("64200")]
    assert all(o.side == "sell" for o in [r.request for r in adapter._open_orders.values()])


@pytest.mark.asyncio
async def test_long_places_orders_at_decreasing_prices():
    settings = make_settings()
    adapter = await make_adapter("64000")

    order_ids = await run_quick_entry(settings, "long", Decimal("150"), adapter)

    assert len(order_ids) == 3
    prices = sorted(o.request.price for o in adapter._open_orders.values())
    assert prices == [Decimal("63900"), Decimal("63950"), Decimal("64000")]
    assert all(r.request.side == "buy" for r in adapter._open_orders.values())


@pytest.mark.asyncio
async def test_equal_margin_per_chunk():
    settings = make_settings()
    adapter = await make_adapter("64000")

    await run_quick_entry(settings, "short", Decimal("200"), adapter)

    # 청크당 증거금이 동일해야 한다: qty * price / leverage == chunk_usdt(50)
    # (compute_grid의 step_qty = margin*leverage/price 나눗셈 왕복 오차가 Decimal
    # 정밀도 한계에서 미세하게 남을 수 있어 근사 비교한다)
    for internal in adapter._open_orders.values():
        margin = internal.request.qty * internal.request.price / settings.leverage
        assert abs(margin - Decimal("50")) < Decimal("0.0001")


@pytest.mark.asyncio
async def test_remainder_below_chunk_size_is_dropped():
    settings = make_settings()
    adapter = await make_adapter("64000")

    order_ids = await run_quick_entry(settings, "short", Decimal("120"), adapter)

    assert len(order_ids) == 2  # 120 // 50 = 2 chunks, 20 USDT 남는 건 버림


@pytest.mark.asyncio
async def test_total_below_chunk_size_raises():
    settings = make_settings()
    adapter = await make_adapter("64000")

    with pytest.raises(QuickEntryError):
        await run_quick_entry(settings, "short", Decimal("30"), adapter)


@pytest.mark.asyncio
async def test_chunk_count_over_100_raises():
    # compute_grid()는 100단계(TOTAL_STEPS)까지만 받는다(strategy/grid.py) — quick_entry가
    # 이를 재사용하는 이상 101개 이상 청크를 요청하면 그 원본 ValueError 대신 명확한
    # QuickEntryError로 막아야 한다(2026-08-05 매트릭스 테스트로 발견한 실제 버그의 회귀 테스트).
    settings = make_settings()
    adapter = await make_adapter("64000")

    with pytest.raises(QuickEntryError):
        await run_quick_entry(settings, "short", Decimal("5050"), adapter)  # 101 chunks


@pytest.mark.asyncio
async def test_chunk_count_exactly_100_succeeds():
    settings = make_settings()
    adapter = await make_adapter("64000")

    order_ids = await run_quick_entry(settings, "short", Decimal("5000"), adapter)

    assert len(order_ids) == 100
