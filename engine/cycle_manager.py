"""SPEC 97줄 COOLDOWN 타이머 — 사이클 종료(전량 청산) 후 `cooldown_minutes` 동안
재진입을 금지했다가, 시간이 지나면 새 격자를 계산해 다음 사이클을 시작한다
(docs/phase3-plan.md "다중 사이클 지원").

`GridEngine`은 시세/설정에 접근권이 없어 스스로 "언제, 어떤 가격 기준으로" 다음
사이클을 시작할지 판단할 수 없다 — `reset_for_new_cycle(grid_rows)`를 호출해주는
쪽이 필요할 뿐이다. 이 모듈이 그 역할이다: COOLDOWN 진입을 감지하고, 설정된 시간만큼
기다린 뒤, `engine/grid_setup.py`의 `build_grid_rows()`로 현재가 기준 새 격자를 계산해
넘겨준다.

halted 상태에서 다음 사이클로 재진입하면 안 된다는 보장은 이 모듈이 직접 지키는 게
아니라 `GridEngine.reset_for_new_cycle()` 내부의 `_check_not_halted()`가 막아준다
(halted가 발생하는 경로들은 이미 대부분 다른 태스크(FillRouter/가격감시 루프)에서
예외를 던져 `main.py`의 전체 종료 로직을 이미 태우므로, 이 모듈이 별도로 halted를
감시할 필요는 없다).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config.settings import Settings
from engine.grid_engine import EngineState, GridEngine
from engine.grid_setup import build_grid_rows
from exchange.base import ContractSpec
from exchange.orangex.adapter import OrangeXAdapter

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 10


@dataclass
class CycleManager:
    engine: GridEngine
    market_data_adapter: OrangeXAdapter
    contract_spec: ContractSpec
    settings: Settings
    poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS
    binance_http_client: Optional[httpx.AsyncClient] = None

    async def run(self) -> None:
        """호출부가 태스크로 돌리다가 필요 시 취소(CancelledError)하는 방식을 전제로
        한다. 사이클을 무한히 반복한다: COOLDOWN 진입 대기 -> 대기 -> 새 사이클 시작."""
        while True:
            await self._wait_until_cooldown()
            logger.info(
                "COOLDOWN 진입 감지 — %d분 대기 후 다음 사이클 시작", self.settings.cooldown_minutes
            )
            await asyncio.sleep(self.settings.cooldown_minutes * 60)
            await self.start_next_cycle()

    async def _wait_until_cooldown(self) -> None:
        while self.engine.state != EngineState.COOLDOWN:
            await asyncio.sleep(self.poll_interval_seconds)

    async def start_next_cycle(self) -> None:
        """대기가 끝난 뒤 실제로 다음 사이클을 시작한다. `run()`과 분리해뒀기 때문에
        테스트나 수동 트리거에서 대기 없이 바로 호출할 수도 있다."""
        grid_rows = await build_grid_rows(
            self.settings, self.market_data_adapter, self.contract_spec, self.binance_http_client
        )
        self.engine.reset_for_new_cycle(grid_rows)
        logger.info("다음 사이클 시작: base_price=%s", grid_rows[0].entry_price)
