"""즉시 진입("숏!"/"롱!") 도구 — 2026-08-05 사용자 요청.

현재가부터 시작해 `settings.grid_tick`(기본 50 USDT) 간격으로, 사용자가 지정한
가격 범위(price_range_usdt, "3k"/"5k" 등 — 현재가 기준 ±얼마까지 밀고 들어갈지)
끝까지 지정가 매수/매도 주문을 한 번에 걸어놓는다. 주문 개수는
`price_range_usdt // grid_tick`으로 정해진다(예: 현재가 62,000일 때 range=3,000이면
65,000까지 50 USDT 간격으로 60개). 주문 1개당 증거금은 `settings.quick_entry_chunk_usdt`
(기본 50 USDT)로 가격 범위와 무관하게 고정이다.

**주의**: `price_range_usdt`는 증거금 총액이 아니라 가격 범위다 — 2026-08-05 사용자가
launcher.py 실사용 중 "3k/5k는 마진 금액이 아니라 현재가 기준 +-(롱/숏) 가격 범위"라고
정정함. 이전 구현은 이 값을 증거금 총액으로 오해해 total_usdt // quick_entry_chunk_usdt로
청크 개수를 정했었다(기본 설정에서는 grid_tick과 quick_entry_chunk_usdt가 둘 다 50이라
숫자가 우연히 같게 나와서 겉으로는 안 드러났었음).

`engine/grid_engine.py`의 정식 격자 엔진과는 완전히 독립적이다 — RSI 진입 필터,
자동 TP 재등록, SL 등록, hybrid reset 전부 없음. 진입 주문만 걸고 끝나며, 청산은
`manual_mode`와 동일하게 사용자가 거래소에서 직접 관리한다.

수량/청산가 등 재무 계산은 새로 만들지 않고 `strategy.grid.compute_grid()`를
그대로 재사용한다 — weights를 전부 1로 균등하게 줘서 청크마다 동일 증거금이
배정되게 한다(골든 테스트가 지키는 검증된 계산과 동일 공식).
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from config.settings import Settings
from exchange.base import Direction, ExchangeAdapter, OrderRequest
from strategy.grid import TOTAL_STEPS, compute_grid

logger = logging.getLogger(__name__)


class QuickEntryError(Exception):
    """즉시 진입을 진행하면 안 되는 상황(청크 수 0 등)."""


def compute_chunk_count(settings: Settings, price_range_usdt: Decimal) -> int:
    """price_range_usdt(현재가 기준 ±가격 범위)를 grid_tick 간격으로 나눴을 때
    걸리는 주문 개수. launcher.py가 실행 전 미리보기(개수/총 증거금)에도 쓴다."""
    tick = settings.grid_tick
    num_chunks = int(price_range_usdt // tick)
    if num_chunks < 1:
        raise QuickEntryError(
            f"가격 범위({price_range_usdt} USDT)가 격자 간격({tick} USDT)보다 작아 주문을 하나도 만들 수 없음"
        )
    if num_chunks > TOTAL_STEPS:
        # compute_grid()가 100단계(TOTAL_STEPS)까지만 받는다(strategy/grid.py) — 재사용하는
        # 이상 이 상한을 그대로 물려받는다. 추측해서 잘라내지 않고 사용자에게 명시적으로
        # 알려서 범위를 줄이거나 격자 간격을 키우게 한다.
        raise QuickEntryError(
            f"청크 개수({num_chunks}개)가 {TOTAL_STEPS}개를 넘음 — 가격 범위를 줄이거나 "
            f"격자 간격(GRID_TICK={tick} USDT)을 키워주세요"
        )
    return num_chunks


async def run_quick_entry(
    settings: Settings,
    direction: Direction,
    price_range_usdt: Decimal,
    adapter: ExchangeAdapter,
) -> list[str]:
    """direction 방향으로 현재가부터 price_range_usdt만큼(grid_tick 간격) 지정가
    주문을 전부 즉시 걸고, 접수된 order_id 목록을 반환한다. 주문 1개당 증거금은
    settings.quick_entry_chunk_usdt로 고정."""
    num_chunks = compute_chunk_count(settings, price_range_usdt)
    chunk_margin = settings.quick_entry_chunk_usdt
    total_equity = chunk_margin * num_chunks

    ticker = await adapter.get_ticker(settings.symbol)
    rows = compute_grid(
        direction=direction,
        base_price=ticker.last_price,
        tick=settings.grid_tick,
        weights=[Decimal("1")] * num_chunks,
        equity=total_equity,
        leverage=settings.leverage,
        maint_margin_rate=settings.maint_margin_rate,
        sl_pct=settings.sl_pct,
    )

    side = "buy" if direction == "long" else "sell"
    order_ids: list[str] = []
    for row in rows:
        order = OrderRequest(
            instrument=settings.symbol,
            side=side,
            price=row.entry_price,
            qty=row.step_qty,
            client_order_id=f"quick-{direction}-{row.index}-{uuid.uuid4().hex[:8]}",
        )
        result = await adapter.place_limit_order(order)
        order_ids.append(result.order_id)
        logger.info(
            "[quick-entry %d/%d] %s %s @ %s USDT | 레버리지 %sx | 진입 마진 %s USDT | order_id=%s",
            row.index + 1, num_chunks, side, row.step_qty, row.entry_price,
            settings.leverage, row.step_margin, result.order_id,
        )
    return order_ids
