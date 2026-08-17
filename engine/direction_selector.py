"""`DIRECTION=auto`에서 "이번 사이클을 롱으로 갈지 숏으로 갈지"를 15분봉 RSI(14)로
결정한다 (2026-08-17 사용자 결정).

`engine/entry_scheduler.py`의 일봉 RSI 게이트(SPEC 90줄, ≤30/≥70)와는 목적이 다르다:
저건 "지금 들어가도 되나"를 묻는 **게이트**라 통과 못 하면 그냥 더 기다리면 되지만,
여기는 **방향 선택**이라 답을 못 내면 할 수 있는 안전한 기본 동작이 없다. 방향을
잘못 고르면 격자 100단계가 통째로 반대로 깔린다. 그래서 데이터가 부족하거나 조회가
실패하면 기본값으로 폴백하지 않고 `DirectionDecisionError`로 멈춘다(SPEC 0번).

멈추는 게 안전한 이유: 이 결정은 **계좌가 flat일 때만** 내려진다(최초 기동 시,
그리고 사이클이 완전히 청산된 직후). 포지션을 든 채로 방향을 다시 묻는 경로는 없다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

import httpx

from engine.entry_filter import direction_from_rsi
from strategy.indicators import compute_rsi
from strategy.liquidation import Direction
from strategy.market_data import closed_candles, fetch_candles, interval_to_ms

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
AUTO_DIRECTION_INTERVAL = "15m"
# 바이낸스 순단/레이트리밋 대응. 모듈 상수로 둬서 테스트가 ()로 비우고 즉시 실패시킨다.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (5, 15, 30)


class DirectionDecisionError(Exception):
    """15분봉 RSI를 확정할 수 없어 방향을 고를 수 없을 때. 절대 기본 방향으로
    폴백하지 않는다 — 위 모듈 docstring 참고."""


@dataclass(frozen=True)
class DirectionDecision:
    direction: Direction
    rsi: Decimal
    # 어느 봉을 마지막 완결봉으로 봤는지 — 로그/사후 검증용(같은 봉으로 두 번 판정하는
    # 상황이나, 진행 중 봉이 잘못 섞여 들어온 경우를 나중에 추적할 수 있게 남긴다).
    last_closed_open_time_ms: int


async def decide_direction_from_rsi(
    instrument: str,
    http_client: Optional[httpx.AsyncClient] = None,
    interval: str = AUTO_DIRECTION_INTERVAL,
    period: int = RSI_PERIOD,
) -> DirectionDecision:
    interval_ms = interval_to_ms(interval)
    candles = await fetch_candles(
        instrument, interval=interval, limit=period + 2, http_client=http_client
    )
    closed = closed_candles(candles, interval_ms=interval_ms)
    if len(closed) < period + 1:
        raise DirectionDecisionError(
            f"RSI({period}) 계산에 완결된 {interval} 봉이 {period + 1}개 필요한데 {len(closed)}개뿐 — "
            "방향을 추측하지 않고 중단한다"
        )

    rsi = compute_rsi([c.close for c in closed], period=period)
    return DirectionDecision(
        direction=direction_from_rsi(rsi),
        rsi=rsi,
        last_closed_open_time_ms=closed[-1].open_time_ms,
    )


async def decide_direction_with_retry(
    instrument: str,
    http_client: Optional[httpx.AsyncClient] = None,
    interval: str = AUTO_DIRECTION_INTERVAL,
    period: int = RSI_PERIOD,
    delays: Sequence[int] = RETRY_DELAYS_SECONDS,
) -> DirectionDecision:
    """일시적 조회 실패는 재시도하고, 끝내 실패하면 그대로 올린다(폴백 없음)."""
    attempts = len(delays) + 1
    for attempt, delay in enumerate([*delays, None], start=1):
        try:
            return await decide_direction_from_rsi(
                instrument, http_client=http_client, interval=interval, period=period
            )
        except (DirectionDecisionError, httpx.HTTPError) as e:
            if delay is None:
                raise DirectionDecisionError(
                    f"{attempts}회 시도했으나 방향을 확정하지 못함: {e!r}"
                ) from e
            logger.warning(
                "방향 판정 실패(%d/%d) — %d초 후 재시도: %r", attempt, attempts, delay, e
            )
            await asyncio.sleep(delay)
    raise AssertionError("도달 불가")  # pragma: no cover
