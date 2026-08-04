"""실제 체결 1건을 만들어 WS `user.trades.{instrument}.raw` 알림의 진짜 필드 스키마를
관찰한다 (사용자 명시적 요청, 2026-07-30 — "1번부터 순서대로 해": docs/phase3-plan.md
"아직 만들지 않은 것" 1번, `OrangeXAdapter._parse_trade_to_fill()`의 `fee` 필드
미검증을 해소하기 위한 게이트).

지금까지의 라이브 실주문 테스트와 동일한 안전 한도를 그대로 따른다
(scripts/orangex_test_stop_order.py, scripts/orangex_place_first_live_order.py):
  - 증거금 목표 1 USDT 근방, 수량은 계약 최소단위(min_qty)로 고정.
  - 계좌가 현재 flat이므로(읽기전용으로 사전 확인함) reduce_only=False로 아주 작은
    LONG을 열고, 확인 즉시 reduce_only=True로 닫아 순노출을 다시 0으로 되돌린다.
  - 두 주문 모두 현재가를 살짝 넘겨 "marketable limit"으로 걸어 즉시 체결을 유도한다
    (오래 미체결로 남지 않도록 — 이게 이 스크립트의 핵심 목적: 체결을 실제로
    발생시켜서 WS user.trades 알림의 진짜 스키마를 관찰하는 것).

절차:
  1. WS 연결/인증/구독(user.trades, user.orders 둘 다 — 스키마 비교용)을 주문 전에
     먼저 끝내서 알림을 놓치지 않는다.
  2. 진입 주문(marketable BUY, reduce_only=False) 접수 -> get_order_state로 체결 확인.
  3. WS로 들어온 원본 메시지를 전부 캡처해서 출력(진짜 관심사).
  4. 청산 주문(marketable SELL, reduce_only=True)으로 순노출 원복.
  5. 결과는 docs/api-notes.md에 별도로 기록한다(이 스크립트가 자동으로 하지 않음).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from decimal import ROUND_HALF_UP, Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError
from exchange.orangex.ws_client import OrangeXWsClient

INSTRUMENT = "BTC-USDT-PERPETUAL"
POSITION_SIDE = "LONG"


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
    ws = OrangeXWsClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
    )
    captured: list[dict] = []

    async def capture_loop() -> None:
        async for msg in ws.notifications():
            captured.append(msg)
            print(f"[WS EVENT] {json.dumps(msg, ensure_ascii=False)}")

    try:
        # 사전 확인 (읽기전용)
        position = await client.call("/private/get_user_position", {"instrument_name": INSTRUMENT})
        positions = position if isinstance(position, list) else position.get("positions", [])
        existing = next((p for p in positions if p.get("instrument_name") == INSTRUMENT), None)
        if existing is not None:
            raise SystemExit(
                f"[ABORT] 계좌가 flat이 아님(size={existing.get('size')}) — 이 스크립트는 "
                "flat 상태 전제로 설계됐다. 수동으로 확인 후 재실행할 것."
            )

        ticker = await client.call("/public/ticker", {"instrument_name": INSTRUMENT}, authed=False)
        last_price = Decimal(str(ticker["last_price"]))
        instr = await client.call("/public/get_instruments", {"instrument_name": INSTRUMENT}, authed=False)
        instruments = instr if isinstance(instr, list) else instr.get("instruments", [instr])
        spec = next(i for i in instruments if i.get("instrument_name") == INSTRUMENT)
        min_qty = Decimal(str(spec["min_qty"]))
        tick_size = Decimal(str(spec["tick_size"]))
        qty = min_qty
        print(f"[INFO] last_price={last_price}, min_qty={min_qty}, qty(this test)={qty}")

        await ws.connect()
        await ws.subscribe([f"user.trades.{INSTRUMENT}.raw", f"user.orders.{INSTRUMENT}.raw"])
        reader = asyncio.create_task(capture_loop())

        # 1. 진입 (marketable BUY) — 현재가보다 확실히 높게 걸어 즉시 체결 유도
        entry_price = (last_price * Decimal("1.01")).quantize(tick_size)
        entry_coid = f"wsobs-entry-{uuid.uuid4().hex[:12]}"
        entry_params = {
            "instrument_name": INSTRUMENT, "amount": str(qty), "type": "limit",
            "price": str(entry_price), "time_in_force": "good_til_cancelled",
            "post_only": False, "reduce_only": False, "position_side": POSITION_SIDE,
            "custom_order_id": entry_coid,
        }
        print(f"[INFO] 진입 주문: {entry_params}")
        entry_result = await client.call("/private/buy", entry_params)
        entry_order_id = str(entry_result.get("order", entry_result)["order_id"])
        print(f"[OK] entry order_id={entry_order_id}")

        entry_filled = False
        for attempt in range(10):
            await asyncio.sleep(2)
            state = await client.call("/private/get_order_state", {"order_id": entry_order_id})
            print(f"[POLL entry {attempt}] order_state={state.get('order_state')}, filled_amount={state.get('filled_amount')}")
            if state.get("order_state") == "filled":
                entry_filled = True
                break

        if not entry_filled:
            print("[WARN] 진입 주문이 예상 시간 내 체결 안 됨 — 취소 시도")
            try:
                await client.call("/private/cancel", {"order_id": entry_order_id})
            except OrangeXError as e:
                print(f"[ERROR] 취소 실패: {e.code} {e.message} — 수동 확인 필요, order_id={entry_order_id}")
            return

        # WS 알림이 도착할 시간을 준다
        await asyncio.sleep(5)
        print(f"[INFO] 지금까지 캡처된 WS 메시지 수: {len(captured)}")

        # 2. 청산 (marketable SELL, reduce_only) — 순노출을 다시 0으로
        exit_price = (last_price * Decimal("0.99")).quantize(tick_size)
        exit_coid = f"wsobs-exit-{uuid.uuid4().hex[:12]}"
        exit_params = {
            "instrument_name": INSTRUMENT, "amount": str(qty), "type": "limit",
            "price": str(exit_price), "time_in_force": "good_til_cancelled",
            "post_only": False, "reduce_only": True, "position_side": POSITION_SIDE,
            "custom_order_id": exit_coid,
        }
        print(f"[INFO] 청산 주문: {exit_params}")
        exit_result = await client.call("/private/sell", exit_params)
        exit_order_id = str(exit_result.get("order", exit_result)["order_id"])
        print(f"[OK] exit order_id={exit_order_id}")

        for attempt in range(10):
            await asyncio.sleep(2)
            state = await client.call("/private/get_order_state", {"order_id": exit_order_id})
            print(f"[POLL exit {attempt}] order_state={state.get('order_state')}, filled_amount={state.get('filled_amount')}")
            if state.get("order_state") == "filled":
                break

        await asyncio.sleep(5)
        print(f"[INFO] 최종 캡처된 WS 메시지 수: {len(captured)}")
        for i, msg in enumerate(captured):
            print(f"[CAPTURED {i}] {json.dumps(msg, ensure_ascii=False)}")

        final_position = await client.call("/private/get_user_position", {"instrument_name": INSTRUMENT})
        final_positions = final_position if isinstance(final_position, list) else final_position.get("positions", [])
        final_existing = next((p for p in final_positions if p.get("instrument_name") == INSTRUMENT), None)
        print(f"[INFO] 최종 포지션: {final_existing}")

        reader.cancel()

    finally:
        await ws.close()
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
