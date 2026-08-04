"""condition_type=STOP 조건부 주문이 실제로 트리거되어 체결되는지 라이브 검증
(사용자 명시적 요청, 2026-07-30 계획 승인 — Phase 3 SL 설계의 분기 결정 게이트).

docs/api-notes.md §5는 `condition_type=STOP`/`trigger_price`/`trigger_price_type`
파라미터가 문서에 존재한다고만 기록했을 뿐, 실제로 가격이 트리거에 도달했을 때
정말로 시장가 체결이 일어나는지는 한 번도 라이브로 확인한 적이 없다. 이 스크립트는
그 공백을 메운다.

절차:
  1. 현재 포지션(get_user_position)과 현재가(ticker)를 읽는다 (읽기전용).
  2. 트리거 조건이 "이미 충족된" 상태로 조건부 주문을 걸어(현재가와 거의 동일한
     trigger_price) 자연스러운 가격 변동을 기다리지 않고도 트리거 여부를 빠르게
     확인한다 — 애매함을 줄이기 위한 의도적 설계.
  3. 기존 포지션이 있으면 reduce_only=True로 걸어 "진짜 SL처럼 포지션을 줄이는지"
     확인한다. flat이면 reduce_only=False로 진입 조건부 주문으로 테스트한다.
  4. 증거금은 이전 실주문 테스트와 동일하게 최대 1 USDT로 제한한다.
  5. 주문 접수 후 get_order_state를 몇 차례(2~3초 간격) 재조회해 order_state/
     filled_amount 변화를 관찰한다. 동시에 get_user_position으로 실제 포지션
     변화도 함께 확인해 "주문 상태만 바뀌고 실제 체결은 없는" 위장 신호를 배제한다.
  6. 트리거가 확인되지 않으면 /private/cancel로 정리한다.
  7. reduce_only=False로 실제 포지션이 열린 경우, 정리를 위해 반대 방향
     reduce_only 지정가 주문으로 원상복구를 시도한다(즉시체결 유도).

결과는 성공/실패 관계없이 docs/api-notes.md에 기록한다 (별도 작업).
SPEC 3번 규칙: 사용자가 명시적으로 요청했으므로 실행 가능.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import ROUND_HALF_UP, Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"
TARGET_MARGIN = Decimal("1")  # 이전 실주문 테스트와 동일한 한도
POLL_INTERVAL_SECONDS = 3
POLL_ATTEMPTS = 15  # 최대 ~45초 대기 (1틱 근접 트리거가 자연스러운 시세 변동으로 걸리길 기다림)


def round_to_step(raw_qty: Decimal, step: Decimal) -> Decimal:
    steps = (raw_qty / step).to_integral_value(rounding=ROUND_HALF_UP)
    return steps * step


async def get_position(client: OrangeXClient) -> dict | None:
    result = await client.call("/private/get_user_position", {"instrument_name": INSTRUMENT})
    positions = result if isinstance(result, list) else result.get("positions", [])
    return next((p for p in positions if p.get("instrument_name") == INSTRUMENT), None)


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        # 1. 현재 상태 조회 (읽기전용)
        position = await get_position(client)
        ticker = await client.call("/public/ticker", {"instrument_name": INSTRUMENT}, authed=False)
        last_price = Decimal(str(ticker["last_price"]))
        instr_result = await client.call(
            "/public/get_instruments", {"instrument_name": INSTRUMENT}, authed=False
        )
        instruments = instr_result if isinstance(instr_result, list) else instr_result.get("instruments", [instr_result])
        spec = next(i for i in instruments if i.get("instrument_name") == INSTRUMENT)
        min_qty = Decimal(str(spec["min_qty"]))
        min_notional = Decimal(str(spec["min_notional"]))
        tick_size = Decimal(str(spec["tick_size"]))

        print(f"[INFO] 현재가(last_price)={last_price}, min_qty={min_qty}, min_notional={min_notional}")

        if position is not None:
            avg_price = Decimal(str(position["average_price"]))
            leverage = Decimal(str(position["leverage"]))
            position_side = position["position_side"]
            existing_qty = abs(Decimal(str(position["size"])))
            print(
                f"[INFO] 기존 포지션 있음: position_side={position_side}, avg_price={avg_price}, "
                f"leverage={leverage}, size={position['size']}"
            )
            reduce_only = True
            # SHORT 포지션의 SL은 가격이 오를 때 매수(BUY)로 청산하는 것이 자연스럽다.
            # LONG 포지션의 SL은 가격이 내릴 때 매도(SELL)로 청산.
            side = "buy" if position_side == "SHORT" else "sell"
            qty = min(existing_qty, round_to_step(min_qty, min_qty))
            if qty < min_qty:
                qty = min_qty
        else:
            print("[INFO] 계좌가 flat 상태 -> 조건부 진입 주문으로 테스트 (reduce_only=False)")
            leverage = Decimal("50")  # 2026-07-30 실주문에서 확인된 계좌 실제 설정값 가정
            position_side = "SHORT"  # 계좌의 기존 트레이딩 방향 관례를 따름
            side = "sell"
            reduce_only = False
            raw_qty = (TARGET_MARGIN * leverage) / last_price
            qty = round_to_step(raw_qty, min_qty)
            if qty < min_qty:
                qty = min_qty

        # 트리거 조건이 "이미 충족된" 상태를 만든다: BUY-stop은 트리거가 현재가보다
        # 낮으면(가격이 이미 트리거를 상회) 즉시 조건 충족, SELL-stop은 트리거가
        # 현재가보다 높으면 즉시 조건 충족하는 것이 일반적인 관례라고 가정하고 테스트한다.
        # (이 가정 자체가 검증 대상 중 하나 — 안 맞으면 그냥 트리거가 안 걸릴 뿐, 안전상
        # 문제는 없다.)
        # 1차 시도(offset=5틱)는 24초 동안 order_state=open으로만 머물고 전혀
        # 트리거되지 않았다 — "이미 조건 충족" 방식(level-trigger)이 아니라 배치 이후
        # 실제 가격이 트리거를 "가로지르는"(cross) 이벤트가 필요한 것으로 추정된다.
        # offset을 1틱으로 좁혀 정상적인 시세 변동만으로도 짧은 시간 안에 실제로
        # 가로지르도록 유도한다.
        if side == "buy":
            trigger_price = (last_price - tick_size * 1).quantize(tick_size)
        else:
            trigger_price = (last_price + tick_size * 1).quantize(tick_size)

        price = trigger_price  # 지정가도 트리거가와 동일하게 맞춰 체결 가능성을 높임
        notional = qty * price
        if notional < min_notional:
            raise SystemExit(f"[ERROR] 계산된 명목가치 {notional}가 최소명목가치 {min_notional} 미만 — 테스트 중단")

        client_order_id = f"stoptest-{uuid.uuid4().hex[:16]}"
        params = {
            "instrument_name": INSTRUMENT,
            "amount": str(qty),
            "type": "limit",
            "price": str(price),
            "time_in_force": "good_til_cancelled",
            "post_only": False,
            "reduce_only": reduce_only,
            "position_side": position_side,
            "custom_order_id": client_order_id,
            "condition_type": "STOP",
            "trigger_price": str(trigger_price),
            "trigger_price_type": 2,  # 2 = last price (docs/api-notes.md §5)
        }
        print(f"[INFO] side={side}, reduce_only={reduce_only}, qty={qty}, price={price}, "
              f"trigger_price={trigger_price} (last_price={last_price} 대비 이미 충족 조건)")
        print(f"[INFO] params={params}")

        method = "/private/buy" if side == "buy" else "/private/sell"
        result = await client.call(method, params)
        order_envelope = result.get("order", result) if isinstance(result, dict) else result
        order_id = str(order_envelope["order_id"])
        print(f"[OK] order_id={order_id}")

        # 폴링: order_state 변화와 실제 포지션 변화를 함께 관찰
        pre_test_qty = abs(Decimal(str(position["size"]))) if position else Decimal("0")
        triggered = False
        for attempt in range(1, POLL_ATTEMPTS + 1):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                state = await client.call("/private/get_order_state", {"order_id": order_id})
            except (OrangeXError, KeyError) as e:
                print(f"[WARN] attempt {attempt}: get_order_state 실패({e!r}), 재시도")
                continue
            print(
                f"[POLL {attempt}] order_state={state.get('order_state')}, "
                f"filled_amount={state.get('filled_amount')}, error_code={state.get('error_code')}, "
                f"condition_type={state.get('condition_type')}, trigger_price={state.get('trigger_price')}"
            )
            if state.get("order_state") == "filled" or Decimal(str(state.get("filled_amount", "0"))) > 0:
                triggered = True
                print("[RESULT] 트리거 및 체결 확인됨 — condition_type=STOP이 실제로 동작함")
                break
            if state.get("order_state") in ("cancelled", "canceled", "rejected"):
                print(f"[RESULT] 주문이 {state.get('order_state')} 상태로 종료됨 — 트리거 전 종료, 실패로 간주")
                break

        # 실제 포지션 변화도 교차 확인
        post_position = await get_position(client)
        post_qty = abs(Decimal(str(post_position["size"]))) if post_position else Decimal("0")
        print(f"[INFO] 테스트 전 포지션 수량={pre_test_qty}, 테스트 후 포지션 수량={post_qty}")

        if not triggered:
            print(f"[INFO] {POLL_ATTEMPTS * POLL_INTERVAL_SECONDS}초 내 트리거 미확인 — 미체결 주문 정리(취소) 진행")
            try:
                await client.call("/private/cancel", {"order_id": order_id})
                print("[OK] 취소 완료")
            except OrangeXError as e:
                print(f"[ERROR] 취소 실패: {e.code} {e.message} — 수동 확인 필요, order_id={order_id}")
        elif not reduce_only and post_qty > pre_test_qty:
            # flat에서 새로 진입된 경우 원상복구 시도 (반대방향 reduce_only 즉시체결 유도)
            print("[INFO] flat 상태에서 신규 진입됨 — 원상복구용 반대방향 reduce_only 주문 시도")
            close_side = "buy" if side == "sell" else "sell"
            close_method = "/private/buy" if close_side == "buy" else "/private/sell"
            close_price = (
                (last_price * Decimal("0.95")) if close_side == "buy" else (last_price * Decimal("1.05"))
            ).quantize(tick_size)
            close_params = {
                "instrument_name": INSTRUMENT,
                "amount": str(post_qty),
                "type": "limit",
                "price": str(close_price),
                "time_in_force": "good_til_cancelled",
                "post_only": False,
                "reduce_only": True,
                "position_side": position_side,
                "custom_order_id": f"stopcleanup-{uuid.uuid4().hex[:16]}",
            }
            print(f"[INFO] cleanup params={close_params}")
            try:
                cleanup_result = await client.call(close_method, close_params)
                print("[OK] cleanup order:", cleanup_result)
            except OrangeXError as e:
                print(f"[ERROR] 원상복구 주문 실패: {e.code} {e.message} — 수동 확인 필요")

    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
