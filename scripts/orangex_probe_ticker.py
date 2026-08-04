"""공개 가격 조회(ticker/orderbook) 엔드포인트 탐색 (읽기전용, 인증 불필요).

포지션이 방금 flat이 되어 avg_price로 현재가를 추정할 수 없게 됐다.
cancel_order 테스트용 주문 가격을 정하려면 대략적인 현재가가 필요한데,
docs/api-notes.md에는 ticker류 공개 엔드포인트가 문서화되어 있지 않다.
Deribit 계열 API에서 흔한 이름들을 후보로 시도해본다 — 전부 공개(authed=False)
엔드포인트라 실패해도 "No service found"만 반환될 뿐 부작용이 없다.
"""
from __future__ import annotations

import asyncio

from exchange.orangex.client import OrangeXClient, OrangeXError

INSTRUMENT = "BTC-USDT-PERPETUAL"
CANDIDATES = [
    "/public/ticker",
    "/public/get_ticker",
    "/public/get_order_book",
    "/public/get_last_trades_by_instrument",
    "/public/get_index_price",
    "/public/get_mark_price",
]


async def main() -> None:
    client = OrangeXClient(client_id="", client_secret="")
    try:
        for method in CANDIDATES:
            try:
                r = await client.call(method, {"instrument_name": INSTRUMENT}, authed=False)
                print(f"[OK] {method}: {r}")
            except OrangeXError as e:
                print(f"[FAIL] {method}: {e.code} {e.message}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
