# Phase 3 계획 — 실행 엔진 (SL/TP 라이브 검증 반영)

이 문서는 SPEC.md Phase 3(라인 86-100)의 실행 계획을 담는다. SPEC.md 원문 자체는 사용자 요청으로 아직 수정하지 않았다 — 결정 사항은 여기 기록하고, SPEC.md와 다른 부분은 이 문서가 우선한다.

## 배경

SPEC.md Phase 3는 원래 "체결마다 TP 취소 후 재등록"과 "4~5차 진입 시 거래소 SL 필수 등록(실패 시 강제청산)"을 요구했다. Phase 0/2 시점에는 SL을 거래소에 실제로 등록하는 방법이 불확실해(`docs/api-notes.md` §5) 사용자가 스코프에서 제외했었다.

**2026-07-30, Phase 3 설계 중 재검증**: `condition_type=STOP` 조건부 주문이 실제로 거래소에 등록되는 진짜 SL임을 라이브로 확인했다 (`docs/api-notes.md` §6 항목18, `scripts/orangex_test_stop_order.py`/`orangex_test_stop_order_cancel.py`). 트리거 시 실제 시장가 체결이 일어나고(포지션 수량 실제 감소로 교차검증), 트리거 전 주문은 기존 `cancel_order`로 정상 취소된다. 따라서 **SPEC 원래 설계를 그대로 구현한다** (아래 "Branch A" 확정).

핵심 발견: OrangeX의 STOP 트리거는 **crossing-trigger**다 — 주문 시점에 조건이 이미 참이어도 발동하지 않고, 이후 실제 가격이 trigger_price를 가로질러야 발동한다. 구현 시 trigger_price는 항상 "주문 시점 가격 기준 아직 미도달 방향"으로 설정해야 한다.

## TP/SL 최종 설계

- **TP**: 별도 API 불필요. `place_limit_order`로 목표가에 반대방향 지정가 주문(이미 라이브 검증됨). 체결마다 평단이 바뀌면 기존 TP 주문을 `cancel_order`로 취소 후 새 평단 기준으로 재등록.
- **SL (4~5차 진입 전용, SPEC 그대로)**: `condition_type=STOP` 조건부 주문으로 평단∓3%에 등록. 평단 갱신 시 취소(`cancel_order`, 이미 STOP 주문에도 동작 확인됨) → 재등록(취소-확인-등록 순서 엄수). **등록 실패 시 SPEC 원문대로 즉시 전량 시장가 청산 + 봇 정지.**
- 3차+ 진입 후 평단 도달 시 50% 시장가 청산(hybrid reset)은 SPEC 그대로 유지.

## `ExchangeAdapter` 인터페이스 변경 (`exchange/base.py`)

- `place_stop_order(order: StopOrderRequest) -> OrderResult` 추가 (기존에 "의도적으로 제외"됐던 `place_stop_order`를 복원 — 근거였던 docstring 갱신 필요)
- `StopOrderRequest` 데이터클래스 추가: `instrument, side, trigger_price, qty, client_order_id, reduce_only, trigger_price_type="last"` 등 (`condition_type`은 OrangeX 구현 세부사항이라 어댑터 내부에서만 사용, 인터페이스는 거래소 중립적으로 유지)
- `OrangeXAdapter.place_stop_order`: `condition_type=STOP`, `trigger_price_type=2`(last price)로 라이브 검증된 파라미터 그대로 구현
- `PaperAdapter.place_stop_order`: 시뮬레이션 구현 — `on_price_tick`에서 STOP 주문의 trigger_price를 가로지르면 시장가 체결로 처리 (crossing-trigger 동작을 그대로 재현: 등록 시점에 이미 조건이 충족돼 있어도 트리거하지 않고, 이후 가격이 실제로 가로지를 때만 체결)

## 실행 엔진 골격 (신규 `engine/` 디렉터리)

- **상태 머신**: `IDLE → SCOUTING → LADDERING → TP_PENDING → CLOSING → COOLDOWN`
- **진입 필터**: 일봉 RSI(14) ≤30(롱)/≥70(숏), ATR 급등 시 격자 간격 확대. `pandas-ta` 의존성 추가 필요(미설치). **선결 조사**: OHLC/캔들 공개 엔드포인트 존재 여부 미확인 — 지금까지 `ticker`/`order_book`/`last_trades`만 확인됨, 캔들 엔드포인트는 조사된 바 없음
- **롤링 격자 주문**: 100단계 전부가 아니라 현재가 기준 앞쪽 N개(기본 5, `config/settings.py`에 `max_open_grid_orders` 추가)만 유지
- **체결마다**: `strategy/grid.py`의 `compute_grid` 재사용해 평단/청산가/TP/SL 재계산 → TP 지정가 재등록 + (4~5차라면) SL STOP 주문 재등록
- **재시작 복구**: 거래소의 실제 포지션/미체결 주문으로 로컬 상태 재구성. **라이브 블로커**: `get_open_orders()`가 OrangeX에서 "No service found" — 해소 전까지 라이브 재시작 복구 불가. `PaperAdapter`로 먼저 개발/테스트, OrangeX 전환은 이 이슈 해소 후
- **가격 밴드 제약**: `order_price_low_rate`/`high_rate`(0.5~1.5배) 반영 필요

## `config/settings.py` 추가 필드

- `max_open_grid_orders: int = 5`
- RSI/ATR 파라미터
- `maker_fee`/`taker_fee` (실측 0.02%/0.06%)
- SL 관련: `sl_pct`(기존 `strategy/targets.py`에 있음), STOP 주문 trigger_price_type 기본값

## 테스트 전략

- 신규 엔진 로직은 `PaperAdapter` + 결정론적 가격 틱 시뮬레이션으로 유닛/골든 테스트
- `condition_type=STOP` 관련 `OrangeXAdapter` 코드는 `test_orangex_adapter.py` 패턴으로 Mock 기반 테스트 추가 (crossing-trigger 특성 포함)
- 라이브 주문이 관여하는 모든 테스트는 사전에 사용자에게 명시적으로 알리고 진행 (SPEC 0번 원칙)

## 구현 현황 (2026-07-30)

- **완료**: `exchange/base.py`에 `StopOrderRequest`/`place_stop_order`, `MarketOrderRequest`/`place_market_order` 추가. `OrangeXAdapter`/`PaperAdapter` 둘 다 구현.
  - `PaperAdapter.place_stop_order`는 crossing-trigger 특성(등록 시점에 조건이 이미 참이어도 발동 안 함)을 그대로 재현한다.
  - `PaperAdapter.place_market_order`는 `_last_price`(마지막 `on_price_tick` 호출값) 기준 즉시체결로 시뮬레이션한다. `_last_price`가 없으면 `NoKnownPriceError`.
  - `OrangeXAdapter.place_market_order`는 문서상 `type="market"` 파라미터 기반으로 구현했으나 **2026-07-30 기준 라이브 미검증** — place_stop_order/place_limit_order와 달리 실제 체결 여부 확인 안 됨. SPEC 3번 규칙에 따라 사용자가 명시적으로 요청하기 전까지 라이브 호출 금지.
- **완료**: `strategy/indicators.py`(RSI/ATR, Wilder 평활화, Decimal 전용 — pandas-ta 대신 직접 구현해 float 미사용 원칙 유지), `strategy/market_data.py`(바이낸스 공개 API로 일봉 캔들 조달 — OrangeX에 캔들 엔드포인트가 없어(9개 후보 전부 "No service found", `scripts/orangex_probe_candles.py`) 사용자 승인(2026-07-30)으로 외부 API를 캔들 전용 보조 소스로 채택. 실제 주문/체결은 전부 OrangeX).
- **완료**: `engine/grid_engine.py` — `GridEngine` 클래스로 상태 머신/롤링 격자 주문/체결 시 TP·SL(4~5차) 재등록/hybrid reset(3차+)/SL 등록 실패 시 강제청산+정지(`EngineHaltedError`)까지 구현. `strategy.grid.compute_grid()`가 이미 누적 계산해둔 값을 인덱스로 조회하는 방식이라 별도 재계산 로직 없음. `open_qty` 필드로 hybrid reset 이후에도 실제 보유 수량을 정확히 추적(가격 필드는 grid_rows 그대로, 수량만 별도 추적).
- **완료**: `engine/entry_filter.py` — RSI(14) ≤30(롱)/≥70(숏) 게이트만 구현. ATR 급등 시 "격자 간격 확대"는 SPEC에 구체적 배율/공식이 없어 미구현(추측 금지 원칙).
- 테스트: `tests/test_indicators.py`, `tests/test_market_data.py`(httpx.MockTransport), `tests/test_grid_engine.py`(PaperAdapter로 실제 체결시키며 검증) — 전체 스위트 67개 통과.
- **완료(2026-07-30)**: `engine/fill_router.py` — `FillRouter`가 `watch_fills()` 스트림을 `GridEngine` 이벤트로 라우팅한다. `Fill.order_id`를 엔진이 들고 있는 order_id(`resting_grid_order_ids`/`tp_order_id`/`sl_order_id`)와 직접 비교해 매칭한다(client_order_id 문자열 prefix 파싱에 의존하지 않음). 매칭 안 되는 Fill(hybrid reset/강제청산 시장가 체결처럼 엔진이 이미 동기적으로 처리한 것)은 무시한다.
  - `GridEngine`에 `on_sl_filled()` 추가(`on_tp_filled()`와 대칭 — SL 트리거로 전량 청산되면 잔여 격자 주문·TP 주문 취소 후 COOLDOWN 전이).
  - 구현 중 발견한 버그: `PaperAdapter.on_price_tick`/`fill_order`(지정가·STOP 체결)가 `_fill_queue`에 전혀 쌓이지 않아 `watch_fills()`가 시장가 주문 체결만 관측하고 있었다. `_apply_fill`에서 공통으로 큐잉하도록 수정(`exchange/paper.py`) — 이 수정 전에는 `FillRouter`가 실제로는 아무것도 라우팅하지 못했을 것.
  - 테스트: `tests/test_fill_router.py` — route() 단위 테스트(격자 진입/TP/SL 매칭, 미매칭 무시) + `run()`을 백그라운드 태스크로 돌려 `PaperAdapter.watch_fills()`를 실제로 소비하는 end-to-end 테스트 2개(TP 경로, SL crossing-trigger 경로). 전체 스위트 73개 통과.
  - `OrangeXAdapter.watch_fills`는 여전히 `NotImplementedError` — 라이브 전환 전 별도 구현 필요(웹소켓 여부 등 미조사).
- **완료(2026-07-30)**: `engine/entry_scheduler.py` — `EntryScheduler`가 RSI 일봉 확인을 "언제" 수행할지의 실제 루프를 담당한다. `IDLE`이면 `GridEngine.start_scouting()`(신규 추가한 IDLE→SCOUTING 전이 메서드)을 먼저 호출하고, `SCOUTING`인 동안 `poll_interval_seconds`(기본 3600, `config/settings.py`의 `rsi_poll_interval_seconds` — SPEC에 구체적 주기가 없어 임의 기본값)마다 바이낸스 일봉을 가져와 RSI(14)를 계산, `passes_rsi_filter` 통과 시 `start_laddering()`을 호출한다.
  - `_closed_candles()`가 아직 마감 안 된(진행 중인) 당일 봉을 제외한다 — 바이낸스 klines가 마지막 행으로 진행 중 봉을 같이 주기 때문에, 안 걸러내면 하루 중 신호가 계속 뒤집힐 수 있음.
  - 테스트: `tests/test_entry_scheduler.py` — `_closed_candles` 단위 테스트 2개, `check_once()` 단위 테스트 3개(단조 증가/감소 종가로 RSI 극단값 0/100을 결정론적으로 유도), `run()`을 백그라운드 태스크로 돌려 IDLE→SCOUTING→LADDERING 전이를 실제로 검증하는 end-to-end 테스트 1개. 전체 스위트 79개 통과.
  - ATR 급등 시 격자 간격 확대는 여전히 미구현 — SPEC에 배율/공식이 없어 `engine/entry_filter.py`와 동일한 이유로 보류.
- **완료(2026-07-30)**: `engine/restart_recovery.py` — `reconstruct_state()`/`build_recovered_engine()`이 거래소의 `get_position()`/`get_open_orders()`만으로 `GridEngine` 상태를 재구성한다. 재시작 직후엔 엔진 메모리가 없어 `FillRouter`처럼 order_id로 매칭할 수 없으므로, `GridEngine`이 실제로 붙이는 client_order_id prefix 규칙("grid-{index}-...", "tp-{index}-...", "sl-{index}-...")에 의존한다. 조금이라도 앞뒤가 안 맞으면(TP 없는데 포지션 있음, tier4+인데 SL 없음, 미체결 격자 인덱스가 불연속, 포지션 수량이 이론치/hybrid reset 수량 어느 쪽과도 안 맞음 등) `RestartRecoveryError`로 막고 절대 추측하지 않는다(SPEC 100줄 원칙).
  - **알려진 한계(이 모듈 단독으로는 여전히 유효, 시스템 전체로는 해소됨)**: `GridEngine.halted`(SL 등록 실패로 강제청산+정지)는 거래소 상태만으로 구분 불가 — 정상 COOLDOWN과 강제청산 후 상태가 둘 다 "포지션 flat, 주문 없음"으로 동일하게 보여서 이 모듈은 그런 경우 전부 `IDLE`(재스카우팅 허용)로 복구한다. **2026-07-30 갱신**: `engine/halt_flag.py`가 `main.py`에서 이 모듈보다 먼저 실행돼 halted 흔적이 있으면 애초에 이 모듈까지 도달하지 못하게 막으므로, 실제 운용에서는 더 이상 문제가 안 된다.
  - **2026-07-30 갱신**: `get_open_orders()` 라이브 블로커 해소됨(아래 참고) — PaperAdapter 기준 테스트는 여전히 유효하고, OrangeX 라이브로 이 모듈 자체를 끝까지 기동해본 적은 아직 없다(다음 작업 후보).
  - 테스트: `tests/test_restart_recovery.py` — 실제 `GridEngine`+`PaperAdapter`로 시나리오를 만든 뒤 그 엔진 인스턴스를 버리고 새로 재구성해 원래 상태와 비교하는 방식. 정상 경로 6개(IDLE/LADDERING(SL 없음)/LADDERING(SL 있음)/TP_PENDING/hybrid reset 후/`build_recovered_engine`으로 이어서 체결까지) + 불일치 감지 9개. 전체 스위트 94개 통과.
- **완료(2026-07-30)**: `_reregister_tp`/`_reregister_sl`(`engine/grid_engine.py`)의 `cancel_order` 예외 미처리 수정 — 아래 있던 미해결 항목을 실제로 고쳤다. 기존 TP/SL이 이미 체결/트리거로 사라진 상태에서 취소를 시도해 실패하면, 추측해서 새 주문을 걸지 않고 SL 등록 실패와 동일하게(SPEC 규정) `_force_close_and_halt` + `EngineHaltedError`로 처리하도록 통일했다.
  - `_force_close_and_halt` 자체도 강화: `halted=True`/`state=CLOSING`을 강제청산 시장가 주문 시도보다 **먼저** 확정하도록 순서를 바꿨다 — 그 시장가 주문마저 실패해도(최악의 경우) 엔진이 halted 판정 없이 "멀쩡한 척" 계속 동작하는 상황을 막기 위함(`_check_not_halted()`가 이후 모든 호출을 막는 유일한 안전장치라 이 플래그가 항상 먼저 서 있어야 함).
  - 테스트: `tests/test_grid_engine.py`에 3개 추가(TP 취소 실패, SL 취소 실패, 강제청산 시장가 주문마저 실패해도 halted는 반드시 설정됨). 전체 스위트 97개 통과.
- **완료(2026-07-30)**: `ExchangeAdapter.get_ticker()`/`Ticker` 추가(`exchange/base.py`) — `OrangeXAdapter`는 이미 라이브 검증된 `/public/ticker`(docs/api-notes.md §6 항목15)로, `PaperAdapter`는 마지막 `on_price_tick()` 값을 그대로 돌려주는 방식으로 구현. `config/settings.py`에 `maint_margin_rate`(0.005)/`sl_pct`(0.03, docs/phase1-report.md 확정값)/`price_poll_interval_seconds`(기본 5초, 임의값) 추가.
- **완료(2026-07-30)**: `main.py` 신규 — 지금까지 만든 조각(GridEngine/FillRouter/EntryScheduler/restart_recovery)을 실제로 조립하는 진입점. `build_grid_rows()`가 실시간 ticker로 base_price를 잡고 `find_max_feasible_step`으로 불가능 단계를 잘라내고 `find_min_order_shortfalls`가 하나라도 있으면 시작을 거부한다(아래 참고). `run()`은 `build_recovered_engine`으로 상태 복구 → `FillRouter`/`EntryScheduler`/현재가 관찰 루프(`_price_watch_loop`, hybrid reset 조건 확인 + PaperAdapter일 때 `on_price_tick` 주입)를 태스크로 묶어 돌리고, 하나라도 예외로 죽으면 전부 취소 후 예외를 올린다(`finally`로 항상 정리 — `run()` 자체가 취소돼도 백그라운드 태스크가 고아로 안 남게 함).
  - **와이어링하다 발견한 버그 2건, 둘 다 수정**: (1) `config/settings.py`의 `symbol` 기본값이 `"BTC-USDT-PERP"`였는데 OrangeX 실제 instrument_name은 `"BTC-USDT-PERPETUAL"`이다(docs/api-notes.md 다수 항목에서 라이브 확인됨, `strategy/market_data.py`의 바이낸스 심볼 매핑 키도 이 표기를 씀) — 이 기본값으로 실행했으면 `get_contract_spec`/`get_ticker`/RSI 캔들 조달이 전부 조용히 실패했을 것. `"BTC-USDT-PERPETUAL"`로 수정. (2) `run()`이 `asyncio.wait(...)`을 정상적으로 빠져나갈 때만 백그라운드 태스크를 정리했는데, `run()` 자체가 외부에서 취소되면(예: 프로세스 종료 신호) 그 정리 코드에 도달하지 못해 `fill_router`/`entry_scheduler`/`price_watch` 태스크가 고아로 계속 도는 문제가 있었다 — `try/finally`로 모든 종료 경로에서 항상 취소+정리하도록 수정.
  - **테스트**: `run()` 전체 조립이 실제로 IDLE→SCOUTING→LADDERING까지 도달하고 취소 시 백그라운드 태스크가 고아 없이 정리되는지 확인하는 스모크 테스트(`tests/test_main.py`), `build_grid_rows`/`build_execution_adapter`는 `tests/test_grid_setup.py`로 분리(아래 참고).

- **완료(2026-07-30)**: 다중 사이클 지원. 직전까지는 `GridEngine`이 한 사이클(IDLE→...→COOLDOWN)만 전제해 COOLDOWN 이후 그대로 멈춰 있었다 — 이번에 실제로 채웠다.
  - `engine/grid_setup.py` 신규 — `main.py`에 있던 `build_grid_rows`/`build_execution_adapter`/`build_market_data_adapter`/`StartupError`를 이쪽으로 옮겼다(`CycleManager`도 동일한 격자 재계산 로직이 필요해서 — `main.py`가 `cycle_manager.py`를 가져오고 `cycle_manager.py`가 `main.py`를 가져오는 순환 참조를 피하려고 공용 모듈로 분리).
  - `GridEngine.reset_for_new_cycle(grid_rows)` 추가(동기 메서드) — COOLDOWN 상태에서만 호출 가능(그 외 상태면 `ValueError`), halted면 기존과 동일하게 `_check_not_halted()`가 막는다. `filled_step_count`/`resting_grid_order_ids`/`tp_order_id`/`sl_order_id`/`hybrid_reset_done`/`open_qty`를 전부 초기화하고 `state=IDLE`로 되돌린다. 엔진 자신은 시세/설정에 접근권이 없어 새 grid_rows는 호출하는 쪽이 계산해서 넘겨야 한다.
  - `engine/cycle_manager.py` 신규 — `CycleManager`가 COOLDOWN 진입을 폴링으로 감지하고(`cycle_manager_poll_interval_seconds`, 기본 10초, 임의값), `cooldown_minutes`(기존 설정값, 30분)만큼 기다린 뒤 `build_grid_rows()`로 현재가 기준 새 격자를 계산해 `reset_for_new_cycle()`을 호출한다.
  - **다중 사이클 지원 작업 중 발견한 진짜 버그, 수정함**: `EntryScheduler.run()`이 IDLE→SCOUTING 전이 체크를 `while True` 루프 진입 **전에 딱 한 번만** 했다. `CycleManager`가 COOLDOWN 이후 상태를 다시 IDLE로 되돌려도, 이미 루프 안에 들어가 있는 스케줄러는 그 체크를 다시 안 해서 두 번째 사이클에 절대 재진입하지 못했을 것 — IDLE 체크를 루프 안으로 옮겨서 매 반복마다 확인하도록 고쳤다.
  - `main.py`의 `run()`에 `cycle_manager` 태스크를 4번째 백그라운드 태스크로 추가(기존 fill_router/entry_scheduler/price_watch와 동일하게 `asyncio.wait(FIRST_EXCEPTION)` + `finally` 정리 대상).
  - 테스트: `tests/test_grid_engine.py`에 `reset_for_new_cycle` 3개(정상 초기화/COOLDOWN 아니면 거부/halted면 거부), `tests/test_entry_scheduler.py`에 재진입 버그 회귀 테스트 1개, `tests/test_cycle_manager.py` 신규(COOLDOWN 감지 후 직접 호출로 리셋 검증 + `run()` 백그라운드 루프가 실제로 COOLDOWN을 감지해 자동으로 리셋하는지 end-to-end 1개). 전체 스위트 107개 → **113개 통과**.

- **완료(2026-07-30)**: `OrangeXAdapter.watch_fills()` 구현 — **완전 검증됨(연결/인증/구독/실제 체결 스키마 전부)**. `exchange/orangex/ws_client.py`(`OrangeXWsClient`) 신규. 1차로 연결/인증/구독을 읽기전용 검증했고(`scripts/orangex_probe_ws_fills.py`), 2차로 사용자 명시적 요청 하에 실제 0.001 BTC 체결(진입+즉시청산, 순노출 원복)을 발생시켜 `scripts/orangex_observe_live_fill_ws.py`로 `user.trades.{instrument}.raw` 알림의 실제 필드 스키마까지 캡처했다(docs/api-notes.md §6 항목19). `fee` 필드명이 정확히 "fee"로 확정돼 기존 파싱 코드 수정 없이 그대로 맞았다. `custom_order_id`는 실제 payload에 없었지만 기존 방어 처리(`.get(...,"")`)로 문제없음. 실제 payload를 `tests/test_orangex_adapter.py`의 회귀 테스트로 고정해둠.
  - `engine/grid_setup.py`의 `build_execution_adapter`가 live 모드에서 `OrangeXWsClient`도 같이 구성해 `OrangeXAdapter`에 넘기도록 배선함.
  - 테스트: `tests/test_orangex_ws_client.py`(5개), `tests/test_orangex_adapter.py`에 `watch_fills` 관련 8개(실제 라이브 payload 회귀 테스트 포함). 전체 스위트 113개 → **125개 통과**.
- 새 의존성 추가: `websockets` 패키지(pip install만 함, requirements 파일 없는 프로젝트라 별도 기록 안 됨 — 주의).

- **완료(2026-07-30)**: `place_market_order`의 OrangeX 라이브 검증. 사용자 명시적 요청으로 `scripts/orangex_test_market_order.py` 실행(0.001 BTC 진입 시장가 → 청산 시장가) — 즉시 체결 확인, STOP 주문 같은 crossing-trigger 등 예상 밖 특성 없음. `exchange/base.py`의 `MarketOrderRequest` docstring에서 "라이브 미검증" 문구 제거.
  - **검증 도중 실제 사고 발생 → 새 버그 발견 및 수정**: 진입 주문은 체결됐는데 직후 `get_order_state` 호출이 `KeyError: 'result'`로 죽어 스크립트가 중단, 청산 못한 LONG 0.001 BTC가 잠깐 미청산 상태로 남았다(수동으로 `scripts/orangex_cleanup_open_long.py`로 정리, 최종 flat 확인). 원인은 이미 문서화돼 있던 §6 항목16(주문 접수 직후 즉시 조회 시 서버 인덱싱 지연)인데, 그동안 "재시도 정책은 사용자와 상의 후 반영"으로 미뤄뒀던 것 — 이번에 실제로 재현된 세 번째 관찰 데이터가 생겨서 `OrangeXAdapter._get_order_state_with_retry()`로 반영했다. 즉시 1회 시도 후 실패하면 2/3/5초 간격으로 최대 3회 재시도(관찰된 "2초 성공"/"5초 반영" 값 그대로, 총 대기 10초), 그래도 실패하면 `OrangeXResponseSchemaError`. `place_limit_order`/`place_stop_order`/`place_market_order` 전부 이 재시도를 쓰도록 통일.
  - 테스트: `tests/test_orangex_adapter.py`에 `place_market_order` 기본 동작 1개 + 재시도 성공/소진 2개(`QueuedClient`로 같은 메서드가 여러 번 다르게 응답하는 시나리오 지원, `asyncio.sleep` 패치로 실제 대기 없이 검증). 전체 스위트 125개 → **128개 통과**.

- **완료(2026-07-30, 최종)**: ATR 급등 시 격자 간격 확대. 처음엔 사용자에게 배율/공식을 직접 물어서 "지금은 미구현으로 남겨두기"로 보류했었으나, 이후 사용자가 "물어보지 말고 알아서 판단해서 진행"으로 결정 권한을 넘겨줘서 자체 기본값으로 구현했다. `engine/entry_filter.py`에 `compute_atr_tick_multiplier(atr_today, atr_yesterday)` 추가 — 오늘자 ATR(14)이 어제자보다 30% 넘게 크면 그 비율만큼(최대 2배 상한) tick을 넓히고, 아니면 배율 1(변화 없음). 이 임계값(1.3)/상한(2.0) 전부 SPEC/엑셀 근거가 아니라 이 구현이 정한 값임을 코드에 명시해뒀다. `engine/grid_setup.py`의 `build_grid_rows()`가 매 사이클 계산 시 바이낸스 일봉으로 이 배율을 구해 `settings.grid_tick`에 곱한 뒤 `compute_grid()`에 넘긴다(완결 일봉이 부족하면 배율 1 — 값을 추측해서 만들지 않고 보수적으로 미확대).
  - 리팩터링: `_closed_candles`(당일 진행 중 봉 제외)가 `engine/entry_scheduler.py` 전용이었는데 ATR 쪽도 필요해져서 `strategy/market_data.py`의 공개 함수 `closed_candles()`로 옮기고 양쪽이 공유하도록 정리.
  - `main.py`/`CycleManager`의 테스트 주입용 파라미터명을 `entry_scheduler_http_client` → `binance_http_client`로 변경(이제 RSI뿐 아니라 ATR도 같은 바이낸스 클라이언트를 씀).
  - 테스트: `tests/test_entry_filter.py` 신규(RSI 필터 3개 + ATR 배율 5개), `tests/test_grid_setup.py`에 급등 감지/데이터 부족 시 미확대 2개 추가, `tests/test_market_data.py`에 `closed_candles` 이동 테스트 3개. 전체 스위트 135개 → **145개 통과**.

- **완료(2026-07-30)**: halted 상태의 재시작 후 영속화. `engine/halt_flag.py` 신규(`check_halt_flag`/`write_halt_flag`/`clear_halt_flag`) — `main.py`의 `run()`이 시작 시 `check_halt_flag(settings.halt_flag_path)`(기본 `state/halted.json`)를 호출해 플래그가 있으면 아무것도 구성하지 않고 즉시 거부하고, 백그라운드 태스크에서 `EngineHaltedError`가 올라오면 그 시점에 `write_halt_flag()`로 이유를 기록한다. 재개하려면 사람이 거래소 상태를 직접 확인하고 파일을 지워야 한다 — `EngineHaltedError`가 이미 갖고 있던 "수동 확인 후 재개" 철학을 재시작에도 연장한 것. `.gitignore`에 `state/` 추가.
  - 테스트: `tests/test_halt_flag.py`(5개, 실제 파일 I/O로 검증), `tests/test_main.py`에 2개 추가(플래그 있으면 즉시 거부 / `fill_router`를 통해 실제로 `EngineHaltedError`가 발생했을 때 플래그가 기록되는지 end-to-end — tier4까지 60개 실제 체결 대신 엔진 카운터를 tier4 시점으로 맞추고 그 한 단계만 실제로 체결시켜 FillRouter가 진짜로 처리하게 함). 전체 스위트 128개 → **135개 통과**.

- **완료(2026-07-30)**: `get_open_orders()` OrangeX 라이브 블로커 해소 — **Phase 3 최후의 라이브 블로커였다.** 기존에 실패하던 문서 메서드명(`get_open_order_by_instrument`, 단수형 "order")이 아니라 `/private/get_open_orders_by_instrument`(복수형 "orders")가 실제로 동작함을 확인했다(`scripts/orangex_find_open_orders_endpoint_v2.py`). `get_positions`처럼 "성공은 하지만 항상 빈 배열"인 함정일 수 있어 실제 미체결 주문 하나를 걸고(즉시체결 안 되는 지정가) 조회→나타남 확인→취소→사라짐까지 교차 검증했다(`scripts/orangex_verify_get_open_orders.py`, 읽기전용에 가까운 최소 개입 — 주문 하나 걸고 바로 취소). `exchange/orangex/adapter.py`의 `get_open_orders()`를 이 메서드로 전환. 이걸로 `engine/restart_recovery.py`가 라이브에서도 원칙적으로 동작 가능해졌다(다만 이 모듈 자체를 OrangeX 라이브로 끝까지 기동해본 적은 아직 없음 — 다음 작업). 테스트 스위트 135개 그대로 통과(메서드명만 바뀌고 테스트는 기존 것 갱신).

- **완료(2026-08-04)**: `max_stage` 미구현 발견 및 수정. 다른 컴퓨터로 이어서 작업 시작하며 재점검하다가 `config/settings.py`의 `max_stage`(SPEC 110번 "사용자가 정한 max_stage를 넘는 진입 금지")가 필드로만 존재하고 어떤 엔진 코드에서도 읽히지 않는다는 걸 발견했다 — `engine/grid_engine.py`의 `_refresh_grid_orders()`가 `grid_rows` 길이(가용잔고 기준 feasibility 절삭치, 기본 설정에서 91단계)까지 그대로 진입해, `.env`에 `MAX_STAGE=3`을 넣어도 실제로는 4~5차까지 진입할 수 있는 상태였다.
  - 수정: `engine/grid_setup.py`의 `build_grid_rows()`에 `settings.max_stage * STEPS_PER_TIER`로 절삭하는 로직을 feasibility 절삭보다 먼저 추가. `CycleManager`도 동일 함수를 재사용하므로 다음 사이클에도 자동 적용됨.
  - 테스트: `tests/test_grid_setup.py`의 기존 feasibility 절삭 테스트는 `max_stage=5`로 올려 절삭 우선순위를 분리했고, `test_build_grid_rows_truncates_to_max_stage` 신규 추가. `tests/test_main.py`의 tier4 시나리오 테스트도 `max_stage=4`로 명시적으로 올림(기본값 3으로는 tier4에 도달 불가). 전체 스위트 145개 → **146개 통과**.
  - **참고**: 이 발견은 사용자가 "300 USDT로 이 전략을 돌리면 얼마나 급락까지 버티는지" 질문에 답하려고 `compute_grid`를 직접 돌려보는 과정에서 나왔다 — 그 계산 자체의 결론은: 현재 100단계 weight 프로파일은 min_notional(10 USDT) 제약 때문에 300 USDT로는 최소주문 미달 단계가 64개 발생해 `StartupError`로 시작 자체가 거부된다(병합 로직 미구현, 아래 항목과 동일). equity 크기와 무관하게 청산가는 leverage/weight 구조로만 결정되며, tier5까지 다 채웠다고 가정하면 현재가 대비 약 -11.5% 하락에서 청산된다.

- **완료(2026-08-04)**: `manual_mode` 추가 — 사용자 요청("그냥 현재가 기준으로 무조건 매수/매도 체결하고, tp/sl은 수동으로 하는 모드"). RSI 진입 필터 없이 현재가 기준 격자 진입(매수/매도 체결)만 자동화하고, TP 재등록/SL 등록(4~5차)/hybrid reset(3차+ 50% 자동청산)은 전부 꺼서 청산을 사용자가 거래소에서 직접 수동으로 관리하게 한다. `config/settings.py`에 `manual_mode: bool = False`(기본값 False, 기존 완전자동 동작 그대로 유지) 추가, `.env`는 `MANUAL_MODE=true`로 켠다.
  - `engine/grid_engine.py`: `GridEngine.manual_mode` 필드 추가. `on_fill()`이 manual_mode면 `_reregister_tp`/`_reregister_sl` 호출을 건너뛰고, 마지막 단계까지 다 체결돼도 `TP_PENDING`으로 넘어가지 않고 `LADDERING`을 유지(TP 자체가 없어 기다릴 상태가 없음). `maybe_hybrid_reset()`은 manual_mode면 맨 앞에서 `False`를 반환해 절대 발동 안 함.
  - `engine/entry_scheduler.py`: `EntryScheduler(manual_mode=...)` 추가 — SCOUTING 상태에서 RSI 확인(`check_once`) 대신 바로 `start_laddering()`을 호출.
  - `engine/restart_recovery.py`: manual_mode에서는 (1) 사용자가 거래소에 직접 건 TP/SL 등 봇이 모르는 client_order_id 주문을 만나도 `RestartRecoveryError`로 막지 않고 조용히 건너뛴다(엔진 추적 대상이 아니라고 판단), (2) 포지션이 있는데 TP 미체결 주문이 없어도(TP를 아예 안 걸므로 당연함) 에러 안 냄, (3) tier4+ SL 필수 검증과 hybrid reset 수량 정합성 검증(이론치 cum_qty와 정확히 일치해야 함)을 전부 생략하고 실제 포지션 수량을 추측 없이 그대로 신뢰한다(사용자가 수동으로 부분청산했을 수 있어서). `_parse_client_order_id`/`reconstruct_state`/`build_recovered_engine` 전부 `manual_mode` 파라미터 추가.
  - `main.py`: `build_recovered_engine`/`EntryScheduler` 생성 시 `settings.manual_mode` 전달.
  - **알려진 한계**: manual_mode에서는 `on_tp_filled`/`on_sl_filled`가 절대 호출되지 않으므로 `EngineState.COOLDOWN`에 자동으로 도달할 방법이 없다 — `CycleManager`가 COOLDOWN을 기다리는 루프는 무해하게 영원히 대기만 한다(다중 사이클 자동 전환은 manual_mode에서는 사실상 미지원, 필요하면 사용자가 프로세스를 직접 재시작해야 함). 이건 의도된 동작이다(청산 판단 자체를 사용자에게 넘겼으므로 "사이클 종료" 시점도 봇이 알 수 없음).
  - 테스트: `tests/test_grid_engine.py`(3개), `tests/test_entry_scheduler.py`(1개, RSI 캔들 조회 자체가 호출 안 되는지 검증), `tests/test_restart_recovery.py`(5개), `tests/test_main.py`(1개, end-to-end 와이어링). 전체 스위트 146개 → **156개 통과**.

- **완료(2026-08-04)**: 코드 리뷰로 발견한 버그 3건 수정.
  1. **[심각] WS 연결 끊김 시 `FillRouter`가 예외 없이 영원히 멈추던 문제.** `exchange/orangex/ws_client.py`의 `_read_loop()`는 `asyncio.create_task()`로 띄워지고 아무도 결과를 기다리지 않아, 연결이 끊기면(재연결/하트비트 정책 자체가 여전히 미확인, §6 항목6/19) 태스크가 조용히 죽고 `notifications()` 큐에는 더 이상 아무것도 안 들어와 `OrangeXAdapter.watch_fills()` → `FillRouter.run()`이 예외 없이 무한 대기했다. `main.py`의 `asyncio.wait(..., FIRST_EXCEPTION)`는 예외가 나야만 감지하므로 이 상황을 절대 못 잡았다 — 봇이 살아있는 것처럼 보이지만 그 순간부터 모든 체결을 인식 못 해 TP/SL 재등록이 조용히 멈추는, SPEC 0번이 우려하는 정확히 그 시나리오였다.
     - 수정: `_read_loop()`가 연결 종료(정상 EOF든 예외든, `asyncio.CancelledError`로 인한 명시적 `close()`는 제외)를 감지하면 `_CLOSED_SENTINEL`을 큐에 넣어 `notifications()`를 깨우고 `OrangeXWsConnectionClosedError`(또는 원인이 된 원본 예외)를 던지도록 수정. 응답을 기다리던 `call()`들의 pending future도 같이 깨움. **자동 재연결은 의도적으로 구현하지 않음** — 끊긴 동안 놓친 체결이 있을 수 있어 추측 대신 사람이 재시작 후 restart_recovery로 실제 상태를 재확인하게 하는 게 안전하다고 판단.
     - 테스트: `tests/test_orangex_ws_client.py`에 3개 추가(정상 EOF/네트워크 에러로 인한 종료 각각이 예외를 던지는지, pending call()도 같이 깨어나는지).
  2. **[중간] `restart_recovery.py`가 정상 상태 하나를 오류로 오판하던 문제.** `start_laddering()` 직후 첫 체결이 나기 전(포지션 flat, 진입 지정가 주문만 미체결)은 완전히 정상인 LADDERING 상태인데, `reconstruct_state()`의 flat 체크가 이 경우도 무조건 `RestartRecoveryError`로 막아서 이 타이밍에 봇이 재시작되면 복구가 원천적으로 불가능했다.
     - 수정: flat일 때 TP/SL 주문 존재는 여전히 에러로 막되, 미체결 격자 주문만 있는 경우는 index 0부터 연속인지만 확인하고 `LADDERING`(filled_step_count=0)으로 정상 복구하도록 분리.
     - 테스트: `tests/test_restart_recovery.py` — 기존 `test_flat_position_with_leftover_order_raises`(이제는 정상 케이스이므로 성공 검증으로 교체)를 `test_reconstructs_laddering_with_zero_fills`로 바꾸고, 여전히 막혀야 하는 두 케이스(`test_flat_position_with_tp_order_raises`, `test_flat_position_with_non_contiguous_from_zero_grid_orders_raises`)를 신규 추가.
  3. **[낮음~중간] `_get_order_state_with_retry`가 `(KeyError, TypeError)`만 재시도하던 문제.** 이 재시도 로직 자체가 실제 사고(주문 체결됐는데 상태조회 실패로 포지션 미청산, §6 항목16) 때문에 만든 건데, 네트워크 순단(`httpx.TransportError`)이나 서버측 일시 오류(`httpx.HTTPStatusError` 5xx)는 재시도 없이 바로 예외를 던져 같은 유형의 사고가 다른 경로로 재발할 여지가 있었다.
     - 수정: `httpx.TransportError`와 `httpx.HTTPStatusError`(status>=500)도 재시도 대상에 추가. 4xx(인증/파라미터 오류 등 시간이 지나도 해결 안 되는 오류)와 `OrangeXError`(코드별 의미가 불명확한 게 많아 추측 금지)는 여전히 즉시 실패.
     - 테스트: `tests/test_orangex_adapter.py`에 3개 추가(네트워크 에러 재시도, 5xx 재시도, 4xx는 재시도 없이 즉시 실패).
  - 전체 스위트 156개 → **164개 통과**.

- **완료(2026-08-04)**: `direction="both"`(롱/숏 동시 운용) 추가 — 사용자 요청("direction은 양쪽다, 즉 long, short 둘 다 하는 모드도 추가"). 구현 방식은 하나의 프로세스 안에서 롱/숏 각각 완전히 독립된 `GridEngine`+`FillRouter`+`EntryScheduler`+`CycleManager`+어댑터 스택을 동시에 돌리는 것 — `equity_usdt`를 반씩 자동 분할(사용자 선택).
  - `config/settings.py`: `direction`에 `"both"` 추가.
  - `main.py`: 기존 `run()` 본문을 `_run_single_direction()`으로 분리하고, `run()`은 `direction != "both"`면 그대로 한 번 호출, `"both"`면 `Settings.model_copy(update=...)`로 롱용/숏용 사본(각각 `equity_usdt/2`, 독립된 `halt_flag_path` — `_derive_halt_flag_path()`로 `state/halted_long.json`/`state/halted_short.json`처럼 분리)을 만들어 두 스택을 동시에 태스크로 돌린다. 한쪽만 halted여도 다른 쪽은 재시작 가능하도록 halt flag 파일 자체를 분리한 게 핵심 — 공유했다면 한쪽 사고로 멀쩡한 다른 쪽까지 재시작을 못 하게 막았을 것.
  - **헤지 모드 계좌에서 롱/숏 포지션이 동시에 존재할 때 서로 뒤섞이는 버그를 발견해 같이 고침**: `OrangeXAdapter.get_position()`/`get_open_orders()`가 원래 `position_side` 구분 없이 instrument만 보고 첫 매치(또는 전체)를 반환했다 — 롱 담당 어댑터가 숏 포지션/주문을 자기 것으로 착각할 수 있는 심각한 잠재 버그였다. 두 메서드 다 어댑터 생성 시 받은 `self._position_side`로 필터링하도록 수정(position_side가 없으면 기존 동작 유지, 하위호환). **주의**: `get_open_orders_by_instrument` 응답에도 `position_side` 필드가 있다는 가정은 다른 엔드포인트(`get_order_state` 등)에서 관찰된 스키마로부터 유추한 것이라 이 특정 엔드포인트로는 아직 라이브 재검증 못 함 — live에서 `direction="both"` 처음 켤 때 반드시 확인 필요.
  - REST 클라이언트(`OrangeXClient`)는 계정 전체 레이트리밋(10 req/s)을 지키려고 롱/숏이 공유(`build_execution_adapter`의 `shared_client` 파라미터 추가)하되, WS 연결(`OrangeXWsClient`)은 `notifications()`가 단일 소비자용 큐라 공유하면 체결 스트림을 반씩 나눠 갖게 되는 문제가 있어 방향마다 독립적으로 새로 만듦(같은 채널을 중복 구독해도 각자 전체 스트림을 받으므로 무해함).
  - PaperAdapter는 아예 손대지 않음 — 방향마다 완전히 별개의 `PaperAdapter` 인스턴스(자기 몫의 가상 자금/포지션)를 만들면 자연스럽게 격리되므로.
  - **중요 — 최소 시드 2배 필요**: [[이전 계산]](이 문서보다 대화 기록에만 있음) 기준 단일 방향 최소 시드가 현재가/레버리지에 따라 약 5,400 USDT였는데, `both` 모드는 그 금액을 반으로 나눠 쓰므로 **총 자금이 그 최소값의 약 2배(약 10,800 USDT, 레버리지 20배 기준) 이상은 있어야 양쪽 다 시작 가능**하다. 부족하면 `build_grid_rows()`가 (기존과 동일하게) 최소 주문 미달로 시작을 거부한다 — 실제로 equity_usdt=10000으로 both를 테스트하다 이 상황이 재현되어 확인함.
  - 테스트: `tests/test_orangex_adapter.py` 3개(헤지모드 position_side 필터링 get_position/get_open_orders, 하위호환), `tests/test_main.py` 3개(`_derive_halt_flag_path`, execution_adapter 사전주입 거부, 롱/숏 독립 엔진 end-to-end 와이어링). 전체 스위트 164개 → **170개 통과**. paper 모드로 실제 시장 데이터 대상 end-to-end 스모크 테스트도 완료(롱/숏 둘 다 LADDERING 도달, `[long]`/`[short]` 태그로 구분된 로그 확인).
  - **아직 launcher.py 대화형 메뉴에는 방향 선택지가 없음** — 지금은 `.env`의 `DIRECTION=both`로만 켤 수 있다. 필요하면 후속 작업으로 메뉴에 추가 가능.

- **완료(2026-08-04)**: 사용자가 프로젝트 폴더에 넣어둔 `제까깟-마틴게이-3k.xlsx`(V14, "3k 타격형" — 3-tier/60단계 압축 설계) 참고 검증 및 그 결과로 발견된 버그 2건 수정.
  - **검증**: xlsx의 E열 60개 가중치는 `config/weights.csv`의 앞 60개(1~3차, 10,11,...,29/30,32,...,68/70,74,...,146, 합계 3530)와 완전히 동일 — 새 데이터 아님, 기존 100단계 프로파일의 앞부분을 3-tier 전용으로 재구성한 버전. xlsx 캐시값(가용잔고/SL가)을 `strategy/grid.compute_grid()`로 재현해 소수점 10자리 이하까지 일치 확인 — 재무 공식 자체는 이미 정확히 구현돼 있었음.
  - **버그 1 [안전, 심각]**: `engine/grid_engine.py`의 `MANDATORY_SL_MIN_TIER=4`(SL 필수 등록 기준)가 5-tier 풀 구조 전용 하드코딩 상수였다. `MAX_STAGE=3`(3-tier 압축 운용, xlsx가 "3차 필수 SL"로 명시)으로 쓰면 major_tier가 4에 절대 도달 못 해 **SL이 영원히 등록되지 않는** 상태였다.
    - 수정: `MANDATORY_SL_MIN_TIER` 모듈 상수를 제거하고 `GridEngine.mandatory_sl_min_tier`(기본값 4, 기존 5-tier 동작 그대로 하위호환) 필드로 전환. `engine/restart_recovery.py`의 `reconstruct_state`/`build_recovered_engine`도 동일 파라미터를 받아 GridEngine과 일관되게 검증하도록 변경. `config/settings.py`에 `mandatory_sl_min_tier: int = 4` 추가, `main.py`가 배선. `.env`/`.env.example`에 `MANDATORY_SL_MIN_TIER` 항목 추가(현재 3-tier 운용 기준 3으로 설정) + README 표에도 추가.
  - **버그 2 [자금효율]**: `engine/grid_setup.py`의 `build_grid_rows()`가 `max_stage` 절삭을 `compute_grid()` 호출 **이후**(반환된 rows 리스트만 자르는 방식)에 적용하고 있었다. `compute_grid()`의 `weight_sum`은 넘겨받은 weights 리스트 전체 합이라, `MAX_STAGE=3`으로 60단계만 실제로 쓰더라도 weight_sum은 항상 100단계 전체 합(17130)이었다 — 그 결과 60단계 가중치 합(3530, 전체의 20.6%)만큼만 equity가 배정되고 **나머지 약 79%는 어느 단계에도 배정되지 않은 채 낭비**됐다. xlsx는 반대로 60개 가중치만으로 정규화(분모 3530)해 equity 전액을 3-tier에 배정하는 설계였다.
    - 수정: `build_grid_rows()`가 `compute_grid()`를 호출하기 **전에** `weights` 리스트 자체를 `max_stage * STEPS_PER_TIER`로 잘라서 넘기도록 변경(반환된 rows를 사후 절삭하던 방식 제거). `strategy/grid.py`의 `compute_grid()`가 정확히 `TOTAL_STEPS`(100)개만 받던 검증을 `1..TOTAL_STEPS`개로 완화(골든 테스트는 항상 100개를 그대로 넘기므로 영향 없음, `tests/test_golden.py` 그대로 통과 확인).
    - **효과**: 라이브 가격/leverage=40/max_stage=3 기준 최소 시드가 수정 전 약 2,832 USDT에서 수정 후 약 **548 USDT**로 감소(같은 3-tier 구조에 자금이 낭비 없이 배정되므로).
  - 테스트: `tests/test_grid_setup.py`에 재정규화 검증 신규 1개(`test_build_grid_rows_renormalizes_weights_to_active_tiers`) + 기존 `test_build_grid_rows_truncates_to_max_stage` 업데이트(feasibility가 60단계보다 먼저 걸리는 게 이제 정상임을 반영), `tests/test_grid_engine.py`/`tests/test_restart_recovery.py`에 커스텀 `mandatory_sl_min_tier` 관련 신규 3개. `tests/test_main.py`의 `direction="both"` 테스트도 새 증거금 배정값(28.3, 이전 5.8)으로 갱신. 전체 스위트 170개 → **174개 통과**.

- **완료(2026-08-05)**: 즉시 진입("숏!"/"롱!") 도구 추가 — 사용자 요청("지금 숏! 하면 현재가부터
  50테더씩 타더더덕 체결되게끔 하는 모드도 추가해줘"). 명확화 결과: launcher.py 메뉴에 추가,
  총 금액(3k/5k/직접입력)을 사용자가 정하고 그걸 50 USDT(설정 가능, `QUICK_ENTRY_CHUNK_USDT`)
  단위로 나눠 지정가 주문을 명령 입력 시점에 전부 한 번에 예약 걸어두는 방식, TP/SL은
  manual_mode와 동일하게 사용자가 직접 관리.
  - `quick_entry.py` 신규 — `run_quick_entry()`가 `strategy.grid.compute_grid()`를 그대로
    재사용한다(weights를 전부 1로 균등하게 줘서 청크마다 동일 증거금이 배정되게 함 — 새
    재무 공식을 만들지 않고 골든 테스트가 지키는 검증된 계산을 그대로 씀). `engine/grid_engine.py`의
    정식 엔진과는 완전히 독립적 — RSI 필터/TP 재등록/SL 등록/hybrid reset 전부 없음.
  - `config/settings.py`에 `quick_entry_chunk_usdt: Decimal = 50` 추가.
  - `launcher.py`: 최상위 메뉴를 "[0] 무엇을 할까요?"로 바꿔 "봇 실행"/"즉시 진입" 분기.
    기존 `main()` 본문은 `_run_bot()`으로 분리(하위호환, 동작 동일). 즉시 진입은 방향(숏/롱)
    → 총 금액(3k/5k/직접입력) → 연습/실전 모드를 순서대로 물어보고, 실전이면 봇 실행과
    동일한 이중 확인 절차(먼저 `_configure_mode()`의 일반 경고, 이어서 방향/금액을 명시한
    두 번째 확인)를 거친다.
  - **구현 중 발견한 버그, 수정함**: (1) `compute_grid()`는 100단계(`TOTAL_STEPS`)까지만
    받는데(strategy/grid.py), quick_entry가 청크 개수를 그대로 넘기다 보니 청크 100개를
    넘는 조합(예: 총액 10만/청크 50=2000개)에서 어댑터/엔진과 무관한 원본 `ValueError`가
    그대로 새어나갔다 — `num_chunks > TOTAL_STEPS`를 명시적으로 검증해 `QuickEntryError`로
    막도록 수정(총액을 줄이거나 청크를 키우라고 안내). (2) launcher.py의 연습 모드 경로에서
    `PaperAdapter`가 갓 생성돼 현재가를 모르는 상태(`on_price_tick` 미호출)라 `get_ticker()`가
    `NoKnownPriceError`로 죽었다 — main.py의 상시 가격관찰 루프가 하던 일을 launcher가 대신
    실행 직전에 한 번 `market_data_adapter.get_ticker()`로 조회해 주입하도록 수정.
  - **테스트**: `tests/test_quick_entry.py` 신규(7개 — 방향별 가격 진행/청크당 균등 증거금/나머지
    버림/총액 미달 시 에러/100개 초과 시 에러/정확히 100개 성공). 전체 스위트 174개 → **181개
    통과**. 추가로 pytest 밖에서 direction×amount×chunk 23개 조합 탐색 스모크(총액 0/음수/소수점/
    거대값/청크 크기 커스텀 등)와 launcher.py 대화형 흐름을 다양한 입력(정상/비정상 숫자,
    실전 확인 취소 등)으로 in-process stdin 재현 검증 — 전부 paper 모드로만 실행, 실주문 없음.
  - **정정(2026-08-05, 같은 날 실사용 중 발견)**: 위 구현은 launcher의 "3,000/5,000 USDT"
    선택지를 **증거금 총액**으로 해석했었는데, 사용자가 실제로 의도한 건 **현재가 기준
    ±가격 범위**였다("3k/5k는 마진 금액이 아니라 현재가 기준 +-(롱/숏) 가격 범위 ... 현재가가
    62k라면 3k 선택시 65k까지 50불 단위로 60개 체결"). 기본 설정(`GRID_TICK`=`QUICK_ENTRY_
    CHUNK_USDT`=50)에서는 두 해석이 숫자상 우연히 같은 결과(3000/50=60)를 내서 겉으로
    드러나지 않았었다. `quick_entry.py`를 정정: 주문 개수는 이제 `price_range_usdt //
    grid_tick`으로 정하고(신규 `compute_chunk_count()`), 청크당 증거금은 `quick_entry_
    chunk_usdt`로 범위와 완전히 독립적으로 고정한다. `launcher.py`도 미리보기(주문
    개수/개당·총 증거금/레버리지)를 실행 확인 전에 출력하도록 추가. 주문 접수 로그에도
    레버리지/체결가/진입 마진을 명시(사용자 요청). `tests/test_quick_entry.py`에 회귀
    테스트 2개 추가(grid_tick과 quick_entry_chunk_usdt를 다르게 둬도 개수/증거금이 안
    섞이는지). 전체 스위트 181개 → **183개 통과**.

- **완료(2026-08-05)**: `launcher.py` 메뉴를 번호 입력 대신 방향키(↑/↓)+Enter로 고를 수
  있게 함(사용자 요청 — "번호 입력 말고 방향키와 엔터로 할 수 있도록"). `_ask_choice()`가
  `sys.stdin.isatty()`(실제 콘솔에 붙어 있는지)와 `msvcrt` 존재 여부(Windows 전용, 이
  프로젝트는 run.bat로만 배포되므로 문제없음)로 분기 — 참이면 `msvcrt.getch()`로 화살표
  raw 입력을 받아 ANSI 커서 이동으로 메뉴를 다시 그리는 `_ask_choice_by_arrows()`를 쓰고,
  아니면(파이프/자동화/테스트로 stdin을 리다이렉트한 경우) 기존 `_ask_choice_by_number()`로
  그대로 폴백한다 — 이 세션에서 만든 in-process stdin 재현 테스트 하네스가 전부 폴백
  경로를 타므로 그대로 유효함(재검증: 183개 통과 그대로). 실전 매매 확인용 `"실행"` 타이핑
  게이트는 의도적으로 건드리지 않음(Enter 연타로 실수 확정되는 걸 막는 안전장치라 메뉴
  선택으로 바꾸지 않는 게 맞다고 판단).

- **완료(2026-08-05)**: 즉시 진입 증거금 배분을 균등에서 weights.csv 비중 기반으로 재정정
  — 사용자 정정: "진입 마진은 항상 50usdt가 아니야. 엑셀에 기재된 비중대로 진입 마진 설계."
  `quick_entry.py`의 `run_quick_entry()`가 이제 `strategy.weights.load_weights()`로 실제
  엑셀 비중을 가져와 `settings.equity_usdt` 전액을 배분한다(균등 청크 대신 메인 격자
  엔진과 완전히 동일한 마진 배분 로직) — `settings.quick_entry_chunk_usdt` 필드는 제거.
  - **주문 개수(price_range_usdt // grid_tick, 위 항목에서 정정된 설계)는 그대로 유지**하되,
    `weights.csv`를 그 개수만큼 슬라이스해서(`max_stage` 절삭과 동일한 재정규화 원리,
    engine/grid_setup.py 참고) 넘긴다 — **한 번 즉시 진입을 실행할 때마다 그 방향에 배정된
    EQUITY_USDT 전액이 소진되고, 가격 범위가 좁을수록(단계 수가 적을수록) 단계당 증거금은
    오히려 커진다.**
  - `compute_preview_rows()` 신규 — 실행 확인 전 마진 미리보기용. margin은 가격과 무관하게
    weight/equity 비율로만 정해지므로, 실제 현재가 조회 없이(오프라인) 더미 base_price로
    `compute_grid()`를 호출해도 `step_margin`만큼은 실행 시와 정확히 동일하다(entry_price/
    step_qty 등 가격 의존 필드는 이 미리보기에서 버려짐). `launcher.py`의 미리보기 출력을
    `단계당 증거금 O ~ O USDT (총 증거금 O USDT)`로 변경.
  - `config/settings.py`/`.env.example`/`README.md`에서 `QUICK_ENTRY_CHUNK_USDT` 제거.
  - **테스트**: `tests/test_quick_entry.py` 재작성 — weights.csv 앞 5개 값(10,11,12,13,14)과
    equity=10000/leverage=20으로 손계산한 값(1666.7/1833.3/2000.0/2166.7/2333.3, 합계
    정확히 10000.0)을 회귀 기준으로 사용. 청크 개수가 달라져도 총 증거금은 항상
    equity_usdt 전액이라는 불변식 테스트 추가. 전체 스위트 183개 → **182개**(테스트
    통합으로 순감소, 실질 커버리지는 늘어남 — 균등분배 전제였던 낡은 assertion들을
    weights.csv 기반 assertion으로 교체).

- **완료(2026-08-05)**: 즉시 진입 로그 정리 + 레버리지 실행 전 입력 (사용자 요청 —
  "order_id와 INFO는 안 보여줘도돼. 그리고 레버린지는 그 전에 설정 가능하도록.").
  - `quick_entry.py`의 주문별 로그에서 `order_id` 제거. `launcher.py`의
    `logging.basicConfig(...)`에 `format="%(message)s"`를 추가해 `INFO:quick_entry:`
    같은 로깅 프리픽스 없이 메시지만 그대로 출력하도록 변경.
  - `launcher.py`: 진입 범위 다음 단계로 레버리지 입력을 추가(`.env`의 `LEVERAGE`가
    기본값, 이번 실행에만 적용되고 `.env`는 안 바뀜). `Settings().model_copy(update=
    {"leverage": ...})`로 오버라이드해 미리보기 계산과 실제 실행(`_go()`)이 동일한
    값을 쓰도록 함(`direction="both"`에서 이미 쓰던 패턴과 동일).
  - 거래소 쪽 실제 레버리지(`set_leverage`)는 건드리지 않음 — 기존 메인 봇도
    어디서도 `set_leverage`를 호출하지 않고 `settings.leverage`를 순수 수량 계산용
    파라미터로만 쓰는 기존 설계를 그대로 따름(실거래소 레버리지는 사용자가 거래소
    앱에서 직접 맞춰야 함, 기존과 동일한 한계).

- **완료(2026-08-05)**: 메뉴에서 잘못 골랐을 때 이전 단계로 돌아가는 기능 추가(사용자
  요청 — "잘못 입력해서 이전 단계로 돌아가는 것도 만들어줘"). `_GoBack` 예외 신설.
  - 방향키 메뉴(`_ask_choice_by_arrows`)는 ←(Left), 번호 폴백(`_ask_choice_by_number`)은
    `0`, 텍스트 입력(`_ask_amount`)은 `b`/`뒤로`를 누르면 `_GoBack`을 던진다.
  - `_configure_mode()`의 실전 매매 확인("실행" 타이핑)도 취소 시 기존처럼 흐름 전체를
    끝내는 대신 `_GoBack`을 던지도록 변경 — 취소하면 프로그램이 죽는 대신 이전 단계로
    돌아간다.
  - `_run_bot()`(모드→청산방식)과 `_run_quick_entry()`(방향→범위→레버리지→미리보기→모드)
    를 각각 단계 인덱스를 갖는 while 루프로 재작성해 `_GoBack`을 잡아 바로 전 단계로
    되돌린다. 각 흐름의 **첫** 단계에서 뒤로가면 예외를 그대로 재발생시켜(`raise`) 함수
    자체를 빠져나가고, `main()`이 이를 잡아 최상위 "[0] 무엇을 할까요?" 메뉴로 되돌린다
    (`main()`도 while 루프로 재작성). 미리보기 계산이 `QuickEntryError`로 실패하면(범위가
    너무 작거나 100단계를 넘음) 진입 범위 단계로 자동으로 돌려보낸다.
  - paper 모드로 세 가지 뒤로가기 경로 전부 실제 실행까지 확인: 중간 단계에서 뒤로(방향
    재선택 후 정상 완료), 실전 확인 취소(레버리지 단계로 복귀 후 재시도로 정상 완료),
    첫 단계에서 뒤로(최상위 메뉴 복귀 후 다른 흐름 선택 정상 진행).
  - **참고**: 이 세션 도중 `test_main.py::test_run_writes_halt_flag_when_engine_halts_via_fill_router`가
    launcher.py 변경과 무관하게 실패하기 시작한 걸 발견 — `git stash`로 되돌린 커밋된
    상태에서도 동일하게 실패함을 확인해 이 변경과 무관한 기존 이슈임을 검증했다(원인
    미조사, 다음 세션에서 확인 필요 — 로그상 `가용잔고가 음수로 전환되는 단계 발견 —
    72단계까지만 사용`으로 이전 관측값(60단계)과 달라진 것으로 보아 완전히 모킹되지
    않은 라이브 시세 의존성이 있는 것으로 추정).

- **완료(2026-08-05, 실전 사고 수정)**: 즉시 진입을 실전으로 실행했는데 화면엔 "주문
  N개 접수 완료"가 떴지만 거래소 확인 결과 아무 주문도 안 걸려있었던 사고 수정.
  읽기전용으로 실제 계좌를 조회해 세션 시작 때와 포지션/미체결 주문이 완전히
  동일함을 확인 — 즉 새 주문이 하나도 안 걸린 게 맞았고, 부분 체결 등 위험한
  중간 상태는 아니었다.
  - **근본 원인 2가지**:
    1. `launcher.py`의 `_go()`가 `build_execution_adapter()`에 넘기는 `settings`를
       `leverage`만 오버라이드하고 `direction`은 그대로 뒀다 — `OrangeXAdapter`의
       `position_side`가 `.env`의 `DIRECTION`(당시 `long`)으로 설정된 채, 사용자가
       즉시 진입에서 고른 방향(`short`)의 매도 주문을 걸었다. 헤지 모드 계좌에서
       `position_side` 불일치는 주문을 접수 직후 거래소가 자동 취소한다(기존에
       이미 문서화된 error_code 5998 패턴, `exchange/orangex/adapter.py` 모듈
       docstring 참고) — `direction="both"` 지원 시 이미 겪었던 문제인데, 이번엔
       quick_entry라는 새 경로에서 `.env`의 DIRECTION과 실행 시 선택한 방향이
       달라질 수 있다는 걸 놓쳤다.
    2. `run_quick_entry()`가 `place_limit_order()`의 반환값(`OrderResult.status`)을
       전혀 확인하지 않았다 — 거래소가 즉시 취소해도 `place_limit_order()` 자체는
       예외를 던지지 않고 `status="cancelled"`인 정상 `OrderResult`를 돌려주는데,
       `order_ids.append(...)`만 하고 넘어가서 전부 취소됐어도 "접수 완료"로
       잘못 보고했다.
    - **paper 모드로는 이 버그를 절대 발견할 수 없었던 이유**: `PaperAdapter`는
      `position_side` 개념 자체가 없어 이 시나리오를 재현 못 한다 — 이번 세션
      내내 paper 모드로 아무리 폭넓게 테스트해도 못 잡을 수밖에 없었던 사각지대.
  - **수정**: `launcher.py`의 `_go()`가 `model_copy(update={"leverage": ..., "direction":
    direction})`로 `direction`도 함께 오버라이드하도록 수정. `quick_entry.py`의
    `run_quick_entry()`가 매 주문마다 `result.status`를 확인해 `cancelled`/`rejected`면
    즉시 `QuickEntryError`를 던지도록 수정(이미 접수된 개수도 메시지에 포함).
  - **테스트**: `tests/test_quick_entry.py`에 `_RejectingAdapter`(PaperAdapter를 상속해
    지정한 호출 순번부터 거래소의 즉시 취소를 재현하는 테스트 더블) 신규, 회귀 테스트
    2개 추가(전체 즉시취소 시 에러, 부분 취소 시 접수된 것까지만 남고 에러). 전체
    스위트 182개 → **185개 통과**.
  - **다음 확인 필요**: 코드는 고쳤지만 이 세션에서 실제 라이브로 재검증은 안 함(사용자
    명시적 요청 있을 때만 라이브 테스트 진행하는 SPEC 0번 원칙) — 다음에 사용자가
    실전으로 다시 시도할 때 이번엔 거래소에 실제로 주문이 걸리는지 반드시 확인 필요.

- **완료(2026-08-06, 두 번째 실전 사고 수정)**: position_side 수정 후에도 사용자가
  실전으로 다시 시도했는데 또 안 됐다("또 했는데 안돼"). 읽기전용 재조회 결과 이번에도
  새 주문이 하나도 안 걸려있었음(위험한 부분체결 없음). 진짜 근본 원인을 찾음:
  **`min_trade_amount`(수량 증가 단위, 예: BTC 0.001) 필드를 코드 어디서도 반영한 적이
  없었다.** `docs/api-notes.md` §4에 이미 문서화돼 있었지만(`min_qty`와 별개 필드로
  명시) `exchange/base.py`의 `ContractSpec`에는 애초에 이 필드가 없었다 — `compute_grid()`
  는 나눗셈으로 수량을 계산하므로 결과가 `0.006760034349168474805147131123`처럼 소수점
  20자리 넘게 이어지는데, 이런 값을 그대로 주문에 넣으면 거래소가 정밀도 불일치로 즉시
  거부한다(라이브 `/public/get_instruments` 재조회로 BTC-USDT-PERPETUAL의 `min_trade_
  amount="0.001"`, `quantityPrec=3` 확인).
  - **왜 이전까지 아무도 못 봤는지**: 지금까지 라이브로 검증됐던 모든 주문 스크립트
    (`scripts/orangex_test_market_order.py` 등)는 전부 `"0.001"` 같은 손으로 쓴 깔끔한
    값만 썼다 — `compute_grid()`가 만든 실제 계산값을 라이브로 보낸 건 quick_entry가
    처음이었다. **`engine/grid_engine.py`(메인 자동매매 봇)도 정확히 동일하게 `row.
    step_qty`를 반올림 없이 그대로 주문에 넣는다** — `main.py`를 `trading_mode=live`로
    끝까지 기동해본 적이 아직 없어서(기존에 알려진 한계) 이 문제가 지금까지 드러나지
    않았을 뿐, 메인 봇도 실전에서는 100% 동일하게 실패할 것이다. **메인 봇은 아직
    이 수정을 반영 안 함 — 다음 작업으로 반드시 처리할 것.**
  - **수정(quick_entry.py만)**: `ContractSpec`에 `qty_step: Decimal = Decimal("0")`
    필드 추가(기본값 0="미확인"은 기존 테스트 호출부 전부와 하위호환). `OrangeXAdapter.
    get_contract_spec()`이 `min_trade_amount`를 파싱해 채움. `exchange/base.py`에
    `round_qty_to_step(qty, step)` 공용 헬퍼 신규(내림 — 반올림으로 올리면 의도한
    증거금을 넘어설 수 있어서). `quick_entry.py`의 `run_quick_entry()`에 `contract_spec`
    파라미터 추가, 주문 걸기 전 `round_qty_to_step()`으로 내림하고 반올림 후 min_qty/
    min_notional 미달이면 `QuickEntryError`로 명확히 막음(애매하게 잘려서 거래소가 또
    조용히 거부하게 두지 않음). `launcher.py`가 이미 갖고 있던 `contract_spec`을 그대로
    전달하도록 배선.
  - **테스트**: `tests/test_quick_entry.py`의 `make_spec()` 기본값을 실제 BTC 값(qty_
    step=0.001)으로 바꾸고, 순수 비중 계산만 검증하던 기존 테스트들은 `qty_step=0`
    으로 명시해 반올림 간섭을 배제. 신규 2개(수량이 0.001 배수로 내림되는지, 반올림
    후 미달 시 명확한 에러). 전체 스위트 185개 그대로 통과(신규 2개 + 조정).
  - paper 모드 스모크로 확인: 이전엔 `0.006760034349168474805147131123` 같던 로그의
    수량이 이제 `0.006`처럼 깔끔하게 나온다(연습 모드도 실제 라이브 계약스펙을 조회해
    쓰므로 이 수정이 그대로 반영됨).
  - **다음 확인 필요(우선순위 높음)**: (1) 이번에도 라이브 재검증은 안 함 — 다음 실전
    시도 때 실제로 주문이 걸리는지 확인. (2) **`engine/grid_engine.py`에 동일한 반올림
    로직을 적용하는 작업이 시급함** — 지금 상태로 메인 봇을 라이브로 켜면 quick_entry와
    똑같이 모든 주문이 조용히(또는 이번 quick_entry 수정 이후엔 최소한 명시적 에러로)
    실패할 것이다.

- **완료(2026-08-06)**: 실제로 min_qty 미달 상황을 겪고 나서, 사용자 요청("지금 설정으로
  최대 범위가 얼마인지 알려주는 안내도 추가해")으로 실행 전 미리 알려주도록 개선.
  - `quick_entry.py`: `_rounded_qty_or_none()`(반올림+min_qty/min_notional 판정을 한
    곳에 모음, `run_quick_entry()`와 아래 신규 함수가 공유) 신규. `compute_max_feasible_
    chunk_count(settings, direction, adapter, contract_spec)` 신규 — 실제 현재가로
    1~100단계까지 순서대로 시도해 전 단계가 반올림 후에도 최소 주문 조건을 만족하는
    최대 단계 수를 계산한다.
  - `launcher.py`의 STEP_PREVIEW를 완전히 재작성 — 이전엔 (더미 가격으로) 증거금
    미리보기만 하고 실제 최소수량 판정은 `_go()`의 실제 실행 시점(모드 선택/실전 확인을
    다 거친 뒤)에야 이뤄졌다. 이제 미리보기 단계에서 실제 현재가로 `compute_max_
    feasible_chunk_count()`를 먼저 계산해, 선택한 범위가 이 한도를 넘으면 "현재 설정
    (EQUITY_USDT=.., LEVERAGE=..)으로는 최대 N단계(진입 범위 M USDT)까지만 가능합니다"
    라고 즉시 안내하고 진입 범위 단계로 돌려보낸다. 성공하는 경우에도 미리보기 줄
    아래에 "(현재 설정 기준 최대 진입 범위: M USDT, 최대 N단계)"를 항상 표시해 다음에
    참고할 수 있게 함.
  - paper 모드로 확인: `.env`(`EQUITY_USDT=150`, `LEVERAGE=40`) 기준 실제 최대 범위가
    1,650 USDT(33단계)로 계산됨 — 3,000 프리셋을 고르면 미리보기 단계에서 바로 걸러져
    범위 재선택으로 돌아가고, 1,650 이하를 고르면 정상 진행되는 것까지 확인.
  - 전체 스위트 185개 통과(신규 함수는 기존 `run_quick_entry()` 통합 테스트로 간접
    커버 — 별도 유닛 테스트는 다음에 추가 가능).

## 아직 만들지 않은 것 (다음 작업)
- **[긴급/안전] `engine/grid_engine.py`에 수량 정밀도(qty_step) 반올림 적용**: quick_entry.py는
  2026-08-06에 고쳤지만(`round_qty_to_step()`, `exchange/base.py`), 메인 자동매매 봇
  (`_refresh_grid_orders`/`_reregister_tp`/`_reregister_sl`/`_force_close_and_halt`/
  `maybe_hybrid_reset` — `engine/grid_engine.py`)은 여전히 `compute_grid()`의 미가공
  수량을 그대로 주문에 넣는다. **지금 상태로 메인 봇을 라이브로 켜면 모든 주문이 거래소
  정밀도 불일치로 실패할 것**(quick_entry가 겪은 것과 동일 원인). `GridEngine`이 `contract_
  spec`(또는 최소 `qty_step`)을 갖고 있지 않아 생성자/`build_recovered_engine`/`CycleManager`
  까지 함께 손봐야 하는 더 큰 변경 — 메인 봇을 라이브로 켜기 전 반드시 먼저 처리할 것.
- **최소 주문 미달 단계 병합 로직 실제 구현**: 정책은 이미 결정됐음(docs/phase1-report.md: 다음 단계에 합산). 병합하려면 `compute_grid()`의 누적 계산(cum_qty/avg_price/liq_price/TP/SL이 전부 이전 단계에 순차적으로 의존)을 병합 인식형으로 다시 짜야 하는데, 이건 골든 테스트(`tests/test_golden.py`, 엑셀 원본 대조)가 지키는 핵심 재무 계산이라 서둘러 손대면 실제 계산 오류를 만들 위험이 크다. default 설정에서는 이 상황 자체가 발생 안 함을 확인했고 지금은 안전하게 시작을 거부만 하므로, 실제로 이 상황이 발생하는 설정을 쓰게 될 때 제대로 다시 설계해서 구현하는 게 낫다고 판단해 미룸.
- **`engine/restart_recovery.py`를 OrangeX 라이브로 실제 기동해서 끝까지 검증**: `get_open_orders()` 블로커는 풀렸지만, 이 모듈이 라이브 데이터로 실제로 상태를 정확히 재구성하는지는 아직 실전 확인 전.
- **`main.py`를 `trading_mode=live`로 실제 기동**: 지금까지는 전부 개별 조각(watch_fills, place_market_order, get_open_orders 등)을 따로따로 라이브 검증했다 — `main.py` 전체를 live 모드로 처음부터 끝까지 돌려본 적은 없음.

## 미해결 항목

- RSI/ATR용 캔들 데이터 소스 미확인
- 주문/취소 직후 상태 반영 지연 — `_get_order_state_with_retry()`로 완화했으나 근본 원인(서버측 인덱싱 지연 추정)은 여전히 확정 안 됨
- `trigger_price_type=1`(mark price) 미검증, 트리거 슬리피지 정도 미확인
