"""get_order_history_by_instrument이 진짜 open 주문도 반환하는지 검증 (v2, 지연 반영).

v1(orangex_test_get_open_orders.py)에서 주문 접수 직후 get_order_state가 두 번 다
KeyError('result')로 실패했다(docs/api-notes.md §6 항목16, 재현됨 — 서버측 처리
지연으로 추정). 이번엔 접수 후 2초 대기 후 조회하고, 취소 후에도 5초 대기 후
검증한다(취소 API 응답은 즉시 성공해도 order_state 반영에는 지연이 있음을
2026-07-30 앞선 테스트에서 이미 확인함).
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
LEVERAGE = Decimal("50")


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

        client_order_id = f"openorderstest2-{uuid.uuid4().hex[:12]}"
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
        print(f"[OK] order_id={order_id}, 2초 대기 중...")
        await asyncio.sleep(2)

        state = await client.call("/private/get_order_state", {"order_id": order_id})
        print(f"[STATE] order_state={state.get('order_state')} filled={state.get('filled_amount')}")

        history = await client.call(
            "/private/get_order_history_by_instrument", {"instrument_name": INSTRUMENT}
        )
        matches = [o for o in history if o.get("order_id") == order_id]
        print(f"[CHECK] history에서 발견: {len(matches)}건")
        if matches:
            print(f"[CHECK] *** order_state = {matches[0].get('order_state')!r} *** <- 이게 'open'이면 get_open_orders() 대체 확정")

    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        if order_id is not None:
            print(f"[CLEANUP] cancelling order_id={order_id}")
            try:
                cancel_result = await client.call("/private/cancel", {"order_id": order_id})
                print("[OK] cancel:", cancel_result)
                print("[CLEANUP] 5초 대기 후 최종 확인...")
                await asyncio.sleep(5)
                final_state = await client.call("/private/get_order_state", {"order_id": order_id})
                print(f"[FINAL STATE] order_state={final_state.get('order_state')} filled={final_state.get('filled_amount')}")
                if final_state.get("order_state") not in ("canceled", "cancelled"):
                    print("[ALERT] 5초 뒤에도 취소가 반영 안 됨 — 수동 확인 필요!")
            except OrangeXError as e:
                print(f"[ERROR] cleanup 취소 실패: {e.code} {e.message} — 수동 확인 필요!")
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
