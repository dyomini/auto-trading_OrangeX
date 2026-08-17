"""main.py 유닛/와이어링 테스트.

`build_grid_rows`/`build_execution_adapter`는 `tests/test_grid_setup.py`가 담당한다
(2026-07-30 다중 사이클 지원 작업 중 main.py에서 engine/grid_setup.py로 이동함 —
CycleManager도 같은 로직이 필요해서). 여기서는 `run()` 전체 조립이 실제로 동작하는지
(생성자 인자, isinstance 분기, 임포트 등 와이어링 실수를 잡기 위한) 얕은 스모크
테스트로만 검증한다 — 그 안에서 쓰이는 GridEngine/FillRouter/EntryScheduler/
CycleManager/restart_recovery 각각의 깊은 로직은 이미 각자의 테스트 파일이 담당한다.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from config.settings import Settings
from engine.grid_engine import EngineHaltedError, EngineState
from engine.halt_flag import HaltedFlagPresentError, write_halt_flag
from exchange.base import OrderRequest, StopOrderRequest
from exchange.orangex.adapter import OrangeXAdapter
from exchange.paper import PaperAdapter
from main import _derive_halt_flag_path, run

INSTRUMENT = "BTC-USDT-PERPETUAL"


class FakeOrangeXClient:
    """tests/test_orangex_adapter.py의 FakeClient와 동일한 패턴."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict, bool]] = []

    async def call(self, method, params=None, authed=True):
        self.calls.append((method, dict(params or {}), authed))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return response


def make_market_data_adapter(last_price: str = "64000", min_qty: str = "0.001", min_notional: str = "10") -> OrangeXAdapter:
    client = FakeOrangeXClient(
        {
            "/public/get_instruments": {
                "instruments": [
                    {
                        "instrument_name": INSTRUMENT,
                        "tick_size": "50",
                        "min_qty": min_qty,
                        "min_notional": min_notional,
                        "contract_size": "1",
                    }
                ]
            },
            "/public/ticker": {"last_price": last_price},
        }
    )
    return OrangeXAdapter(client)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        trading_mode="paper",
        symbol=INSTRUMENT,
        direction="long",
        equity_usdt=Decimal("10000"),
        leverage=Decimal("20"),
        grid_tick=Decimal("50"),
        max_open_grid_orders=2,
        # .env가 있는 개발 머신에서 테스트가 그 값에 오염되지 않도록 결과에 영향을
        # 주는 필드를 전부 명시적으로 고정한다. pydantic-settings는 kwargs로 안 넘긴
        # 필드를 .env에서 읽어오기 때문에, 예전엔 로컬 .env의 MANUAL_MODE=TRUE가
        # 흘러들어와 SL 경로 테스트가 조용히 무력화됐다(2026-08-17 발견).
        manual_mode=False,
        sl_enabled=True,
        grid_preset=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _binance_klines_response(direction: str) -> list[list]:
    """단조 증가/감소 종가로 RSI 극단값(0/100)을 결정론적으로 유도한다
    (tests/test_entry_scheduler.py와 동일한 기법)."""
    from datetime import datetime, timezone
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    one_day_ms = 24 * 60 * 60 * 1000
    n = 16
    if direction == "long":
        closes = [str(70000 - i * 100) for i in range(15)] + ["68000"]
    else:
        closes = [str(60000 + i * 100) for i in range(15)] + ["61500"]
    rows = []
    for i, close in enumerate(closes):
        if i == n - 1:
            open_time_ms = now_ms - 3_600_000
        else:
            offset = (n - 1 - i)
            open_time_ms = now_ms - 3_600_000 - offset * one_day_ms
        rows.append([open_time_ms, close, close, close, close])
    return rows


@pytest.mark.asyncio
async def test_run_wires_up_and_reaches_laddering_then_shuts_down_cleanly():
    """전체 조립이 실제로 동작하는지 확인하는 얕은 스모크 테스트 — RSI 필터를 확실히
    통과하는 시세를 주입해 IDLE -> SCOUTING -> LADDERING까지 실제로 도달하는지 보고,
    이후 태스크를 취소했을 때 고아 태스크 없이 깔끔히 정리되는지 검증한다(fill_router/
    entry_scheduler/price_watch/cycle_manager 4개 태스크 전부)."""
    settings = make_settings()
    market_data_adapter = make_market_data_adapter(last_price="64000")

    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_binance_klines_response("long"))

    binance_client = httpx.AsyncClient(transport=httpx.MockTransport(binance_handler))

    ready_engines = []
    task = asyncio.create_task(
        run(
            settings,
            market_data_adapter=market_data_adapter,
            binance_http_client=binance_client,
            on_engine_ready=ready_engines.append,
        )
    )
    try:
        for _ in range(500):
            if ready_engines and ready_engines[0].state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        assert ready_engines, "on_engine_ready가 호출되지 않음 — 와이어링 실패"
        assert ready_engines[0].state == EngineState.LADDERING
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await binance_client.aclose()

    # run()이 취소된 뒤 실제로 백그라운드 태스크가 다 정리됐는지 확인 —
    # asyncio.all_tasks()에 fill_router/entry_scheduler/price_watch/cycle_manager가
    # 안 남아있어야 함.
    remaining = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert remaining == []


@pytest.mark.asyncio
async def test_run_manual_mode_ladders_immediately_without_rsi_check():
    """manual_mode=True로 와이어링됐는지 end-to-end로 확인한다 — RSI 캔들 조회(limit=16,
    engine/entry_scheduler.py의 _RSI_PERIOD+2)가 호출되면(=RSI 필터를 여전히 타고
    있다는 뜻) 즉시 실패하는 핸들러를 준다. ATR 급등 판정용 조회(limit=18, engine/
    grid_setup.py)는 build_grid_rows()가 manual_mode와 무관하게 항상 수행하므로
    정상 응답을 준다."""
    settings = make_settings(manual_mode=True)
    market_data_adapter = make_market_data_adapter(last_price="64000")

    def binance_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("limit") == "16":
            raise AssertionError("manual_mode에서는 RSI 캔들 조회가 호출되면 안 됨")
        return httpx.Response(200, json=_binance_klines_response("long"))

    binance_client = httpx.AsyncClient(transport=httpx.MockTransport(binance_handler))

    ready_engines = []
    task = asyncio.create_task(
        run(
            settings,
            market_data_adapter=market_data_adapter,
            binance_http_client=binance_client,
            on_engine_ready=ready_engines.append,
        )
    )
    try:
        for _ in range(500):
            if ready_engines and ready_engines[0].state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        assert ready_engines, "on_engine_ready가 호출되지 않음 — 와이어링 실패"
        assert ready_engines[0].state == EngineState.LADDERING
        assert ready_engines[0].manual_mode is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await binance_client.aclose()


@pytest.mark.asyncio
async def test_run_refuses_to_start_when_halt_flag_present(tmp_path):
    """halted로 정지했던 이전 실행의 흔적이 있으면(engine/halt_flag.py) 아무것도
    구성하지 않고(어댑터조차 안 만듦) 즉시 거부해야 한다."""
    flag_path = tmp_path / "halted.json"
    write_halt_flag(str(flag_path), "이전 실행에서 SL 등록 실패")
    settings = make_settings(halt_flag_path=str(flag_path))

    with pytest.raises(HaltedFlagPresentError, match="SL 등록 실패"):
        await run(settings)


class _FailingStopPaperAdapter(PaperAdapter):
    async def place_stop_order(self, order: StopOrderRequest):
        raise RuntimeError("시뮬레이션: SL 등록 실패")


@pytest.mark.asyncio
async def test_run_writes_halt_flag_when_engine_halts_via_fill_router(tmp_path):
    """EngineHaltedError가 실제 백그라운드 태스크(fill_router)에서 발생했을 때 main.py가
    플래그 파일을 남기고 종료하는지 end-to-end로 검증한다. tier4(major_tier>=4) 격자
    행을 실제로 60개 다 채우는 대신, 엔진 내부 카운터를 tier4 시점으로 직접 맞추고
    그 한 행에 대해서만 실제 주문을 걸어 체결시킨다 — FillRouter가 이 체결을 실제로
    라우팅해서 on_fill -> _reregister_sl -> 실패 -> EngineHaltedError 경로를 그대로 탄다."""
    flag_path = tmp_path / "halted.json"
    # 이 테스트는 tier4(index 60) 행이 필요 — max_stage 기본값(3, =60단계 상한)이면
    # tier4에 아예 도달 못 하므로 넉넉히 올려준다.
    settings = make_settings(halt_flag_path=str(flag_path), max_stage=4)
    market_data_adapter = make_market_data_adapter(last_price="64000")
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    execution_adapter = _FailingStopPaperAdapter(
        instrument=INSTRUMENT, contract_spec=contract_spec, initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )

    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_binance_klines_response("long"))

    binance_client = httpx.AsyncClient(transport=httpx.MockTransport(binance_handler))

    ready_engines = []
    task = asyncio.create_task(
        run(
            settings,
            market_data_adapter=market_data_adapter,
            execution_adapter=execution_adapter,
            binance_http_client=binance_client,
            on_engine_ready=ready_engines.append,
        )
    )
    try:
        for _ in range(500):
            if ready_engines and ready_engines[0].state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        engine = ready_engines[0]
        tier4_row = engine.grid_rows[60]
        assert tier4_row.major_tier == 4  # STEPS_PER_TIER=20 -> index 60부터 4차

        order = await execution_adapter.place_limit_order(
            OrderRequest(
                instrument=INSTRUMENT, side="buy", price=tier4_row.entry_price,
                qty=tier4_row.step_qty, client_order_id="grid-60-test",
            )
        )
        engine.filled_step_count = 60
        engine.open_qty = tier4_row.step_qty
        engine.resting_grid_order_ids[60] = order.order_id

        # on_price_tick이 방금 건 주문의 지정가와 정확히 같은 값으로 틱을 주므로
        # crossing 조건(price <= order.price)이 충족돼 이 안에서 바로 체결되고
        # _fill_queue에 쌓인다 — 별도로 fill_order()를 부를 필요 없음.
        await execution_adapter.on_price_tick(tier4_row.entry_price)

        with pytest.raises(EngineHaltedError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
        await binance_client.aclose()

    assert flag_path.exists()
    saved = flag_path.read_text(encoding="utf-8")
    assert "SL" in saved or "정지" in saved or "halted" in saved.lower()


def test_derive_halt_flag_path_appends_direction_to_stem():
    assert _derive_halt_flag_path("state/halted.json", "long") == str(Path("state/halted_long.json"))
    assert _derive_halt_flag_path("state/halted.json", "short") == str(Path("state/halted_short.json"))


@pytest.mark.asyncio
async def test_run_both_rejects_preinjected_execution_adapter():
    settings = make_settings(direction="both")
    market_data_adapter = make_market_data_adapter(last_price="64000")
    fake_adapter = PaperAdapter(
        instrument=INSTRUMENT,
        contract_spec=await market_data_adapter.get_contract_spec(INSTRUMENT),
        initial_equity=Decimal("10000"), leverage=Decimal("20"),
        maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )

    with pytest.raises(ValueError, match="both"):
        await run(settings, market_data_adapter=market_data_adapter, execution_adapter=fake_adapter)


@pytest.mark.asyncio
async def test_run_both_directions_wires_up_independent_long_and_short_engines(tmp_path):
    """direction="both"가 실제로 롱/숏 완전히 독립된 두 엔진을 동시에 LADDERING까지
    도달시키는지 end-to-end로 확인한다. RSI 조건은 롱(<=30)/숏(>=70)이 동시에 참일 수
    없으므로 manual_mode로 RSI 필터 자체를 건너뛰게 해서 둘 다 결정론적으로 LADDERING에
    도달하게 만든다."""
    # equity_usdt=20000 -> 방향당 10000(다른 테스트들의 기본 equity와 동일) — 정확히
    # 반씩 나뉘고도 최소 주문 수량을 넉넉히 통과하도록.
    settings = make_settings(
        direction="both", manual_mode=True, equity_usdt=Decimal("20000"),
        halt_flag_path=str(tmp_path / "halted.json"),
    )
    market_data_adapter = make_market_data_adapter(last_price="64000")

    def binance_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_binance_klines_response("long"))

    binance_client = httpx.AsyncClient(transport=httpx.MockTransport(binance_handler))

    ready_engines = []
    task = asyncio.create_task(
        run(
            settings,
            market_data_adapter=market_data_adapter,
            binance_http_client=binance_client,
            on_engine_ready=ready_engines.append,
        )
    )
    try:
        for _ in range(500):
            if len(ready_engines) >= 2 and all(e.state == EngineState.LADDERING for e in ready_engines):
                break
            await asyncio.sleep(0)
        assert len(ready_engines) == 2
        directions = {e.direction for e in ready_engines}
        assert directions == {"long", "short"}

        long_engine = next(e for e in ready_engines if e.direction == "long")
        short_engine = next(e for e in ready_engines if e.direction == "short")

        # 롱은 시작가보다 낮게, 숏은 시작가보다 높게 격자가 잡혀야 함(정반대 방향).
        assert long_engine.grid_rows[1].entry_price < long_engine.grid_rows[0].entry_price
        assert short_engine.grid_rows[1].entry_price > short_engine.grid_rows[0].entry_price

        # 전체 자금(20000)이 반씩(10000씩) 분배됐는지 — equity=10000, max_stage=3(기본값)
        # 기준 1단계 증거금은 60단계(3-tier) 가중치 합(3530)으로 재정규화된 값인 28.3이어야
        # 한다(2026-08-04, weight_sum 재정규화 수정 전에는 100단계 전체 합(17130) 기준
        # 5.8이었음 — max_stage로 안 쓰는 tier4/5 가중치가 분모에 남아 equity 대부분이
        # 미배정으로 낭비되던 값).
        assert long_engine.grid_rows[0].cum_margin == Decimal("28.3")
        assert short_engine.grid_rows[0].cum_margin == Decimal("28.3")

        # 서로 다른 PaperAdapter(독립된 자금/포지션)를 쓰고 있어야 함.
        assert long_engine.adapter is not short_engine.adapter
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await binance_client.aclose()

    # halted 플래그도 방향별로 분리된 경로를 쓰므로 원래 경로 자체는 생성되지 않아야 함.
    assert not Path(settings.halt_flag_path).exists()


# --------------------------------------------------------------------------
# DIRECTION=auto — 사이클마다 15분봉 RSI(14)가 방향을 정한다 (2026-08-17).
# --------------------------------------------------------------------------

FIFTEEN_MIN_MS = 15 * 60 * 1000


def _binance_15m_klines(expect: str) -> list[list]:
    """마지막 행은 아직 진행 중인 봉이라 closed_candles가 걸러낸다.
    완결봉 15개가 단조 증가/감소라 RSI가 100/0으로 결정론적으로 나온다."""
    from datetime import datetime, timezone
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if expect == "short":
        closes = [str(60000 + i * 100) for i in range(15)] + ["99999"]
    else:
        closes = [str(70000 - i * 100) for i in range(15)] + ["1"]
    rows = []
    n = len(closes)
    for i, close in enumerate(closes):
        if i == n - 1:
            open_time_ms = now_ms - 60_000  # 진행 중
        else:
            open_time_ms = now_ms - 60_000 - (n - 1 - i) * FIFTEEN_MIN_MS
        rows.append([open_time_ms, close, close, close, close])
    return rows


def _make_15m_client(expect: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["interval"] == "15m", "auto 모드는 15분봉을 봐야 한다"
        return httpx.Response(200, json=_binance_15m_klines(expect))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _run_auto_until_engine(settings, market_data_adapter, client, ready_engines):
    task = asyncio.create_task(
        run(
            settings,
            market_data_adapter=market_data_adapter,
            binance_http_client=client,
            on_engine_ready=ready_engines.append,
        )
    )
    for _ in range(2000):
        if ready_engines and ready_engines[0].state == EngineState.LADDERING:
            break
        await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_run_auto_picks_short_when_15m_rsi_at_or_above_50():
    settings = make_settings(direction="auto")
    market_data_adapter = make_market_data_adapter(last_price="64000")
    client = _make_15m_client("short")
    ready_engines: list = []

    task = await _run_auto_until_engine(settings, market_data_adapter, client, ready_engines)
    try:
        assert ready_engines, "엔진이 조립되지 않음"
        engine = ready_engines[0]
        assert engine.direction == "short"
        # 방향이 compute_grid까지 실제로 전달됐는지 격자 기울기로 확인한다 —
        # 숏이면 진입가가 현재가 위쪽으로 올라간다.
        assert engine.grid_rows[1].entry_price > engine.grid_rows[0].entry_price
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()


@pytest.mark.asyncio
async def test_run_auto_picks_long_when_15m_rsi_below_50():
    settings = make_settings(direction="auto")
    market_data_adapter = make_market_data_adapter(last_price="64000")
    client = _make_15m_client("long")
    ready_engines: list = []

    task = await _run_auto_until_engine(settings, market_data_adapter, client, ready_engines)
    try:
        assert ready_engines
        engine = ready_engines[0]
        assert engine.direction == "long"
        assert engine.grid_rows[1].entry_price < engine.grid_rows[0].entry_price
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()


@pytest.mark.asyncio
async def test_run_auto_keeps_tp_enabled_and_skips_daily_rsi_gate():
    """auto는 진입 게이트만 끄고 TP 자동 재등록은 살아 있어야 한다. 일봉(1d) 조회가
    한 번이라도 나가면 핸들러가 터진다."""
    settings = make_settings(direction="auto")
    market_data_adapter = make_market_data_adapter(last_price="64000")
    client = _make_15m_client("long")
    ready_engines: list = []

    task = await _run_auto_until_engine(settings, market_data_adapter, client, ready_engines)
    try:
        engine = ready_engines[0]
        assert engine.state == EngineState.LADDERING
        assert engine.manual_mode is False  # TP/hybrid reset이 켜져 있어야 한다
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()


@pytest.mark.asyncio
async def test_run_auto_rejects_manual_mode():
    """manual_mode에서는 COOLDOWN에 도달하는 경로가 없어 방향 재판정 시점이 영원히
    오지 않는다 — 조용히 한 사이클만 돌게 두지 않고 명확히 거부한다."""
    settings = make_settings(direction="auto", manual_mode=True)
    market_data_adapter = make_market_data_adapter()

    with pytest.raises(ValueError, match="manual_mode"):
        await run(settings, market_data_adapter=market_data_adapter)


@pytest.mark.asyncio
async def test_run_auto_rejects_preinjected_execution_adapter():
    settings = make_settings(direction="auto")
    market_data_adapter = make_market_data_adapter()
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    adapter = PaperAdapter(
        instrument=INSTRUMENT, contract_spec=contract_spec, initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )

    with pytest.raises(ValueError, match="execution_adapter"):
        await run(settings, market_data_adapter=market_data_adapter, execution_adapter=adapter)


@pytest.mark.asyncio
async def test_run_auto_rebuilds_stack_with_new_direction_after_cycle_completes():
    """이 파일의 핵심 e2e: 사이클 1(롱)을 실제로 TP까지 체결시켜 COOLDOWN에 넣고,
    그 사이에 15분봉 RSI를 뒤집어서 사이클 2가 **숏으로, 새 엔진 인스턴스로** 다시
    조립되는지 확인한다. FIRST_COMPLETED + CycleOutcome 반환 경로 전체를 탄다."""
    settings = make_settings(
        direction="auto", cooldown_minutes=0, cycle_manager_poll_interval_seconds=0,
        price_poll_interval_seconds=0, max_open_grid_orders=1,
    )
    market_data_adapter = make_market_data_adapter(last_price="64000")
    expect = {"value": "long"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["interval"] == "15m"
        return httpx.Response(200, json=_binance_15m_klines(expect["value"]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ready: list = []

    task = asyncio.create_task(
        run(settings, market_data_adapter=market_data_adapter,
            binance_http_client=client, on_engine_ready=ready.append)
    )
    try:
        for _ in range(5000):
            if ready and ready[0].state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        assert ready, "사이클 1 엔진이 조립되지 않음"
        engine = ready[0]
        assert engine.direction == "long"
        adapter = engine.adapter

        # 사이클 1을 실제로 끝낸다. index0의 진입가는 base_price(=현재가)와 같아서
        # _price_watch_loop의 on_price_tick으로 이미 체결되고 FillRouter가 라우팅한다 —
        # 그 결과로 TP가 걸릴 때까지 기다렸다가, TP를 체결시켜 COOLDOWN까지 보낸다.
        for _ in range(5000):
            if engine.tp_order_id is not None:
                break
            await asyncio.sleep(0)
        assert engine.tp_order_id is not None, "index0 체결이 FillRouter로 라우팅되지 않음"

        tp_price = engine.grid_rows[engine.filled_step_count - 1].target_tp_price
        await adapter.fill_order(engine.tp_order_id, qty=engine.open_qty, price=tp_price)
        for _ in range(5000):
            if engine.state == EngineState.COOLDOWN:
                break
            await asyncio.sleep(0)
        assert engine.state == EngineState.COOLDOWN

        # 다음 사이클은 숏이 나오도록 RSI를 뒤집는다
        expect["value"] = "short"

        for _ in range(20000):
            if len(ready) >= 2:
                break
            await asyncio.sleep(0)

        assert len(ready) >= 2, "사이클 2 스택이 재조립되지 않음"
        assert ready[1] is not ready[0], "같은 엔진 인스턴스를 재사용하면 안 된다"
        assert ready[1].direction == "short"
        assert ready[1].grid_rows[1].entry_price > ready[1].grid_rows[0].entry_price
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()


@pytest.mark.asyncio
async def test_run_fixed_direction_still_resets_in_place():
    """회귀 방어: 고정 방향 모드가 실수로 재조립 모드로 바뀌지 않았는지.
    사이클이 끝나도 엔진 인스턴스는 그대로여야 한다(on_engine_ready 1회만 호출)."""
    settings = make_settings(
        direction="long", cooldown_minutes=0, cycle_manager_poll_interval_seconds=0,
        price_poll_interval_seconds=0, max_open_grid_orders=1, manual_mode=True,
    )
    market_data_adapter = make_market_data_adapter(last_price="64000")
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    adapter = PaperAdapter(
        instrument=INSTRUMENT, contract_spec=contract_spec, initial_equity=Decimal("10000"),
        leverage=Decimal("20"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0006"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_binance_klines_response("long"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ready: list = []

    task = asyncio.create_task(
        run(settings, market_data_adapter=market_data_adapter, execution_adapter=adapter,
            binance_http_client=client, on_engine_ready=ready.append)
    )
    try:
        for _ in range(5000):
            if ready and ready[0].state == EngineState.LADDERING:
                break
            await asyncio.sleep(0)
        assert len(ready) == 1
        for _ in range(2000):
            await asyncio.sleep(0)
        assert len(ready) == 1, "고정 방향인데 스택이 재조립됐다"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()
