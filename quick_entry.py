"""즉시 진입("숏!"/"롱!") 도구 — 2026-08-05 사용자 요청.

현재가부터 시작해 `settings.grid_tick`(기본 50 USDT) 간격으로, 사용자가 지정한
가격 범위(price_range_usdt, "3k"/"5k" 등 — 현재가 기준 ±얼마까지 밀고 들어갈지)
끝까지 지정가 매수/매도 주문을 한 번에 걸어놓는다. 주문 개수는
`price_range_usdt // grid_tick`으로 정해진다(예: 현재가 62,000일 때 range=3,000이면
65,000까지 50 USDT 간격으로 60개).

**주문 1개당 증거금은 균등하지 않다.** `config/weights.csv`(엑셀 E열 원본, 메인
격자 엔진과 동일 소스)의 비중대로 `settings.equity_usdt` 전액을 배분한다 — 2026-08-05
사용자 정정: "진입 마진은 항상 50usdt가 아니야. 엑셀에 기재된 비중대로 진입 마진
설계." 앞쪽 단계일수록 비중이 작고 뒤쪽 단계일수록 커지는 마틴게일 설계 그대로다
(`engine/grid_setup.py`의 `build_grid_rows()`가 `max_stage`로 앞쪽 N개만 잘라 쓸 때와
동일하게, 여기서도 사용할 num_chunks개만 잘라서 그 안에서 비중을 재정규화한다 —
즉 **한 번 즉시 진입을 실행할 때마다 그 방향에 배정된 equity_usdt 전액이 소진된다**,
가격 범위가 좁을수록(주문 개수가 적을수록) 단계당 증거금은 오히려 커진다).

**주의**: `price_range_usdt`는 증거금 총액이 아니라 가격 범위다 — 2026-08-05 사용자가
launcher.py 실사용 중 "3k/5k는 마진 금액이 아니라 현재가 기준 +-(롱/숏) 가격 범위"라고
정정함.

`engine/grid_engine.py`의 정식 격자 엔진과는 완전히 독립적이다 — RSI 진입 필터,
자동 TP 재등록, SL 등록, hybrid reset 전부 없음. 진입 주문만 걸고 끝나며, 청산은
`manual_mode`와 동일하게 사용자가 거래소에서 직접 관리한다.

수량/마진 등 재무 계산은 새로 만들지 않고 `strategy.grid.compute_grid()`를 그대로
재사용한다(골든 테스트가 지키는 검증된 계산과 동일 공식, 메인 격자 엔진과 100%
동일한 마진 배분 로직).
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from config.settings import Settings
from exchange.base import Direction, ExchangeAdapter, OrderRequest
from strategy.grid import TOTAL_STEPS, GridStepResult, compute_grid
from strategy.weights import load_weights

logger = logging.getLogger(__name__)


class QuickEntryError(Exception):
    """즉시 진입을 진행하면 안 되는 상황(청크 수 0 등)."""


def compute_chunk_count(settings: Settings, price_range_usdt: Decimal) -> int:
    """price_range_usdt(현재가 기준 ±가격 범위)를 grid_tick 간격으로 나눴을 때
    걸리는 주문 개수. launcher.py가 실행 전 미리보기(개수/증거금)에도 쓴다."""
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


def compute_preview_rows(settings: Settings, num_chunks: int) -> list[GridStepResult]:
    """실행 전 증거금 미리보기용. 마진(step_margin)은 weight/equity 비율로만 정해지고
    가격과 무관하므로, 실제 현재가 조회 없이(오프라인으로) 더미 base_price로
    compute_grid()를 호출해도 step_margin만큼은 실제 진입 시와 동일하다 — entry_price/
    step_qty/avg_price 등 가격에 의존하는 필드는 이 미리보기에서 의미 없다(실행 시
    run_quick_entry()가 실제 현재가로 다시 계산한다)."""
    weights = load_weights()[:num_chunks]
    return compute_grid(
        direction="long",  # 방향은 entry_price 부호만 바꿀 뿐 margin에는 영향 없음
        base_price=Decimal("1"),
        tick=settings.grid_tick,
        weights=weights,
        equity=settings.equity_usdt,
        leverage=settings.leverage,
        maint_margin_rate=settings.maint_margin_rate,
        sl_pct=settings.sl_pct,
    )


async def run_quick_entry(
    settings: Settings,
    direction: Direction,
    price_range_usdt: Decimal,
    adapter: ExchangeAdapter,
) -> list[str]:
    """direction 방향으로 현재가부터 price_range_usdt만큼(grid_tick 간격) 지정가
    주문을 전부 즉시 걸고, 접수된 order_id 목록을 반환한다. 증거금은 weights.csv
    비중대로 settings.equity_usdt 전액을 배분한다."""
    num_chunks = compute_chunk_count(settings, price_range_usdt)
    weights = load_weights()[:num_chunks]

    ticker = await adapter.get_ticker(settings.symbol)
    rows = compute_grid(
        direction=direction,
        base_price=ticker.last_price,
        tick=settings.grid_tick,
        weights=weights,
        equity=settings.equity_usdt,
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
            "[quick-entry %d/%d] %s %s @ %s USDT | 레버리지 %sx | 진입 마진 %s USDT",
            row.index + 1, num_chunks, side, row.step_qty, row.entry_price,
            settings.leverage, row.step_margin,
        )
    return order_ids
