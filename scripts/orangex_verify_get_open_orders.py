"""/private/get_open_orders_by_instrument이 진짜로 미체결 주문을 반환하는지 검증
(get_positions가 "성공은 하지만 있어도 항상 빈 배열"이었던 함정과 같은지 확인 필요,
docs/api-notes.md §6 항목13). scripts/orangex_find_open_orders_endpoint_v2.py에서
이 엔드포인트가 (계좌가 flat/무주문이던 시점에) 빈 배열을 반환하는 걸 확인했는데,
그게 "정상적으로 없어서 빈 배열"인지 "있어도 항상 빈 배열"인지는 실제로 미체결
주문을 하나 만들어봐야 구분된다.

절차: 현재가에서 크게 떨어진(체결 안 될) 지정가 주문을 하나 걸고 -> 이 엔드포인트로
조회해서 실제로 나타나는지 확인 -> 취소 -> 다시 조회해서 사라지는지 확인.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import ROUND_HALF_UP, Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"
POSITION_SIDE = "LONG"


def round_to_step(raw: Decimal, step: Decimal) -> Decimal:
    steps = (raw / step).to_integral_value(rounding=ROUND_HALF_UP)
    return steps * step


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        ticker = await client.call("/public/ticker", {"instrument_name": INSTRUMENT}, authed=False)
        last_price = Decimal(str(ticker["last_price"]))
        instr = await client.call("/public/get_instruments", {"instrument_name": INSTRUMENT}, authed=False)
        instruments = instr if isinstance(instr, list) else instr.get("instruments", [instr])
        spec = next(i for i in instruments if i.get("instrument_name") == INSTRUMENT)
        min_qty = Decimal(str(spec["min_qty"]))
        tick_size = Decimal(str(spec["tick_size"]))

        before = await client.call("/private/get_open_orders_by_instrument", {"instrument_name": INSTRUMENT})
        print(f"[INFO] 주문 걸기 전 get_open_orders_by_instrument: {before}")

        # 현재가 대비 20% 낮은 매수 지정가 — 즉시 체결 안 되도록
        price = round_to_step(last_price * Decimal("0.8"), tick_size)
        coid = f"openorderstest-{uuid.uuid4().hex[:12]}"
        params = {
            "instrument_name": INSTRUMENT, "amount": str(min_qty), "type": "limit",
            "price": str(price), "time_in_force": "good_til_cancelled",
            "post_only": False, "reduce_only": False, "position_side": POSITION_SIDE,
            "custom_order_id": coid,
        }
        print(f"[INFO] 미체결 예상 주문: {params}")
        result = await client.call("/private/buy", params)
        order_id = str(result.get("order", result)["order_id"])
        print(f"[OK] order_id={order_id}")

        await asyncio.sleep(5)  # §6 항목16 지연 이슈 회피

        during = await client.call("/private/get_open_orders_by_instrument", {"instrument_name": INSTRUMENT})
        print(f"[RESULT] 주문 건 후 get_open_orders_by_instrument: {during}")
        found = any(str(o.get("order_id")) == order_id for o in during) if isinstance(during, list) else False
        print(f"[RESULT] 방금 건 주문이 목록에 실제로 나타남: {found}")

        await client.call("/private/cancel", {"order_id": order_id})
        print("[OK] 취소 완료")
        await asyncio.sleep(5)

        after = await client.call("/private/get_open_orders_by_instrument", {"instrument_name": INSTRUMENT})
        print(f"[RESULT] 취소 후 get_open_orders_by_instrument: {after}")

    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
