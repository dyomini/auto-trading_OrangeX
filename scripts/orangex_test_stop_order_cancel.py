"""condition_type=STOP 주문의 취소(cancel) 동작 확인 — orangex_test_stop_order.py 후속.

앞선 테스트로 STOP 트리거 자체는 실제로 체결됨을 확인했다. SPEC의 "체결마다 기존
SL 취소 후 재등록" 요구사항을 만족하려면, 트리거되기 전 상태의 STOP 주문을 정상적으로
취소할 수 있어야 한다. 이번엔 트리거가 당분간 걸리지 않도록 현재가에서 충분히
떨어진 trigger_price로 걸고, 곧바로 cancel_order를 호출해 order_state가
open(untriggered) -> canceled로 바뀌는지 확인한다.

SPEC 3번 규칙: 사용자가 명시적으로 요청했으므로 실행 가능.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        pos_result = await client.call("/private/get_user_position", {"instrument_name": INSTRUMENT})
        positions = pos_result if isinstance(pos_result, list) else pos_result.get("positions", [])
        position = next((p for p in positions if p.get("instrument_name") == INSTRUMENT), None)
        if position is None:
            raise SystemExit("[ERROR] 포지션이 없음 — reduce_only 테스트 불가, 수동 확인 필요")

        position_side = position["position_side"]
        existing_qty = abs(Decimal(str(position["size"])))
        side = "buy" if position_side == "SHORT" else "sell"
        qty = min(existing_qty, Decimal("0.001"))
        if qty < Decimal("0.001"):
            qty = Decimal("0.001")

        ticker = await client.call("/public/ticker", {"instrument_name": INSTRUMENT}, authed=False)
        last_price = Decimal(str(ticker["last_price"]))
        # 현재가에서 5% 떨어뜨려 당분간 트리거되지 않도록 함
        if side == "sell":
            trigger_price = (last_price * Decimal("1.05")).quantize(Decimal("0.1"))
        else:
            trigger_price = (last_price * Decimal("0.95")).quantize(Decimal("0.1"))

        client_order_id = f"stopcanceltest-{uuid.uuid4().hex[:12]}"
        params = {
            "instrument_name": INSTRUMENT,
            "amount": str(qty),
            "type": "limit",
            "price": str(trigger_price),
            "time_in_force": "good_til_cancelled",
            "post_only": False,
            "reduce_only": True,
            "position_side": position_side,
            "custom_order_id": client_order_id,
            "condition_type": "STOP",
            "trigger_price": str(trigger_price),
            "trigger_price_type": 2,
        }
        print(f"[INFO] side={side}, qty={qty}, trigger_price={trigger_price} (last_price={last_price} 대비 5% 이격 -> 당분간 미트리거 예상)")
        method = "/private/buy" if side == "buy" else "/private/sell"
        result = await client.call(method, params)
        order_envelope = result.get("order", result) if isinstance(result, dict) else result
        order_id = str(order_envelope["order_id"])
        print(f"[OK] order_id={order_id}")

        await asyncio.sleep(2)
        state = await client.call("/private/get_order_state", {"order_id": order_id})
        print(f"[STATE before cancel] order_state={state.get('order_state')}, filled_amount={state.get('filled_amount')}")
        if state.get("order_state") != "open":
            raise SystemExit(f"[ERROR] 예상치 못한 상태({state.get('order_state')}) — 취소 테스트 중단, 수동 확인 필요")

        print(f"[INFO] cancelling order_id={order_id}")
        cancel_result = await client.call("/private/cancel", {"order_id": order_id})
        print("[OK] cancel result:", cancel_result)

        await asyncio.sleep(3)
        state_after = await client.call("/private/get_order_state", {"order_id": order_id})
        print(
            f"[STATE after cancel] order_state={state_after.get('order_state')}, "
            f"filled_amount={state_after.get('filled_amount')}, error_code={state_after.get('error_code')}"
        )
        if state_after.get("order_state") in ("cancelled", "canceled"):
            print("[RESULT] STOP 주문 취소 정상 동작 확인됨")
        else:
            print(f"[RESULT] 취소가 반영되지 않음({state_after.get('order_state')}) — 추가 확인 필요")

    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
