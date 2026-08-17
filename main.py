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

**롱/숏 동시 운용을 지원한다** (`direction="both"`, 2026-08-04 사용자 요청). 이 경우
`run()`은 `equity_usdt`를 반씩 나눠 롱용/숏용 `Settings` 사본을 각각 만들고,
`_run_single_direction()`(원래 `run()` 본문이었던 로직)을 완전히 독립된 두 벌로 동시에
돌린다 — 각자 자기 몫의 자금/GridEngine/주문/포지션만 다루고 서로 절대 침범하지 않는다.
헤지 모드 계좌에서 같은 instrument에 롱/숏 포지션이 동시에 존재할 때 서로 뒤섞이지
않도록 `OrangeXAdapter.get_position()`/`get_open_orders()`가 자신의 `position_side`로
필터링하고(exchange/orangex/adapter.py), REST 클라이언트는 계정 전체 레이트리밋을
지키기 위해 두 방향이 공유하되(`build_execution_adapter`의 `shared_client`), WS 연결은
`OrangeXWsClient.notifications()`가 단일 소비자용 큐라서 반드시 방향마다 독립적으로 새로
만든다(같은 채널을 두 번 구독해도 각자 전체 스트림을 받으므로 문제없음).
"""
from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable, Optional

import httpx

from config.settings import Settings
from engine.cycle_manager import CycleManager, CycleOutcome, CycleRestartPolicy
from engine.direction_selector import decide_direction_with_retry
from engine.entry_scheduler import EntryMode, EntryScheduler
from engine.fill_router import FillRouter
from engine.grid_engine import EngineHaltedError, GridEngine
from engine.grid_setup import (
    StartupError,
    build_execution_adapter,
    build_grid_rows,
    build_market_data_adapter,
)
from engine.halt_flag import check_halt_flag, write_halt_flag
from engine.restart_recovery import (
    RestartRecoveryError,
    build_recovered_engine,
    detect_open_direction,
)
from strategy.liquidation import Direction
from exchange.base import ContractSpec, ExchangeAdapter
from exchange.orangex.adapter import OrangeXAdapter
from exchange.orangex.client import OrangeXClient
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
        if engine.filled_step_count > 0:
            avg_price = engine.grid_rows[engine.filled_step_count - 1].avg_price
            margin = engine.open_qty * avg_price / settings.leverage
        else:
            margin = Decimal("0")
        margin = margin.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        logger.info(
            "[%s] 현재가: %s USDT | 상태: %s | 진행 단계: %d/%d | 사용 증거금: %s USDT",
            settings.direction, ticker.last_price, engine.state.value, engine.filled_step_count,
            len(engine.grid_rows), margin,
        )
        await asyncio.sleep(settings.price_poll_interval_seconds)


def _entry_mode_for(settings: Settings) -> EntryMode:
    """진입 게이트 방식 결정. `manual_mode`는 청산 자동화를 끄는 플래그지 진입 게이트가
    아니지만, 그 모드에서는 RSI를 기다릴 이유가 없어 즉시 진입으로 매핑한다
    (2026-08-17 EntryMode 분리 이전의 동작을 그대로 보존)."""
    if settings.manual_mode:
        return EntryMode.IMMEDIATE
    return EntryMode.RSI_GATED


def _derive_halt_flag_path(base_path: str, direction: str) -> str:
    """direction="both"에서 롱/숏이 halt flag 파일을 공유하면 한쪽만 halted여도 다른
    멀쩡한 쪽까지 재시작을 못 하게 막아버린다 — 방향별로 별도 파일을 쓰도록 경로를
    분리한다 (예: state/halted.json -> state/halted_long.json)."""
    p = Path(base_path)
    return str(p.with_stem(f"{p.stem}_{direction}"))


async def _run_single_direction(
    settings: Settings,
    market_data_adapter: OrangeXAdapter,
    contract_spec: ContractSpec,
    execution_adapter: Optional[ExchangeAdapter] = None,
    binance_http_client: Optional[httpx.AsyncClient] = None,
    on_engine_ready: Optional[Callable[[GridEngine], None]] = None,
    entry_mode: Optional[EntryMode] = None,
    cycle_restart_policy: CycleRestartPolicy = CycleRestartPolicy.RESET_IN_PLACE,
) -> Optional[CycleOutcome]:
    """방향 하나(`settings.direction`이 "long" 또는 "short")의 엔진 스택 전체를 조립해서
    계속 돌린다. 원래 `run()`의 본문이었으나, direction="both" 지원을 위해 `run()`이
    이 함수를 롱/숏 각각의 독립된 `Settings` 사본으로 두 번(동시에) 호출하도록 분리했다.
    단일 방향 모드에서는 `run()`이 그냥 이 함수를 한 번만 호출한다."""
    # halted로 정지했던 이전 실행이 있으면(engine/halt_flag.py) 사람이 확인/정리하기
    # 전까지 아예 시작하지 않는다 — restart_recovery는 거래소 상태만으로 halted와 정상
    # COOLDOWN을 구분 못 하므로(그 모듈 "알려진 한계" 참고) 이게 유일한 방어선이다.
    check_halt_flag(settings.halt_flag_path)

    if execution_adapter is None:
        execution_adapter = build_execution_adapter(settings, contract_spec)
    grid_rows = await build_grid_rows(settings, market_data_adapter, contract_spec)

    try:
        engine = await build_recovered_engine(
            execution_adapter,
            settings.symbol,
            settings.direction,
            grid_rows,
            max_open_grid_orders=settings.max_open_grid_orders,
            manual_mode=settings.manual_mode,
            mandatory_sl_min_tier=settings.mandatory_sl_min_tier,
            sl_enabled=settings.sl_enabled,
            # 거래소 수량 정밀도(qty_step) 반올림용 — 없으면 compute_grid()의 미가공
            # 수량이 그대로 나가 라이브에서 전 주문이 거부된다(2026-08-17).
            contract_spec=contract_spec,
        )
    except RestartRecoveryError as e:
        raise StartupError(f"재시작 복구 실패 — 거래소 상태와 격자 계산이 안 맞음: {e!r}") from e

    if settings.grid_preset is not None:
        # 프리셋이 .env의 LEVERAGE/MAX_STAGE를 덮어쓰므로(config/settings.py의
        # _apply_grid_preset) 실제로 무슨 값이 쓰이는지 반드시 눈에 보이게 남긴다.
        logger.info(
            "격자 프리셋 %s 적용: max_stage=%d, leverage=%s배 (.env의 LEVERAGE/MAX_STAGE는 무시됨)",
            settings.grid_preset, settings.max_stage, settings.leverage,
        )
    logger.info(
        "봇 기동: mode=%s symbol=%s direction=%s leverage=%s 복구된 state=%s filled_step_count=%d",
        settings.trading_mode, settings.symbol, settings.direction, settings.leverage,
        engine.state, engine.filled_step_count,
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
        entry_mode=entry_mode if entry_mode is not None else _entry_mode_for(settings),
    )
    cycle_manager = CycleManager(
        engine=engine,
        market_data_adapter=market_data_adapter,
        contract_spec=contract_spec,
        settings=settings,
        poll_interval_seconds=settings.cycle_manager_poll_interval_seconds,
        restart_policy=cycle_restart_policy,
    )

    cycle_task = asyncio.create_task(cycle_manager.run(), name=f"cycle_manager-{settings.direction}")
    tasks = [
        asyncio.create_task(fill_router.run(), name=f"fill_router-{settings.direction}"),
        asyncio.create_task(entry_scheduler.run(), name=f"entry_scheduler-{settings.direction}"),
        asyncio.create_task(
            _price_watch_loop(market_data_adapter, execution_adapter, engine, settings),
            name=f"price_watch-{settings.direction}",
        ),
        cycle_task,
    ]

    try:
        # FIRST_EXCEPTION이 아니라 FIRST_COMPLETED다. 두 가지 이유:
        # (1) REBUILD_STACK 정책에서 cycle_manager가 "사이클 끝남"을 값으로 반환하는데,
        #     FIRST_EXCEPTION은 정상 종료한 태스크로는 깨지 않는다(ALL_COMPLETED로 degrade).
        # (2) 그 degrade 자체가 원래 잠재적 행(hang)이었다 — 네 태스크 중 하나가 예외
        #     없이 끝나면 FillRouter가 죽은 채 포지션을 든 상태로 영원히 대기하게 된다.
        #     이제는 예상 밖 정상 종료를 RuntimeError로 드러낸다.
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            # FIRST_COMPLETED에서는 취소된 태스크도 done에 들어온다. 그 상태로
            # task.exception()을 부르면 CancelledError가 엉뚱한 데서 튀어나온다.
            if task.cancelled():
                raise asyncio.CancelledError(f"{task.get_name()}가 외부에서 취소됨")
            exc = task.exception()
            if exc is not None:
                logger.critical("[%s] 태스크 %s가 예외로 종료됨 — 이 방향 봇 정지: %r", settings.direction, task.get_name(), exc)
                if isinstance(exc, EngineHaltedError):
                    # 재시작해도 다시 조용히 거래를 시작하지 않도록 플래그를 남긴다
                    # (engine/halt_flag.py — restart_recovery만으로는 halted와 정상
                    # COOLDOWN을 구분 못 함).
                    write_halt_flag(settings.halt_flag_path, str(exc))
                raise exc
        if cycle_task in done:
            return cycle_task.result()
        raise RuntimeError(
            "예외 없이 정상 종료된 태스크가 있음 — 원인을 추측하지 않고 이 방향 봇을 정지한다: "
            + ", ".join(t.get_name() for t in done)
        )
    finally:
        # 정상 종료든(위 raise) run() 자체가 외부에서 취소되든, 백그라운드 태스크가
        # 고아 상태로 계속 도는 일이 없도록 항상 여기서 정리한다.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _build_shared_rest_client(settings: Settings) -> Optional[OrangeXClient]:
    """방향/사이클을 넘나들며 공유하는 REST 클라이언트. live가 아니면 None
    (`build_execution_adapter`가 무시한다).

    `auth_grant_type` 명시가 핵심이다 — 기본값 `client_signature`는 이 프로젝트에서
    안정적으로 성공한 적이 없다(2026-08-06 실전 사고, engine/grid_setup.py 참고).
    클라이언트를 공유하는 이유는 계정 전체 레이트리밋(10 req/s)이 클라이언트별로
    독립 스로틀되면 합쳐서 한도를 넘길 수 있어서다."""
    if settings.trading_mode != "live":
        return None
    return OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )


async def _assert_cycle_closed_out(
    adapter: ExchangeAdapter, instrument: str, direction: str
) -> None:
    """다음 사이클을 (다른 방향일 수도 있는 채로) 시작하기 전에, 방금 끝난 방향이
    정말로 비었는지 **그 방향 어댑터로** 확인한다.

    새 사이클의 어댑터는 새 `position_side`로만 조회하므로(헤지 모드 필터링,
    `exchange/orangex/adapter.py`) 직전 방향에 남은 잔여 포지션을 원리적으로 볼 수
    없다 — 여기서 막지 않으면 의도치 않은 양방향 보유가 조용히 만들어진다."""
    position = await adapter.get_position(instrument)
    if position.qty != 0:
        raise StartupError(
            f"직전 사이클({direction}) 포지션이 flat이 아님(qty={position.qty}) — "
            "다음 사이클을 시작하지 않는다. 거래소에서 직접 확인해라."
        )
    leftovers = [
        o for o in await adapter.get_open_orders(instrument)
        if o.status in ("open", "partially_filled")
    ]
    if leftovers:
        raise StartupError(
            f"직전 사이클({direction})의 미체결 주문 {len(leftovers)}건이 남아있음 — "
            "다음 사이클을 시작하지 않는다."
        )


async def _resolve_direction_for_next_cycle(
    settings: Settings,
    contract_spec: ContractSpec,
    shared_client: Optional[OrangeXClient],
    binance_http_client: Optional[httpx.AsyncClient],
    probe_exchange: bool,
) -> Direction:
    """이번 사이클의 방향을 정한다. 거래소에 이어받을 상태가 있으면 그게 RSI보다 우선한다.

    `probe_exchange`는 프로세스 시작 직후(첫 사이클)에만 True다 — 그 이후의 사이클은
    `_assert_cycle_closed_out()`이 직전 방향이 완전히 비었음을 이미 확인했으므로
    프로브가 불필요하고, 매 사이클 WS 연결 2개를 새로 여는 낭비만 된다."""
    if probe_exchange and settings.trading_mode == "live":
        long_probe = build_execution_adapter(
            settings.model_copy(update={"direction": "long"}), contract_spec,
            shared_client=shared_client,
        )
        short_probe = build_execution_adapter(
            settings.model_copy(update={"direction": "short"}), contract_spec,
            shared_client=shared_client,
        )
        try:
            recovered = await detect_open_direction(long_probe, short_probe, settings.symbol)
        finally:
            await long_probe.aclose()
            await short_probe.aclose()
        if recovered is not None:
            logger.warning(
                "재시작 복구: 거래소에 %s 포지션/주문이 남아있어 15분봉 RSI 재판정을 "
                "건너뛰고 그 방향으로 이어받는다", recovered,
            )
            return recovered

    decision = await decide_direction_with_retry(settings.symbol, http_client=binance_http_client)
    logger.info(
        "15분봉 RSI(14)=%s -> 이번 사이클 방향: %s (임계값 50)", decision.rsi, decision.direction
    )
    return decision.direction


async def _run_auto_direction(
    settings: Settings,
    market_data_adapter: OrangeXAdapter,
    contract_spec: ContractSpec,
    binance_http_client: Optional[httpx.AsyncClient] = None,
    on_engine_ready: Optional[Callable[[GridEngine], None]] = None,
) -> None:
    """`DIRECTION=auto` — 사이클마다 15분봉 RSI(14)로 방향을 다시 정하고(>=50 숏, <50 롱),
    그 방향으로 실행 어댑터/엔진/백그라운드 태스크를 통째로 새로 조립해 한 사이클을 돌린다.

    방향을 제자리에서 바꾸지 않고 스택째 다시 만드는 이유: `OrangeXAdapter`의
    `position_side`가 생성 시점에 고정되고, 헤지 모드에서 이 값이 틀리면 주문이 접수
    직후 자동 취소된다(error 5998). 네 태스크가 도는 중에 락 없이 private 필드를 바꾸는
    건 이 프로젝트가 이미 여러 번 당한 유형의 사고다."""
    if settings.manual_mode:
        raise ValueError(
            'direction="auto"와 manual_mode=True는 같이 쓸 수 없다 — manual_mode에서는 '
            "TP/청산 자동화가 꺼져 있어 사이클이 COOLDOWN에 도달하는 경로 자체가 없고, "
            "그러면 방향을 다시 판정할 시점이 영원히 오지 않는다."
        )

    shared_client = _build_shared_rest_client(settings)
    # PaperAdapter는 direction을 아예 안 받으므로(방향 무관) 사이클 간 재사용한다 —
    # 매번 새로 만들면 가상 잔고/포지션이 초기화돼 다중 사이클 검증이 무의미해진다.
    reuse_adapter = settings.trading_mode != "live"
    execution_adapter: Optional[ExchangeAdapter] = None
    probe_exchange = True

    try:
        while True:
            direction = await _resolve_direction_for_next_cycle(
                settings, contract_spec, shared_client, binance_http_client, probe_exchange
            )
            probe_exchange = False
            cycle_settings = settings.model_copy(update={"direction": direction})

            if execution_adapter is None:
                execution_adapter = build_execution_adapter(
                    cycle_settings, contract_spec, shared_client=shared_client
                )

            outcome = await _run_single_direction(
                cycle_settings, market_data_adapter, contract_spec, execution_adapter,
                binance_http_client, on_engine_ready,
                entry_mode=EntryMode.IMMEDIATE,
                cycle_restart_policy=CycleRestartPolicy.REBUILD_STACK,
            )
            if outcome is not CycleOutcome.REBUILD_REQUESTED:
                return

            await _assert_cycle_closed_out(execution_adapter, cycle_settings.symbol, direction)
            if not reuse_adapter:
                await execution_adapter.aclose()
                execution_adapter = None
    finally:
        if execution_adapter is not None:
            await execution_adapter.aclose()
        if shared_client is not None:
            await shared_client.aclose()


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
    참조를 얻거나 나중에 헬스체크/모니터링을 붙일 자리로 쓸 수 있다.

    `settings.direction != "both"`면 `_run_single_direction()`을 그냥 한 번 호출한다
    (기존과 동일한 동작). `"both"`면 `execution_adapter`를 미리 주입할 수 없다 — 방향별로
    2개가 필요해서다(테스트는 대신 두 개를 만들어 `_run_single_direction()`을 직접
    호출하면 된다)."""
    if market_data_adapter is None:
        market_data_adapter = build_market_data_adapter(settings)
    contract_spec = await market_data_adapter.get_contract_spec(settings.symbol)

    if settings.direction == "auto":
        if execution_adapter is not None:
            raise ValueError(
                'direction="auto"에서는 execution_adapter를 미리 주입할 수 없음 — '
                "사이클마다 방향이 바뀌어 새로 만든다"
            )
        await _run_auto_direction(
            settings, market_data_adapter, contract_spec, binance_http_client, on_engine_ready
        )
        return

    if settings.direction != "both":
        await _run_single_direction(
            settings, market_data_adapter, contract_spec, execution_adapter, binance_http_client, on_engine_ready
        )
        return

    if execution_adapter is not None:
        raise ValueError('direction="both"에서는 execution_adapter를 미리 주입할 수 없음 — 방향별로 2개 필요')

    half_equity = settings.equity_usdt / 2
    long_settings = settings.model_copy(update={
        "direction": "long",
        "equity_usdt": half_equity,
        "halt_flag_path": _derive_halt_flag_path(settings.halt_flag_path, "long"),
    })
    short_settings = settings.model_copy(update={
        "direction": "short",
        "equity_usdt": half_equity,
        "halt_flag_path": _derive_halt_flag_path(settings.halt_flag_path, "short"),
    })

    # REST 클라이언트는 계정 전체 레이트리밋을 지키려고 공유(위 모듈 docstring 참고).
    # paper 모드에서는 애초에 REST 호출이 없으니 None으로 둬도 build_execution_adapter가
    # 그냥 무시한다.
    shared_client: Optional[OrangeXClient] = None
    if settings.trading_mode == "live":
        # auth_grant_type 명시 필요 — engine/grid_setup.py의 build_execution_adapter()
        # 모듈 docstring 참고(2026-08-06 실전 사고: 기본값 client_signature는 이 프로젝트
        # 에서 안정적으로 성공한 적이 없음).
        shared_client = OrangeXClient(
            client_id=settings.api_key.get_secret_value(),
            client_secret=settings.api_secret.get_secret_value(),
            auth_grant_type="client_credentials",
        )
    long_adapter = build_execution_adapter(long_settings, contract_spec, shared_client=shared_client)
    short_adapter = build_execution_adapter(short_settings, contract_spec, shared_client=shared_client)

    both_tasks = [
        asyncio.create_task(
            _run_single_direction(long_settings, market_data_adapter, contract_spec, long_adapter, binance_http_client, on_engine_ready),
            name="run-long",
        ),
        asyncio.create_task(
            _run_single_direction(short_settings, market_data_adapter, contract_spec, short_adapter, binance_http_client, on_engine_ready),
            name="run-short",
        ),
    ]
    try:
        done, _pending = await asyncio.wait(both_tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        for task in both_tasks:
            task.cancel()
        await asyncio.gather(*both_tasks, return_exceptions=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx가 API 호출마다 자동으로 찍는 "HTTP Request: ..." 로그는 사용자에게 의미 있는
    # 정보가 아니라 순수 잡음이라 숨긴다 — 대신 _price_watch_loop가 현재가/상태를 사람이
    # 읽기 쉬운 형태로 매 폴링마다 찍어준다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("사용자가 Ctrl+C로 종료를 요청함 — 프로그램을 종료합니다.")


if __name__ == "__main__":
    main()
