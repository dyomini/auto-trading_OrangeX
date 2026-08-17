"""Phase 3 실행 엔진 — 격자 운용 상태 머신 (SPEC.md 86~100줄, docs/phase3-plan.md).

이 모듈은 PaperAdapter 기준으로 개발/테스트했다. 한때 OrangeXAdapter로의 라이브
전환을 막던 `get_open_orders()` 블로커(문서 메서드명 `get_open_order_by_instrument`가
"No service found"였던 문제)는 2026-07-30 실제 동작하는 이름
(`get_open_orders_by_instrument`, 복수형)을 찾아 해소했다(docs/api-notes.md §6 항목17,
`exchange/orangex/adapter.py`).

다루는 범위:
  - 상태 머신: IDLE -> SCOUTING -> LADDERING -> TP_PENDING -> CLOSING -> COOLDOWN
  - 롤링 격자 주문(앞쪽 max_open_grid_orders개만 유지)
  - 체결마다 평단/TP/SL 재계산 -> TP 취소 후 재등록(전 구간), SL 취소 후 재등록(4~5차만)
  - 3차+ 진입 후 평단 도달 시 50% 시장가 청산(hybrid reset) + 잔여 포지션 기준 TP/SL 재등록
  - SL 등록 실패 시 봇 정지(EngineHaltedError) — SPEC 규정

정확한 평단/청산가/TP/SL은 `strategy.grid.compute_grid()`가 이미 100단계 전부에
대해 "이 단계까지 순서대로 체결됐다면"을 가정하고 누적 계산해뒀으므로, 여기서는
그 결과를 인덱스로 조회하기만 한다 — 별도로 재계산하지 않는다.

watch_fills() 기반 실시간 체결 스트림과의 연동은 `engine/fill_router.py`(`FillRouter`)가
담당한다 — Fill.order_id를 이 엔진이 들고 있는 order_id와 매칭해 on_fill/on_tp_filled/
on_sl_filled로 라우팅한다. `OrangeXAdapter.watch_fills`도 연결/인증/구독/실제 체결
스키마까지 라이브로 검증 완료됨(docs/api-notes.md §6 항목19).

RSI 일봉 확인을 언제 수행할지의 스케줄링 루프는 `engine/entry_scheduler.py`
(`EntryScheduler`)가 담당한다 — IDLE -> SCOUTING 전이(`start_scouting()`)와 주기적
RSI 폴링 후 필터 통과 시 `start_laddering()` 호출까지 처리한다. ATR 급등 시 격자
간격 확대는 여전히 미구현(SPEC에 배율 공식이 없음, `engine/entry_filter.py` 참고).

COOLDOWN 이후 다음 사이클로 재진입하는 타이머는 `engine/cycle_manager.py`
(`CycleManager`)가 담당한다 — SPEC 97줄이 요구하는 COOLDOWN 대기(기본 30분)를 재고
`reset_for_new_cycle()`을 호출한다. 이 엔진 자체는 "몇 시에 재시작할지"를 모른다 —
그저 COOLDOWN 상태에서 `reset_for_new_cycle(새_grid_rows)`가 호출되면 초기화만 한다.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

from exchange.base import (
    ContractSpec,
    ExchangeAdapter,
    MarketOrderRequest,
    OrderRequest,
    StopOrderRequest,
    round_qty_to_step,
)
from strategy.grid import GridStepResult
from strategy.liquidation import Direction

logger = logging.getLogger(__name__)

HYBRID_RESET_MIN_TIER = 3
HYBRID_RESET_FRACTION = Decimal("0.5")


class OrderQtyTooSmallError(Exception):
    """거래소 수량 정밀도(qty_step)로 내린 결과가 최소 주문 수량/명목가치에 미달.
    애매한 주문을 내보내 거래소가 조용히 거부하게 두지 않고 여기서 명시적으로 막는다
    (SPEC 0번). 정상 운용에서는 `engine/grid_setup.py`의 `build_grid_rows()`가 기동
    시점에 이미 걸러내므로 도달하지 않아야 하는 방어선이다."""


def rounded_cum_qty(
    grid_rows: list[GridStepResult], count: int, qty_step: Decimal
) -> Decimal:
    """0..count-1 단계를 실제 주문 수량(내림 적용)으로 누적한 값.

    `grid_rows[i].cum_qty`는 미가공 수량의 누적이라 실제 포지션과 다르다 — 엔진이
    거래소에 보내는 건 항상 `round_qty_to_step()`으로 내린 값이기 때문이다.
    `engine/restart_recovery.py`가 거래소 실측 포지션과 대조할 때 반드시 이 함수를
    써야 엔진과 동일한 산술이 된다(양쪽이 어긋나면 재시작이 무조건 실패한다)."""
    total = Decimal("0")
    for row in grid_rows[:count]:
        total += round_qty_to_step(row.step_qty, qty_step)
    return total


class EngineState(Enum):
    IDLE = "IDLE"
    SCOUTING = "SCOUTING"
    LADDERING = "LADDERING"
    TP_PENDING = "TP_PENDING"
    CLOSING = "CLOSING"
    COOLDOWN = "COOLDOWN"


class EngineHaltedError(Exception):
    """SL 등록 실패 등 SPEC이 봇 강제 정지를 요구하는 상황에서 발생.
    발생 이후 엔진은 더 이상 어떤 주문도 내지 않는다 — 재개하려면 사용자가
    수동으로 상태를 확인하고 새 GridEngine을 만들어야 한다."""


@dataclass
class GridEngine:
    adapter: ExchangeAdapter
    instrument: str
    direction: Direction
    grid_rows: list[GridStepResult]
    max_open_grid_orders: int = 5
    # True면 진입(격자 매수/매도 체결)만 자동화하고 TP 재등록/SL 등록/hybrid reset은
    # 전부 건너뛴다 — 청산은 사용자가 거래소에서 직접 수동으로 관리한다는 전제
    # (config/settings.py의 manual_mode, 2026-08-04 사용자 요청).
    manual_mode: bool = False
    # 이 tier 이상 진입 시 거래소 SL 필수 등록(config/settings.py의 mandatory_sl_min_tier).
    # 기본값 4는 5-tier 풀 구조(SPEC 원안, "4~5차") 기준 — max_stage를 낮춰 쓰면
    # (예: 3-tier 압축 설계) 이 값도 같이 낮춰야 major_tier가 실제로 도달 가능한
    # 값이 된다(2026-08-04, 제까깟-마틴게이-3k.xlsx 검증 후 설정 가능하게 뺌).
    mandatory_sl_min_tier: int = 4
    # False면 거래소 SL(STOP 주문)을 아예 등록하지 않는다 — SPEC Phase 3의 "4~5차 SL 필수,
    # 등록 실패 시 전량 청산 후 정지"에서 의도적으로 벗어난 것이다(2026-08-17 사용자 결정
    # "sl은 안 걸어도돼", docs/phase3-plan.md에 이탈 사유 기록). mandatory_sl_min_tier를
    # 큰 값으로 우회하는 대신 명시적 플래그를 쓰는 이유: max_stage를 올리면(3k -> 5k
    # 프리셋) 우회값이 조용히 무력화될 수 있어서다.
    sl_enabled: bool = True
    # 거래소 수량 정밀도(qty_step)/최소 주문 조건 판정용. None이면 반올림하지 않고
    # 미가공 수량을 그대로 주문에 넣는다(기존 테스트 호출부와 하위호환) — 라이브
    # 운용에서는 반드시 실제 조회한 스펙을 넘겨야 한다(main.py가 배선한다).
    contract_spec: Optional[ContractSpec] = None

    state: EngineState = field(default=EngineState.IDLE)
    filled_step_count: int = 0
    resting_grid_order_ids: dict[int, str] = field(default_factory=dict)
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    halted: bool = False
    hybrid_reset_done: bool = False
    # 실제 보유 수량 — grid_rows[i].cum_qty는 "i번째까지 전부 정상 진입만 있었다면"의
    # 이론치라 hybrid reset처럼 도중에 줄어드는 이벤트를 반영하지 못한다. TP/SL/강제청산
    # 주문 수량은 항상 이 필드를 기준으로 한다 (가격 필드는 grid_rows 그대로 사용 —
    # 평단/청산가/TP/SL 가격은 진입 쪽 누적에서만 나오므로 reduce_only 청산과 무관하다).
    open_qty: Decimal = field(default=Decimal("0"))

    def _entry_side(self) -> str:
        return "buy" if self.direction == "long" else "sell"

    def _exit_side(self) -> str:
        """포지션을 줄이는 방향 — TP/SL/hybrid reset 전부 이 방향."""
        return "sell" if self.direction == "long" else "buy"

    def _current_row(self) -> GridStepResult:
        return self.grid_rows[self.filled_step_count - 1]

    def _check_not_halted(self) -> None:
        if self.halted:
            raise EngineHaltedError("엔진이 정지 상태 — 재시작 복구 절차 필요")

    def _order_qty(self, raw_qty: Decimal, price: Decimal, what: str) -> Decimal:
        """주문에 실제로 넣을 수량 — 거래소 수량 단위로 내리고 최소 조건을 검증한다.

        `contract_spec`이 없으면 원본을 그대로 돌려준다(하위호환). `price`는 명목가치
        판정용이며 시장가 청산에서는 마지막으로 아는 가격을 넘긴다."""
        if self.contract_spec is None:
            return raw_qty
        qty = round_qty_to_step(raw_qty, self.contract_spec.qty_step)
        if qty < self.contract_spec.min_qty or qty * price < self.contract_spec.min_notional:
            raise OrderQtyTooSmallError(
                f"{what}: 수량 {raw_qty}를 {self.contract_spec.qty_step} 단위로 내리면 {qty} — "
                f"최소 주문 수량({self.contract_spec.min_qty}) 또는 명목가치"
                f"({self.contract_spec.min_notional})에 미달한다"
            )
        return qty

    def _closing_qty(self, raw_qty: Decimal) -> Decimal:
        """청산 수량 — 내림만 하고 최소 조건 미달이어도 **예외를 던지지 않는다.**
        강제청산 경로는 이미 비상 상황이라 여기서 멈추면 포지션이 그대로 남는다.
        거부는 거래소가 하게 두되 반드시 로그를 남긴다(내림 때문에 dust가 남을 수 있음)."""
        if self.contract_spec is None:
            return raw_qty
        qty = round_qty_to_step(raw_qty, self.contract_spec.qty_step)
        if qty != raw_qty:
            logger.info("청산 수량 내림: %s -> %s (잔여 dust %s)", raw_qty, qty, raw_qty - qty)
        if qty < self.contract_spec.min_qty:
            logger.warning(
                "청산 수량 %s가 최소 주문 수량(%s) 미달 — 거래소가 거부할 수 있다. 수동 확인 필요",
                qty, self.contract_spec.min_qty,
            )
        return qty

    async def start_scouting(self) -> None:
        """IDLE -> SCOUTING 전이. 진입 필터(RSI 등) 통과를 기다리는 상태로만 들어가고
        아직 아무 주문도 걸지 않는다(engine/entry_scheduler.py가 호출)."""
        self._check_not_halted()
        self.state = EngineState.SCOUTING

    async def start_laddering(self) -> None:
        """SCOUTING의 진입 필터를 통과한 뒤 호출 — 최초 격자 주문들을 건다."""
        self._check_not_halted()
        self.state = EngineState.LADDERING
        await self._refresh_grid_orders()

    async def _refresh_grid_orders(self) -> None:
        if self.state != EngineState.LADDERING:
            return
        slots = self.max_open_grid_orders - len(self.resting_grid_order_ids)
        if slots <= 0:
            return
        next_indices = [
            i
            for i in range(self.filled_step_count, len(self.grid_rows))
            if i not in self.resting_grid_order_ids
        ][:slots]

        for idx in next_indices:
            row = self.grid_rows[idx]
            order = OrderRequest(
                instrument=self.instrument,
                side=self._entry_side(),
                price=row.entry_price,
                qty=self._order_qty(row.step_qty, row.entry_price, f"격자 진입 {idx}단계"),
                client_order_id=f"grid-{idx}-{uuid.uuid4().hex[:8]}",
            )
            result = await self.adapter.place_limit_order(order)
            self.resting_grid_order_ids[idx] = result.order_id

    async def on_fill(self, grid_index: int) -> None:
        """grid_index번 격자 단계 진입 주문이 체결됐을 때 호출한다. 어떤 인덱스가
        체결됐는지 식별하는 건 호출하는 쪽(watch_fills 소비 루프)의 책임이다."""
        self._check_not_halted()

        self.resting_grid_order_ids.pop(grid_index, None)
        self.filled_step_count = max(self.filled_step_count, grid_index + 1)
        row = self._current_row()
        # 실제로 주문에 넣었던(내림된) 수량으로 누적한다 — 미가공 step_qty로 더하면
        # 엔진이 아는 수량과 거래소 실제 포지션이 어긋나고, 그 오차가 TP/SL/청산 주문
        # 수량에 그대로 전파된다.
        self.open_qty += self._order_qty(row.step_qty, row.entry_price, f"격자 진입 {grid_index}단계")

        if not self.manual_mode:
            await self._reregister_tp(row)
            if self.sl_enabled and row.major_tier >= self.mandatory_sl_min_tier:
                await self._reregister_sl(row)

        if self.filled_step_count >= len(self.grid_rows):
            if not self.manual_mode:
                self.state = EngineState.TP_PENDING
            # manual_mode: 걸어둘 격자 주문이 더 없어도 LADDERING 상태를 유지한다 —
            # 이후 청산 판단/실행은 전부 사용자 몫이라 봇이 별도로 기다릴 상태가 없다.
        else:
            await self._refresh_grid_orders()

    async def _reregister_tp(self, row: GridStepResult) -> None:
        if self.tp_order_id is not None:
            try:
                await self.adapter.cancel_order(self.tp_order_id)
            except Exception as e:
                # 취소하려던 TP가 이미 체결/소멸된 상태라면(거래소가 우리보다 먼저 처리한
                # 경합) 남은 실제 포지션이 우리가 알고 있는 것과 다를 수 있다 — 추측해서
                # 새 TP를 걸지 않고, SL 등록 실패와 동일하게 즉시 강제청산 후 정지한다.
                await self._force_close_and_halt(row)
                raise EngineHaltedError(f"기존 TP 주문 취소 실패 — 안전을 위해 봇을 정지했다: {e!r}") from e
            self.tp_order_id = None
        order = OrderRequest(
            instrument=self.instrument,
            side=self._exit_side(),
            price=row.target_tp_price,
            qty=self._order_qty(self.open_qty, row.target_tp_price, f"TP 재등록 {row.index}단계"),
            client_order_id=f"tp-{row.index}-{uuid.uuid4().hex[:8]}",
            reduce_only=True,
        )
        result = await self.adapter.place_limit_order(order)
        self.tp_order_id = result.order_id

    async def _reregister_sl(self, row: GridStepResult) -> None:
        if self.sl_order_id is not None:
            try:
                await self.adapter.cancel_order(self.sl_order_id)
            except Exception as e:
                # _reregister_tp와 동일한 이유(위 주석 참고) — SL이 이미 트리거/소멸된
                # 상태에서 재등록을 시도하는 경합이면 추측하지 않고 강제청산+정지한다.
                await self._force_close_and_halt(row)
                raise EngineHaltedError(f"기존 SL 주문 취소 실패 — 안전을 위해 봇을 정지했다: {e!r}") from e
            self.sl_order_id = None
        order = StopOrderRequest(
            instrument=self.instrument,
            side=self._exit_side(),
            trigger_price=row.sl_price,
            qty=self._order_qty(self.open_qty, row.sl_price, f"SL 재등록 {row.index}단계"),
            client_order_id=f"sl-{row.index}-{uuid.uuid4().hex[:8]}",
            reduce_only=True,
        )
        try:
            result = await self.adapter.place_stop_order(order)
        except Exception as e:
            # SPEC: 4~5차 SL 등록 실패 시 즉시 전량 시장가 청산 + 봇 정지.
            await self._force_close_and_halt(row)
            raise EngineHaltedError(f"SL 등록 실패 — SPEC 규정에 따라 봇을 정지했다: {e!r}") from e
        self.sl_order_id = result.order_id

    async def _force_close_and_halt(self, row: GridStepResult) -> None:
        # halted/state를 시장가 청산 시도보다 먼저 확정한다 — place_market_order 자체가
        # 실패해도(예: 이미 flat이라 reduce_only 주문이 거부됨) "정지됨" 판정은 절대
        # 흔들리면 안 된다(_check_not_halted가 이후 모든 호출을 막는 유일한 안전장치).
        self.halted = True
        self.state = EngineState.CLOSING
        order = MarketOrderRequest(
            instrument=self.instrument,
            side=self._exit_side(),
            qty=self._closing_qty(self.open_qty),
            client_order_id=f"forceclose-{row.index}-{uuid.uuid4().hex[:8]}",
            reduce_only=True,
        )
        await self.adapter.place_market_order(order)
        self.open_qty = Decimal("0")

    async def maybe_hybrid_reset(self, current_price: Decimal) -> bool:
        """3차+ 진입 후 평단 도달 시 50% 시장가 청산. 이미 실행했으면 재실행하지 않는다."""
        self._check_not_halted()
        if self.manual_mode:
            return False
        if self.hybrid_reset_done or self.filled_step_count == 0:
            return False
        row = self._current_row()
        if row.major_tier < HYBRID_RESET_MIN_TIER:
            return False

        reached = current_price >= row.avg_price if self.direction == "long" else current_price <= row.avg_price
        if not reached:
            return False

        close_qty = self._closing_qty(self.open_qty * HYBRID_RESET_FRACTION)
        if self.contract_spec is not None and close_qty < self.contract_spec.min_qty:
            # hybrid reset은 선택적 리스크 완화라, 최소 주문 조건을 못 채우면 거부당할
            # 주문을 보내는 대신 이번엔 건너뛴다(다음 폴링에서 다시 판정된다).
            logger.warning("hybrid reset 청산 수량이 최소 주문 미달 — 이번 판정은 건너뜀")
            return False
        order = MarketOrderRequest(
            instrument=self.instrument,
            side=self._exit_side(),
            qty=close_qty,
            client_order_id=f"hybrid-{row.index}-{uuid.uuid4().hex[:8]}",
            reduce_only=True,
        )
        await self.adapter.place_market_order(order)
        self.hybrid_reset_done = True
        self.open_qty -= close_qty

        await self._reregister_tp(row)
        if self.sl_enabled and row.major_tier >= self.mandatory_sl_min_tier:
            await self._reregister_sl(row)
        return True

    async def on_tp_filled(self) -> None:
        """TP 주문이 체결돼 포지션이 전량 청산됐을 때 호출한다."""
        self.state = EngineState.CLOSING
        for order_id in list(self.resting_grid_order_ids.values()):
            await self.adapter.cancel_order(order_id)
        self.resting_grid_order_ids.clear()
        if self.sl_order_id is not None:
            await self.adapter.cancel_order(self.sl_order_id)
            self.sl_order_id = None
        self.tp_order_id = None
        self.open_qty = Decimal("0")
        self.state = EngineState.COOLDOWN

    async def on_sl_filled(self) -> None:
        """SL 스톱 주문이 트리거돼 포지션이 전량 청산됐을 때 호출한다(on_tp_filled와 대칭)."""
        self.state = EngineState.CLOSING
        for order_id in list(self.resting_grid_order_ids.values()):
            await self.adapter.cancel_order(order_id)
        self.resting_grid_order_ids.clear()
        if self.tp_order_id is not None:
            await self.adapter.cancel_order(self.tp_order_id)
            self.tp_order_id = None
        self.sl_order_id = None
        self.open_qty = Decimal("0")
        self.state = EngineState.COOLDOWN

    def reset_for_new_cycle(self, grid_rows: list[GridStepResult]) -> None:
        """COOLDOWN 대기가 끝난 뒤 새 사이클을 시작하기 위해 상태를 초기화한다
        (SPEC 97줄 — engine/cycle_manager.py가 COOLDOWN 타이머를 재고 나서 호출).

        `grid_rows`는 호출하는 쪽이 새로 계산해서 넘겨야 한다 — 이 엔진은 시세/설정에
        접근권이 없어(compute_grid의 base_price는 "지금" 현재가여야 함) 스스로 재계산할
        수 없다. COOLDOWN 상태에서만 호출 가능하고, halted 상태면 SPEC 규정대로 사람이
        직접 확인할 때까지 재개하지 않는다(_check_not_halted)."""
        self._check_not_halted()
        if self.state != EngineState.COOLDOWN:
            raise ValueError(f"COOLDOWN 상태에서만 새 사이클을 시작할 수 있음 (현재: {self.state})")

        self.grid_rows = grid_rows
        self.state = EngineState.IDLE
        self.filled_step_count = 0
        self.resting_grid_order_ids = {}
        self.tp_order_id = None
        self.sl_order_id = None
        self.hybrid_reset_done = False
        self.open_qty = Decimal("0")
