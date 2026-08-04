"""대화형 실행 메뉴 — run.bat가 실행한다.

컴퓨터/개발에 익숙하지 않은 사용자가 매번 `.env` 파일을 직접 열어 TRADING_MODE/
MANUAL_MODE를 고치지 않고도, 실행할 때마다 물어봐서 그 값을 env var로 덮어써 준다
(pydantic-settings는 환경변수를 .env 파일보다 우선한다). 개발자는 이 메뉴 없이
`python main.py`를 직접 실행해도 동일하게 동작한다 — 이 스크립트는 순전히 사용성을
위한 얇은 래퍼다.
"""
from __future__ import annotations

import os


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


def main() -> None:
    print("=" * 50)
    print("           코인 자동매매 봇")
    print("=" * 50)

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


if __name__ == "__main__":
    main()
