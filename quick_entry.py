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
from typing import Optional

from config.settings import Settings
from exchange.base import ContractSpec, Direction, ExchangeAdapter, OrderRequest, round_qty_to_step
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


def _rounded_qty_or_none(row: GridStepResult, contract_spec: ContractSpec) -> Optional[Decimal]:
    """row의 수량을 거래소 정밀도(qty_step)로 내림하고, 최소 주문 수량/명목가치를
    만족하면 그 값을, 아니면 None을 반환한다. run_quick_entry()의 실제 검증과
    compute_max_feasible_chunk_count()의 사전 판정이 항상 같은 기준을 쓰도록
    로직을 한 곳에 모아둔다."""
    qty = round_qty_to_step(row.step_qty, contract_spec.qty_step)
    if qty < contract_spec.min_qty or qty * row.entry_price < contract_spec.min_notional:
        return None
    return qty


async def compute_max_feasible_chunk_count(
    settings: Settings,
    direction: Direction,
    adapter: ExchangeAdapter,
    contract_spec: ContractSpec,
) -> int:
    """현재 설정(equity_usdt/leverage)과 실제 현재가 기준으로, 반올림 후에도 모든
    단계가 최소 주문 수량/명목가치를 만족하는 최대 청크 개수(0..TOTAL_STEPS)를
    계산한다. launcher.py가 실행 전 "최대 진입 범위" 안내에 쓴다(2026-08-06 사용자
    요청 — "지금 설정으로 최대 범위가 얼마인지 알려주는 안내도 추가해"). 0이면 지금
    설정으로는 1단계도 성공 못 함(자금/레버리지 자체가 min_qty 대비 너무 작음)."""
    ticker = await adapter.get_ticker(settings.symbol)
    weights_all = load_weights()
    last_ok = 0
    for n in range(1, TOTAL_STEPS + 1):
        rows = compute_grid(
            direction=direction,
            base_price=ticker.last_price,
            tick=settings.grid_tick,
            weights=weights_all[:n],
            equity=settings.equity_usdt,
            leverage=settings.leverage,
            maint_margin_rate=settings.maint_margin_rate,
            sl_pct=settings.sl_pct,
        )
        if any(_rounded_qty_or_none(row, contract_spec) is None for row in rows):
            break
        last_ok = n
    return last_ok


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
    contract_spec: ContractSpec,
) -> list[str]:
    """direction 방향으로 현재가부터 price_range_usdt만큼(grid_tick 간격) 지정가
    주문을 전부 즉시 걸고, 접수된 order_id 목록을 반환한다. 증거금은 weights.csv
    비중대로 settings.equity_usdt 전액을 배분한다.

    `contract_spec.qty_step`으로 수량을 거래소가 요구하는 정밀도로 내림한다 —
    2026-08-06 실전 사고로 발견: compute_grid()의 나눗셈 결과(소수 20자리 이상)를
    그대로 보내면 거래소가 정밀도 불일치로 주문을 즉시 거부한다(위 모듈 docstring
    ContractSpec.qty_step 참고)."""
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
        qty = _rounded_qty_or_none(row, contract_spec)
        if qty is None:
            raise QuickEntryError(
                f"{row.index + 1}/{num_chunks}번째 단계가 정밀도 반영 후 최소 주문 수량/명목가치에 "
                f"미달함(min_qty={contract_spec.min_qty}, min_notional={contract_spec.min_notional}) "
                "— 증거금이 너무 잘게 쪼개졌습니다. 가격 범위를 줄여 단계 수를 줄이거나 EQUITY_USDT/"
                "레버리지를 늘려보세요. "
                f"앞서 접수된 {len(order_ids)}개는 이미 걸려있을 수 있으니 거래소에서 확인하세요."
            )
        order = OrderRequest(
            instrument=settings.symbol,
            side=side,
            price=row.entry_price,
            qty=qty,
            client_order_id=f"quick-{direction}-{row.index}-{uuid.uuid4().hex[:8]}",
        )
        result = await adapter.place_limit_order(order)
        # 콘솔엔 안 보여주고(사용자 요청) 파일 로그에만 DEBUG로 order_id/전체 상태를
        # 남긴다 — 2026-08-06 실전 사고 때 화면 스크롤이 사라져서 어떤 order_id가
        # 어떤 상태로 응답했는지 재구성 못 했던 문제(사용자 요청으로 파일 로깅 추가)
        # 재발 방지용. get_order_state(order_id)로 사후에 직접 재조회할 때 필요하다.
        logger.debug(
            "[quick-entry %d/%d] place_limit_order 응답: order_id=%s client_order_id=%s "
            "status=%s filled_qty=%s avg_fill_price=%s (요청: side=%s price=%s qty=%s)",
            row.index + 1, num_chunks, result.order_id, result.client_order_id, result.status,
            result.filled_qty, result.avg_fill_price, side, row.entry_price, qty,
        )
        if result.status in ("cancelled", "rejected"):
            # 거래소가 접수 직후 곧바로 취소/거부한 경우(예: position_side 불일치로
            # 헤지 모드 계좌에서 자동 취소, error_code 5998) — order_id는 정상적으로
            # 발급됐으니 place_limit_order() 자체는 예외를 던지지 않는다. 여기서 확인
            # 안 하고 넘어가면 "접수 완료"로 잘못 보고하게 된다(2026-08-05 실전 사고로
            # 발견 — 실제로는 주문이 하나도 안 걸렸는데 성공했다고 표시됨).
            raise QuickEntryError(
                f"{row.index + 1}/{num_chunks}번째 주문이 거래소에서 즉시 {result.status} "
                f"처리됨(order_id={result.order_id}) — position_side/증거금 등을 확인하세요. "
                f"앞서 접수된 {len(order_ids)}개는 이미 걸려있을 수 있으니 거래소에서 확인하세요."
            )
        order_ids.append(result.order_id)
        logger.info(
            "[quick-entry %d/%d] %s %s @ %s USDT | 레버리지 %sx | 진입 마진 %s USDT",
            row.index + 1, num_chunks, side, qty, row.entry_price,
            settings.leverage, row.step_margin,
        )
    return order_ids
