"""대화형 실행 메뉴 — run.bat가 실행한다.

컴퓨터/개발에 익숙하지 않은 사용자가 매번 `.env` 파일을 직접 열어 TRADING_MODE/
MANUAL_MODE를 고치지 않고도, 실행할 때마다 물어봐서 그 값을 env var로 덮어써 준다
(pydantic-settings는 환경변수를 .env 파일보다 우선한다). 개발자는 이 메뉴 없이
`python main.py`를 직접 실행해도 동일하게 동작한다 — 이 스크립트는 순전히 사용성을
위한 얇은 래퍼다.

2026-08-05: "즉시 진입"(숏!/롱!) 메뉴 추가 — `quick_entry.py`를 감싸서, 현재가부터
지정가 주문 여러 개를 한 번에 걸어놓는 수동 진입을 봇 자동매매와 별개로 실행할 수
있게 한다.
"""
from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation


def _ask_choice(prompt: str, options: dict[str, str], default: str) -> str:
    print(prompt)
    for key, label in options.items():
        marker = " (기본값)" if key == default else ""
        print(f"  {key}. {label}{marker}")
    choice = input("번호를 입력하고 Enter를 누르세요: ").strip()
    return choice if choice in options else default


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
    confirm = input('\n정말 실전 매매를 시작하려면 "실행" 이라고 입력하세요: ').strip()
    if confirm != "실행":
        print("\n취소되었습니다.")
        return ""

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
    trading_mode = _configure_mode()
    if not trading_mode:
        return
    _configure_exit_style()

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
    raw = input(f"{prompt} (숫자만, 기본값 {default}): ").strip()
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
    from config.settings import Settings
    from quick_entry import QuickEntryError, compute_chunk_count

    print("\n[즉시 진입] 현재가부터 지정가 매수/매도 주문을 한 번에 여러 개 걸어놓습니다.")
    print(" 진입만 자동으로 걸립니다 — 익절/손절/청산은 반드시 거래소에서 직접 관리하세요.\n")

    direction_choice = _ask_choice(
        "방향을 선택하세요.",
        {"1": "숏 (매도 진입)", "2": "롱 (매수 진입)"},
        default="1",
    )
    direction = "short" if direction_choice == "1" else "long"

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

    settings_preview = Settings()
    try:
        num_chunks = compute_chunk_count(settings_preview, price_range_usdt)
    except QuickEntryError as e:
        print(f"\n실행하지 못했습니다: {e}")
        return
    total_margin = settings_preview.quick_entry_chunk_usdt * num_chunks
    print(
        f"\n-> 주문 {num_chunks}개, 개당 증거금 {settings_preview.quick_entry_chunk_usdt} USDT "
        f"(총 증거금 {total_margin} USDT, 레버리지 {settings_preview.leverage}x)"
    )

    trading_mode = _configure_mode()
    if not trading_mode:
        return

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
    # 화면에 실제로 보이려면 로깅을 켜야 한다 — main.py의 main()과 동일한 설정.
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def _go() -> None:
        from engine.grid_setup import build_execution_adapter, build_market_data_adapter
        from exchange.paper import PaperAdapter
        from quick_entry import run_quick_entry

        settings = Settings()
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

    action_choice = _ask_choice(
        "\n[0] 무엇을 할까요?",
        {
            "1": "봇 실행 (자동매매)",
            "2": "즉시 진입 (숏!/롱! - 지금 바로 지정가 주문 쌓기)",
        },
        default="1",
    )
    if action_choice == "2":
        _run_quick_entry()
    else:
        _run_bot()


if __name__ == "__main__":
    main()
