"""메인 자동매매 봇을 **연습(paper) 모드로** 원하는 설정으로 잠깐 돌려보는 테스트 도구.

`.env`를 전혀 건드리지 않는다 — 명령줄 인자로 준 값만 덮어써서 그 조합으로 봇을
조립하고, 정해진 시간 동안 돌린 뒤 상태 요약을 출력한다. `DIRECTION=auto`/`both`처럼
`launcher.py` 메뉴에 아직 선택지가 없는 모드를 시험해보는 게 주 용도다.

**주문은 전부 PaperAdapter(인메모리 시뮬레이터)로만 나간다** — 거래소로 실제 주문이
가지 않는다. 다만 현재가/계약스펙(OrangeX 공개 엔드포인트)과 캔들(바이낸스 공개 API)은
실제 값을 조회하므로, 진짜 시세 위에서 격자가 어떻게 깔리는지 그대로 볼 수 있다.

사용 예:
    python scripts/paper_run.py --direction auto --preset 3k --equity 1500 --seconds 30
    python scripts/paper_run.py --direction both --preset 3k --equity 3000 --seconds 60
    python scripts/paper_run.py --direction both --preset 1k --equity 300 --target 0.002

주의:
  - `--equity`는 가상 자금이지만 최소 주문 수량 검증은 **실제 계약 스펙**으로 하므로,
    프리셋별 최소 시드를 못 넘기면 시작을 거부한다(그때 필요한 금액을 알려준다).
  - `both`는 자금을 반씩 나눠 쓰므로 최소 시드가 단방향의 2배다.
  - `auto`는 `MANUAL_MODE=true`와 같이 못 쓴다(이 스크립트는 항상 false로 강제한다).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

# `python scripts/paper_run.py`로 바로 실행할 수 있도록 저장소 루트를 import 경로에 넣는다
# (pyproject.toml의 pythonpath 설정은 pytest 전용이라 일반 실행에는 적용되지 않는다).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings
from engine.grid_setup import StartupError
from main import run

logger = logging.getLogger("paper_run")


def _summarize(engines: list) -> None:
    if not engines:
        print("\n엔진이 조립되지 않았습니다 — 위 로그의 오류를 확인하세요.")
        return
    print(f"\n{'=' * 78}\n조립된 엔진 {len(engines)}개\n{'=' * 78}")
    for i, e in enumerate(engines, 1):
        top, bottom = e.grid_rows[0].entry_price, e.grid_rows[-1].entry_price
        margin = (
            e.open_qty * e.grid_rows[e.filled_step_count - 1].avg_price / Decimal("40")
            if e.filled_step_count else Decimal("0")
        )
        print(
            f"[{i}] 방향 {e.direction:5} | 상태 {e.state.value:10} | "
            f"격자 {len(e.grid_rows):>3}단계 ({top} ~ {bottom})"
        )
        print(
            f"    체결 {e.filled_step_count:>3}단계, 보유 {e.open_qty} BTC | "
            f"TP={'있음' if e.tp_order_id else '없음'} SL={'있음' if e.sl_order_id else '없음'} | "
            f"manual_mode={e.manual_mode} sl_enabled={e.sl_enabled}"
        )
        if e.contract_spec is not None:
            from exchange.base import round_qty_to_step
            sample = [
                str(round_qty_to_step(r.step_qty, e.contract_spec.qty_step))
                for r in e.grid_rows[:5]
            ]
            print(f"    주문 수량 샘플: {sample}  (qty_step={e.contract_spec.qty_step})")


async def _main(args: argparse.Namespace) -> None:
    overrides = {
        "trading_mode": "paper",   # 실주문 방지 — 이 스크립트에서는 절대 바꾸지 않는다
        "direction": args.direction,
        "manual_mode": False,      # auto와 병용 불가 + TP 자동을 봐야 하므로 항상 끈다
        "equity_usdt": Decimal(str(args.equity)),
        "price_poll_interval_seconds": args.poll,
    }
    if args.preset:
        overrides["grid_preset"] = args.preset
    if args.leverage:
        overrides["leverage"] = Decimal(str(args.leverage))
        overrides["grid_preset"] = None
    if args.target is not None:
        overrides["combined_tp_roe"] = Decimal(str(args.target))
    if args.sl:
        overrides["sl_enabled"] = True
    else:
        overrides["sl_enabled"] = False

    settings = Settings().model_copy(update=overrides)
    print(
        f"설정: mode=paper direction={settings.direction} preset={settings.grid_preset} "
        f"max_stage={settings.max_stage} leverage={settings.leverage} "
        f"equity={settings.equity_usdt} sl_enabled={settings.sl_enabled} "
        f"combined_tp={settings.combined_tp_roe}\n"
        f"{args.seconds}초 동안 실행합니다 (Ctrl+C로 조기 종료)\n"
    )

    engines: list = []
    task = asyncio.create_task(run(settings, on_engine_ready=engines.append))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=args.seconds)
    except asyncio.TimeoutError:
        print(f"\n{args.seconds}초 경과 — 종료합니다.")
    except StartupError as e:
        print(f"\n[시작 거부] {e}")
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 요약과 함께 보여준다
        print(f"\n[오류] {e!r}")
    finally:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    _summarize(engines)


def main() -> None:
    p = argparse.ArgumentParser(description="봇을 연습(paper) 모드로 잠깐 돌려보는 테스트 도구")
    p.add_argument("--direction", default="auto", choices=["long", "short", "auto", "both"])
    p.add_argument("--preset", default=None, choices=["3k", "5k"], help="격자 프리셋(레버리지 40배 고정)")
    p.add_argument("--leverage", type=float, default=None, help="프리셋 대신 레버리지 직접 지정")
    p.add_argument("--equity", type=float, default=1500, help="가상 운용 자금(USDT)")
    p.add_argument("--seconds", type=int, default=30, help="몇 초 동안 돌릴지")
    p.add_argument("--poll", type=int, default=3, help="현재가/손익 폴링 주기(초)")
    p.add_argument("--target", type=float, default=None, help="both 모드 합산 목표 수익률(예: 0.10)")
    p.add_argument("--sl", action="store_true", help="SL 등록을 켠다(기본은 끔)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\n사용자가 중단했습니다.")


if __name__ == "__main__":
    main()
