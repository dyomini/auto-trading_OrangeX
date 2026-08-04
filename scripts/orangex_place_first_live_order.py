"""이 시스템에서 실행하는 최초의 실주문 (사용자 명시적 요청, 2026-07-30).

BTC-USDT-PERPETUAL 숏 포지션 추가용 매도 지정가 주문:
  - price=64660, amount=0.002 (증거금 2 USDT * 레버리지 50배 = 명목가치 100 USDT 기준,
    quantityPrec=3이라 0.0015467을 반올림)

1차 시도(2026-07-30, amount=0.008)는 API 키의 trade scope가 read-only라 `Access denied`
(code 2033)로 거부됨 — 사용자가 앱에서 쓰기 권한을 켠 뒤 더 작은 사이즈로 재시도.

2차 시도(amount=0.002, leverage 파라미터 없음)는 API 호출 자체는 성공(order_id 발급)했지만
`get_order_state`로 확인해보니 `order_state=canceled`(error_code 5998, 체결 0)였다.

3차 시도(leverage="50" 추가)도 동일하게 취소됨 — 응답에 여전히 leverage=25가 찍혀서 leverage
파라미터는 서버가 무시하는 것으로 보임(가설 기각). 대신 두 시도 모두 get_order_state 응답에
`"position_side": "BOTH"`가 찍혀 있었다. 계좌는 dual_side_position=true(헤지 모드)이고 실제
포지션은 position_side=SHORT인데, 주문에 position_side를 안 넘기면 서버가 기본값 BOTH(원웨이
모드 규약)로 처리해 기존 SHORT 포지션과 모드가 충돌 → 자동 취소되는 것으로 추정된다. 4차
시도에서 `position_side: "SHORT"`를 명시해 재검증한다.

place_limit_order()의 실제 응답 envelope이 문서에 없어 라이브 검증이 안 된 상태이므로,
adapter를 거치지 않고 client.call()로 직접 호출해 원본 응답을 그대로 확인한다.
SPEC 3번 규칙: 사용자가 명시적으로 요청했으므로 실행 가능.
"""
from __future__ import annotations

import asyncio
import uuid

from config.settings import Settings
from exchange.orangex.client import OrangeXClient, OrangeXError


async def main() -> None:
    settings = Settings()
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    client_order_id = f"grid-{uuid.uuid4().hex[:20]}"
    params = {
        "instrument_name": "BTC-USDT-PERPETUAL",
        "amount": "0.002",
        "type": "limit",
        "price": "64660",
        "time_in_force": "good_til_cancelled",
        "post_only": False,
        "reduce_only": False,
        "position_side": "SHORT",
        "custom_order_id": client_order_id,
    }
    print(f"[INFO] custom_order_id={client_order_id}")
    print(f"[INFO] params={params}")
    try:
        result = await client.call("/private/sell", params)
        print("[OK] /private/sell result:")
        print(result)
    except OrangeXError as e:
        print(f"[ERROR] OrangeX error {e.code}: {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
