"""quick_entry.py 유닛 테스트 — PaperAdapter로 실제 주문 접수까지 검증한다.

price_range_usdt는 증거금 총액이 아니라 현재가 기준 ±가격 범위다(2026-08-05 사용자
정정 — "3k/5k는 마진 금액이 아니라 현재가 기준 +-(롱/숏) 가격 범위"). 주문 개수는
price_range_usdt // grid_tick으로 정해진다.

증거금은 균등 분배가 아니라 config/weights.csv 비중대로 equity_usdt 전액을 배분한다
(2026-08-05 사용자 재정정 — "진입 마진은 항상 50usdt가 아니야. 엑셀에 기재된 비중대로
진입 마진 설계"). weights.csv의 앞 5개 값(10,11,12,13,14)과 equity_usdt=10000,
leverage=20 기준으로 손계산한 값(1666.7/1833.3/2000.0/2166.7/2333.3, 합계 10000.0
정확히 일치)을 그대로 회귀 기준으로 쓴다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from config.settings import Settings
from exchange.base import ContractSpec
from exchange.paper import PaperAdapter
from quick_entry import QuickEntryError, compute_chunk_count, compute_preview_rows, run_quick_entry

INSTRUMENT = "BTC-USDT-PERPETUAL"


def make_settings(**overrides) -> Settings:
    defaults = dict(
        trading_mode="paper",
        symbol=INSTRUMENT,
        equity_usdt=Decimal("10000"),
        leverage=Decimal("20"),
        grid_tick=Decimal("50"),
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


def test_compute_chunk_count_uses_grid_tick():
    settings = make_settings(grid_tick=Decimal("100"))
    assert compute_chunk_count(settings, Decimal("3000")) == 30  # 3000/100


def test_compute_preview_rows_follows_weights_csv_proportions():
    # weights.csv 앞 5개: 10,11,12,13,14 (합계 60). equity=10000, leverage=20으로
    # 손계산한 값과 정확히 일치해야 한다.
    settings = make_settings()
    rows = compute_preview_rows(settings, 5)

    margins = [row.step_margin for row in rows]
    assert margins == [
        Decimal("1666.7"), Decimal("1833.3"), Decimal("2000.0"),
        Decimal("2166.7"), Decimal("2333.3"),
    ]
    assert sum(margins) == Decimal("10000.0")  # equity_usdt 전액 소진
    # 마틴게일 설계: 뒤로 갈수록 증거금이 커져야 한다.
    assert margins == sorted(margins)


@pytest.mark.asyncio
async def test_short_places_orders_at_increasing_prices_with_weighted_margin():
    settings = make_settings()
    adapter = await make_adapter("64000")

    order_ids = await run_quick_entry(settings, "short", Decimal("250"), adapter)

    assert len(order_ids) == 5  # 250 // grid_tick(50)
    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert len(open_orders) == 5
    by_price = {o.request.price: o for o in adapter._open_orders.values()}
    prices = sorted(by_price)
    assert prices == [Decimal("64000"), Decimal("64050"), Decimal("64100"), Decimal("64150"), Decimal("64200")]
    assert all(o.side == "sell" for o in [r.request for r in adapter._open_orders.values()])

    # 가격이 오를수록(진입이 깊어질수록) 증거금도 weights.csv대로 커져야 한다.
    margins = [by_price[p].request.qty * p / settings.leverage for p in prices]
    expected = [Decimal("1666.7"), Decimal("1833.3"), Decimal("2000.0"), Decimal("2166.7"), Decimal("2333.3")]
    for actual, exp in zip(margins, expected):
        assert abs(actual - exp) < Decimal("0.01")


@pytest.mark.asyncio
async def test_long_places_orders_at_decreasing_prices():
    settings = make_settings()
    adapter = await make_adapter("64000")

    order_ids = await run_quick_entry(settings, "long", Decimal("150"), adapter)

    assert len(order_ids) == 3  # 150 // grid_tick(50)
    prices = sorted(o.request.price for o in adapter._open_orders.values())
    assert prices == [Decimal("63900"), Decimal("63950"), Decimal("64000")]
    assert all(r.request.side == "buy" for r in adapter._open_orders.values())


@pytest.mark.asyncio
async def test_total_margin_equals_equity_regardless_of_chunk_count():
    # 청크 개수가 달라도(가격 범위가 달라도) 총 증거금은 항상 equity_usdt 전액이어야
    # 한다 — weights.csv 슬라이스가 재정규화되기 때문(2026-08-04 max_stage 버그 수정과
    # 동일한 원리, engine/grid_setup.py 참고).
    settings = make_settings()
    adapter = await make_adapter("64000")

    await run_quick_entry(settings, "short", Decimal("100"), adapter)  # 2 chunks

    total_margin = sum(
        internal.request.qty * internal.request.price / settings.leverage
        for internal in adapter._open_orders.values()
    )
    assert abs(total_margin - settings.equity_usdt) < Decimal("0.1")


@pytest.mark.asyncio
async def test_range_below_tick_size_raises():
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
