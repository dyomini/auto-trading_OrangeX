"""engine/entry_scheduler.py 유닛 테스트.

바이낸스 klines 응답은 httpx.MockTransport로 흉내 낸다(tests/test_market_data.py와
동일 패턴). RSI 필터 통과 여부는 정확한 RSI 값이 아니라 "단조 증가/감소 종가는
avg_loss(또는 avg_gain)가 0이 돼 RSI가 0 또는 100이 된다"는 compute_rsi의 극단값
성질을 이용해 결정론적으로 만든다(tests/test_indicators.py가 이미 검증한 공식).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from engine.entry_scheduler import EntryMode, EntryScheduler
from engine.grid_engine import EngineState, GridEngine
from exchange.base import ContractSpec
from exchange.paper import PaperAdapter
from strategy.grid import GridStepResult

INSTRUMENT = "BTC-USDT-PERPETUAL"
ONE_DAY_MS = 24 * 60 * 60 * 1000


def make_row(index: int) -> GridStepResult:
    return GridStepResult(
        index=index, major_tier=1, sub_step=1, entry_price=Decimal("64000"), weight=Decimal("1"),
        step_qty=Decimal("0.01"), step_margin=Decimal("100"), cum_qty=Decimal("0.01"),
        cum_margin=Decimal("100"), avg_price=Decimal("64000"), available_balance=Decimal("1000"),
        liq_price=Decimal("50000"), target_roe=Decimal("0.1"), target_tp_price=Decimal("64640"),
        sl_price=Decimal("62080"),
    )


def make_engine() -> GridEngine:
    adapter = PaperAdapter(
        instrument=INSTRUMENT,
        contract_spec=ContractSpec(instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.0001"), min_notional=Decimal("10"), contract_size=Decimal("1")),
        initial_equity=Decimal("10000"), leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )
    return GridEngine(adapter=adapter, instrument=INSTRUMENT, direction="long", grid_rows=[make_row(0)], max_open_grid_orders=1)


def make_mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_kline_rows(closes: list[str], forming_last: bool = True) -> list[list]:
    """closes는 오래된 순서. forming_last=True면 마지막 행의 open_time을 아직 안 지난
    (진행 중인) 시각으로 만들어 _closed_candles가 걸러내는지 검증할 수 있게 한다."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    n = len(closes)
    rows = []
    for i, close in enumerate(closes):
        if forming_last and i == n - 1:
            open_time_ms = now_ms - 3_600_000  # 1시간 전 — 아직 이 봉의 1일이 안 지남
        else:
            offset_from_last = (n - 1 - i)
            open_time_ms = now_ms - 3_600_000 - offset_from_last * ONE_DAY_MS
        rows.append([open_time_ms, close, close, close, close])
    return rows


@pytest.mark.asyncio
async def test_check_once_starts_laddering_when_rsi_passes_long_filter():
    # 15개 완결봉을 단조 감소시켜 avg_gain=0 -> RSI=0 (<=30 통과) 강제.
    closes = [str(70000 - i * 100) for i in range(15)] + ["68000"]  # 마지막은 진행 중 봉(값 무관)
    rows = make_kline_rows(closes)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = make_mock_client(handler)
    engine = make_engine()
    await engine.start_scouting()
    scheduler = EntryScheduler(engine=engine, instrument=INSTRUMENT, direction="long", http_client=client)

    passed = await scheduler.check_once()
    await client.aclose()

    assert passed is True
    assert scheduler.last_rsi == Decimal("0")
    assert engine.state == EngineState.LADDERING


@pytest.mark.asyncio
async def test_check_once_does_not_start_laddering_when_rsi_fails_filter():
    # 단조 증가 -> avg_loss=0 -> RSI=100, 롱 필터(<=30) 통과 못 함.
    closes = [str(60000 + i * 100) for i in range(15)] + ["61500"]
    rows = make_kline_rows(closes)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = make_mock_client(handler)
    engine = make_engine()
    await engine.start_scouting()
    scheduler = EntryScheduler(engine=engine, instrument=INSTRUMENT, direction="long", http_client=client)

    passed = await scheduler.check_once()
    await client.aclose()

    assert passed is False
    assert scheduler.last_rsi == Decimal("100")
    assert engine.state == EngineState.SCOUTING


@pytest.mark.asyncio
async def test_check_once_skips_when_not_enough_closed_candles():
    closes = [str(64000 + i) for i in range(10)]  # 15개 미만
    rows = make_kline_rows(closes)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = make_mock_client(handler)
    engine = make_engine()
    await engine.start_scouting()
    scheduler = EntryScheduler(engine=engine, instrument=INSTRUMENT, direction="long", http_client=client)

    passed = await scheduler.check_once()
    await client.aclose()

    assert passed is False
    assert engine.state == EngineState.SCOUTING


@pytest.mark.asyncio
async def test_run_transitions_idle_to_scouting_then_laddering_on_pass():
    closes = [str(70000 - i * 100) for i in range(15)] + ["68000"]
    rows = make_kline_rows(closes)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = make_mock_client(handler)
    engine = make_engine()
    assert engine.state == EngineState.IDLE
    scheduler = EntryScheduler(engine=engine, instrument=INSTRUMENT, direction="long", poll_interval_seconds=3600, http_client=client)

    task = asyncio.create_task(scheduler.run())
    try:
        for _ in range(200):
            if engine.state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.LADDERING
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()


@pytest.mark.asyncio
async def test_run_immediate_mode_skips_rsi_and_ladders_immediately():
    """EntryMode.IMMEDIATE에서는 RSI 확인 자체를 안 해야 한다 — 호출되면 예외를
    던지는 핸들러로 확인한다(바이낸스 API가 호출조차 안 됐다는 걸 실패로 검증)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("IMMEDIATE 모드에서는 RSI 캔들 조회가 호출되면 안 됨")

    client = make_mock_client(handler)
    engine = make_engine()
    assert engine.state == EngineState.IDLE
    scheduler = EntryScheduler(
        engine=engine, instrument=INSTRUMENT, direction="long",
        poll_interval_seconds=3600, http_client=client, entry_mode=EntryMode.IMMEDIATE,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        for _ in range(200):
            if engine.state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.LADDERING
        assert scheduler.last_rsi is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()


@pytest.mark.asyncio
async def test_run_reenters_scouting_after_engine_reset_to_idle():
    """다중 사이클 지원(2026-07-30)의 버그 수정 검증. 예전 구현은 IDLE 체크를 루프
    진입 전 딱 한 번만 했다 — engine/cycle_manager.py의 CycleManager가 COOLDOWN 이후
    reset_for_new_cycle()로 상태를 다시 IDLE로 되돌려도, 이미 while 루프 안에 있는
    이 스케줄러는 절대 두 번째 SCOUTING에 들어가지 못했다. 매 반복마다 IDLE 여부를
    다시 확인하도록 고쳤으므로, 여기서는 reset_for_new_cycle이 하는 일(state=IDLE,
    resting_grid_order_ids 초기화)을 그대로 흉내 내 두 번째 진입까지 검증한다."""
    closes = [str(70000 - i * 100) for i in range(15)] + ["68000"]
    rows = make_kline_rows(closes)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = make_mock_client(handler)
    engine = make_engine()
    scheduler = EntryScheduler(engine=engine, instrument=INSTRUMENT, direction="long", poll_interval_seconds=0, http_client=client)

    task = asyncio.create_task(scheduler.run())
    try:
        for _ in range(200):
            if engine.state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.LADDERING
        assert engine.resting_grid_order_ids  # 1차 사이클에서 격자 주문이 걸림

        # CycleManager.reset_for_new_cycle()가 하는 일을 흉내낸다.
        engine.state = EngineState.IDLE
        engine.resting_grid_order_ids = {}

        for _ in range(200):
            if engine.resting_grid_order_ids:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.LADDERING  # 2차 사이클도 재진입 성공
        assert engine.resting_grid_order_ids
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()


@pytest.mark.asyncio
async def test_immediate_mode_still_registers_tp_on_fill():
    """이 테스트가 요구사항을 고정한다: 진입 게이트를 끄더라도 TP 자동 재등록은
    반드시 살아 있어야 한다. 예전처럼 manual_mode로 게이트를 껐다면 TP까지 같이
    꺼져서 이 테스트가 실패한다(그게 EntryMode를 분리한 이유다)."""
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("IMMEDIATE 모드에서는 RSI 캔들 조회가 호출되면 안 됨")

    client = make_mock_client(handler)
    engine = make_engine()
    assert engine.manual_mode is False  # TP/hybrid reset은 켜져 있어야 한다
    scheduler = EntryScheduler(
        engine=engine, instrument=INSTRUMENT, direction="long",
        poll_interval_seconds=3600, http_client=client, entry_mode=EntryMode.IMMEDIATE,
    )

    task = asyncio.create_task(scheduler.run())
    try:
        for _ in range(200):
            if engine.state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.LADDERING

        # 첫 격자 주문을 실제로 체결시켜 TP가 걸리는지 확인
        index = min(engine.resting_grid_order_ids)
        row = engine.grid_rows[index]
        await engine.adapter.fill_order(
            engine.resting_grid_order_ids[index], qty=row.step_qty, price=row.entry_price
        )
        await engine.adapter.on_price_tick(row.entry_price)
        await engine.on_fill(index)

        assert engine.tp_order_id is not None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()
