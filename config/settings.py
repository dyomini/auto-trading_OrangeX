"""SPEC.md 4번 섹션의 .env 스키마를 그대로 로드하는 설정.

API_KEY/API_SECRET은 SecretStr로 감싸 로그·repr에 절대 노출되지 않도록 한다.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.presets import GridPresetName, resolve_preset


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
    # "both"(2026-08-04 사용자 요청, 롱/숏 동시 운용)면 main.py의 run()이 equity_usdt를
    # 반씩 나눠(각자 EQUITY_USDT/2) 롱용/숏용 GridEngine 스택을 완전히 독립적으로 하나의
    # 프로세스 안에서 같이 돌린다 — 서로의 주문/체결/포지션을 절대 침범하지 않는다
    # (OrangeXAdapter.get_position/get_open_orders의 position_side 필터링, 아래 참고).
    # "auto"(2026-08-17 사용자 요청)는 사이클마다 15분봉 RSI(14)로 방향을 다시 정한다
    # (>=50 숏, <50 롱). equity 분할 없이 전액을 한 방향에 쓰고, 방향이 바뀌면 어댑터
    # 스택을 통째로 재조립한다(main._run_auto_direction). manual_mode와 병용 불가.
    direction: Literal["long", "short", "both", "auto"] = "long"
    equity_usdt: Decimal = Decimal("10000")
    leverage: Decimal = Decimal("20")
    grid_tick: Decimal = Decimal("50")
    # 격자 프리셋(2026-08-17). 지정하면 max_stage/leverage를 config/presets.py의 값으로
    # **덮어쓴다** — 아래 _apply_grid_preset 참고. None이면 기존처럼 아래 두 값을 그대로 쓴다.
    grid_preset: Optional[GridPresetName] = None
    max_stage: int = 3
    # 4~5차 진입 시 거래소 SL 필수 등록(SPEC 원안, 100단계/5-tier 풀 구조 기준 확정값).
    # max_stage를 3으로 낮춰 쓰면(예: 제까깟-마틴게이-3k.xlsx 3-tier 압축 설계) major_tier가
    # 4에 절대 도달하지 못해 이 기본값 그대로면 SL이 영원히 등록되지 않는다 — 2026-08-04
    # 사용자 확인 하에 3-tier 운용에서는 .env에서 3으로 낮춰 쓴다(xlsx의 "3차 필수 SL" 규칙).
    mandatory_sl_min_tier: int = 4
    # False면 거래소 SL(STOP 주문)을 아예 등록하지 않는다 (2026-08-17 사용자 결정).
    # SPEC Phase 3의 "4~5차 SL 필수"에서 벗어나는 설정이며, 이 경우 격자 최심 주문가와
    # 청산가 사이의 완충(3k 프리셋 기준 약 2%p)이 유일한 방어선이 된다.
    sl_enabled: bool = True
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

    @model_validator(mode="after")
    def _apply_grid_preset(self) -> "Settings":
        """`grid_preset`이 지정되면 `max_stage`/`leverage`를 프리셋 값으로 덮어쓴다.

        여기서 한 번에 해결하는 이유: 두 값을 읽는 곳이 여러 군데
        (`engine/grid_setup.py`, `main.py`, `quick_entry.py`)라 각자 프리셋을 해석하게
        하면 언젠가 한 곳이 빠져서 조용히 어긋난다. 설정 객체가 이미 해석된 값을 들고
        있으면 read site는 프리셋의 존재 자체를 몰라도 된다."""
        if self.grid_preset is None:
            return self
        max_stage, leverage = resolve_preset(self.grid_preset, self.grid_tick)
        # model_validator(mode="after")에서의 대입은 재검증을 다시 트리거하지 않는다.
        object.__setattr__(self, "max_stage", max_stage)
        object.__setattr__(self, "leverage", leverage)
        return self
