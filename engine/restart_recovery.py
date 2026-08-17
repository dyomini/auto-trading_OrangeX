"""SPEC.md 99~100줄 재시작 복구 (docs/phase3-plan.md "아직 만들지 않은 것").

봇을 재시작하면 로컬 GridEngine 상태를 신뢰하지 않고, 거래소의 실제 포지션·미체결
주문으로부터 상태를 재구성한다. 조금이라도 앞뒤가 안 맞으면 절대 추측해서 진행하지
않고 `RestartRecoveryError`로 명시적으로 막는다 — SPEC 100줄 "불일치 시 자동 진행하지
말고 정지 후 알림" 원칙 그대로다.

재시작 직후에는 엔진 메모리가 아예 없어서(`engine/fill_router.py`처럼 order_id를
엔진이 들고 있는 값과 비교하는 방식 자체가 불가능) 어떤 미체결 주문이 격자 진입/TP/SL
중 무엇인지 구분할 다른 수가 없다. 그래서 여기서는 `GridEngine`이 실제로 붙이는
client_order_id의 prefix 명명 규칙("grid-{index}-...", "tp-{index}-...",
"sl-{index}-...")에 의존한다 — `grid_engine.py`의 client_order_id 생성부와 이름이
바뀌면 이 모듈도 같이 바꿔야 한다.

**알려진 한계**: `GridEngine.halted`(SL 등록 실패로 강제청산+정지된 상태)는 거래소
상태만으로는 이 모듈 혼자서는 절대 구분할 수 없다 — 정상적으로 사이클을 끝내고
COOLDOWN에 들어간 것과 SL 등록 실패로 강제청산된 것 둘 다 "포지션 flat, 미체결 주문
없음"으로 똑같이 보인다. 그래서 이 모듈은 그런 경우를 전부 `IDLE`(재스카우팅 허용)로
복구한다 — 다만 이제는 `main.py`가 이 모듈을 호출하기 **전에** `engine/halt_flag.py`로
"직전 실행이 halted였는지" 자체를 먼저 걸러내므로(2026-07-30), 시스템 전체로 보면
halted 상태가 재시작으로 조용히 풀리는 문제는 해소돼 있다.

`get_open_orders()`는 2026-07-30 실제 동작하는 메서드명
(`/private/get_open_orders_by_instrument`, 복수형 — 문서/기존 코드가 쓰던 단수형
`get_open_order_by_instrument`는 "No service found"였음)을 찾아 라이브 블로커가
풀렸다(docs/api-notes.md §6 항목17, `exchange/orangex/adapter.py`). 다만 이 모듈
자체(`reconstruct_state`/`build_recovered_engine`)를 OrangeXAdapter로 실제 라이브
기동해서 끝까지 검증한 적은 아직 없다 — PaperAdapter 기준으로만 테스트했다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from engine.grid_engine import (
    HYBRID_RESET_FRACTION,
    HYBRID_RESET_MIN_TIER,
    EngineState,
    GridEngine,
    rounded_cum_qty,
)
from exchange.base import ContractSpec, ExchangeAdapter, round_qty_to_step
from strategy.grid import GridStepResult
from strategy.liquidation import Direction

_KNOWN_KINDS = {"grid", "tp", "sl", "hybrid", "forceclose"}


class RestartRecoveryError(Exception):
    """거래소 상태와 로컬 격자 설계가 앞뒤가 안 맞아 자동 복구를 포기해야 할 때
    발생한다(SPEC 100줄 "불일치 시 자동 진행하지 말고 정지 후 알림"). 호출부는 이걸
    잡아서 봇을 시작하지 말고 사람에게 알려야 한다."""


@dataclass(frozen=True)
class RecoveredState:
    state: EngineState
    filled_step_count: int
    open_qty: Decimal
    resting_grid_order_ids: dict[int, str] = field(default_factory=dict)
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    hybrid_reset_done: bool = False


def _parse_client_order_id(client_order_id: str, manual_mode: bool = False) -> Optional[tuple[str, Optional[int]]]:
    """알려진 kind가 아니면 기본적으로 막는다(SPEC 100번 원칙) — 다만 manual_mode에서는
    사용자가 거래소에 직접 건 TP/SL 주문이 섞여 있는 게 정상이라, 그런 주문은 엔진이
    만든 게 아니라고 보고 `None`을 반환해 조용히 건너뛴다."""
    parts = client_order_id.split("-")
    if len(parts) < 2 or parts[0] not in _KNOWN_KINDS:
        if manual_mode:
            return None
        raise RestartRecoveryError(f"인식할 수 없는 client_order_id 형식: {client_order_id!r}")
    kind = parts[0]
    try:
        index = int(parts[1])
    except ValueError as e:
        raise RestartRecoveryError(f"client_order_id의 인덱스를 정수로 파싱 못 함: {client_order_id!r}") from e
    return kind, index


async def reconstruct_state(
    adapter: ExchangeAdapter,
    instrument: str,
    grid_rows: list[GridStepResult],
    direction: Direction,
    manual_mode: bool = False,
    mandatory_sl_min_tier: int = 4,
    sl_enabled: bool = True,
    contract_spec: Optional[ContractSpec] = None,
) -> RecoveredState:
    position = await adapter.get_position(instrument)
    open_orders = await adapter.get_open_orders(instrument)

    if position.qty != 0 and position.direction != direction:
        raise RestartRecoveryError(
            f"거래소 포지션 방향({position.direction})이 설정된 방향({direction!r})과 다름"
        )

    resting_grid_order_ids: dict[int, str] = {}
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None

    for order in open_orders:
        if order.status not in ("open", "partially_filled"):
            continue
        parsed = _parse_client_order_id(order.client_order_id, manual_mode=manual_mode)
        if parsed is None:
            continue  # manual_mode에서 사용자가 직접 건 주문 — 엔진 추적 대상 아님
        kind, index = parsed

        if kind == "grid":
            if index is None or not (0 <= index < len(grid_rows)):
                raise RestartRecoveryError(f"격자 범위를 벗어난 grid 주문: {order.client_order_id!r}")
            if index in resting_grid_order_ids:
                raise RestartRecoveryError(f"같은 grid_index({index})에 미체결 주문이 2개 이상 존재")
            resting_grid_order_ids[index] = order.order_id
        elif kind == "tp":
            if tp_order_id is not None:
                raise RestartRecoveryError("미체결 TP 주문이 2개 이상 존재")
            tp_order_id = order.order_id
        elif kind == "sl":
            if sl_order_id is not None:
                raise RestartRecoveryError("미체결 SL 주문이 2개 이상 존재")
            sl_order_id = order.order_id
        else:
            # hybrid/forceclose는 시장가 주문이라 즉시 체결되어야 정상 — 미체결로
            # 남아있는 것 자체가 이례적이라 추측하지 않고 막는다.
            raise RestartRecoveryError(
                f"시장가 주문({kind})이 미체결 상태로 남아있음 — 불일치: {order.client_order_id!r}"
            )

    if position.qty == 0:
        if tp_order_id is not None or sl_order_id is not None:
            raise RestartRecoveryError(
                "포지션은 없는데 미체결 TP/SL 주문이 남아있음 — 정리 안 된 상태로 추정"
            )
        if not resting_grid_order_ids:
            return RecoveredState(state=EngineState.IDLE, filled_step_count=0, open_qty=Decimal("0"))

        # 2026-08-04 코드 리뷰로 발견한 버그의 수정: start_laddering() 직후 첫 체결이
        # 나기 전(포지션 flat, 진입 지정가 주문만 미체결로 남음)은 완전히 정상인 LADDERING
        # 상태인데, 예전엔 이것도 무조건 에러로 막아서 봇이 이 타이밍에 재시작되면
        # 복구가 아예 불가능했다. index 0부터 연속인지만 확인하고(0 fills니 시작점은
        # 항상 0이어야 함) 정상 LADDERING으로 복구한다.
        indices = sorted(resting_grid_order_ids)
        expected = list(range(indices[-1] + 1))
        if indices != expected:
            raise RestartRecoveryError(
                f"체결이 하나도 없는데 격자 주문 인덱스가 0부터 연속이 아님: {indices}"
            )
        return RecoveredState(
            state=EngineState.LADDERING,
            filled_step_count=0,
            open_qty=Decimal("0"),
            resting_grid_order_ids=resting_grid_order_ids,
        )

    if tp_order_id is None and not manual_mode:
        raise RestartRecoveryError("포지션은 있는데 미체결 TP 주문이 없음 — 불일치(SPEC상 체결마다 TP 재등록됨)")

    if resting_grid_order_ids:
        indices = sorted(resting_grid_order_ids)
        filled_step_count = indices[0]
        expected = list(range(indices[0], indices[-1] + 1))
        if indices != expected:
            raise RestartRecoveryError(f"미체결 격자 주문 인덱스가 연속적이지 않음: {indices}")
        state = EngineState.LADDERING
    else:
        filled_step_count = len(grid_rows)
        # manual_mode는 TP를 아예 안 걸므로 TP_PENDING 개념이 없다 — LADDERING 그대로 둔다
        # (engine/grid_engine.py의 on_fill()이 manual_mode에서 같은 판단을 한다).
        state = EngineState.LADDERING if manual_mode else EngineState.TP_PENDING

    row = grid_rows[filled_step_count - 1]

    if manual_mode:
        # 사용자가 거래소에서 직접 부분청산/TP/SL을 걸어둘 수 있어 실제 수량이 이론치
        # (row.cum_qty)와 안 맞는 게 정상이다 — 추측 없이 실측 포지션 수량을 그대로
        # 신뢰한다. hybrid_reset/SL 자동화 자체가 꺼져 있어 아래 값은 쓰이지 않는다.
        hybrid_reset_done = False
    else:
        # sl_enabled=False면 엔진이 애초에 SL을 걸지 않으므로 존재/부재 검증 자체가
        # 성립하지 않는다(2026-08-17 사용자 결정). GridEngine.sl_enabled와 반드시
        # 같은 값을 받아야 한다 — 어긋나면 정상 상태를 오류로 막는다.
        if sl_enabled:
            sl_required = row.major_tier >= mandatory_sl_min_tier
            if sl_required and sl_order_id is None:
                raise RestartRecoveryError(
                    f"tier {row.major_tier}(>= {mandatory_sl_min_tier})인데 미체결 SL 주문이 없음 — SPEC상 필수"
                )
            if not sl_required and sl_order_id is not None:
                raise RestartRecoveryError(f"tier {row.major_tier}인데 SL 주문이 존재함 — 예상 밖 상태")

        # 엔진은 항상 qty_step으로 내린 수량을 주문에 넣으므로, 실제 포지션은 미가공
        # row.cum_qty가 아니라 "내림된 step_qty들의 합"이다. GridEngine과 반드시 동일한
        # 산술을 써야 한다(rounded_cum_qty / _closing_qty가 하는 것과 같은 순서).
        qty_step = contract_spec.qty_step if contract_spec is not None else Decimal("0")
        expected_qty = rounded_cum_qty(grid_rows, filled_step_count, qty_step)
        # hybrid reset은 그 시점의 보유 수량 절반을 다시 내림해서 청산한다 —
        # 잔량은 (전체 - 내림된 절반)이지 전체의 정확한 절반이 아니다.
        expected_after_hybrid = expected_qty - round_qty_to_step(
            expected_qty * HYBRID_RESET_FRACTION, qty_step
        )

        if row.major_tier < HYBRID_RESET_MIN_TIER:
            if position.qty != expected_qty:
                raise RestartRecoveryError(
                    f"tier {row.major_tier}에서 hybrid reset이 있을 수 없는데 포지션 수량({position.qty})이 "
                    f"이론치({expected_qty})와 다름"
                )
            hybrid_reset_done = False
        elif position.qty == expected_qty:
            hybrid_reset_done = False
        elif position.qty == expected_after_hybrid:
            hybrid_reset_done = True
        else:
            raise RestartRecoveryError(
                f"포지션 수량({position.qty})이 이론치({expected_qty}) 또는 hybrid reset 이후 수량"
                f"({expected_after_hybrid}) 어느 쪽과도 안 맞음"
            )

    return RecoveredState(
        state=state,
        filled_step_count=filled_step_count,
        open_qty=position.qty,
        resting_grid_order_ids=resting_grid_order_ids,
        tp_order_id=tp_order_id,
        sl_order_id=sl_order_id,
        hybrid_reset_done=hybrid_reset_done,
    )


async def build_recovered_engine(
    adapter: ExchangeAdapter,
    instrument: str,
    direction: Direction,
    grid_rows: list[GridStepResult],
    max_open_grid_orders: int = 5,
    manual_mode: bool = False,
    mandatory_sl_min_tier: int = 4,
    sl_enabled: bool = True,
    contract_spec: Optional[ContractSpec] = None,
) -> GridEngine:
    """`reconstruct_state()`로 재구성한 값을 실제로 사용 가능한 `GridEngine`에 채워 넣는다."""
    recovered = await reconstruct_state(
        adapter, instrument, grid_rows, direction,
        manual_mode=manual_mode, mandatory_sl_min_tier=mandatory_sl_min_tier,
        sl_enabled=sl_enabled, contract_spec=contract_spec,
    )
    engine = GridEngine(
        adapter=adapter,
        instrument=instrument,
        direction=direction,
        grid_rows=grid_rows,
        max_open_grid_orders=max_open_grid_orders,
        manual_mode=manual_mode,
        mandatory_sl_min_tier=mandatory_sl_min_tier,
        sl_enabled=sl_enabled,
        contract_spec=contract_spec,
    )
    engine.state = recovered.state
    engine.filled_step_count = recovered.filled_step_count
    engine.open_qty = recovered.open_qty
    engine.resting_grid_order_ids = dict(recovered.resting_grid_order_ids)
    engine.tp_order_id = recovered.tp_order_id
    engine.sl_order_id = recovered.sl_order_id
    engine.hybrid_reset_done = recovered.hybrid_reset_done
    return engine
