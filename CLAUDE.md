# CLAUDE.md

OrangeX(Deribit 계열 JSON-RPC 2.0 API) USDT 무기한 선물용 **마틴게일 격자 분할진입 봇**.
엑셀로 설계된 100단계(대단계 5 × 소단계 20) 격자 전략을 자동 실행한다.

## 작업 전에 반드시 읽을 것

| 문서 | 내용 |
|---|---|
| `SPEC.md` | 요구사항·절대 규칙·Phase 정의. **사용자 원문이라 수정 금지** |
| `docs/phase3-plan.md` | 실행 엔진 구현 현황 / 아직 만들지 않은 것 / 미해결 항목. **가장 최신 상태** |
| `docs/api-notes.md` | OrangeX API 조사 결과. 엔드포인트·필드 확정 내역 |
| `docs/phase1-report.md` | 격자 계산 검증 결과, 미달 단계 처리 정책 결정 |

`docs/phase3-plan.md`가 매 작업마다 갱신되는 진행 기록이다. 여기 요약은 스냅샷일 뿐이니
작업 시작 전 그 문서를 먼저 읽어라.

## 절대 규칙

**이 봇은 실제 돈을 잃을 수 있다. 추측으로 구현하지 마라.**

1. **추측 금지 (SPEC 0번)** — API 응답 필드나 엔드포인트 동작이 불확실하면 "아마 이럴 것이다"로
   채우지 말고 `OrangeXResponseSchemaError`로 명시적으로 막아라. 모르는 건 `TODO(질문):`으로
   남기고 보고해라.
2. **라이브 주문 금지 (SPEC 3번)** — 실제로 주문을 넣거나 상태를 바꾸는 호출은 사용자가
   명시적으로 요청하기 전엔 실행하지 마라. 조회(GET류)는 read-only라 자유롭게 실행해도 된다.
3. **`Decimal`만 사용** — 가격·수량·금액 계산에 `float` 금지. 반올림 오차가 곧 손실이다.
4. **`strategy/grid.py`의 `compute_grid()`는 골든 테스트(`tests/test_golden.py`, 엑셀 원본 대조)가
   지키는 핵심 재무 계산이다.** 반올림·보정 로직을 여기 넣지 말고 주문 직전 단계에서 처리해라.

## 개발 환경

```bash
pip install -r requirements.txt      # httpx, pydantic-settings, websockets, pytest
python -m pytest -q                  # pythonpath는 pyproject.toml에서 "." 로 설정됨
```

`.env`는 gitignore다(실제 API 키). `.env.example`을 복사해서 쓴다.
**클론한 환경에는 `.env`가 없으므로 거래소 라이브 호출은 불가능하다** — paper 모드와 pytest만 쓸 수 있다.

비개발자용 진입점은 `install.bat` → `run.bat` → `launcher.py`(대화형 메뉴)다.
배치파일에 한글을 넣으면 `chcp 65001`이 파일 중간부터 안 먹는 cmd.exe 버그가 있어서
**한글 UI는 전부 Python 쪽에서 처리한다.**

## 구조

```
strategy/     순수 계산 (거래소 무관) — grid, weights, targets, liquidation, feasibility, fees, indicators
exchange/     base.py(ExchangeAdapter 인터페이스, ContractSpec, round_qty_to_step)
              paper.py(인메모리 체결 시뮬레이터), orangex/(client, auth, ws_client, adapter)
engine/       grid_engine(상태머신), fill_router, entry_scheduler, cycle_manager,
              restart_recovery, grid_setup(공용 조립 로직), halt_flag,
              direction_selector(15분봉 RSI 방향 판정), combined_pnl_monitor(both 합산 익절)
config/       settings(.env 스키마), presets(3k/5k 프리셋), weights.csv
main.py       메인 자동매매 봇 진입점 (위 조각들을 조립)
quick_entry.py 즉시 진입 도구 — 격자 자동매매와 독립, 진입만 자동화(TP/SL은 사용자가 직접)
launcher.py   대화형 메뉴 (봇 실행 / 즉시 진입 분기)
```

상태 머신: `IDLE → SCOUTING → LADDERING → TP_PENDING → CLOSING → COOLDOWN`

## OrangeX API 함정 (반복해서 사고를 낸 것들)

- **인증은 `client_credentials`만 쓴다.** 기본값 `client_signature`는 이 프로젝트에서 안정적으로
  성공한 적이 없다(간헐적 실패). `OrangeXClient` 생성 시 **항상 명시**할 것.
  REST는 `access_token`을 `Authorization: bearer` 헤더로, WS는 반대로 params에 넣는다.
- **문서의 메서드명이 실제로 없는 경우가 흔하다** — `cancel_by_id`→`cancel`,
  `get_positions`→`get_user_position`, `get_open_order_by_instrument`→`get_open_orders_by_instrument`.
  안 되면 단/복수형과 유사 명명을 시도해봐라.
- **`get_positions`는 성공하지만 항상 빈 배열을 반환한다**(서버 버그 추정). 실제 포지션은
  `/private/get_user_position`으로 조회한다. 새 조회 엔드포인트를 확정할 땐 반드시
  **실제 데이터가 있는 상태**로 교차 검증해라.
- **`min_trade_amount`(수량 증가 단위, BTC=0.001)를 반드시 반영해라.** `compute_grid()`가
  나눗셈으로 만드는 수량은 소수점이 20자리 넘게 이어져서 그대로 보내면 거래소가 즉시 거부한다.
  `exchange/base.py`의 `ContractSpec.qty_step` + `round_qty_to_step()`(내림)을 써라.
- **헤지 모드 계좌는 `position_side`가 틀리면 주문이 접수 직후 자동 취소된다**(error_code 5998).
  어댑터가 받은 방향과 실제 주문 방향이 일치하는지 확인해라.
- **주문 접수/취소 직후 `get_order_state`에 지연이 있다**(서버 인덱싱 추정).
  `OrangeXAdapter._get_order_state_with_retry()`로 완화돼 있다.
- 심볼은 `BTC-USDT-PERPETUAL`이다. **`SPEC.md` 4장의 `BTC-USDT-PERP`는 오탈자**이니 따라 쓰지 마라.
- OrangeX에는 캔들(OHLC) 엔드포인트가 없어서 RSI/ATR용 일봉은 **바이낸스 공개 API**로 받는다
  (`strategy/market_data.py`, 사용자 승인됨). 주문/체결은 전부 OrangeX.

## 라이브 검증 방법론 (비싸게 배운 것)

**"화면에 성공 메시지가 떴다"는 라이브 검증의 증거가 되지 않는다.**
실전 진입이 네 번 연속 실패했는데 매번 원인이 달랐고, 마지막엔 `trading_mode` 오버라이드 누락으로
**실전을 골라도 `PaperAdapter`가 조용히 돌고 있었다**. 화면엔 실전 경고·확인 절차가 전부 정상
표시됐다. 로그의 `order_id`가 UUID(PaperAdapter의 `uuid4()`)인지 순수 숫자 문자열(진짜 OrangeX)인지
대조해서야 잡혔다.

- 라이브 검증은 **거래소 응답의 원본 필드까지 직접 대조**해라.
- **paper 모드 테스트로는 원리적으로 못 잡는 버그가 있다** — `position_side`, 헤지 모드,
  `qty_step`처럼 실전 전용 로직은 PaperAdapter에 개념 자체가 없다. paper 테스트가 다 통과해도
  안심하지 말고 코드 리뷰로 별도 확인해라.

## 현재 상태

Phase 0~3 완료. paper 모드는 바로 쓸 수 있고, `quick_entry`는 실전 진입까지 검증됐다
(2026-08-05, 실제 order_id 확인). **`main.py` 전체를 `trading_mode=live`로 처음부터 끝까지
기동해본 적은 아직 없다.**

**2026-08-17 대규모 개편** (자세한 내역은 `docs/phase3-plan.md`):
- `GRID_PRESET=1k|3k|5k` — 현재가 기준 ±가격 범위 프리셋. **레버리지 40배 고정**이고
  `MAX_STAGE`/`LEVERAGE`를 덮어쓴다. 최소 시드 1k≈65 / 3k≈548 / 5k≈2,656 USDT(both는 2배).
  **1k는 2026-08-18 추가**(소액 시드용, 20단계=1 tier). tier가 1개뿐이라
  `MANDATORY_SL_MIN_TIER`가 2 이상이면 필수 SL이 영원히 안 걸린다 — `main.py`가 경고한다.
- `DIRECTION=auto` — 사이클마다 **15분봉** RSI(14)로 방향 결정(≥50 숏, <50 롱).
  방향이 바뀌면 어댑터 스택을 통째로 재조립한다(position_side가 생성 시 고정이라).
- `DIRECTION=both` **재정의** — 양방향 동시 진입 + 롱/숏 **합산** 손익이 투입 증거금 대비
  `COMBINED_TP_ROE`(기본 10%) 도달 시 양쪽 청산 후 즉시 재진입. 개별 TP/SL은 전부 꺼진다.
- ATR 격자 확대 제거, `SL_ENABLED=false`로 SL 미등록 가능.
- `engine/grid_engine.py`의 qty_step 미적용 블로커 **해소**(5개 주문 지점 + open_qty 누적).

**SPEC 이탈 항목**(사용자 결정, `SPEC.md`는 원문이라 수정 안 함):
일봉 RSI 30/70 게이트 → 15분봉 50 방향결정 / ATR 배제 / 4~5차 SL 필수 → 미등록 /
both의 의미 변경. auto는 임계값이 50이라 **"아직 들어가지 마라"는 브레이크가 없다** —
봇이 상시 포지션을 든다. SL도 안 걸면 격자 최심 주문가와 청산가 사이 완충
(3k/40배 기준 약 2.08%p)이 유일한 방어선이다.

**Phase 4(리스크 가드)는 전부 미구현이다.** `.env`의 `DAILY_LOSS_LIMIT_PCT`는 선언만 되고
코드 어디서도 안 쓰이는 죽은 설정값이다(일일 손실 킬 스위치 없음). 청산가 근접 경보, 잔고 재검증,
API 키 권한 확인, 가격 급변 방어도 전부 없다. SPEC에 "협상 불가"로 적혀 있지만 아직 안 됐다 —
**사용자에게 "이미 다 안전하다"고 오해하게 만들지 마라.**

**라이브 기동 전 남은 확인 2건**: (1) OrangeX 계좌의 실제 레버리지가 40배인지
(`set_leverage()`는 봇 경로에서 호출된 적이 없다 — `settings.leverage`는 수량 계산용
지역값일 뿐이다), (2) `get_open_orders()`의 `position_side` 필터링 라이브 검증
(다른 엔드포인트 스키마로부터 유추한 것).

테스트는 **271개 전부 통과**한다. 오랫동안 "Python 3.14 asyncio 문제"로 기록돼 있던
실패 1건은 실제로는 테스트 격리 문제였다 — `Settings`가 kwargs로 안 넘긴 필드를 CWD의
`.env`에서 읽어와 로컬 `MANUAL_MODE=TRUE`가 흘러들어간 것. `tests/conftest.py`가
`.env` 로딩을 꺼서 차단한다. **테스트에서 `Settings`를 만들 땐 결과에 영향 주는 필드를
명시적으로 넘겨라.**

## 작업 방식

- 테스트 먼저 작성(TDD). 계산 엔진은 테스트 없이 코드 작성 금지.
- 커밋은 작게, 메시지는 명확하게. 커밋 메시지는 한국어로 쓴다.
- 의미 있는 변경을 마치면 `origin/main`에 커밋+푸시한다(사용자가 상시 승인함).
  단, `.env`·`docs/orangex-live-samples.json`·`logs/`는 절대 스테이징하지 마라.
- 작업이 끝나면 `docs/phase3-plan.md`에 무엇을 왜 바꿨는지 기록을 남겨라.
