"""거래소 어댑터 추상 인터페이스 (SPEC.md Phase 2, 79~84줄).

`place_stop_order`(SL 등록)는 한때 스코프에서 제외됐었다 — OrangeX의 조건부 주문
메커니즘이 문서만으로 확정되지 않았기 때문(docs/api-notes.md §5). 2026-07-30,
Phase 3 설계 중 `condition_type=STOP`이 실제 거래소 등록형 SL로 동작함을 라이브로
검증해(docs/api-notes.md §6 항목18, docs/phase3-plan.md) 다시 포함시켰다. 트리거는
crossing-trigger(주문 이후 실제 가격이 trigger_price를 가로질러야 발동, 등록 시점에
조건이 이미 참이어도 발동하지 않음)로 확인됐다 — 구현체는 이 특성을 반영해야 한다.

모든 가격·수량·금액 필드는 Decimal이다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal, Optional

Side = Literal["buy", "sell"]
Direction = Literal["long", "short"]
OrderStatus = Literal["open", "partially_filled", "filled", "cancelled", "rejected"]


def round_qty_to_step(qty: Decimal, step: Decimal) -> Decimal:
    """qty를 거래소가 요구하는 수량 증가 단위(step, 예: BTC-USDT-PERPETUAL은 0.001)의
    배수로 내림한다. 2026-08-06 실전 사고로 발견 — `strategy.grid.compute_grid()`가
    만드는 수량은 나눗셈 결과라 소수점이 20자리 넘게 이어지는데, 거래소는 정해진
    정밀도 배수만 받아들여 그렇지 않으면 주문이 즉시 거부된다(ContractSpec.qty_step
    주석 참고).

    `step<=0`(정밀도 정보를 못 가져온 경우)이면 원본을 그대로 돌려준다 — 추측해서
    자르지 않고 호출하는 쪽이 최소 정밀도를 실제로 아는 경우에만 적용되게 한다.
    내림(ROUND_DOWN)을 쓰는 이유: 반올림(HALF_UP)으로 올리면 의도한 증거금/명목가치를
    넘어설 수 있어, 항상 의도한 값 이하로만 맞춘다."""
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class Balance:
    equity: Decimal
    available: Decimal


@dataclass(frozen=True)
class PortfolioPnl:
    """계좌 전체의 미실현손익과 투입 증거금 — 거래소가 직접 알려주는 값.

    로컬 계산(현재가 × 수량 - 평단 × 수량)과 달리 펀딩비·실제 체결가·실제 수수료가
    이미 반영돼 있다. **계좌 전체 합계**라 instrument별/봇별 구분이 없다는 점에 주의
    (2026-08-18 사용자 확인: 이 계좌는 이 봇 전용으로 쓰므로 문제되지 않음).
    """

    unrealized_pnl: Decimal
    initial_margin: Decimal


@dataclass(frozen=True)
class Position:
    instrument: str
    direction: Optional[Direction]  # None이면 무포지션(flat)
    qty: Decimal
    avg_price: Decimal


@dataclass(frozen=True)
class ContractSpec:
    instrument: str
    tick_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    contract_size: Decimal
    # 2026-08-06 실전 사고로 발견: OrangeX `/public/get_instruments`의 `min_trade_amount`
    # ("최소 거래 수량 스텝", docs/api-notes.md §4)는 `min_qty`("최소 거래 수량")와 별개
    # 필드다 — 주문 수량은 이 값의 배수여야 한다(BTC-USDT-PERPETUAL 기준 0.001). 기존
    # 코드 어디서도 이 필드를 반영하지 않아 compute_grid()가 만든 고정밀도 소수 수량
    # (예: 0.006760034349168474805147131123)을 그대로 주문에 넣었고, 실전에서 이 정밀도
    # 불일치로 주문이 즉시 거부됐다(quick_entry.py 실전 실행 2회 연속 실패, 원인 규명 후
    # 발견). 기본값 0은 "미확인/반영 안 함"을 뜻하며 기존 테스트 호출부와 하위호환된다
    # — 반올림이 필요한 곳(quick_entry.py 등)은 실제 조회한 값을 명시적으로 넘겨야 한다.
    qty_step: Decimal = Decimal("0")


@dataclass(frozen=True)
class OrderRequest:
    instrument: str
    side: Side
    price: Decimal
    qty: Decimal
    client_order_id: str
    reduce_only: bool = False
    post_only: bool = False


@dataclass(frozen=True)
class StopOrderRequest:
    """조건부(트리거형) 주문 — SL 등록용. trigger_price_type은 OrangeX 라이브 검증(§6 항목18)
    기준 "last"만 확인됐고 "mark"는 미검증이다."""

    instrument: str
    side: Side
    trigger_price: Decimal
    qty: Decimal
    client_order_id: str
    reduce_only: bool = True
    trigger_price_type: Literal["last", "mark"] = "last"


@dataclass(frozen=True)
class MarketOrderRequest:
    """시장가 청산용 (SPEC 3차+ hybrid reset — 평단 도달 시 50% 시장가 청산, 그리고
    강제청산/정지 경로). OrangeX 구현(buy/sell의 type="market")은 2026-07-30 사용자
    명시적 요청으로 라이브 검증 완료 — 0.001 BTC 진입/청산 둘 다 즉시 체결 확인됨
    (scripts/orangex_test_market_order.py, docs/api-notes.md §6)."""

    instrument: str
    side: Side
    qty: Decimal
    client_order_id: str
    reduce_only: bool = False


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    client_order_id: str
    status: OrderStatus
    filled_qty: Decimal
    avg_fill_price: Optional[Decimal]


@dataclass(frozen=True)
class Ticker:
    """격자 기준가(base_price) 조달용 현재가. OrangeX `/public/ticker`가 last_price 외에도
    mark_price/best_bid_price/best_ask_price 등을 주지만(docs/api-notes.md §6 항목15),
    지금까지 이 값을 쓰는 유일한 용도(compute_grid의 base_price)에는 last_price로 충분해
    당장은 그것만 담는다."""

    instrument: str
    last_price: Decimal


@dataclass(frozen=True)
class Fill:
    order_id: str
    client_order_id: str
    side: Side
    price: Decimal
    qty: Decimal
    fee: Decimal


class ExchangeAdapter(ABC):
    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_position(self, instrument: str) -> Position: ...

    @abstractmethod
    async def get_contract_spec(self, instrument: str) -> ContractSpec: ...

    @abstractmethod
    async def get_ticker(self, instrument: str) -> Ticker: ...

    async def get_portfolio_pnl(self) -> Optional[PortfolioPnl]:
        """계좌 전체의 미실현손익/투입 증거금을 거래소에서 직접 읽어온다.

        지원하지 않는 구현(예: `PaperAdapter`)은 `None`을 반환하고, 호출부는 그때
        로컬 계산으로 폴백한다 — abstract가 아닌 이유다."""
        return None

    async def aclose(self) -> None:
        """이 어댑터가 **직접 만들어 소유한** 자원을 정리한다. 기본은 no-op이라
        `PaperAdapter`처럼 정리할 게 없는 구현은 아무것도 안 해도 된다(abstract가
        아닌 이유). `direction="auto"`처럼 사이클마다 어댑터를 새로 만드는 경로에서
        이전 어댑터의 WS 연결이 새지 않도록 호출한다."""
        return None

    @abstractmethod
    async def set_leverage(self, instrument: str, leverage: Decimal) -> None: ...

    @abstractmethod
    async def place_limit_order(self, order: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def place_stop_order(self, order: StopOrderRequest) -> OrderResult: ...

    @abstractmethod
    async def place_market_order(self, order: MarketOrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    async def get_open_orders(self, instrument: str) -> list[OrderResult]: ...

    @abstractmethod
    def watch_fills(self, instrument: str) -> AsyncIterator[Fill]: ...
