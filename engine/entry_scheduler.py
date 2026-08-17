"""SPEC.md 90줄 진입 필터(RSI ≤30/≥70)를 실제로 "언제" 확인할지의 스케줄링 루프
(docs/phase3-plan.md "아직 만들지 않은 것" 중 RSI/ATR 폴링 스케줄링).

`engine/entry_filter.py`는 RSI 값 하나를 받아 통과 여부만 판단하는 순수 함수라
"언제 새 일봉을 가져와 확인할지"는 다루지 않는다 — 이 모듈이 그 부분을 채운다.

일봉 RSI는 완결된(마감된) 일봉 기준이어야 하루 중에 신호가 계속 뒤집히지 않는다.
바이낸스 klines는 아직 마감 안 된 당일(진행 중) 봉을 마지막 행으로 함께 반환하므로,
그 마지막 봉의 시가 시각(open_time) + 1일이 아직 현재 UTC 시각을 지나지 않았다면
버리고 그 이전까지만 RSI 계산에 쓴다.

폴링 주기는 기본 1시간이다 — 일봉 자체는 UTC 자정에 한 번만 바뀌므로 더 잦은 폴링은
낭비지만, 프로세스 재시작이나 자정 근처 지연에도 하루 안에는 최신값을 따라잡도록
안전 마진을 둔 값이다(SPEC에 구체적인 폴링 주기가 없어 임의로 정한 기본값 — 필요하면
호출부에서 조정 가능).
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from enum import Enum
from typing import Optional

import httpx

from engine.entry_filter import passes_rsi_filter
from engine.grid_engine import EngineState, GridEngine
from strategy.indicators import compute_rsi
from strategy.market_data import closed_candles, fetch_daily_candles

logger = logging.getLogger(__name__)

_RSI_PERIOD = 14
_DEFAULT_POLL_INTERVAL_SECONDS = 3600


class EntryMode(Enum):
    """SCOUTING에서 LADDERING으로 넘어가는 조건.

    2026-08-17에 `manual_mode`에서 분리했다. 예전엔 "RSI를 건너뛴다"를 `manual_mode`로
    표현했는데, `manual_mode`는 `GridEngine`에서 **TP 재등록과 hybrid reset까지** 끄는
    플래그다. `DIRECTION=auto`는 RSI 게이트는 필요 없지만 TP는 반드시 켜야 해서
    (방향 결정 자체가 이미 RSI로 끝났으므로 기다릴 것이 없다) 두 개념을 갈랐다.
    """

    RSI_GATED = "rsi_gated"   # SPEC 90줄: 일봉 RSI ≤30/≥70을 통과할 때까지 SCOUTING 유지
    IMMEDIATE = "immediate"   # 게이트 없이 IDLE -> SCOUTING -> LADDERING 즉시 진행


class EntryScheduler:
    """IDLE 상태의 GridEngine을 SCOUTING으로 전이시키고, SCOUTING인 동안 주기적으로
    완결된 일봉 RSI를 확인해 진입 필터를 통과하면 engine.start_laddering()을 호출한다."""

    def __init__(
        self,
        engine: GridEngine,
        instrument: str,
        direction: str,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
        http_client: Optional[httpx.AsyncClient] = None,
        entry_mode: EntryMode = EntryMode.RSI_GATED,
    ) -> None:
        self.engine = engine
        self.instrument = instrument
        self.direction = direction
        self.poll_interval_seconds = poll_interval_seconds
        self._http_client = http_client
        self.entry_mode = entry_mode
        self.last_rsi: Optional[Decimal] = None

    async def run(self) -> None:
        """호출부가 태스크로 돌리다가 필요 시 취소(CancelledError)하는 방식을 전제로 한다.

        IDLE 체크를 루프 밖에서 한 번만 하지 않고 매 반복마다 한다 — `engine/
        cycle_manager.py`가 COOLDOWN 이후 `reset_for_new_cycle()`로 상태를 다시 IDLE로
        되돌리는 다중 사이클 동작을 지원하려면, 이 스케줄러가 그 재진입도 매번 잡아내야
        한다(2026-07-30, 다중 사이클 지원 작업 중 발견— 원래는 최초 1회만 체크해서
        두 번째 사이클부터는 절대 다시 SCOUTING에 들어가지 못하는 버그였다)."""
        while True:
            if self.engine.state == EngineState.IDLE:
                await self.engine.start_scouting()
            if self.engine.state == EngineState.SCOUTING:
                if self.entry_mode is EntryMode.IMMEDIATE:
                    logger.info("진입 게이트 없음(EntryMode.IMMEDIATE) — 즉시 LADDERING 시작")
                    await self.engine.start_laddering()
                else:
                    await self.check_once()
            await asyncio.sleep(self.poll_interval_seconds)

    async def check_once(self) -> bool:
        """RSI를 한 번 확인하고 필터를 통과하면 start_laddering()을 호출한다. run()과
        분리해뒀기 때문에 수동으로 한 번만 확인하고 싶을 때도 직접 호출할 수 있다."""
        candles = await fetch_daily_candles(
            self.instrument, limit=_RSI_PERIOD + 2, http_client=self._http_client
        )
        closed = closed_candles(candles)
        if len(closed) < _RSI_PERIOD + 1:
            logger.warning(
                "RSI(%d) 계산에 필요한 완결 일봉이 부족함(%d개) — 이번 폴링은 건너뜀",
                _RSI_PERIOD, len(closed),
            )
            return False

        rsi = compute_rsi([c.close for c in closed], period=_RSI_PERIOD)
        self.last_rsi = rsi

        if not passes_rsi_filter(self.direction, rsi):
            logger.info(
                "RSI(14)=%s — %s 진입 조건(%s) 미충족, 계속 대기",
                rsi, self.direction, "≤30" if self.direction == "long" else "≥70",
            )
            return False

        logger.info("RSI(14)=%s — 진입 조건 충족, LADDERING 시작", rsi)
        await self.engine.start_laddering()
        return True
