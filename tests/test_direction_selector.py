"""engine/direction_selector.py 유닛 테스트 (2026-08-17).

바이낸스 15분봉 klines를 httpx.MockTransport로 흉내낸다 —
tests/test_market_data.py / test_entry_scheduler.py와 같은 기법.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from engine.direction_selector import (
    DirectionDecisionError,
    decide_direction_from_rsi,
    decide_direction_with_retry,
)

INSTRUMENT = "BTC-USDT-PERPETUAL"
FIFTEEN_MIN_MS = 15 * 60 * 1000


def make_client(closes: list[str], last_is_forming: bool = True) -> httpx.AsyncClient:
    """closes의 마지막 값은 기본적으로 '아직 진행 중인 봉'이 되도록 open_time을 잡는다."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    n = len(closes)
    rows = []
    for i, close in enumerate(closes):
        if i == n - 1 and last_is_forming:
            open_time_ms = now_ms - 60_000  # 1분 전 시작 -> 15분봉이라 아직 진행 중
        else:
            offset = n - 1 - i
            open_time_ms = now_ms - 60_000 - offset * FIFTEEN_MIN_MS
        rows.append([open_time_ms, close, close, close, close])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["interval"] == "15m", "15분봉을 요청해야 한다"
        return httpx.Response(200, json=rows)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_returns_short_when_rsi_at_or_above_50():
    # 단조 상승 종가 -> RSI 100 (test_entry_scheduler.py와 같은 결정론적 유도 기법)
    closes = [str(60000 + i * 100) for i in range(16)] + ["99999"]
    client = make_client(closes)

    decision = await decide_direction_from_rsi(INSTRUMENT, http_client=client)
    await client.aclose()

    assert decision.rsi == Decimal("100")
    assert decision.direction == "short"


@pytest.mark.asyncio
async def test_returns_long_when_rsi_below_50():
    closes = [str(70000 - i * 100) for i in range(16)] + ["1"]
    client = make_client(closes)

    decision = await decide_direction_from_rsi(INSTRUMENT, http_client=client)
    await client.aclose()

    assert decision.rsi == Decimal("0")
    assert decision.direction == "long"


@pytest.mark.asyncio
async def test_ignores_the_forming_candle():
    """진행 중 봉을 포함하면 답이 뒤집히도록 데이터를 구성해, 실제로 제외되는지 본다.
    완결봉은 전부 단조 상승(RSI 100 -> short)인데, 마지막 진행 중 봉만 폭락값이다 —
    이걸 잘못 포함하면 RSI가 뚝 떨어져 long으로 뒤집힌다."""
    closes = [str(60000 + i * 100) for i in range(16)] + ["1"]
    client = make_client(closes, last_is_forming=True)

    decision = await decide_direction_from_rsi(INSTRUMENT, http_client=client)
    await client.aclose()

    assert decision.direction == "short"  # 진행 중 봉을 포함했다면 long이 됐을 것
    # 마지막 완결봉(진행 중 봉 바로 앞)을 기준으로 삼았는지 확인
    assert decision.last_closed_open_time_ms > 0


@pytest.mark.asyncio
async def test_raises_when_not_enough_closed_candles():
    """부족하면 기본 방향으로 폴백하지 않고 반드시 예외 — 방향을 잘못 고르면
    격자 전체가 반대로 깔린다."""
    client = make_client(["64000"] * 5)

    with pytest.raises(DirectionDecisionError, match="추측하지 않고"):
        await decide_direction_from_rsi(INSTRUMENT, http_client=client)
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_reraises_after_exhausting_delays():
    client = make_client(["64000"] * 3)  # 항상 부족 -> 매번 실패

    with pytest.raises(DirectionDecisionError, match="1회 시도"):
        await decide_direction_with_retry(INSTRUMENT, http_client=client, delays=())
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    """첫 호출만 네트워크 에러, 두 번째는 정상 — 재시도가 실제로 동작하는지."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    closes = [str(60000 + i * 100) for i in range(16)] + ["99999"]
    rows = []
    for i, close in enumerate(closes):
        offset = len(closes) - 1 - i
        open_time_ms = now_ms - 60_000 - offset * FIFTEEN_MIN_MS
        rows.append([open_time_ms, close, close, close, close])

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("시뮬레이션: 네트워크 순단")
        return httpx.Response(200, json=rows)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    decision = await decide_direction_with_retry(INSTRUMENT, http_client=client, delays=(0,))
    await client.aclose()

    assert calls["n"] == 2
    assert decision.direction == "short"


@pytest.mark.asyncio
async def test_unsupported_interval_is_rejected_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("지원 안 하는 interval인데 요청이 나갔다")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(Exception, match="지원하지 않는"):
        await decide_direction_from_rsi(INSTRUMENT, http_client=client, interval="7m")
    await client.aclose()
