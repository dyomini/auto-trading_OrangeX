"""`DIRECTION=both` — 롱/숏을 동시에 깔아두고, **합산 손익이 투입 증거금 대비 목표
수익률(기본 10%)에 도달하면 양쪽을 전량 청산**한 뒤 즉시 재진입한다
(2026-08-17 사용자 요청).

기존 `both`(롱/숏이 각자 TP로 독립적으로 끝나는 두 개의 봇)를 이 의미로 **교체**했다.
청산 판단 주체가 완전히 다르다 — 여기서는 개별 TP를 아예 걸지 않고, 이 모니터가
유일한 청산 권한을 가진다.

**손익 계산 — 거래소 값을 우선한다** (2026-08-18 변경).
`get_assets_info`의 `total_upl`(미실현손익)과 `total_initial_margin_*`(투입 증거금)을
그대로 읽어 쓴다(`ExchangeAdapter.get_portfolio_pnl()`). 이 값에는 펀딩비·실제 체결가·
실제 수수료가 **이미 반영**돼 있어서, 로컬 추정보다 정확하다.

계좌 전체 합계라 instrument/방향 구분이 없다는 한계가 있지만, 이 계좌를 이 봇 전용으로
쓴다는 전제에서는 무의미한 트레이드오프다(2026-08-18 사용자 확인). 다만 `quick_entry`나
수동 매매로 연 포지션이 있으면 그 손익까지 섞이므로, 봇 실행 중에는 같은 계좌로
다른 포지션을 잡지 말아야 한다.

`PaperAdapter`는 이 값을 줄 수 없으므로(`get_portfolio_pnl()`이 None) **연습 모드에서는
로컬 계산으로 폴백**한다:

    pnl_side    = qty * (price - avg)      # 롱
                  qty * (avg - price)      # 숏
    margin_side = qty * avg / leverage
    roe = (pnl_long + pnl_short - 예상수수료) / (margin_long + margin_short)

로컬 계산의 수수료는 진입 maker + 청산 taker 추정치다. 40배에서 왕복 수수료는 증거금
대비 약 3.2%라 10% 목표에서 무시할 수 없다 — 차감하지 않으면 "10% 달성"이라고 청산했는데
실제로는 7%도 안 되는 상황이 된다.

라이브에서는 **두 값을 항상 나란히 로그에 남긴다** — 거래소 값이 처음으로 실제 포지션과
함께 관측되는 시점에 로컬 계산과 대조할 수 있어야 하기 때문이다(이 프로젝트가 비싸게
배운 "원본 필드까지 직접 대조" 원칙).

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
from exchange.base import PortfolioPnl
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

    # "exchange"면 거래소가 알려준 값, "local"이면 현재가로 직접 계산한 값.
    source: str = "local"

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

    async def _exchange_snapshot(self) -> Optional[CombinedPnlSnapshot]:
        """거래소가 직접 알려주는 미실현손익/증거금. 지원하지 않으면(paper) None.

        어느 엔진의 어댑터로 물어도 같은 계좌 전체 값이라 하나만 쓴다."""
        for engine in self.engines.values():
            portfolio: Optional[PortfolioPnl] = await engine.adapter.get_portfolio_pnl()
            if portfolio is None:
                return None
            margin = portfolio.initial_margin
            roe = portfolio.unrealized_pnl / margin if margin > 0 else Decimal("0")
            return CombinedPnlSnapshot(
                pnl=portfolio.unrealized_pnl,
                margin=margin,
                # 거래소 값에는 이미 수수료가 반영돼 있어 따로 뺄 게 없다.
                fees=Decimal("0"),
                roe=roe,
                source="exchange",
            )
        return None

    async def snapshot(self, price: Decimal) -> CombinedPnlSnapshot:
        """판정에 쓸 스냅샷 — 거래소 값이 있으면 그걸, 없으면 로컬 계산을 쓴다.
        라이브에서는 대조할 수 있도록 로컬 계산도 함께 로그에 남긴다."""
        local = self.compute(price)
        exchange = await self._exchange_snapshot()
        if exchange is None:
            return local
        logger.info(
            "손익 대조 | 거래소: %s USDT / 증거금 %s (ROE %s%%) | 로컬 추정: %s / %s (ROE %s%%)",
            exchange.pnl.quantize(Decimal("0.01")),
            exchange.margin.quantize(Decimal("0.01")),
            (exchange.roe * 100).quantize(Decimal("0.01")),
            local.net_pnl.quantize(Decimal("0.01")),
            local.margin.quantize(Decimal("0.01")),
            (local.roe * 100).quantize(Decimal("0.01")),
        )
        if local.margin > 0 and exchange.margin == 0:
            # 엔진은 포지션을 들고 있다는데 거래소는 증거금 0이라고 한다 — 주문 직후
            # 인덱싱 지연일 수도, 계좌가 어긋난 것일 수도 있다. 어느 쪽이든 이 값으로
            # 청산 판단을 내리면 안 되므로 이번 폴링은 로컬 값으로 넘긴다.
            logger.warning(
                "거래소가 보고한 투입 증거금이 0인데 엔진은 %s USDT를 들고 있다고 본다 — "
                "이번 판정은 로컬 계산으로 대체한다", local.margin.quantize(Decimal("0.01")),
            )
            return local
        return exchange

    async def run(self) -> CycleOutcome:
        """호출부가 태스크로 돌리다가 필요 시 취소하는 방식을 전제로 한다.
        목표 도달 시 양쪽을 청산하고 `REBUILD_REQUESTED`를 반환한다."""
        while True:
            if len(self.engines) < 2:
                # 양쪽 스택이 아직 다 조립되지 않았다 — 한쪽만 보고 판정하면 안 된다.
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            ticker = await self.market_data_adapter.get_ticker(self.settings.symbol)
            snap = await self.snapshot(ticker.last_price)
            self.last_snapshot = snap

            if snap.margin > 0:
                logger.info(
                    "합산 손익[%s]: %s USDT / 투입 증거금 %s USDT -> ROE %s%% (목표 %s%%)",
                    snap.source,
                    snap.net_pnl.quantize(Decimal("0.01")),
                    snap.margin.quantize(Decimal("0.01")),
                    (snap.roe * 100).quantize(Decimal("0.01")),
                    (self.target_roe * 100).quantize(Decimal("0.01")),
                )
                if snap.roe >= self.target_roe:
                    logger.info("합산 목표 수익률 도달 — 양방향 전량 청산 후 즉시 재진입한다")
                    await self.close_both()
                    return CycleOutcome.REBUILD_REQUESTED

            await asyncio.sleep(self.poll_interval_seconds)
