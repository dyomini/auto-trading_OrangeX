"""halted 상태의 재시작 후 영속화 (docs/phase3-plan.md "아직 만들지 않은 것",
사용자 요청 2026-07-30).

`GridEngine.halted`(SL/TP 재등록 실패로 강제청산+정지)는 거래소 상태만으로는 재시작
후 정상 COOLDOWN과 구분할 수 없다(`engine/restart_recovery.py` "알려진 한계" 참고) —
둘 다 "포지션 flat, 미체결 주문 없음"으로 똑같이 보여서 `reconstruct_state()`가 그런
경우 전부 IDLE(재스카우팅 허용)로 복구해버린다. 즉 이 파일 없이는 halted로 정지한
봇이 재시작 한 번으로 조용히 다시 거래를 시작해버릴 수 있다.

그래서 halted가 되는 순간(`main.py`가 `EngineHaltedError`를 감지했을 때) 로컬 파일에
그 사실을 남기고, 다음 기동 시 그 파일이 있으면 시작 자체를 거부한다. 재개하려면
사람이 직접 거래소의 실제 포지션/미체결 주문을 확인하고 이 파일을 지워야 한다 —
`EngineHaltedError`가 이미 갖고 있던 설계 철학("재개하려면 사용자가 수동으로 상태를
확인해야 한다")을 재시작에도 그대로 연장한 것뿐이다.

파일 위치는 `Settings.halt_flag_path`(기본 `state/halted.json`)로 바꿀 수 있다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class HaltedFlagPresentError(Exception):
    """이전 실행이 halted 상태로 끝났고 아직 사람이 확인/정리하지 않았을 때 발생한다.
    호출부(main.py)는 이 예외를 잡아서 봇을 시작하지 말고 그대로 종료해야 한다."""


def check_halt_flag(path: str) -> None:
    flag_path = Path(path)
    if not flag_path.exists():
        return
    try:
        data = json.loads(flag_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    raise HaltedFlagPresentError(
        f"이전 실행이 halted 상태로 정지했음(플래그 파일: {flag_path}) — 봇을 시작하지 "
        "않는다. 거래소의 실제 포지션/미체결 주문을 수동으로 확인한 뒤, 문제 없으면 "
        f"이 파일을 지우고 재시작할 것. 기록된 정보: {data}"
    )


def write_halt_flag(path: str, reason: str) -> None:
    flag_path = Path(path)
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.write_text(
        json.dumps({"reason": reason, "timestamp": time.time()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clear_halt_flag(path: str) -> None:
    """사람이 확인 후 수동으로 재개할 때 쓸 수 있는 헬퍼 — 파일을 직접 지워도 동일하다."""
    Path(path).unlink(missing_ok=True)
