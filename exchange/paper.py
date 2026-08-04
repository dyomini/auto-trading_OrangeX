"""인메모리 체결 시뮬레이터 (SPEC.md 82줄).

수수료는 전부 메이커 요율로 가정한다 — 격자 진입/TP 주문은 전부 미리 걸어둔
지정가(taker가 아니라 maker) 체결이라는 전략 설계상의 가정이며, docs/phase1-report.md
§3에서도 동일하게 명시했다. 슬리피지는 이번 스코프에서는 0으로 둔다(지정가 주문은
지정한 가격에 체결된다고 가정) — 필요해지면 생성자에 슬리피지 bps를 추가하면 된다.

`place_stop_order`(SL)는 OrangeX 라이브 검증(docs/api-notes.md §6 항목18)으로 확인된
crossing-trigger 특성을 재현한다: 주문 시점에 이미 트리거 조건이 참이어도 발동하지
않고, 이후 실제 가격 틱이 조건을 (거짓→참으로) "가로지를" 때만 체결된다. 트리거
체결은 시장가 체결에 해당하므로 taker 수수료를 적용한다.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from exchange.base import (
    Balance,
    ContractSpec,
    Direction,
    ExchangeAdapter,
    Fill,
    MarketOrderRequest,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    StopOrderRequest,
    Ticker,
)


class NoKnownPriceError(Exception):
    """시장가 주문 체결에 필요한 현재가를 아직 모를 때(on_price_tick이 한 번도 호출 안 됨)."""


class DuplicateClientOrderId(Exception):
    pass


class OrderNotFoundError(Exception):
    pass


@dataclass
class _InternalOrder:
    order_id: str
    request: OrderRequest
    status: OrderStatus = "open"
    filled_qty: Decimal = Decimal("0")
    filled_notional: Decimal = Decimal("0")

    @property
    def avg_fill_price(self) -> Optional[Decimal]:
        if self.filled_qty == 0:
            return None
        return self.filled_notional / self.filled_qty

    def to_result(self) -> OrderResult:
        return OrderResult(
            order_id=self.order_id,
            client_order_id=self.request.client_order_id,
            status=self.status,
            filled_qty=self.filled_qty,
            avg_fill_price=self.avg_fill_price,
        )


@dataclass
class _InternalStopOrder:
    order_id: str
    request: StopOrderRequest
    reference_price: Optional[Decimal]  # 마지막으로 비교한 가격 (crossing 판정 기준선)
    status: OrderStatus = "open"
    filled_qty: Decimal = Decimal("0")
    filled_notional: Decimal = Decimal("0")

    @property
    def avg_fill_price(self) -> Optional[Decimal]:
        if self.filled_qty == 0:
            return None
        return self.filled_notional / self.filled_qty

    def to_result(self) -> OrderResult:
        return OrderResult(
            order_id=self.order_id,
            client_order_id=self.request.client_order_id,
            status=self.status,
            filled_qty=self.filled_qty,
            avg_fill_price=self.avg_fill_price,
        )


def _stop_condition_met(side: str, trigger_price: Decimal, price: Decimal) -> bool:
    return price <= trigger_price if side == "sell" else price >= trigger_price


class PaperAdapter(ExchangeAdapter):
    def __init__(
        self,
        instrument: str,
        contract_spec: ContractSpec,
        initial_equity: Decimal,
        leverage: Decimal,
        maker_fee: Decimal,
        taker_fee: Decimal,
    ) -> None:
        self.instrument = instrument
        self.contract_spec = contract_spec
        self.leverage = leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee

        self._equity = initial_equity
        self._position_qty = Decimal("0")
        self._position_avg_price = Decimal("0")
        self._position_direction: Optional[Direction] = None

        self._open_orders: dict[str, _InternalOrder] = {}
        self._stop_orders: dict[str, _InternalStopOrder] = {}
        self._client_order_ids: set[str] = set()
        self._fill_queue: asyncio.Queue[Fill] = asyncio.Queue()
        self._last_price: Optional[Decimal] = None

    async def get_balance(self) -> Balance:
        used_margin = self._position_qty * self._position_avg_price / self.leverage
        return Balance(equity=self._equity, available=self._equity - used_margin)

    async def get_position(self, instrument: str) -> Position:
        return Position(
            instrument=instrument,
            direction=self._position_direction,
            qty=self._position_qty,
            avg_price=self._position_avg_price,
        )

    async def get_contract_spec(self, instrument: str) -> ContractSpec:
        return self.contract_spec

    async def get_ticker(self, instrument: str) -> Ticker:
        # PaperAdapter는 자체적으로 가격을 만들어내지 않는다 — on_price_tick()으로
        # 마지막에 주입된 값을 그대로 돌려줄 뿐이다(실제 시세 소스는 호출부가 결정).
        if self._last_price is None:
            raise NoKnownPriceError("on_price_tick이 한 번도 호출되지 않아 현재가를 모름")
        return Ticker(instrument=instrument, last_price=self._last_price)

    async def set_leverage(self, instrument: str, leverage: Decimal) -> None:
        self.leverage = leverage

    async def place_limit_order(self, order: OrderRequest) -> OrderResult:
        if order.client_order_id in self._client_order_ids:
            raise DuplicateClientOrderId(order.client_order_id)

        internal = _InternalOrder(order_id=str(uuid.uuid4()), request=order)
        self._client_order_ids.add(order.client_order_id)
        self._open_orders[internal.order_id] = internal
        return internal.to_result()

    async def place_stop_order(self, order: StopOrderRequest) -> OrderResult:
        if order.client_order_id in self._client_order_ids:
            raise DuplicateClientOrderId(order.client_order_id)

        # reference_price=현재 마지막 관측가. crossing-trigger 재현을 위해 등록 시점에
        # 조건이 이미 참이어도 절대 즉시 체결시키지 않는다 (아래 on_price_tick 참고).
        internal = _InternalStopOrder(
            order_id=str(uuid.uuid4()), request=order, reference_price=self._last_price
        )
        self._client_order_ids.add(order.client_order_id)
        self._stop_orders[internal.order_id] = internal
        return internal.to_result()

    async def place_market_order(self, order: MarketOrderRequest) -> OrderResult:
        if self._last_price is None:
            raise NoKnownPriceError("on_price_tick이 한 번도 호출되지 않아 현재가를 모름")

        fee = order.qty * self._last_price * self.taker_fee
        self._update_position(order.side, self._last_price, order.qty, fee)
        order_id = str(uuid.uuid4())
        fill = Fill(
            order_id=order_id,
            client_order_id=order.client_order_id,
            side=order.side,
            price=self._last_price,
            qty=order.qty,
            fee=fee,
        )
        self._fill_queue.put_nowait(fill)
        return OrderResult(
            order_id=order_id,
            client_order_id=order.client_order_id,
            status="filled",
            filled_qty=order.qty,
            avg_fill_price=self._last_price,
        )

    async def cancel_order(self, order_id: str) -> None:
        if order_id in self._open_orders:
            del self._open_orders[order_id]
            return
        if order_id in self._stop_orders:
            del self._stop_orders[order_id]
            return
        raise OrderNotFoundError(order_id)

    async def get_open_orders(self, instrument: str) -> list[OrderResult]:
        limit_orders = [
            o.to_result()
            for o in self._open_orders.values()
            if o.request.instrument == instrument and o.status in ("open", "partially_filled")
        ]
        stop_orders = [
            o.to_result()
            for o in self._stop_orders.values()
            if o.request.instrument == instrument and o.status in ("open", "partially_filled")
        ]
        return limit_orders + stop_orders

    def watch_fills(self, instrument: str) -> AsyncIterator[Fill]:
        return self._watch_fills_gen()

    async def _watch_fills_gen(self) -> AsyncIterator[Fill]:
        while True:
            yield await self._fill_queue.get()

    async def on_price_tick(self, price: Decimal) -> list[Fill]:
        self._last_price = price
        fills: list[Fill] = []
        for internal in list(self._open_orders.values()):
            crossed = (
                internal.request.side == "buy"
                and price <= internal.request.price
            ) or (
                internal.request.side == "sell"
                and price >= internal.request.price
            )
            if not crossed:
                continue
            remaining = internal.request.qty - internal.filled_qty
            fill = self._apply_fill(internal, remaining, internal.request.price, self.maker_fee, self._open_orders)
            fills.append(fill)

        for stop_internal in list(self._stop_orders.values()):
            ref = stop_internal.reference_price
            if ref is None:
                # 등록 이후 첫 틱은 기준선만 세우고 트리거 판정은 하지 않는다 —
                # crossing 여부를 비교할 "이전 가격"이 아직 없기 때문.
                stop_internal.reference_price = price
                continue
            side = stop_internal.request.side
            trigger_price = stop_internal.request.trigger_price
            was_met = _stop_condition_met(side, trigger_price, ref)
            now_met = _stop_condition_met(side, trigger_price, price)
            if not was_met and now_met:
                remaining = stop_internal.request.qty - stop_internal.filled_qty
                fill = self._apply_fill(stop_internal, remaining, trigger_price, self.taker_fee, self._stop_orders)
                fills.append(fill)
            else:
                stop_internal.reference_price = price
        return fills

    async def fill_order(self, order_id: str, qty: Decimal, price: Decimal) -> OrderResult:
        internal = self._open_orders.get(order_id)
        if internal is None:
            raise OrderNotFoundError(order_id)
        self._apply_fill(internal, qty, price, self.maker_fee, self._open_orders)
        return internal.to_result()

    def _apply_fill(
        self,
        internal: _InternalOrder | _InternalStopOrder,
        qty: Decimal,
        price: Decimal,
        fee_rate: Decimal,
        orders: dict,
    ) -> Fill:
        fee = qty * price * fee_rate

        internal.filled_qty += qty
        internal.filled_notional += qty * price
        if internal.filled_qty >= internal.request.qty:
            internal.status = "filled"
            del orders[internal.order_id]
        else:
            internal.status = "partially_filled"

        self._update_position(internal.request.side, price, qty, fee)

        fill = Fill(
            order_id=internal.order_id,
            client_order_id=internal.request.client_order_id,
            side=internal.request.side,
            price=price,
            qty=qty,
            fee=fee,
        )
        self._fill_queue.put_nowait(fill)
        return fill

    def _update_position(self, side: str, price: Decimal, qty: Decimal, fee: Decimal) -> None:
        self._equity -= fee

        opening_direction: Direction = "long" if side == "buy" else "short"

        if self._position_direction is None or self._position_qty == 0:
            self._position_direction = opening_direction
            self._position_qty = qty
            self._position_avg_price = price
            return

        if self._position_direction == opening_direction:
            new_qty = self._position_qty + qty
            self._position_avg_price = (
                self._position_avg_price * self._position_qty + price * qty
            ) / new_qty
            self._position_qty = new_qty
            return

        # 반대 방향 체결 -> 청산(및 필요 시 반전)
        close_qty = min(qty, self._position_qty)
        direction_sign = Decimal("1") if self._position_direction == "long" else Decimal("-1")
        realized = close_qty * (price - self._position_avg_price) * direction_sign
        self._equity += realized
        self._position_qty -= close_qty

        remaining = qty - close_qty
        if self._position_qty == 0:
            if remaining > 0:
                self._position_direction = opening_direction
                self._position_qty = remaining
                self._position_avg_price = price
            else:
                self._position_direction = None
                self._position_avg_price = Decimal("0")
