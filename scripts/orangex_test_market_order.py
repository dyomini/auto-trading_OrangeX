"""place_market_order()가 실제로 시장가 체결되는지 라이브 검증 (사용자 명시적 요청,
2026-07-30 — "1번부터 순서대로 해"의 2번: docs/phase3-plan.md "아직 만들지 않은 것"
`place_market_order`의 OrangeX 라이브 검증. hybrid reset(3차+ 평단 도달 시 50%
시장가 청산)과 강제청산(_force_close_and_halt)이 이 메서드에 의존하므로 실제로
체결되는지 확인해야 그 두 안전장치를 신뢰할 수 있다).

지금까지의 실주문 테스트와 동일한 안전 한도(scripts/orangex_observe_live_fill_ws.py
와 동일 패턴): 계좌 flat 사전 확인 -> 최소단위(0.001 BTC)로 시장가 진입 ->
get_order_state/get_user_position으로 실제 체결·포지션 변화 교차 확인 -> 즉시
시장가로 청산해서 순노출 원복.

STOP 주문 때(docs/api-notes.md §6 항목18) crossing-trigger라는 예상 밖 특성이
있었던 전례가 있어, 이번에도 "그냥 됨"으로 넘기지 않고 실제 체결가/체결량/포지션
변화를 전부 교차 검증한다.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"
POSITION_SIDE = "LONG"


async def get_position(client: OrangeXClient) -> dict | None:
    result = await client.call("/private/get_user_position", {"instrument_name": INSTRUMENT})
    positions = result if isinstance(result, list) else result.get("positions", [])
    return next((p for p in positions if p.get("instrument_name") == INSTRUMENT), None)


async def place_and_verify(client: OrangeXClient, side: str, qty: Decimal, reduce_only: bool, label: str) -> dict:
    method = "/private/buy" if side == "buy" else "/private/sell"
    coid = f"markettest-{label}-{uuid.uuid4().hex[:12]}"
    params = {
        "instrument_name": INSTRUMENT, "amount": str(qty), "type": "market",
        "reduce_only": reduce_only, "position_side": POSITION_SIDE, "custom_order_id": coid,
    }
    print(f"[INFO] {label} 시장가 주문: {params}")
    result = await client.call(method, params)
    order_id = str(result.get("order", result)["order_id"])
    print(f"[OK] {label} order_id={order_id}")

    for attempt in range(10):
        await asyncio.sleep(1)
        state = await client.call("/private/get_order_state", {"order_id": order_id})
        print(f"[POLL {label} {attempt}] order_state={state.get('order_state')}, "
              f"filled_amount={state.get('filled_amount')}, average_price={state.get('average_price')}")
        if state.get("order_state") in ("filled", "canceled", "cancelled", "rejected"):
            break
    return state


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        existing = await get_position(client)
        if existing is not None:
            raise SystemExit(
                f"[ABORT] 계좌가 flat이 아님(size={existing.get('size')}) — "
                "이 스크립트는 flat 상태 전제로 설계됐다."
            )

        instr = await client.call("/public/get_instruments", {"instrument_name": INSTRUMENT}, authed=False)
        instruments = instr if isinstance(instr, list) else instr.get("instruments", [instr])
        spec = next(i for i in instruments if i.get("instrument_name") == INSTRUMENT)
        min_qty = Decimal(str(spec["min_qty"]))
        qty = min_qty
        print(f"[INFO] min_qty={min_qty}, qty(this test)={qty}")

        entry_state = await place_and_verify(client, "buy", qty, reduce_only=False, label="entry")
        entry_filled = entry_state.get("order_state") == "filled"
        print(f"[RESULT] 진입 시장가 체결 여부: {entry_filled}")

        mid_position = await get_position(client)
        print(f"[INFO] 진입 후 포지션: {mid_position}")

        if not entry_filled:
            print("[WARN] 진입이 filled가 아님 — 청산 단계 스킵, 수동 확인 필요")
            return

        exit_state = await place_and_verify(client, "sell", qty, reduce_only=True, label="exit")
        exit_filled = exit_state.get("order_state") == "filled"
        print(f"[RESULT] 청산 시장가 체결 여부: {exit_filled}")

        final_position = await get_position(client)
        print(f"[INFO] 최종 포지션(flat이어야 함): {final_position}")

        if not exit_filled or final_position is not None:
            print("[WARN] 청산이 완전하지 않을 수 있음 — 수동 확인 필요")

    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
