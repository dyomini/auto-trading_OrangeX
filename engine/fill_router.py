"""watch_fills() 체결 스트림을 GridEngine 이벤트로 라우팅한다 (docs/phase3-plan.md
"아직 만들지 않은 것" 중 watch_fills 연동).

Fill.order_id를 GridEngine이 들고 있는 order_id(resting_grid_order_ids/tp_order_id/
sl_order_id)와 직접 비교해 라우팅한다 — client_order_id 문자열 prefix 파싱에 기대지
않는다(엔진 내부 명명 규칙이 바뀌어도 라우터는 영향받지 않음).

hybrid reset/강제청산 시장가 체결은 GridEngine이 place_market_order 결과를 이미
동기적으로 반영했으므로, 그 체결이 watch_fills로도 흘러들어와도 여기서는 무시한다
(어떤 추적 중인 order_id와도 매칭되지 않음).

부분체결은 다루지 않는다 — GridEngine.on_fill(index) 자체가 "해당 인덱스가 전량
체결됐다"만 가정하는 기존 설계라(체결 수량을 받지 않음) 이 라우터도 동일한 가정을
따른다. 부분체결이 실제로 발생하면 첫 Fill에서 곧바로 완전 체결로 취급된다.
"""
from __future__ import annotations

from dataclasses import dataclass

from engine.grid_engine import GridEngine
from exchange.base import ExchangeAdapter, Fill


@dataclass
class FillRouter:
    adapter: ExchangeAdapter
    engine: GridEngine
    instrument: str

    async def run(self) -> None:
        """watch_fills()를 무한히 소비하며 각 Fill을 route()로 넘긴다. 호출부가
        태스크로 돌리다가 필요 시 취소(CancelledError)하는 방식을 전제로 한다."""
        async for fill in self.adapter.watch_fills(self.instrument):
            await self.route(fill)

    async def route(self, fill: Fill) -> None:
        for idx, order_id in self.engine.resting_grid_order_ids.items():
            if order_id == fill.order_id:
                await self.engine.on_fill(idx)
                return

        if fill.order_id == self.engine.tp_order_id:
            await self.engine.on_tp_filled()
            return

        if fill.order_id == self.engine.sl_order_id:
            await self.engine.on_sl_filled()
            return
