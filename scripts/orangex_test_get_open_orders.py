"""get_open_orders() 대체 구현(get_order_history_by_instrument) 검증 (사용자 명시적 요청, 2026-07-30).

scripts/orangex_find_open_orders_endpoint.py에서 "open 주문만 주는 전용 엔드포인트"는
9개 후보 전부 실패했고, 유일하게 성공한 /private/get_order_history_by_instrument는
전체 이력을 주는 것으로 보여 실제 미체결(open) 주문도 포함하는지 검증되지 않았다.

절차: 최소 수량 주문을 시장가에서 충분히 먼 가격으로 걸어 즉시체결을 피하고,
get_order_history_by_instrument 조회 결과에 order_state=open으로 잡히는지 확인한 뒤
/private/cancel(2026-07-30 검증됨, docs/api-notes.md §6 항목15)로 즉시 정리한다.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import ROUND_HALF_UP, Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"
TARGET_MARGIN = Decimal("1")
PRICE_OFFSET_PCT = Decimal("0.05")
LEVERAGE = Decimal("50")  # 2026-07-30 확인된 계좌 실제 설정값


def round_to_step(raw_qty: Decimal, step: Decimal) -> Decimal:
    steps = (raw_qty / step).to_integral_value(rounding=ROUND_HALF_UP)
    return steps * step


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    order_id: str | None = None
    try:
        ticker = await client.call("/public/ticker", {"instrument_name": INSTRUMENT}, authed=False)
        last_price = Decimal(str(ticker["last_price"]))

        instr_result = await client.call(
            "/public/get_instruments", {"instrument_name": INSTRUMENT}, authed=False
        )
        instruments = instr_result if isinstance(instr_result, list) else instr_result.get("instruments", [instr_result])
        spec = next(i for i in instruments if i.get("instrument_name") == INSTRUMENT)
        min_qty = Decimal(str(spec["min_qty"]))
        min_notional = Decimal(str(spec["min_notional"]))

        price = (last_price * (Decimal("1") + PRICE_OFFSET_PCT)).quantize(Decimal("0.1"))
        raw_qty = (TARGET_MARGIN * LEVERAGE) / price
        amount = max(round_to_step(raw_qty, min_qty), min_qty)
        notional = amount * price
        if notional < min_notional:
            raise SystemExit(f"[ERROR] 명목가치 {notional} < 최소 {min_notional}")
        print(f"[INFO] last_price={last_price}, 주문가={price}, amount={amount}, 명목가치={notional} USDT")

        client_order_id = f"openorderstest-{uuid.uuid4().hex[:12]}"
        params = {
            "instrument_name": INSTRUMENT,
            "amount": str(amount),
            "type": "limit",
            "price": str(price),
            "time_in_force": "good_til_cancelled",
            "post_only": False,
            "reduce_only": False,
            "position_side": "SHORT",
            "custom_order_id": client_order_id,
        }
        result = await client.call("/private/sell", params)
        order = result.get("order", result) if isinstance(result, dict) else result
        order_id = str(order["order_id"])
        print(f"[OK] order_id={order_id}")

        state = await client.call("/private/get_order_state", {"order_id": order_id})
        print(f"[STATE] order_state={state.get('order_state')} filled={state.get('filled_amount')}")
        if state.get("order_state") != "open":
            print("[WARN] 주문이 open이 아님 — 즉시체결됐거나 다른 사유. 아래 취소는 안전을 위해 그래도 시도한다.")

        history = await client.call(
            "/private/get_order_history_by_instrument", {"instrument_name": INSTRUMENT}
        )
        matches = [o for o in history if o.get("order_id") == order_id]
        print(f"[CHECK] get_order_history_by_instrument에서 order_id={order_id} 발견: {len(matches)}건")
        if matches:
            print(f"[CHECK] 해당 항목의 order_state = {matches[0].get('order_state')!r}")
        else:
            print("[CHECK] 이 order_id가 이력에 아예 없음 — 인덱싱 지연일 수 있음")

    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        if order_id is not None:
            print(f"[CLEANUP] cancelling order_id={order_id}")
            try:
                cancel_result = await client.call("/private/cancel", {"order_id": order_id})
                print("[OK] cancel:", cancel_result)
                final_state = await client.call("/private/get_order_state", {"order_id": order_id})
                print(f"[FINAL STATE] order_state={final_state.get('order_state')} filled={final_state.get('filled_amount')}")
            except OrangeXError as e:
                print(f"[ERROR] cleanup 취소 실패: {e.code} {e.message} — 수동 확인 필요!")
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
