"""테스트 전역 설정.

**개발 머신의 `.env`가 테스트 결과를 바꾸지 못하게 막는다.**

`Settings`는 `SettingsConfigDict(env_file=".env")`라, kwargs로 안 넘긴 필드를 CWD의
`.env`에서 읽어온다. 테스트들이 `Settings(**일부만)` 형태로 객체를 만들기 때문에
나머지 필드가 실행하는 사람의 `.env` 값으로 조용히 채워졌다.

이게 두 번 사고를 냈다(둘 다 2026-08-17 발견):
  1. 로컬 `.env`의 `MANUAL_MODE=TRUE`가 흘러들어와 엔진이 SL 등록을 통째로 건너뛰었고,
     그래서 `EngineHaltedError`가 발생하지 않아 halt flag 테스트가 타임아웃했다.
     이건 오랫동안 "Python 3.14의 asyncio 문제"로 잘못 기록돼 있었다.
  2. `.env`에 `GRID_PRESET=3k`를 넣자, 프리셋이 leverage/max_stage를 덮어써서
     "프리셋 없이 명시값을 유지하는지" 검증하는 테스트가 깨졌다.

둘 다 코드 버그가 아니라 테스트 격리 문제였다. 여기서 `.env` 로딩 자체를 끄면
같은 유형이 다시 생기지 않는다 — 테스트는 항상 코드의 기본값 + 명시적으로 넘긴
값으로만 동작한다.
"""
from __future__ import annotations

import pytest

from config.settings import Settings


@pytest.fixture(autouse=True)
def isolate_settings_from_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
