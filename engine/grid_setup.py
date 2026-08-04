"""격자 계산/어댑터 조립 헬퍼 — `main.py`(최초 기동)와 `engine/cycle_manager.py`
(COOLDOWN 이후 다음 사이클 재계산)가 공유한다. 원래 `main.py`에만 있었으나
`CycleManager`도 동일한 로직(현재가 조회 -> compute_grid -> 실행가능성/최소주문
검증)이 필요해 순환 임포트 없이 재사용하려고 분리했다.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import httpx

from config.settings import Settings
from engine.entry_filter import compute_atr_tick_multiplier
from exchange.base import ContractSpec, ExchangeAdapter
from exchange.orangex.adapter import OrangeXAdapter
from exchange.orangex.client import OrangeXClient
from exchange.orangex.ws_client import OrangeXWsClient
from exchange.paper import PaperAdapter
from strategy.feasibility import find_max_feasible_step, find_min_order_shortfalls
from strategy.grid import STEPS_PER_TIER, GridStepResult, compute_grid
from strategy.indicators import compute_atr
from strategy.market_data import closed_candles, fetch_daily_candles
from strategy.weights import load_weights

logger = logging.getLogger(__name__)

_ATR_PERIOD = 14


class StartupError(Exception):
    """봇을 시작하면(또는 다음 사이클을 시작하면) 안 되는 상황(격자 계산 불일치,
    재시작 상태 불일치 등)에서 발생."""


def build_market_data_adapter(settings: Settings) -> OrangeXAdapter:
    """주문 실행과 무관하게 현재가/계약스펙 조회 전용 — `/public/*`만 호출하므로
    API 키가 비어 있어도(예: paper 모드에서 키를 안 넣은 경우) 동작한다
    (docs/api-notes.md §6 항목15, OrangeXClient.call(authed=False)는 토큰을 요구하지 않음)."""
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
    )
    return OrangeXAdapter(client)


def build_execution_adapter(settings: Settings, contract_spec: ContractSpec) -> ExchangeAdapter:
    if settings.trading_mode == "live":
        client = OrangeXClient(
            client_id=settings.api_key.get_secret_value(),
            client_secret=settings.api_secret.get_secret_value(),
        )
        # watch_fills()에 필요 — REST용 OrangeXClient와 별도 연결(exchange/orangex/
        # ws_client.py). 연결/인증/구독 자체는 라이브 확인됨(docs/api-notes.md §6 항목19)
        # 이지만 실제 체결 메시지 스키마(특히 fee)는 아직 미검증 — OrangeXAdapter 참고.
        ws_client = OrangeXWsClient(
            client_id=settings.api_key.get_secret_value(),
            client_secret=settings.api_secret.get_secret_value(),
        )
        return OrangeXAdapter(client, position_side=settings.direction, ws_client=ws_client)
    return PaperAdapter(
        instrument=settings.symbol,
        contract_spec=contract_spec,
        initial_equity=settings.equity_usdt,
        leverage=settings.leverage,
        maker_fee=settings.maker_fee,
        taker_fee=settings.taker_fee,
    )


async def _compute_atr_tick_multiplier(
    settings: Settings, http_client: Optional[httpx.AsyncClient] = None
) -> Decimal:
    """ATR 급등 시 격자 간격(tick)을 넓힐 배율을 계산한다 — 판정 기준/배율 자체는
    `engine/entry_filter.py`의 기본값을 쓴다(SPEC에 값이 없어 이 구현이 정한 것,
    사용자 요청으로 직접 결정). 완결 일봉이 부족하면 배율 1(확대 없음)을 반환한다."""
    candles = await fetch_daily_candles(settings.symbol, limit=_ATR_PERIOD + 4, http_client=http_client)
    closed = closed_candles(candles)
    if len(closed) < _ATR_PERIOD + 2:  # 오늘/어제 ATR 창을 각각 계산하려면 +2 필요
        logger.info(
            "ATR 급등 판정에 필요한 완결 일봉이 부족함(%d개) — 격자 간격 확대 안 함",
            len(closed),
        )
        return Decimal("1")

    atr_today = compute_atr(closed[-(_ATR_PERIOD + 1):], period=_ATR_PERIOD)
    atr_yesterday = compute_atr(closed[-(_ATR_PERIOD + 2):-1], period=_ATR_PERIOD)
    multiplier = compute_atr_tick_multiplier(atr_today, atr_yesterday)
    if multiplier != Decimal("1"):
        logger.warning(
            "ATR 급등 감지(오늘 %s / 어제 %s) — 격자 간격을 %s배로 확대",
            atr_today, atr_yesterday, multiplier,
        )
    return multiplier


async def build_grid_rows(
    settings: Settings,
    market_data_adapter: OrangeXAdapter,
    contract_spec: ContractSpec,
    binance_http_client: Optional[httpx.AsyncClient] = None,
) -> list[GridStepResult]:
    """실시간 현재가를 base_price로 잡아 100단계 격자를 계산하고, SPEC이 요구하는
    사용자 지정 max_stage 절삭(110번) + 실행가능성 절삭(66번) + 최소 주문 미달
    검증(71번) + ATR 급등 시 격자 간격 확대(90번)까지 적용한다. 사이클마다(최초 기동
    시에도, `CycleManager`가 COOLDOWN 이후 재호출할 때도) 매번 새로 호출해야 한다 —
    그 사이 가격/변동성이 움직였으므로 base_price와 tick을 다시 잡아야 하기 때문이다."""
    ticker = await market_data_adapter.get_ticker(settings.symbol)
    weights = load_weights()
    tick_multiplier = await _compute_atr_tick_multiplier(settings, binance_http_client)
    rows = compute_grid(
        direction=settings.direction,
        base_price=ticker.last_price,
        tick=settings.grid_tick * tick_multiplier,
        weights=weights,
        equity=settings.equity_usdt,
        leverage=settings.leverage,
        maint_margin_rate=settings.maint_margin_rate,
        sl_pct=settings.sl_pct,
    )

    max_stage_step_count = settings.max_stage * STEPS_PER_TIER
    if len(rows) > max_stage_step_count:
        logger.info(
            "사용자가 설정한 max_stage=%d 단계로 격자 절삭(%d단계 → %d단계, SPEC 최대 단계 제한 "
            "— max_feasible_step과 별개로 사용자가 정한 상한)",
            settings.max_stage, len(rows), max_stage_step_count,
        )
        rows = rows[:max_stage_step_count]

    feasibility = find_max_feasible_step(rows)
    if not feasibility.all_feasible:
        logger.warning(
            "가용잔고가 음수로 전환되는 단계 발견 — %d단계까지만 사용(SPEC 66번 규정, "
            "최초 미달 단계 index=%s)",
            feasibility.max_feasible_step_count, feasibility.first_infeasible_index,
        )
        rows = rows[: feasibility.max_feasible_step_count]

    shortfalls = find_min_order_shortfalls(
        rows, min_qty=contract_spec.min_qty, min_notional=contract_spec.min_notional
    )
    if shortfalls:
        raise StartupError(
            f"{len(shortfalls)}개 단계가 최소 주문 수량/명목가치 미달인데 병합 로직이 "
            "아직 구현되지 않았음(docs/phase1-report.md 결정: 다음 단계에 합산 — 정책만 "
            f"확정, 구현은 미완). 첫 미달 단계: index={shortfalls[0].index}, "
            f"step_qty={shortfalls[0].step_qty}, notional={shortfalls[0].notional}"
        )
    return rows
