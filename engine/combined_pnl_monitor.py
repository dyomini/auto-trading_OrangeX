"""`DIRECTION=both` — 롱/숏을 동시에 깔아두고, **합산 손익이 투입 증거금 대비 목표
수익률(기본 10%)에 도달하면 양쪽을 전량 청산**한 뒤 즉시 재진입한다
(2026-08-17 사용자 요청).

기존 `both`(롱/숏이 각자 TP로 독립적으로 끝나는 두 개의 봇)를 이 의미로 **교체**했다.
청산 판단 주체가 완전히 다르다 — 여기서는 개별 TP를 아예 걸지 않고, 이 모니터가
유일한 청산 권한을 가진다.

**손익 계산**: `exchange/base.py`의 `Position`에는 미실현손익 필드가 없고(qty/avg_price만
있음), 거래소 응답에 그런 필드가 있는지도 파싱해본 적이 없어서 추측해 쓰지 않는다
(SPEC 0번). 현재가와 엔진이 아는 평단/수량으로 직접 계산한다:

    pnl_side    = qty * (price - avg)      # 롱
                  qty * (avg - price)      # 숏
    margin_side = qty * avg / leverage
    roe = (pnl_long + pnl_short - 예상수수료) / (margin_long + margin_short)

수수료는 진입 maker + 청산 taker를 명목가 기준으로 차감한다. 40배에서 왕복 수수료는
증거금 대비 약 3.2%라 10% 목표에서 무시할 수 없다 — 차감하지 않으면 "10% 달성"이라고
청산했는데 실제로는 7%도 안 되는 상황이 된다.

**구조상 알아둘 것**: 롱 격자는 현재가 아래, 숏 격자는 위에 깔린다. 가격이 한쪽으로
가면 그쪽만 마틴게일로 물량이 불어나고 반대쪽은 앞 단계만 체결된 채 남는다 —
합산 +10%는 보통 되돌림이 나와야 달성된다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from config.settings import Settings
from engine.cycle_manager import CycleOutcome
from engine.grid_engine import GridEngine
from exchange.orangex.adapter import OrangeXAdapter
from strategy.liquidation import Direction

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 5


@dataclass(frozen=True)
class CombinedPnlSnapshot:
    pnl: Decimal            # 미실현 손익 합계 (수수료 차감 전)
    margin: Decimal         # 투입 증거금 합계
    fees: Decimal           # 진입 maker + 청산 taker 예상치
    roe: Decimal            # (pnl - fees) / margin, margin이 0이면 0

    @property
    def net_pnl(self) -> Decimal:
        return self.pnl - self.fees


@dataclass
class CombinedPnlMonitor:
    """양쪽 엔진의 합산 수익률을 폴링하다 목표 도달 시 전량 청산하고 재조립을 요청한다.

    `engines`는 **호출부와 공유하는 살아있는 dict**다 — 롱/숏 스택이 각자 비동기로
    조립되면서 `on_engine_ready` 훅으로 자신을 등록하기 때문에, 양쪽이 다 등록될
    때까지는 판정을 건너뛴다(한쪽만 보고 청산하면 안 된다)."""

    engines: dict[Direction, GridEngine]
    market_data_adapter: OrangeXAdapter
    settings: Settings
    target_roe: Decimal
    poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS
    last_snapshot: Optional[CombinedPnlSnapshot] = field(default=None)

    def compute(self, price: Decimal) -> CombinedPnlSnapshot:
        pnl = Decimal("0")
        margin = Decimal("0")
        fees = Decimal("0")
        for direction, engine in self.engines.items():
            qty = engine.open_qty
            if qty <= 0 or engine.filled_step_count == 0:
                continue
            avg = engine.grid_rows[engine.filled_step_count - 1].avg_price
            pnl += qty * (price - avg) if direction == "long" else qty * (avg - price)
            margin += qty * avg / self.settings.leverage
            # 진입은 전부 지정가(maker), 청산은 시장가(taker).
            fees += qty * avg * self.settings.maker_fee + qty * price * self.settings.taker_fee

        roe = (pnl - fees) / margin if margin > 0 else Decimal("0")
        return CombinedPnlSnapshot(pnl=pnl, margin=margin, fees=fees, roe=roe)

    async def close_both(self) -> None:
        for direction, engine in self.engines.items():
            closed = await engine.close_all_and_cooldown()
            logger.info("[%s] 합산 익절로 전량 청산: %s", direction, closed)

    async def run(self) -> CycleOutcome:
        """호출부가 태스크로 돌리다가 필요 시 취소하는 방식을 전제로 한다.
        목표 도달 시 양쪽을 청산하고 `REBUILD_REQUESTED`를 반환한다."""
        while True:
            if len(self.engines) < 2:
                # 양쪽 스택이 아직 다 조립되지 않았다 — 한쪽만 보고 판정하면 안 된다.
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            ticker = await self.market_data_adapter.get_ticker(self.settings.symbol)
            snapshot = self.compute(ticker.last_price)
            self.last_snapshot = snapshot

            if snapshot.margin > 0:
                logger.info(
                    "합산 손익: %s USDT (수수료 %s 차감 후 %s) / 투입 증거금 %s USDT -> ROE %s%% (목표 %s%%)",
                    snapshot.pnl.quantize(Decimal("0.01")),
                    snapshot.fees.quantize(Decimal("0.01")),
                    snapshot.net_pnl.quantize(Decimal("0.01")),
                    snapshot.margin.quantize(Decimal("0.01")),
                    (snapshot.roe * 100).quantize(Decimal("0.01")),
                    (self.target_roe * 100).quantize(Decimal("0.01")),
                )
                if snapshot.roe >= self.target_roe:
                    logger.info("합산 목표 수익률 도달 — 양방향 전량 청산 후 즉시 재진입한다")
                    await self.close_both()
                    return CycleOutcome.REBUILD_REQUESTED

            await asyncio.sleep(self.poll_interval_seconds)
