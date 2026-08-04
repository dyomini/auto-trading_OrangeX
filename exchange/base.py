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
from decimal import Decimal
from typing import Literal, Optional

Side = Literal["buy", "sell"]
Direction = Literal["long", "short"]
OrderStatus = Literal["open", "partially_filled", "filled", "cancelled", "rejected"]


@dataclass(frozen=True)
class Balance:
    equity: Decimal
    available: Decimal


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
