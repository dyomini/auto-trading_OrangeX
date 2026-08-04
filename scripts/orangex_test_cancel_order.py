"""`cancel_order` 실주문 검증 (사용자 명시적 요청, 2026-07-30, "1테더로만 진행해").

목적: docs/phase2-report.md §7에서 "아직 실주문으로 검증 안 됨"으로 남겨둔
cancel_order를 라이브로 확인한다. 절차:
  1. 현재 포지션(get_user_position)을 읽어 현재가 근사치(avg_price)를 구한다.
  2. avg_price보다 충분히(5%) 높은 매도 지정가를 최소 수량(min_qty)으로 걸어
     즉시 체결될 가능성을 낮춘다 (기존 포지션과 같은 SHORT 방향이라, 혹시
     체결되더라도 기존 전략 방향과 상충하지 않음). 증거금은 최대한 1 USDT에
     가깝게 맞추되, min_qty 스텝 때문에 정확히 1 USDT는 안 될 수 있다.
  3. get_order_state로 order_state=open 확인.
  4. cancel_by_id로 취소.
  5. get_order_state로 재조회해 취소가 반영됐는지 확인.
  6. 참고용으로 get_open_order_by_instrument와 대체 후보 엔드포인트
     (get_open_order_by_currency, get_open_orders)를 취소 전/후로 호출해
     get_open_orders() 어댑터 구현에 쓸 수 있는 엔드포인트가 있는지 탐색한다
     (읽기전용 호출이라 자금 리스크 없음).

SPEC 3번 규칙: 사용자가 명시적으로 요청했으므로 실행 가능.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import ROUND_HALF_UP, Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"
TARGET_MARGIN = Decimal("1")  # 사용자 지시: 1 USDT로만 진행
PRICE_OFFSET_PCT = Decimal("0.05")  # avg_price 대비 +5% (즉시체결 방지)


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
    try:
        # 1. 현재 포지션 확인 (읽기전용) -> flat이면 avg_price를 못 쓰므로 ticker로 대체
        pos_result = await client.call(
            "/private/get_user_position", {"instrument_name": INSTRUMENT}
        )
        positions = pos_result if isinstance(pos_result, list) else pos_result.get("positions", [])
        print("[INFO] get_user_position raw:", positions)
        target = next((p for p in positions if p.get("instrument_name") == INSTRUMENT), None)

        if target is not None:
            avg_price = Decimal(str(target["average_price"]))
            leverage = Decimal(str(target["leverage"]))
            position_side = target["position_side"]
            print(f"[INFO] 기존 포지션 사용: avg_price={avg_price}, leverage={leverage}, position_side={position_side}")
        else:
            # 2026-07-30: 이전 확인 시점과 달리 계좌가 flat이 됨 (get_user_position=[] ->
            # 이번 기회에 flat 케이스도 확인됨, docs/api-notes.md §6 항목13 미확인분 해소).
            # avg_price 대신 /public/ticker의 last_price를 현재가로 사용.
            ticker = await client.call("/public/ticker", {"instrument_name": INSTRUMENT}, authed=False)
            avg_price = Decimal(str(ticker["last_price"]))
            # leverage는 flat 상태에서 조회할 문서화된 엔드포인트가 없어, 2026-07-30
            # 실주문에서 확인된 계좌 실제 설정값(50)을 그대로 가정한다 — 틀려도 주문
            # 자체는 성공하고 실제 leverage는 get_order_state 응답으로 바로 드러난다.
            leverage = Decimal("50")
            position_side = "SHORT"
            print(f"[INFO] flat 상태 -> ticker 기준 last_price={avg_price} 사용, leverage={leverage}(가정) 사용")

        # 2. 계약 스펙 (min_qty) 확인
        instr_result = await client.call(
            "/public/get_instruments", {"instrument_name": INSTRUMENT}, authed=False
        )
        instruments = instr_result if isinstance(instr_result, list) else instr_result.get("instruments", [instr_result])
        spec = next(i for i in instruments if i.get("instrument_name") == INSTRUMENT)
        min_qty = Decimal(str(spec["min_qty"]))
        min_notional = Decimal(str(spec["min_notional"]))

        price = (avg_price * (Decimal("1") + PRICE_OFFSET_PCT)).quantize(Decimal("0.1"))
        raw_qty = (TARGET_MARGIN * leverage) / price
        amount = round_to_step(raw_qty, min_qty)
        if amount < min_qty:
            amount = min_qty
        actual_notional = amount * price
        actual_margin = actual_notional / leverage
        if actual_notional < min_notional:
            raise SystemExit(f"[ERROR] 계산된 명목가치 {actual_notional}가 최소명목가치 {min_notional} 미만")

        print(
            f"[INFO] 주문가={price} (avg_price+{PRICE_OFFSET_PCT*100}%), amount={amount}, "
            f"실제명목가치={actual_notional} USDT, 실제증거금={actual_margin} USDT (목표 1 USDT)"
        )

        # 3. 주문 접수
        client_order_id = f"canceltest-{uuid.uuid4().hex[:16]}"
        order_params = {
            "instrument_name": INSTRUMENT,
            "amount": str(amount),
            "type": "limit",
            "price": str(price),
            "time_in_force": "good_til_cancelled",
            "post_only": False,
            "reduce_only": False,
            "position_side": position_side,
            "custom_order_id": client_order_id,
        }
        print("[INFO] placing order:", order_params)
        result = await client.call("/private/sell", order_params)
        order = result.get("order", result) if isinstance(result, dict) else result
        order_id = str(order["order_id"])
        print(f"[OK] order_id={order_id}")

        state = await client.call("/private/get_order_state", {"order_id": order_id})
        print("[STATE after place]", {
            "order_state": state.get("order_state"),
            "filled_amount": state.get("filled_amount"),
            "error_code": state.get("error_code"),
        })
        if state.get("order_state") != "open":
            raise SystemExit(
                f"[ERROR] 주문이 open 상태가 아님({state.get('order_state')}) — cancel 테스트 중단. "
                "즉시 체결됐거나 다른 사유로 취소됐을 수 있으니 수동 확인 필요."
            )

        # 6a. 취소 전 open orders 조회 (get_open_orders 대체 엔드포인트 탐색, 읽기전용)
        for label, method in [
            ("get_open_order_by_instrument", "/private/get_open_order_by_instrument"),
            ("get_open_order_by_currency", "/private/get_open_order_by_currency"),
            ("get_open_orders", "/private/get_open_orders"),
        ]:
            params = {"instrument_name": INSTRUMENT} if "instrument" in method else {"currency": "USDT"}
            try:
                r = await client.call(method, params)
                print(f"[OK] {label} (취소 전):", r)
            except OrangeXError as e:
                print(f"[ERROR] {label} (취소 전): {e.code} {e.message}")

        # 4. 취소
        print(f"[INFO] cancelling order_id={order_id}")
        cancel_result = await client.call("/private/cancel_by_id", {"order_id": order_id})
        print("[OK] cancel_by_id result:", cancel_result)

        # 5. 취소 확인
        state_after = await client.call("/private/get_order_state", {"order_id": order_id})
        print("[STATE after cancel]", {
            "order_state": state_after.get("order_state"),
            "filled_amount": state_after.get("filled_amount"),
            "error_code": state_after.get("error_code"),
        })

        # 6b. 취소 후 open orders 재조회 (사라졌는지 확인)
        for label, method in [
            ("get_open_order_by_instrument", "/private/get_open_order_by_instrument"),
            ("get_open_order_by_currency", "/private/get_open_order_by_currency"),
            ("get_open_orders", "/private/get_open_orders"),
        ]:
            params = {"instrument_name": INSTRUMENT} if "instrument" in method else {"currency": "USDT"}
            try:
                r = await client.call(method, params)
                print(f"[OK] {label} (취소 후):", r)
            except OrangeXError as e:
                print(f"[ERROR] {label} (취소 후): {e.code} {e.message}")

    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
