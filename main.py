"""봇 엔트리포인트 — 지금까지 만든 조각(GridEngine/FillRouter/EntryScheduler/
CycleManager/restart_recovery)을 실제로 기동한다 (docs/phase3-plan.md "아직 만들지
않은 것"에서 빠져있던 마지막 조립 단계).

**다중 사이클을 지원한다** (2026-07-30). 한 사이클이 COOLDOWN으로 끝나면
`CycleManager`가 `cooldown_minutes`만큼 기다렸다가 현재가 기준으로 격자를 새로 계산해
`GridEngine.reset_for_new_cycle()`을 호출한다 — 그러면 `EntryScheduler`가 (매 반복마다
IDLE 여부를 다시 체크하도록 고친 덕에) 이걸 감지해 SCOUTING부터 다시 시작한다.

**미체결 주문이 최소 수량/명목가치에 미달하는 단계가 있으면 시작(또는 다음 사이클
시작)을 거부한다.** `docs/phase1-report.md`가 이미 정책(다음 단계에 합산)을
결정했지만, 실제 병합 로직(진입가를 어느 쪽 기준으로 할지 등)은 여전히 구현되지
않았다 — 잘못된 주문을 걸지 않도록 추측 대신 명시적으로 막는다
(`engine/grid_setup.py`의 `build_grid_rows()` 참고).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import httpx

from config.settings import Settings
from engine.cycle_manager import CycleManager
from engine.entry_scheduler import EntryScheduler
from engine.fill_router import FillRouter
from engine.grid_engine import EngineHaltedError, GridEngine
from engine.grid_setup import (
    StartupError,
    build_execution_adapter,
    build_grid_rows,
    build_market_data_adapter,
)
from engine.halt_flag import check_halt_flag, write_halt_flag
from engine.restart_recovery import RestartRecoveryError, build_recovered_engine
from exchange.base import ExchangeAdapter
from exchange.orangex.adapter import OrangeXAdapter
from exchange.paper import PaperAdapter

logger = logging.getLogger(__name__)


async def _price_watch_loop(
    market_data_adapter: OrangeXAdapter,
    execution_adapter: ExchangeAdapter,
    engine: GridEngine,
    settings: Settings,
) -> None:
    """현재가를 주기적으로 조회해 hybrid reset 조건을 확인한다 — `GridEngine.
    maybe_hybrid_reset`은 거래소 매칭과 무관하게 봇이 직접 판단해서 시장가로 청산해야
    하는 로직이라 모드와 상관없이 실시간 가격 관찰이 필요하다. PaperAdapter일 때는
    추가로 `on_price_tick()`도 호출해 체결 시뮬레이션 자체를 이 틱으로 굴린다
    (PaperAdapter는 스스로 가격을 만들어내지 않으므로)."""
    while True:
        ticker = await market_data_adapter.get_ticker(settings.symbol)
        if isinstance(execution_adapter, PaperAdapter):
            await execution_adapter.on_price_tick(ticker.last_price)
        await engine.maybe_hybrid_reset(ticker.last_price)
        await asyncio.sleep(settings.price_poll_interval_seconds)


async def run(
    settings: Settings,
    market_data_adapter: Optional[OrangeXAdapter] = None,
    execution_adapter: Optional[ExchangeAdapter] = None,
    binance_http_client: Optional[httpx.AsyncClient] = None,
    on_engine_ready: Optional[Callable[[GridEngine], None]] = None,
) -> None:
    """어댑터/http_client는 테스트에서 목(mock)으로 주입하기 위한 선택 인자다 — 실제
    실행(`main()`)에서는 전부 None이라 항상 실제 구성으로 동작한다. `on_engine_ready`는
    엔진 생성 직후(백그라운드 태스크 시작 전) 호출되는 훅으로, 테스트에서 내부 `engine`
    참조를 얻거나 나중에 헬스체크/모니터링을 붙일 자리로 쓸 수 있다."""
    # halted로 정지했던 이전 실행이 있으면(engine/halt_flag.py) 사람이 확인/정리하기
    # 전까지 아예 시작하지 않는다 — restart_recovery는 거래소 상태만으로 halted와 정상
    # COOLDOWN을 구분 못 하므로(그 모듈 "알려진 한계" 참고) 이게 유일한 방어선이다.
    check_halt_flag(settings.halt_flag_path)

    if market_data_adapter is None:
        market_data_adapter = build_market_data_adapter(settings)
    contract_spec = await market_data_adapter.get_contract_spec(settings.symbol)
    if execution_adapter is None:
        execution_adapter = build_execution_adapter(settings, contract_spec)
    grid_rows = await build_grid_rows(settings, market_data_adapter, contract_spec, binance_http_client)

    try:
        engine = await build_recovered_engine(
            execution_adapter,
            settings.symbol,
            settings.direction,
            grid_rows,
            max_open_grid_orders=settings.max_open_grid_orders,
            manual_mode=settings.manual_mode,
        )
    except RestartRecoveryError as e:
        raise StartupError(f"재시작 복구 실패 — 거래소 상태와 격자 계산이 안 맞음: {e!r}") from e

    logger.info(
        "봇 기동: mode=%s symbol=%s direction=%s 복구된 state=%s filled_step_count=%d",
        settings.trading_mode, settings.symbol, settings.direction, engine.state, engine.filled_step_count,
    )
    if on_engine_ready is not None:
        on_engine_ready(engine)

    fill_router = FillRouter(adapter=execution_adapter, engine=engine, instrument=settings.symbol)
    entry_scheduler = EntryScheduler(
        engine=engine,
        instrument=settings.symbol,
        direction=settings.direction,
        poll_interval_seconds=settings.rsi_poll_interval_seconds,
        http_client=binance_http_client,
        manual_mode=settings.manual_mode,
    )
    cycle_manager = CycleManager(
        engine=engine,
        market_data_adapter=market_data_adapter,
        contract_spec=contract_spec,
        settings=settings,
        poll_interval_seconds=settings.cycle_manager_poll_interval_seconds,
        binance_http_client=binance_http_client,
    )

    tasks = [
        asyncio.create_task(fill_router.run(), name="fill_router"),
        asyncio.create_task(entry_scheduler.run(), name="entry_scheduler"),
        asyncio.create_task(
            _price_watch_loop(market_data_adapter, execution_adapter, engine, settings),
            name="price_watch",
        ),
        asyncio.create_task(cycle_manager.run(), name="cycle_manager"),
    ]

    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc is not None:
                logger.critical("태스크 %s가 예외로 종료됨 — 봇 정지: %r", task.get_name(), exc)
                if isinstance(exc, EngineHaltedError):
                    # 재시작해도 다시 조용히 거래를 시작하지 않도록 플래그를 남긴다
                    # (engine/halt_flag.py — restart_recovery만으로는 halted와 정상
                    # COOLDOWN을 구분 못 함).
                    write_halt_flag(settings.halt_flag_path, str(exc))
                raise exc
    finally:
        # 정상 종료든(위 raise) run() 자체가 외부에서 취소되든, 백그라운드 태스크가
        # 고아 상태로 계속 도는 일이 없도록 항상 여기서 정리한다.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("사용자가 Ctrl+C로 종료를 요청함 — 프로그램을 종료합니다.")


if __name__ == "__main__":
    main()
