"""즉시 진입("숏!"/"롱!") 도구 — 2026-08-05 사용자 요청.

현재가부터 시작해 `settings.quick_entry_chunk_usdt`(기본 50 USDT 증거금)씩 나눠
지정가 매수/매도 주문 여러 개를 한 번에 걸어놓는다. 가격 간격은 메인 봇의 격자와
동일하게 `settings.grid_tick`을 그대로 쓴다.

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


async def run_quick_entry(
    settings: Settings,
    direction: Direction,
    total_usdt: Decimal,
    adapter: ExchangeAdapter,
) -> list[str]:
    """direction 방향으로 total_usdt를 quick_entry_chunk_usdt 단위로 나눠 지정가
    주문을 전부 즉시 걸고, 접수된 order_id 목록을 반환한다."""
    chunk = settings.quick_entry_chunk_usdt
    num_chunks = int(total_usdt // chunk)
    if num_chunks < 1:
        raise QuickEntryError(
            f"총 금액({total_usdt} USDT)이 청크 크기({chunk} USDT)보다 작아 주문을 하나도 만들 수 없음"
        )
    if num_chunks > TOTAL_STEPS:
        # compute_grid()가 100단계(TOTAL_STEPS)까지만 받는다(strategy/grid.py) — 재사용하는
        # 이상 이 상한을 그대로 물려받는다. 추측해서 잘라내지 않고 사용자에게 명시적으로
        # 알려서 총액을 줄이거나 청크를 키우게 한다.
        raise QuickEntryError(
            f"청크 개수({num_chunks}개)가 {TOTAL_STEPS}개를 넘음 — 총 금액을 줄이거나 "
            f"청크 크기({chunk} USDT)를 키워주세요"
        )
    actual_equity = chunk * num_chunks
    if actual_equity != total_usdt:
        logger.info(
            "총 금액 %s USDT를 %s USDT 청크 %d개로 나누면 %s USDT만 실제로 배정됨(나머지는 버림)",
            total_usdt, chunk, num_chunks, actual_equity,
        )

    ticker = await adapter.get_ticker(settings.symbol)
    rows = compute_grid(
        direction=direction,
        base_price=ticker.last_price,
        tick=settings.grid_tick,
        weights=[Decimal("1")] * num_chunks,
        equity=actual_equity,
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
            "[quick-entry %d/%d] side=%s price=%s qty=%s order_id=%s",
            row.index + 1, num_chunks, side, row.entry_price, row.step_qty, result.order_id,
        )
    return order_ids
