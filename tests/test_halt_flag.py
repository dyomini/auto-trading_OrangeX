"""engine/halt_flag.py 유닛 테스트. pytest의 tmp_path 픽스처로 실제 파일 I/O를 검증한다
(디스크에 쓰는 게 이 모듈의 핵심 동작이라 목킹하지 않는다)."""
from __future__ import annotations

import json

import pytest

from engine.halt_flag import HaltedFlagPresentError, check_halt_flag, clear_halt_flag, write_halt_flag


def test_check_halt_flag_passes_when_no_file(tmp_path):
    path = tmp_path / "halted.json"
    check_halt_flag(str(path))  # 예외 없이 통과해야 함


def test_write_then_check_halt_flag_raises(tmp_path):
    path = tmp_path / "state" / "halted.json"  # 중간 디렉터리도 자동 생성돼야 함

    write_halt_flag(str(path), "SL 등록 실패로 강제청산")

    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["reason"] == "SL 등록 실패로 강제청산"
    assert "timestamp" in data

    with pytest.raises(HaltedFlagPresentError, match="SL 등록 실패"):
        check_halt_flag(str(path))


def test_clear_halt_flag_removes_file_and_allows_check_to_pass(tmp_path):
    path = tmp_path / "halted.json"
    write_halt_flag(str(path), "test")

    clear_halt_flag(str(path))

    assert not path.exists()
    check_halt_flag(str(path))  # 다시 통과해야 함


def test_clear_halt_flag_is_safe_when_file_does_not_exist(tmp_path):
    path = tmp_path / "never_written.json"
    clear_halt_flag(str(path))  # 예외 없이 통과해야 함


def test_check_halt_flag_tolerates_corrupted_json(tmp_path):
    """읽는 쪽이 깨진 파일 때문에 죽으면 안 된다 — 그래도 halted 판정 자체는 유지."""
    path = tmp_path / "halted.json"
    path.write_text("not valid json{{{", encoding="utf-8")

    with pytest.raises(HaltedFlagPresentError):
        check_halt_flag(str(path))
