"""quick_entry.py 유닛 테스트 — PaperAdapter로 실제 주문 접수까지 검증한다.

price_range_usdt는 증거금 총액이 아니라 현재가 기준 ±가격 범위다(2026-08-05 사용자
정정 — "3k/5k는 마진 금액이 아니라 현재가 기준 +-(롱/숏) 가격 범위"). 주문 개수는
price_range_usdt // grid_tick으로 정해진다.

증거금은 균등 분배가 아니라 config/weights.csv 비중대로 equity_usdt 전액을 배분한다
(2026-08-05 사용자 재정정 — "진입 마진은 항상 50usdt가 아니야. 엑셀에 기재된 비중대로
진입 마진 설계"). weights.csv의 앞 5개 값(10,11,12,13,14)과 equity_usdt=10000,
leverage=20 기준으로 손계산한 값(1666.7/1833.3/2000.0/2166.7/2333.3, 합계 10000.0
정확히 일치)을 그대로 회귀 기준으로 쓴다.

수량 정밀도(qty_step) 테스트는 make_spec()의 기본값 0.001(실제 BTC-USDT-PERPETUAL의
min_trade_amount, 2026-08-06 실전 사고로 발견)을 쓴다 — 순수 비중 계산만 검증하고
싶은 테스트는 qty_step=0("미확인/반영 안 함")으로 명시해 반올림 간섭을 없앤다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from config.settings import Settings
from exchange.base import ContractSpec, OrderResult
from exchange.paper import PaperAdapter
from quick_entry import QuickEntryError, compute_chunk_count, compute_preview_rows, run_quick_entry

INSTRUMENT = "BTC-USDT-PERPETUAL"


class _RejectingAdapter(PaperAdapter):
    """2026-08-05 실전 사고 회귀 테스트용 — 거래소가 주문 접수 직후 곧바로 취소하는
    상황(예: 헤지 모드 계좌에서 position_side 불일치, error_code 5998)을 재현한다.
    place_limit_order()는 예외를 던지지 않고 status="cancelled"인 OrderResult를 정상
    반환한다는 게 핵심 — run_quick_entry()가 이 status를 직접 확인해야만 잡을 수 있다."""

    def __init__(self, *args, reject_from_index: int = 0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._reject_from_index = reject_from_index
        self._call_count = 0

    async def place_limit_order(self, order):  # type: ignore[override]
        result = await super().place_limit_order(order)
        should_reject = self._call_count >= self._reject_from_index
        self._call_count += 1
        if should_reject:
            # 실제 거래소라면 취소된 주문은 더 이상 미체결 목록에 남지 않는다 — 내부
            # PaperAdapter 장부에서도 지워서 get_open_orders() 검증이 정확하게 한다.
            self._open_orders.pop(result.order_id, None)
            result = OrderResult(
                order_id=result.order_id, client_order_id=result.client_order_id,
                status="cancelled", filled_qty=result.filled_qty, avg_fill_price=result.avg_fill_price,
            )
        return result


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


def make_spec(qty_step: Decimal = Decimal("0.001")) -> ContractSpec:
    # qty_step 기본값 0.001은 실제 BTC-USDT-PERPETUAL의 min_trade_amount(2026-08-06
    # 실전 조회로 확인, docs/api-notes.md §4) 그대로다.
    return ContractSpec(
        instrument=INSTRUMENT, tick_size=Decimal("0.1"), min_qty=Decimal("0.001"),
        min_notional=Decimal("10"), contract_size=Decimal("1"), qty_step=qty_step,
    )


async def make_adapter(last_price: str = "64000", qty_step: Decimal = Decimal("0.001")) -> PaperAdapter:
    adapter = PaperAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(qty_step), initial_equity=Decimal("10000"),
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
    # qty_step=0(반올림 없음)으로 순수 비중 계산만 검증 — 반올림 오차는 별도 테스트에서.
    settings = make_settings()
    adapter = await make_adapter("64000", qty_step=Decimal("0"))

    order_ids = await run_quick_entry(settings, "short", Decimal("250"), adapter, adapter.contract_spec)

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

    order_ids = await run_quick_entry(settings, "long", Decimal("150"), adapter, adapter.contract_spec)

    assert len(order_ids) == 3  # 150 // grid_tick(50)
    prices = sorted(o.request.price for o in adapter._open_orders.values())
    assert prices == [Decimal("63900"), Decimal("63950"), Decimal("64000")]
    assert all(r.request.side == "buy" for r in adapter._open_orders.values())


@pytest.mark.asyncio
async def test_total_margin_equals_equity_regardless_of_chunk_count():
    # 청크 개수가 달라도(가격 범위가 달라도) 총 증거금은 항상 equity_usdt 전액이어야
    # 한다 — weights.csv 슬라이스가 재정규화되기 때문(2026-08-04 max_stage 버그 수정과
    # 동일한 원리, engine/grid_setup.py 참고). qty_step=0으로 반올림 오차를 배제한다.
    settings = make_settings()
    adapter = await make_adapter("64000", qty_step=Decimal("0"))

    await run_quick_entry(settings, "short", Decimal("100"), adapter, adapter.contract_spec)  # 2 chunks

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
        await run_quick_entry(settings, "short", Decimal("30"), adapter, adapter.contract_spec)


@pytest.mark.asyncio
async def test_chunk_count_over_100_raises():
    # compute_grid()는 100단계(TOTAL_STEPS)까지만 받는다(strategy/grid.py) — quick_entry가
    # 이를 재사용하는 이상 101개 이상 청크를 요청하면 그 원본 ValueError 대신 명확한
    # QuickEntryError로 막아야 한다(2026-08-05 매트릭스 테스트로 발견한 실제 버그의 회귀 테스트).
    settings = make_settings()
    adapter = await make_adapter("64000")

    with pytest.raises(QuickEntryError):
        await run_quick_entry(settings, "short", Decimal("5050"), adapter, adapter.contract_spec)  # 101 chunks


@pytest.mark.asyncio
async def test_chunk_count_exactly_100_succeeds():
    settings = make_settings()
    adapter = await make_adapter("64000")

    order_ids = await run_quick_entry(settings, "short", Decimal("5000"), adapter, adapter.contract_spec)

    assert len(order_ids) == 100


@pytest.mark.asyncio
async def test_immediately_cancelled_order_raises_instead_of_reporting_success():
    # 2026-08-05 실전 사고 회귀 테스트: launcher.py가 .env의 DIRECTION으로 어댑터의
    # position_side를 설정한 채(quick-entry에서 고른 방향과 다를 수 있음) 실전으로
    # 실행했더니, 헤지 모드 계좌가 주문을 접수 직후 전부 자동 취소했는데도(place_
    # limit_order 자체는 예외를 안 던짐) 코드가 이를 감지 못하고 "주문 N개 접수
    # 완료"라고 잘못 보고했다. run_quick_entry()가 각 주문의 status를 직접 확인해서
    # cancelled/rejected면 즉시 QuickEntryError로 막아야 한다.
    settings = make_settings()
    adapter = _RejectingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    await adapter.on_price_tick(Decimal("64000"))

    with pytest.raises(QuickEntryError):
        await run_quick_entry(settings, "short", Decimal("250"), adapter, adapter.contract_spec)


@pytest.mark.asyncio
async def test_partial_rejection_stops_and_raises():
    settings = make_settings()
    adapter = _RejectingAdapter(
        instrument=INSTRUMENT, contract_spec=make_spec(), initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
        reject_from_index=2,
    )
    await adapter.on_price_tick(Decimal("64000"))

    with pytest.raises(QuickEntryError):
        await run_quick_entry(settings, "short", Decimal("250"), adapter, adapter.contract_spec)  # 5 chunks, 3rd rejected

    open_orders = await adapter.get_open_orders(INSTRUMENT)
    assert len(open_orders) == 2  # 앞의 2개는 정상 접수됨 — 3번째에서 멈춤


@pytest.mark.asyncio
async def test_qty_rounded_down_to_exchange_step():
    # 2026-08-06 실전 사고 회귀 테스트: compute_grid()가 만드는 수량은 나눗셈 결과라
    # 소수점이 20자리 넘게 이어진다 — 거래소 정밀도(qty_step=0.001)의 배수로 내림해서
    # 보내야 한다. 안 그러면 실전에서 정밀도 불일치로 주문이 즉시 거부된다.
    settings = make_settings()
    adapter = await make_adapter("64000", qty_step=Decimal("0.001"))

    await run_quick_entry(settings, "short", Decimal("250"), adapter, adapter.contract_spec)

    for internal in adapter._open_orders.values():
        qty = internal.request.qty
        # qty가 0.001의 정수배인지 확인 — 나머지가 정확히 0이어야 한다.
        assert (qty / Decimal("0.001")) == (qty / Decimal("0.001")).to_integral_value()


@pytest.mark.asyncio
async def test_shortfall_after_rounding_raises_clear_error():
    # 증거금이 너무 잘게 쪼개져서(예: 매우 좁은 범위 + 낮은 레버리지) 반올림 후 qty가
    # min_qty/min_notional에 못 미치면, 거래소에 보내기 전에 명확한 QuickEntryError로
    # 막아야 한다 — 애매하게 잘려서 거래소가 또 조용히 거부하게 두면 안 된다.
    settings = make_settings(equity_usdt=Decimal("5"), leverage=Decimal("1"))
    adapter = await make_adapter("64000", qty_step=Decimal("0.001"))

    with pytest.raises(QuickEntryError):
        await run_quick_entry(settings, "short", Decimal("250"), adapter, adapter.contract_spec)
