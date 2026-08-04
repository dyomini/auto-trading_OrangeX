"""대화형 실행 메뉴 — run.bat가 실행한다.

컴퓨터/개발에 익숙하지 않은 사용자가 매번 `.env` 파일을 직접 열어 TRADING_MODE/
MANUAL_MODE를 고치지 않고도, 실행할 때마다 물어봐서 그 값을 env var로 덮어써 준다
(pydantic-settings는 환경변수를 .env 파일보다 우선한다). 개발자는 이 메뉴 없이
`python main.py`를 직접 실행해도 동일하게 동작한다 — 이 스크립트는 순전히 사용성을
위한 얇은 래퍼다.

2026-08-05: "즉시 진입"(숏!/롱!) 메뉴 추가 — `quick_entry.py`를 감싸서, 현재가부터
지정가 주문 여러 개를 한 번에 걸어놓는 수동 진입을 봇 자동매매와 별개로 실행할 수
있게 한다.

2026-08-05: 메뉴 선택을 번호 입력 대신 방향키(↑/↓) + Enter로 고를 수 있게 함(사용자
요청). 실제 콘솔에 붙어 있을 때만(`sys.stdin.isatty()`) 방향키 모드로 동작하고,
파이프로 입력을 리다이렉트한 경우(자동화/테스트)는 기존 번호 입력 방식으로 자동
폴백한다 — 개발자가 `python main.py`를 직접 쓰거나 테스트에서 stdin을 주입하는
경로를 그대로 유지하기 위함.

2026-08-05: 잘못 골랐을 때 이전 단계로 돌아갈 수 있게 함(사용자 요청). 방향키
메뉴는 ←, 번호 메뉴는 "0", 텍스트 입력은 "b"를 누르면 `_GoBack`이 발생하고
`_run_bot()`/`_run_quick_entry()`의 단계 루프가 이를 잡아 바로 전 단계를 다시
묻는다. 각 흐름의 첫 단계에서 뒤로가면 최상위 "무엇을 할까요?" 메뉴로 돌아간다
(`main()`이 잡음).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from decimal import Decimal, InvalidOperation

try:
    import msvcrt
except ImportError:  # Windows 전용 — 이 프로젝트는 run.bat/launcher.py로 Windows에서만
    msvcrt = None    # 배포되므로(README 참고) 다른 OS에서는 번호 입력으로만 동작한다.


class _GoBack(Exception):
    """사용자가 방금 프롬프트를 취소하고 이전 단계로 돌아가길 원할 때."""


def _enable_vt_mode() -> None:
    """Windows 콘솔에서 커서 이동 ANSI 이스케이프(화살표 메뉴 다시 그리기용)가
    먹히도록 VT100 처리를 켠다. 최신 Windows는 대부분 기본으로 켜져 있지만, 혹시
    꺼져 있어도 메뉴가 아예 못 쓰게 되진 않도록(최악의 경우 이스케이프 문자가 그대로
    보일 뿐) 실패해도 조용히 넘어간다."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def _ask_choice_by_number(prompt: str, options: dict[str, str], default: str) -> str:
    """기존 번호 입력 방식 — 실제 콘솔이 아닐 때(파이프/테스트)의 폴백."""
    print(prompt)
    for key, label in options.items():
        marker = " (기본값)" if key == default else ""
        print(f"  {key}. {label}{marker}")
    print("  0. (이전 단계로 돌아가기)")
    choice = input("번호를 입력하고 Enter를 누르세요: ").strip()
    if choice == "0":
        raise _GoBack
    return choice if choice in options else default


def _ask_choice_by_arrows(prompt: str, options: dict[str, str], default: str) -> str:
    """방향키(↑/↓)로 항목을 옮기고 Enter로 확정한다. ←는 이전 단계로 돌아간다."""
    keys = list(options.keys())
    selected = keys.index(default) if default in keys else 0

    _enable_vt_mode()
    print(prompt)
    print("(←로 이전 단계, ↑/↓로 이동, Enter로 선택)")
    for _ in keys:
        print()  # 아래에서 덮어쓸 자리를 미리 확보

    def render() -> None:
        sys.stdout.write(f"\x1b[{len(keys)}A")
        for i, key in enumerate(keys):
            marker = "> " if i == selected else "  "
            default_tag = " (기본값)" if key == default else ""
            sys.stdout.write("\x1b[K" + f"{marker}{key}. {options[key]}{default_tag}\n")
        sys.stdout.flush()

    render()
    while True:
        ch = msvcrt.getch()
        if ch in (b"\xe0", b"\x00"):  # 화살표 등 기능키의 첫 바이트
            ch2 = msvcrt.getch()
            if ch2 == b"H":  # Up
                selected = (selected - 1) % len(keys)
                render()
            elif ch2 == b"P":  # Down
                selected = (selected + 1) % len(keys)
                render()
            elif ch2 == b"K":  # Left
                print()
                raise _GoBack
        elif ch in (b"\r", b"\n"):
            print()
            return keys[selected]
        elif ch == b"\x03":  # Ctrl+C
            raise KeyboardInterrupt


def _ask_choice(prompt: str, options: dict[str, str], default: str) -> str:
    if msvcrt is not None and sys.stdin.isatty():
        return _ask_choice_by_arrows(prompt, options, default)
    return _ask_choice_by_number(prompt, options, default)


def _configure_mode() -> str:
    mode_choice = _ask_choice(
        "\n[1] 실행할 모드를 선택하세요.",
        {
            "1": "연습 모드 (가상 자금 - 실제 돈 사용 안 함)",
            "2": "실전 매매 (실제 돈 사용 - 신중하게 선택하세요)",
        },
        default="1",
    )

    if mode_choice != "2":
        os.environ["TRADING_MODE"] = "paper"
        return "paper"

    print("\n" + "!" * 50)
    print("  경고: 실전 매매는 실제 돈으로 거래소에 주문을 넣습니다.")
    print("  반드시 연습 모드로 충분히 결과를 확인한 뒤에만 사용하세요.")
    print("!" * 50)
    confirm = input(
        '\n정말 실전 매매를 시작하려면 "실행" 이라고 입력하세요 '
        "(그 외 입력하면 이전 단계로 돌아갑니다): "
    ).strip()
    if confirm != "실행":
        print("\n확인되지 않아 이전 단계로 돌아갑니다.")
        raise _GoBack

    os.environ["TRADING_MODE"] = "live"
    return "live"


def _configure_exit_style() -> None:
    exit_choice = _ask_choice(
        "\n[2] 청산(익절/손절) 방식을 선택하세요.",
        {
            "1": "완전 자동 (조건 확인 후 진입, 익절/손절/청산까지 전부 봇이 처리)",
            "2": "진입만 자동, 청산은 직접 (조건 확인 없이 즉시 매수/매도만 자동, 익절/손절은 거래소에서 직접 설정)",
        },
        default="1",
    )
    os.environ["MANUAL_MODE"] = "true" if exit_choice == "2" else "false"
    if exit_choice == "2":
        print("\n익절/손절은 봇이 걸지 않습니다 — 반드시 거래소 앱에서 직접 설정하세요.")


def _run_bot() -> None:
    """[모드] -> [청산 방식] 순으로 묻는다. 첫 단계(모드)에서 뒤로가면 이 함수
    자체를 빠져나가 main()이 최상위 메뉴로 되돌린다."""
    STEP_MODE, STEP_EXIT_STYLE, STEP_DONE = range(3)
    step = STEP_MODE
    trading_mode = "paper"

    while step != STEP_DONE:
        try:
            if step == STEP_MODE:
                trading_mode = _configure_mode()
                step = STEP_EXIT_STYLE
            elif step == STEP_EXIT_STYLE:
                _configure_exit_style()
                step = STEP_DONE
        except _GoBack:
            if step == STEP_MODE:
                raise
            step -= 1

    print("\n" + "=" * 50)
    print(" 시작합니다. 화면에 계속 진행 상황이 표시됩니다.")
    print(" 끄고 싶으면 이 창에서 Ctrl+C 를 누르세요.")
    print("=" * 50 + "\n")

    from main import main as run_bot

    try:
        run_bot()
    except Exception as e:  # noqa: BLE001 - 사용자에게 원인을 그대로 보여주기 위함
        print("\n" + "!" * 50)
        print(" 문제가 발생해서 봇이 멈췄습니다.")
        print(f" 오류 내용: {e!r}")
        print("!" * 50)
    finally:
        print("\n프로그램이 종료되었습니다.")
        if trading_mode == "live":
            print("실전 매매 중이었습니다 — 거래소 앱에서 실제 포지션/미체결 주문 상태를 꼭 확인해주세요.")


def _ask_amount(prompt: str, default: Decimal) -> Decimal:
    raw = input(f"{prompt} (숫자만 입력, 'b'=이전 단계, 기본값 {default}): ").strip()
    if raw.lower() in ("b", "뒤로"):
        raise _GoBack
    if not raw:
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation:
        print(f"숫자로 인식하지 못해 기본값({default})을 사용합니다.")
        return default
    if value <= 0:
        print(f"0보다 커야 해서 기본값({default})을 사용합니다.")
        return default
    return value


def _run_quick_entry() -> None:
    """[방향] -> [진입 범위] -> [레버리지] -> [미리보기] -> [모드+실행] 순으로 묻는다.
    각 단계에서 뒤로가면 바로 전 단계를 다시 묻고, 첫 단계(방향)에서 뒤로가면 이
    함수 자체를 빠져나가 main()이 최상위 메뉴로 되돌린다. 미리보기 계산이 실패하면
    (가격 범위가 너무 작거나 커서) 진입 범위 단계로 돌려보낸다."""
    from config.settings import Settings
    from quick_entry import QuickEntryError, compute_chunk_count, compute_preview_rows

    print("\n[즉시 진입] 현재가부터 지정가 매수/매도 주문을 한 번에 여러 개 걸어놓습니다.")
    print(" 진입만 자동으로 걸립니다 — 익절/손절/청산은 반드시 거래소에서 직접 관리하세요.")
    print(" 증거금은 균등 분배가 아니라 config/weights.csv 비중대로(마틴게일 설계 그대로)")
    print(" EQUITY_USDT 전액을 배분합니다 — 한 번 실행할 때마다 이 방향에 배정된 자금이")
    print(" 전부 소진됩니다.\n")

    base_settings = Settings()

    STEP_DIRECTION, STEP_RANGE, STEP_LEVERAGE, STEP_PREVIEW, STEP_MODE, STEP_DONE = range(6)
    step = STEP_DIRECTION
    direction = "short"
    price_range_usdt = Decimal("3000")
    leverage = base_settings.leverage
    num_chunks = 0
    total_margin = Decimal("0")
    trading_mode = "paper"

    while step != STEP_DONE:
        try:
            if step == STEP_DIRECTION:
                direction_choice = _ask_choice(
                    "방향을 선택하세요.",
                    {"1": "숏 (매도 진입)", "2": "롱 (매수 진입)"},
                    default="1",
                )
                direction = "short" if direction_choice == "1" else "long"
                step = STEP_RANGE

            elif step == STEP_RANGE:
                range_choice = _ask_choice(
                    "\n진입 범위(현재가 기준 ±USDT — 이 범위까지 격자 간격으로 주문을 깝니다)를 선택하세요.",
                    {"1": "3,000 USDT", "2": "5,000 USDT", "3": "직접 입력"},
                    default="1",
                )
                if range_choice == "1":
                    price_range_usdt = Decimal("3000")
                elif range_choice == "2":
                    price_range_usdt = Decimal("5000")
                else:
                    price_range_usdt = _ask_amount("진입 범위(현재가 기준 ±USDT)를 입력하세요", Decimal("3000"))
                step = STEP_LEVERAGE

            elif step == STEP_LEVERAGE:
                leverage = _ask_amount(
                    "\n레버리지(배)를 입력하세요 (.env의 LEVERAGE 값과 별개로 이번 실행에만 적용됩니다)",
                    base_settings.leverage,
                )
                step = STEP_PREVIEW

            elif step == STEP_PREVIEW:
                settings_preview = base_settings.model_copy(update={"leverage": leverage})
                try:
                    num_chunks = compute_chunk_count(settings_preview, price_range_usdt)
                    preview_rows = compute_preview_rows(settings_preview, num_chunks)
                except QuickEntryError as e:
                    print(f"\n실행하지 못했습니다: {e}")
                    print("진입 범위를 다시 선택해주세요.")
                    step = STEP_RANGE
                    continue
                total_margin = sum(row.step_margin for row in preview_rows)
                first_margin = preview_rows[0].step_margin
                last_margin = preview_rows[-1].step_margin
                print(
                    f"\n-> 주문 {num_chunks}개, 단계당 증거금 {first_margin} ~ {last_margin} USDT "
                    f"(총 증거금 {total_margin} USDT, 레버리지 {settings_preview.leverage}x)"
                )
                step = STEP_MODE

            elif step == STEP_MODE:
                trading_mode = _configure_mode()
                step = STEP_DONE
        except _GoBack:
            if step == STEP_DIRECTION:
                raise
            step -= 1

    if trading_mode == "live":
        print("\n" + "!" * 50)
        print(f"  경고: {direction} 방향으로 주문 {num_chunks}개(총 증거금 {total_margin} USDT)를 실제로 겁니다.")
        print("!" * 50)
        confirm = input('정말 실행하려면 "실행" 이라고 입력하세요: ').strip()
        if confirm != "실행":
            print("\n취소되었습니다.")
            return

    print("\n주문을 거는 중입니다...\n")
    # quick_entry.py가 주문마다 남기는 레버리지/체결가/진입 마진 로그(사용자 요청)가
    # 화면에 보이도록 로깅을 켠다 — order_id/INFO: 같은 잡음은 안 보이게 메시지만
    # 그대로 출력하는 포맷을 쓴다(사용자 요청).
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def _go() -> None:
        from engine.grid_setup import build_execution_adapter, build_market_data_adapter
        from exchange.paper import PaperAdapter
        from quick_entry import run_quick_entry

        settings = base_settings.model_copy(update={"leverage": leverage})
        market_data_adapter = build_market_data_adapter(settings)
        contract_spec = await market_data_adapter.get_contract_spec(settings.symbol)
        execution_adapter = build_execution_adapter(settings, contract_spec)
        if isinstance(execution_adapter, PaperAdapter):
            # 연습 모드는 main.py의 상시 가격 관찰 루프가 없어 PaperAdapter가 아직
            # 현재가를 모른다(get_ticker가 NoKnownPriceError) — 실행 직전에 한 번
            # 현재가를 주입해준다.
            ticker = await market_data_adapter.get_ticker(settings.symbol)
            await execution_adapter.on_price_tick(ticker.last_price)
        try:
            order_ids = await run_quick_entry(settings, direction, price_range_usdt, execution_adapter)
        except QuickEntryError as e:
            print(f"\n실행하지 못했습니다: {e}")
            return
        print(f"\n주문 {len(order_ids)}개 접수 완료.")

    try:
        asyncio.run(_go())
    except Exception as e:  # noqa: BLE001 - 사용자에게 원인을 그대로 보여주기 위함
        print("\n" + "!" * 50)
        print(" 문제가 발생했습니다.")
        print(f" 오류 내용: {e!r}")
        print("!" * 50)
    finally:
        print("\n프로그램이 종료되었습니다.")
        if trading_mode == "live":
            print("실전 매매였습니다 — 거래소 앱에서 실제 포지션/미체결 주문 상태를 꼭 확인해주세요.")


def main() -> None:
    print("=" * 50)
    print("           코인 자동매매 봇")
    print("=" * 50)

    while True:
        try:
            action_choice = _ask_choice(
                "\n[0] 무엇을 할까요?",
                {
                    "1": "봇 실행 (자동매매)",
                    "2": "즉시 진입 (숏!/롱! - 지금 바로 지정가 주문 쌓기)",
                },
                default="1",
            )
        except _GoBack:
            continue  # 맨 처음 메뉴라 더 돌아갈 곳이 없음 — 그냥 다시 보여줌

        try:
            if action_choice == "2":
                _run_quick_entry()
            else:
                _run_bot()
        except _GoBack:
            continue  # 하위 흐름의 첫 단계에서 뒤로가기 -> 최상위 메뉴로
        break


if __name__ == "__main__":
    main()
