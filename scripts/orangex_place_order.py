"""터미널에서 직접 실주문을 넣기 위한 범용 CLI (2026-07-30).

이 계좌는 헤지 모드(dual_side_position=true)라 --position-side를 반드시 지정해야
한다 — 안 넣으면 서버가 기본값 BOTH로 처리해 기존 포지션과 충돌, 자동 취소된다
(scripts/orangex_place_first_live_order.py의 시행착오 기록 참고).

주문을 넣은 뒤 자동으로 /private/get_order_state를 재조회해서 실제로 오더북에
걸렸는지(order_state=open) 그 자리에서 보여준다 — /private/buy,sell 응답 자체에는
상태 필드가 없기 때문(docs/api-notes.md §6 항목14).

수량은 --amount(기초자산 수량)로 직접 주거나, --margin과 --leverage로 증거금 기준으로
줄 수 있다. 후자는 notional = margin * leverage, qty = notional / price로 계산한 뒤
/public/get_instruments의 실제 min_qty 스텝에 맞춰 반올림한다 (레버리지는 계약마다/계좌
설정마다 달라 추측하지 않고 매번 명시적으로 --leverage로 받는다 — 과거 주문에서 서버 기본
레버리지가 계좌 실제 설정과 달랐던(25 vs 50) 사고 있음).

사용 예:
  python scripts/orangex_place_order.py --side sell --instrument BTC-USDT-PERPETUAL --price 64660 --margin 2 --leverage 50 --position-side SHORT
  python scripts/orangex_place_order.py --side sell --instrument BTC-USDT-PERPETUAL --price 64660 --amount 0.002 --position-side SHORT --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from decimal import ROUND_HALF_UP, Decimal

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OrangeX 실주문 CLI (지정가만 지원)")
    p.add_argument("--instrument", required=True, help="예: BTC-USDT-PERPETUAL")
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--price", required=True, help="지정가")

    qty_group = p.add_mutually_exclusive_group(required=True)
    qty_group.add_argument("--amount", help="주문 수량 (기초자산 단위, 예: BTC) 직접 지정")
    qty_group.add_argument("--margin", help="증거금(마진) 금액 (USDT) — --leverage와 함께 사용")

    p.add_argument("--leverage", help="--margin 사용 시 필수 — 이 심볼의 실제 설정 레버리지 (앱에서 확인)")
    p.add_argument("--position-side", required=True, choices=["LONG", "SHORT"], help="헤지 모드 필수 파라미터")
    p.add_argument("--reduce-only", action="store_true", help="기존 포지션 축소 전용 주문")
    p.add_argument("--post-only", action="store_true", help="메이커 전용 (즉시 체결되면 거부)")
    p.add_argument("--dry-run", action="store_true", help="실제로 보내지 않고 파라미터만 출력")

    args = p.parse_args()
    if args.margin is not None and args.leverage is None:
        p.error("--margin을 쓰려면 --leverage도 필요합니다 (레버리지를 추측하지 않기 위함)")
    return args


async def fetch_min_qty_and_notional(client: OrangeXClient, instrument: str) -> tuple[Decimal, Decimal]:
    result = await client.call("/public/get_instruments", {"instrument_name": instrument}, authed=False)
    instruments = result if isinstance(result, list) else result.get("instruments", [result])
    for item in instruments:
        if item.get("instrument_name") == instrument:
            return Decimal(str(item["min_qty"])), Decimal(str(item["min_notional"]))
    raise SystemExit(f"[ERROR] get_instruments 응답에서 instrument_name={instrument}을 찾지 못함")


def round_to_step(raw_qty: Decimal, step: Decimal) -> Decimal:
    # 0으로 반올림되는 경우도 그대로 반환 — 호출부에서 min_qty 미달로 명시적으로 걸러낸다
    # (마진이 너무 작아 최소수량에 못 미치는 걸 조용히 최소수량으로 뻥튀기하지 않기 위함).
    steps = (raw_qty / step).to_integral_value(rounding=ROUND_HALF_UP)
    return steps * step


async def main() -> None:
    args = parse_args()
    settings = Settings()
    price = Decimal(args.price)

    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    try:
        min_qty, min_notional = await fetch_min_qty_and_notional(client, args.instrument)

        if args.margin is not None:
            margin = Decimal(args.margin)
            leverage = Decimal(args.leverage)
            notional_target = margin * leverage
            raw_qty = notional_target / price
            amount = round_to_step(raw_qty, min_qty)
            actual_notional = amount * price
            actual_margin = actual_notional / leverage
            print(
                f"[INFO] margin={margin} USDT * leverage={leverage} -> "
                f"목표 명목가치={notional_target} USDT -> 원시 수량={raw_qty} -> "
                f"min_qty({min_qty}) 스텝으로 반올림={amount} "
                f"(실제 명목가치={actual_notional} USDT, 실제 증거금={actual_margin} USDT)"
            )
        else:
            amount = Decimal(args.amount)
            print(f"[INFO] 직접 지정된 수량 사용: amount={amount}")

        if amount < min_qty:
            raise SystemExit(f"[ERROR] 계산된 수량 {amount}이 최소수량 {min_qty} 미만입니다.")
        final_notional = amount * price
        if final_notional < min_notional:
            raise SystemExit(
                f"[ERROR] 명목가치 {final_notional} USDT가 최소명목가치 {min_notional} USDT 미만입니다."
            )

        client_order_id = f"grid-{uuid.uuid4().hex[:20]}"
        params = {
            "instrument_name": args.instrument,
            "amount": str(amount),
            "type": "limit",
            "price": str(price),
            "time_in_force": "good_til_cancelled",
            "post_only": args.post_only,
            "reduce_only": args.reduce_only,
            "position_side": args.position_side,
            "custom_order_id": client_order_id,
        }
        method = "/private/buy" if args.side == "buy" else "/private/sell"

        print(f"[INFO] method={method}")
        print(f"[INFO] params={params}")

        if args.dry_run:
            print("[DRY-RUN] 실제로 전송하지 않았습니다.")
            return

        try:
            result = await client.call(method, params)
        except OrangeXError as e:
            print(f"[ERROR] OrangeX error {e.code}: {e.message}")
            return

        order = result.get("order", result) if isinstance(result, dict) else result
        order_id = order.get("order_id")
        print(f"[OK] order_id={order_id}")

        if order_id is None:
            print("[WARN] 응답에 order_id가 없어 상태 재조회를 건너뜁니다:", result)
            return

        state = await client.call("/private/get_order_state", {"order_id": order_id})
        print("[STATE]", {
            "order_state": state.get("order_state"),
            "filled_amount": state.get("filled_amount"),
            "average_price": state.get("average_price"),
            "error_code": state.get("error_code"),
            "position_side": state.get("position_side"),
            "leverage": state.get("leverage"),
        })
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
