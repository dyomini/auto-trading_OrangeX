"""launcher.py 유닛 테스트.

2026-08-06 실전 사고 회귀 테스트: `_run_quick_entry()`의 `_go()`가 실행용 어댑터에
넘기는 `settings`를 만들 때 `leverage`/`direction`만 오버라이드하고 `trading_mode`를
빠뜨렸다. `base_settings`는 방향/범위/레버리지를 묻기도 전에(즉 `_configure_mode()`가
`os.environ["TRADING_MODE"]="live"`를 설정하기 전에) `Settings()`로 딱 한 번 읽혀서
고정되므로, 사용자가 "실전 매매"를 고르고 "실행"을 두 번 입력해 확인해도 실제로는
항상 `.env`의 원래 값(대개 `paper`)으로 `PaperAdapter`가 조용히 실행됐다 — 화면엔
"주문 N개 접수 완료"가 정상적으로 뜨지만 거래소엔 아무 일도 일어나지 않는, 가장
발견하기 어려운 종류의 사고였다(로그 파일의 order_id가 UUID 형식인 걸 보고서야
PaperAdapter가 쓰였다는 걸 알아챘다 — 실제 OrangeX order_id는 순수 숫자 문자열).
"""
from __future__ import annotations

import io
from decimal import Decimal

import pytest

import launcher
from exchange.base import ContractSpec, Ticker


class _FakeMarketDataAdapter:
    async def get_contract_spec(self, symbol: str) -> ContractSpec:
        return ContractSpec(
            instrument=symbol, tick_size=Decimal("0.1"), min_qty=Decimal("0.001"),
            min_notional=Decimal("10"), contract_size=Decimal("1"), qty_step=Decimal("0.001"),
        )

    async def get_ticker(self, symbol: str) -> Ticker:
        return Ticker(instrument=symbol, last_price=Decimal("64000"))


class _DummyExecutionAdapter:
    """PaperAdapter가 아닌 무언가라는 것만 표시하는 더미 — isinstance(x, PaperAdapter)
    체크를 우회해 on_price_tick 프라이밍 경로를 안 타게 한다(실전 경로 흉내)."""


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQUITY_USDT", "1000")
    monkeypatch.setenv("LEVERAGE", "20")
    monkeypatch.setenv("GRID_TICK", "50")
    monkeypatch.setenv("API_KEY", "dummy")
    monkeypatch.setenv("API_SECRET", "dummy")
    monkeypatch.setenv("TRADING_MODE", "paper")  # 시작값은 paper — 위저드가 live로 바꿔야 한다


def test_selecting_live_mode_actually_passes_live_trading_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)

    captured_settings: list = []

    def fake_build_execution_adapter(settings, contract_spec):
        captured_settings.append(settings)
        return _DummyExecutionAdapter()

    def fake_build_market_data_adapter(settings):
        return _FakeMarketDataAdapter()

    async def fake_run_quick_entry(settings, direction, price_range_usdt, adapter, contract_spec):
        captured_settings.append(settings)
        return ["25000999999"]

    monkeypatch.setattr("engine.grid_setup.build_execution_adapter", fake_build_execution_adapter)
    monkeypatch.setattr("engine.grid_setup.build_market_data_adapter", fake_build_market_data_adapter)
    monkeypatch.setattr("quick_entry.run_quick_entry", fake_run_quick_entry)

    # 방향(1=숏) -> 범위(3=직접입력, 250) -> 레버리지(기본값) -> 모드(2=실전) ->
    # 확인1("실행") -> 확인2("실행")
    lines = "1\n3\n250\n\n2\n실행\n실행\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(lines))

    launcher._run_quick_entry()

    assert len(captured_settings) == 2  # build_execution_adapter 1회 + run_quick_entry 1회
    assert all(s.trading_mode == "live" for s in captured_settings), (
        "실전 매매를 선택했는데 실행용 settings.trading_mode가 live가 아님 — "
        "PaperAdapter로 조용히 실행되는 2026-08-06 사고가 재발했음"
    )


def test_selecting_paper_mode_passes_paper_trading_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)

    captured_settings: list = []

    def fake_build_execution_adapter(settings, contract_spec):
        captured_settings.append(settings)
        from exchange.paper import PaperAdapter
        return PaperAdapter(
            instrument=settings.symbol, contract_spec=contract_spec, initial_equity=settings.equity_usdt,
            leverage=settings.leverage, maker_fee=settings.maker_fee, taker_fee=settings.taker_fee,
        )

    def fake_build_market_data_adapter(settings):
        return _FakeMarketDataAdapter()

    async def fake_run_quick_entry(settings, direction, price_range_usdt, adapter, contract_spec):
        captured_settings.append(settings)
        return ["fake-paper-order"]

    monkeypatch.setattr("engine.grid_setup.build_execution_adapter", fake_build_execution_adapter)
    monkeypatch.setattr("engine.grid_setup.build_market_data_adapter", fake_build_market_data_adapter)
    monkeypatch.setattr("quick_entry.run_quick_entry", fake_run_quick_entry)

    # 방향(1=숏) -> 범위(3=직접입력, 250) -> 레버리지(기본값) -> 모드(1=연습, 기본값)
    lines = "1\n3\n250\n\n1\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(lines))

    launcher._run_quick_entry()

    assert len(captured_settings) == 2
    assert all(s.trading_mode == "paper" for s in captured_settings)
