"""최초 설치 마법사 — install.bat가 패키지 설치까지 끝낸 뒤 마지막 단계로 실행한다.

배치 파일(.bat) 안에 한글 텍스트를 넣으면 cmd.exe의 "chcp 65001이 파일 중간부터는
제대로 안 먹는" 고질적인 버그 때문에 화면이 깨질 수 있어(실제로 겪음), 사용자에게
보여줄 한글 안내는 전부 이 스크립트로 옮겼다 — Python은 콘솔에 한글을 문제없이 출력한다.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ENV_PATH = Path(".env")
EXAMPLE_PATH = Path(".env.example")


def main() -> None:
    print("=" * 50)
    print(" 코인 자동매매 봇 - 최초 설정")
    print("=" * 50)

    if ENV_PATH.exists():
        print("\n설정 파일(.env)이 이미 있어서 이 단계는 건너뜁니다.")
        print("설정을 다시 하고 싶으면 .env 파일을 메모장으로 직접 열어 수정하세요.")
    else:
        shutil.copy(EXAMPLE_PATH, ENV_PATH)
        print("\n설정 파일(.env)을 새로 만들었습니다.")
        print("잠시 후 메모장이 열리면 아래 두 항목을 채워주세요:\n")
        print("  API_KEY      : 거래소(OrangeX)에서 발급받은 API 키")
        print("  API_SECRET   : 거래소에서 발급받은 API 시크릿")
        print("\n(API 키를 발급받는 방법은 README.md의 '2. 최초 설정' 부분을 참고하세요)")
        print("나머지 항목은 일단 기본값 그대로 두어도 됩니다.")
        print("\n다 채웠으면 메모장에서 저장(Ctrl+S)하고 창을 닫아주세요.")
        input("\n준비되면 Enter 키를 누르세요...")
        subprocess.run(["notepad.exe", str(ENV_PATH)])

    print("\n" + "=" * 50)
    print(" 설치가 완료되었습니다!")
    print(' 이제 "run.bat" 파일을 더블클릭해서 봇을 시작할 수 있습니다.')
    print(" (처음에는 반드시 '연습 모드'부터 사용해서 동작을 확인해주세요)")
    print("=" * 50)


if __name__ == "__main__":
    main()
