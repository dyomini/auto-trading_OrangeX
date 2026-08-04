"""engine/grid_setup.py 유닛 테스트 (main.py에서 분리 — CycleManager도 이 모듈을 쓴다)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from config.settings import Settings
from engine.grid_setup import StartupError, build_execution_adapter, build_grid_rows
from exchange.base import ContractSpec
from exchange.orangex.adapter import OrangeXAdapter
from exchange.paper import PaperAdapter

INSTRUMENT = "BTC-USDT-PERPETUAL"
ONE_DAY_MS = 24 * 60 * 60 * 1000


def make_binance_client(closes: list[str]) -> httpx.AsyncClient:
    """마지막 값은 아직 마감 안 된(진행 중인) 봉으로 취급되도록(closed_candles가 거름)
    최근 1시간 전 시각을 준다 — tests/test_entry_scheduler.py의 make_kline_rows와 동일 기법."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    n = len(closes)
    rows = []
    for i, close in enumerate(closes):
        if i == n - 1:
            open_time_ms = now_ms - 3_600_000
        else:
            offset = n - 1 - i
            open_time_ms = now_ms - 3_600_000 - offset * ONE_DAY_MS
        rows.append([open_time_ms, close, close, close, close])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_flat_binance_client() -> httpx.AsyncClient:
    """가격 변화 없음 -> true range=0 -> ATR=0 -> 배율 항상 1(확대 없음)."""
    return make_binance_client(["64000"] * 18)


class FakeOrangeXClient:
    """tests/test_orangex_adapter.py의 FakeClient와 동일한 패턴."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict, bool]] = []

    async def call(self, method, params=None, authed=True):
        self.calls.append((method, dict(params or {}), authed))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return response


def make_market_data_adapter(last_price: str = "64000", min_qty: str = "0.001", min_notional: str = "10") -> OrangeXAdapter:
    client = FakeOrangeXClient(
        {
            "/public/get_instruments": {
                "instruments": [
                    {
                        "instrument_name": INSTRUMENT,
                        "tick_size": "50",
                        "min_qty": min_qty,
                        "min_notional": min_notional,
                        "contract_size": "1",
                    }
                ]
            },
            "/public/ticker": {"last_price": last_price},
        }
    )
    return OrangeXAdapter(client)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        trading_mode="paper",
        symbol=INSTRUMENT,
        direction="long",
        equity_usdt=Decimal("10000"),
        leverage=Decimal("20"),
        grid_tick=Decimal("50"),
        max_open_grid_orders=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_build_grid_rows_uses_ticker_price_as_base():
    settings = make_settings()
    market_data_adapter = make_market_data_adapter(last_price="64000")
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    binance_client = make_flat_binance_client()

    rows = await build_grid_rows(settings, market_data_adapter, contract_spec, binance_client)
    await binance_client.aclose()

    assert rows[0].entry_price == Decimal("64000")  # index0 == base_price(long)
    assert rows[1].entry_price == Decimal("63950")  # base_price - tick*1 (ATR 확대 없음)


@pytest.mark.asyncio
async def test_build_grid_rows_truncates_infeasible_steps():
    # equity=10000, leverage=20 -> 알려진 대로 91단계까지만 실행 가능. max_stage 절삭이
    # 먼저 걸리지 않도록 넉넉히 5(=100단계)로 올려서 feasibility 절삭만 단독으로 검증한다.
    settings = make_settings(max_stage=5)
    market_data_adapter = make_market_data_adapter()
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    binance_client = make_flat_binance_client()

    rows = await build_grid_rows(settings, market_data_adapter, contract_spec, binance_client)
    await binance_client.aclose()

    assert len(rows) == 91
    assert all(r.available_balance >= 0 for r in rows)


@pytest.mark.asyncio
async def test_build_grid_rows_truncates_to_max_stage():
    # SPEC 110번 "max_stage를 넘는 진입 금지" — max_stage=3이면 4~5차(tier4/5) 가중치는
    # 애초에 compute_grid()에 넘어가지도 않는다(2026-08-04, weight_sum 재정규화로 수정 —
    # 3k 참고 설계와 동일하게 활성 tier에 equity 전액이 배정되도록). 그 결과 이 equity/
    # leverage 조합에서는 feasibility(가용잔고 소진)가 60단계보다 먼저(54단계) 걸린다 —
    # 이전(재정규화 전)에는 equity 대부분이 미배정으로 남아 60단계 전부 feasible했지만,
    # 그건 자금 비효율의 결과였다. 어느 쪽이든 tier4/5로는 절대 안 들어가야 한다는
    # 핵심만 검증한다.
    settings = make_settings(max_stage=3)
    market_data_adapter = make_market_data_adapter()
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    binance_client = make_flat_binance_client()

    rows = await build_grid_rows(settings, market_data_adapter, contract_spec, binance_client)
    await binance_client.aclose()

    assert len(rows) <= 60
    assert all(r.major_tier <= 3 for r in rows)
    assert rows[-1].major_tier == 3


@pytest.mark.asyncio
async def test_build_grid_rows_renormalizes_weights_to_active_tiers():
    # 2026-08-04 수정 검증: max_stage로 잘라낸 뒤에도 weight_sum이 예전처럼 100단계
    # 전체(17130) 기준이면 안 되고, 실제로 쓰이는 tier들의 가중치 합만으로 재정규화돼야
    # equity가 낭비 없이 배정된다. max_stage=3(가중치 합 3530)에서 1단계 증거금이
    # max_stage=5(가중치 합 17130)보다 커야 한다 — 같은 equity를 더 적은 단계에 나눠
    # 쓰니까 단계당 몫이 커지는 게 당연하다.
    market_data_adapter = make_market_data_adapter()
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)

    binance_client_3 = make_flat_binance_client()
    rows_stage3 = await build_grid_rows(make_settings(max_stage=3), market_data_adapter, contract_spec, binance_client_3)
    await binance_client_3.aclose()

    binance_client_5 = make_flat_binance_client()
    rows_stage5 = await build_grid_rows(make_settings(max_stage=5), market_data_adapter, contract_spec, binance_client_5)
    await binance_client_5.aclose()

    assert rows_stage3[0].step_margin > rows_stage5[0].step_margin
    # 정확한 배율까지 확인: 3530 vs 17130 (config/weights.csv 실제 합), quantize 오차 감안 1% 이내
    expected = rows_stage5[0].step_margin * Decimal("17130") / Decimal("3530")
    assert abs(rows_stage3[0].step_margin - expected) / expected < Decimal("0.01")


@pytest.mark.asyncio
async def test_build_grid_rows_raises_on_min_order_shortfall():
    settings = make_settings(equity_usdt=Decimal("1000"))  # 1차 1단계 수량이 min_qty 미달
    market_data_adapter = make_market_data_adapter()
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    binance_client = make_flat_binance_client()

    with pytest.raises(StartupError, match="최소 주문 수량"):
        await build_grid_rows(settings, market_data_adapter, contract_spec, binance_client)
    await binance_client.aclose()


@pytest.mark.asyncio
async def test_build_grid_rows_widens_tick_on_atr_spike():
    settings = make_settings()
    market_data_adapter = make_market_data_adapter(last_price="64000")
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    # 어제까지는 완만하다가(1씩 변화) 마지막 완결봉에서 크게 뛰어(변화폭 3000) 오늘자
    # ATR(14)이 어제자보다 30% 넘게 커지도록 만든다 — engine/entry_filter.py의 급등 판정.
    closes = [str(64000 + i) for i in range(16)] + [str(64000 + 16 + 3000), "64000"]
    binance_client = make_binance_client(closes)

    rows = await build_grid_rows(settings, market_data_adapter, contract_spec, binance_client)
    await binance_client.aclose()

    tick_used = rows[0].entry_price - rows[1].entry_price
    assert tick_used > settings.grid_tick  # 급등 감지돼 tick이 기본값(50)보다 넓어짐
    assert tick_used <= settings.grid_tick * Decimal("2.0")  # 상한(2배) 이내


@pytest.mark.asyncio
async def test_build_grid_rows_does_not_widen_tick_with_insufficient_candle_history():
    settings = make_settings()
    market_data_adapter = make_market_data_adapter(last_price="64000")
    contract_spec = await market_data_adapter.get_contract_spec(INSTRUMENT)
    binance_client = make_binance_client(["64000"] * 5)  # ATR(14) 계산에 한참 부족

    rows = await build_grid_rows(settings, market_data_adapter, contract_spec, binance_client)
    await binance_client.aclose()

    assert rows[0].entry_price - rows[1].entry_price == settings.grid_tick  # 배율 1(확대 없음)


def test_build_execution_adapter_paper_mode_returns_paper_adapter():
    settings = make_settings(trading_mode="paper")
    contract_spec = ContractSpec(
        instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.001"),
        min_notional=Decimal("10"), contract_size=Decimal("1"),
    )

    adapter = build_execution_adapter(settings, contract_spec)

    assert isinstance(adapter, PaperAdapter)


def test_build_execution_adapter_live_mode_returns_orangex_adapter():
    settings = make_settings(trading_mode="live", api_key="k", api_secret="s")
    contract_spec = ContractSpec(
        instrument=INSTRUMENT, tick_size=Decimal("50"), min_qty=Decimal("0.001"),
        min_notional=Decimal("10"), contract_size=Decimal("1"),
    )

    adapter = build_execution_adapter(settings, contract_spec)

    assert isinstance(adapter, OrangeXAdapter)
    assert adapter._ws_client is not None  # watch_fills()가 쓸 WS 클라이언트도 같이 구성됨
