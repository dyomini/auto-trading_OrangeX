"""SPEC.md 4번 섹션의 .env 스키마를 그대로 로드하는 설정.

API_KEY/API_SECRET은 SecretStr로 감싸 로그·repr에 절대 노출되지 않도록 한다.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    exchange: Literal["orangex"] = "orangex"
    trading_mode: Literal["paper", "live"] = "paper"
    api_key: SecretStr = SecretStr("")
    api_secret: SecretStr = SecretStr("")
    # OrangeX 실제 instrument_name 표기(라이브 검증됨, docs/api-notes.md 다수 항목) — 흔히
    # 쓰는 축약형 "BTC-USDT-PERP"이 아니다. strategy/market_data.py의 바이낸스 심볼
    # 매핑 키도 이 표기를 그대로 쓴다.
    symbol: str = "BTC-USDT-PERPETUAL"
    direction: Literal["long", "short"] = "long"
    equity_usdt: Decimal = Decimal("10000")
    leverage: Decimal = Decimal("20")
    grid_tick: Decimal = Decimal("50")
    max_stage: int = 3
    daily_loss_limit_pct: Optional[Decimal] = None
    cooldown_minutes: int = 30
    max_open_grid_orders: int = 5
    # docs/phase2-report.md §1 라이브 확정치 (Phase 1의 taker=0.05% 가정을 대체)
    maker_fee: Decimal = Decimal("0.0002")
    taker_fee: Decimal = Decimal("0.0006")
    # SPEC에 폴링 주기가 명시돼 있지 않아 임의로 정한 기본값 (engine/entry_scheduler.py 참고)
    rsi_poll_interval_seconds: int = 3600
    # hybrid reset 조건 확인 + PaperAdapter 체결 시뮬레이션용 현재가 폴링 주기 (main.py 참고).
    # OrangeX 레이트리밋 10 req/s(docs/api-notes.md §6 항목6) 대비 여유 있게 임의로 정한 기본값.
    price_poll_interval_seconds: int = 5
    # docs/phase1-report.md 확정값(엑셀 원본에서 역산/확인, 2026-07-27) — compute_grid()의
    # 청산가/SL 계산에 필요. maint_margin_rate는 유지증거금률, sl_pct는 SL 가격 계산용 비율.
    maint_margin_rate: Decimal = Decimal("0.005")
    sl_pct: Decimal = Decimal("0.03")
    # COOLDOWN 진입을 얼마나 자주 확인할지(engine/cycle_manager.py) — SPEC에 명시 없어
    # 임의로 정한 기본값. cooldown_minutes 자체와는 다른 값(그건 COOLDOWN 진입 "후" 대기 시간).
    cycle_manager_poll_interval_seconds: int = 10
    # halted 상태의 재시작 후 영속화용 플래그 파일 경로 (engine/halt_flag.py 참고).
    halt_flag_path: str = "state/halted.json"
    # 2026-08-04 사용자 요청: RSI 진입 필터를 건너뛰고 현재가 기준으로 즉시 격자
    # 진입(매수/매도 체결)만 자동화하고, TP/SL 재등록·hybrid reset 등 청산 관련
    # 자동화는 전부 끈 채 사용자가 거래소에서 직접 수동으로 관리하는 모드.
    # 기본값 False(기존 완전자동 동작 그대로 유지).
    manual_mode: bool = False

    @field_validator("daily_loss_limit_pct", mode="before")
    @classmethod
    def _empty_string_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v
