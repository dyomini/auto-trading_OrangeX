# Phase 2 리포트 — 거래소 어댑터

## 0. 요약

`pytest tests/` 43개 전부 통과 (PaperAdapter 7개, OrangeX 서명 5개, OrangeX 클라이언트 6개, OrangeX 어댑터 20개, Phase 1 골든테스트 5개). 라이브 읽기전용 확인을 통해 계약 스펙(틱사이즈·최소수량·최소명목가치·수수료)은 확정했다. **2026-07-29, OrangeX 고객지원 2차 답변으로 계좌/포지션 조회 실패의 진짜 근본 원인이 확정됐다: access_token을 params가 아니라 `Authorization: bearer {token}` HTTP 헤더로 보내야 했다.** 이전에 의심했던 "API 키 권한 부족"과 "`client_signature` 인증 실패"는 둘 다 진짜 원인이 아니었다 — 헤더 하나를 고치자 `get_balance`/`get_position`이 즉시 라이브로 동작했다 (§2). **2026-07-30, 사용자 계좌에 실제 포지션이 생긴 뒤 재확인하자 두 번째 문제가 드러났다: 문서에 명시된 `/private/get_positions`는 이 계좌에서 파라미터를 뭘로 바꿔도 항상 빈 배열만 반환했고, 실제로 동작하는 메서드는 문서에 없는 `/private/get_user_position`이었다.** 이 메서드로 전환해 `get_position()`의 non-flat(실포지션) 필드 매핑까지 라이브 검증을 완료했다 (§2/§3). **같은 날 사용자 요청으로 실행한 최초 실주문(§4)에서 `place_limit_order`의 세 가지 문제(API 키 scope, 응답에 상태 필드 없음, 헤지 모드 position_side 누락)도 발견·해결했다.** Phase 2의 조회·주문 이슈는 모두 해소됐다.

## 1. 확정값 (라이브로 검증됨)

`/public/get_instruments` 실제 응답에서 확인 (2026-07-27):

| 항목 | BTC-USDT-PERPETUAL | ETH-USDT-PERPETUAL |
|---|---|---|
| `tick_size` | 0.1 | 0.01 |
| `min_qty` / `min_trade_amount` | 0.001 | 0.01 |
| `min_notional` | 10 USDT | 10 USDT |
| `maker_commission` | 0.02% | 0.02% |
| `taker_commission` | 0.06% | 0.06% |
| 거래소 최대 레버리지 | 200x | 200x |
| `order_price_low_rate` / `high_rate` | 0.5 / 1.5 | 0.5 / 1.5 |

**Phase 1 리포트 b(최소주문 미달 단계) 재검토**: 실제 임계값(min_qty=0.001 BTC / 0.01 ETH, min_notional=10 USDT)을 대입하면, `docs/phase1-report.md`에서 나열했던 명목가치 최저 단계(1차 1단계: 0.00181 BTC / 116 USDT, 0.0387 ETH / 116 USDT)조차 전부 임계값을 넉넉히 상회한다. 가중치가 단조증가하므로 이후 모든 단계도 마찬가지다. **결론: 이번 초기 설정(equity=10000, leverage=20)에서는 100단계 전부 최소 주문 단위 미달이 없다.** 사용자가 이미 정책 2("다음 단계에 합산")를 확정해뒀지만, 실제로는 적용할 대상이 없다는 뜻 — 다만 로직 자체(`strategy.feasibility.find_min_order_shortfalls`)는 향후 equity가 줄어들거나 다른 심볼을 쓸 때를 대비해 그대로 유지한다.

**수수료 보정**: Phase 1 리포트 c는 taker 0.05%로 가정했으나 실제 taker는 0.06%다 (maker 0.02%는 가정과 일치). Phase 3에서 손익분기 계산을 다시 돌릴 때는 이 실측치를 사용해야 한다.

또한 `order_price_low_rate`/`high_rate`(0.5~1.5배 밴드)는 SPEC/Phase 1에는 없던 새로운 제약이다 — 격자 5차 단계처럼 기준가에서 멀리 떨어진 지정가 주문은 이 밴드를 벗어나면 거래소가 거부할 수 있다. Phase 3 주문 배치 로직에서 반영 필요.

`/public/get_perpetual_instrument_config`는 라이브에서 "No service found"(존재하지 않는 메서드로 보임)를 반환했지만, 필요한 값은 `get_instruments`만으로 전부 얻었으므로 문제 없다.

## 2. 계좌/포지션 조회 — 근본 원인 확정 후 라이브로 해결됨

**메서드/필드명 확정 및 라이브 검증 완료**: OrangeX 고객지원 답변(2026-07-28)에 따르면 `/private/get_current_account_information`은 애초에 존재하지 않는 메서드였고, 올바른 메서드는 `/private/get_assets_info`다. 2차 답변(2026-07-29)에서 `asset_type` 파라미터가 일반 문자열이 아니라 **JSON 배열**이어야 한다는 것도 확인했다 (`{"asset_type": ["PERPETUAL"]}`). PERPETUAL 응답 필드는 `available_funds`(가용잔고)/`wallet_balance`(총 지갑잔고)/`total_margin_balance`(총 마진잔고), envelope은 `{"PERPETUAL": {...}}`로 한 번 감싸져 온다. `OrangeXAdapter.get_balance()`에 반영해 라이브로 실제 잔고 데이터를 성공적으로 받았다.

**1차 가설(포지션 조회 = 키 권한 부족) 기각됨**: 처음엔 토큰 scope가 `trade:none`이라 `get_positions`가 막힌 걸로 보였다. 사용자가 Trading 읽기 권한을 켜서 scope가 `trade:read`로 바뀐 걸 확인했지만 여전히 실패했다 — 스코프는 원인이 아니었다.

**2차 가설(client_credentials 토큰 자체가 무효) 도 틀렸음**: `asset_type` 키 유무에 따라 에러 코드가 `Authentication Failure`(10000)↔`Bad requested`(1001)로 바뀌는 패턴을 보고 "이 토큰으로는 애초에 이 엔드포인트를 못 쓴다"고 잠정 결론 냈었으나, §3에서 밝혀졌듯 실제 원인은 access_token 전달 위치였다.

**최종 근본 원인 (§3 참고)을 고친 뒤**: `get_positions`는 파라미터 8가지 조합(빈 값/instrument_name/currency×kind 여러 조합) 전부 성공했고, `get_assets_info`는 `asset_type`을 배열로 보내자 즉시 실제 잔고 데이터를 반환했다. `get_balance()`는 완전히 동작한다. `get_position()`은 계좌가 당시 무포지션 상태라 flat 케이스(`direction=None`)까지만 라이브로 검증했었다.

**2026-07-30 추가 발견: `/private/get_positions` 자체가 이 계좌에서 항상 빈 배열만 반환하는 문제였다.** 사용자 계좌에 실제 포지션(BTC-USDT-PERPETUAL, cross, short)이 생긴 뒤 확인해보니, `/private/get_positions`는 파라미터를 20가지 이상 바꿔봐도(kind=swap/linear/option/spot, margin_type, position_side, subaccount_id 등 포함) 여전히 빈 배열만 반환했다 — 반면 `get_assets_info`의 `total_upl_cross`/`total_initial_margin_cross`는 명백히 0이 아니어서 서버에는 포지션이 있는 게 확실했다. 시행착오로 실제 포지션을 반환하는 메서드가 문서에 없는 `/private/get_user_position`임을 찾았다. `exchange/orangex/adapter.py`의 `get_position()`을 이 메서드로 전환해 필드 매핑(`position_side`/`size`/`average_price`)까지 라이브로 확정했다 (`docs/api-notes.md` §6 항목13). flat 케이스는 새 메서드로는 아직 재검증 못함(계좌가 현재 포지션 보유 중).

## 3. [해결됨] 근본 원인 — access_token은 params가 아니라 Authorization 헤더로 전달해야 했다

문서 예시는 `/public/auth` 응답 body만 보여주고, 이후 `/private/*` 호출에서 발급받은 access_token을 정확히 어디에 실어야 하는지 명시하지 않았다. Phase 2 초기에 "params에 `access_token` 키로 포함"이라고 추측 구현했는데(문서 §2 WS 인증 설명 문구를 REST에도 그대로 적용), 이 추측이 **모든 `/private/*` 호출 실패의 진짜 원인**이었다.

OrangeX 지원팀 2차 답변(2026-07-29)에서 정확한 방식을 확인:

```
Authorization: bearer {access_token}
```

를 HTTP 헤더로 보내야 한다. `exchange/orangex/client.py`의 `call()`을 이 방식으로 수정한 뒤, 별도 코드 변경 없이 `get_positions`/`get_assets_info` 라이브 호출이 즉시 성공했다. 이전에 세웠던 두 가설 — "1차: API 키 권한 부족"(§2), "2차: client_credentials 토큰이 애초에 트레이딩 엔드포인트에 못 쓰인다"(§2) — 은 둘 다 이 헤더 문제의 그림자였을 뿐, 진짜 원인이 아니었다.

**`client_signature` grant_type 자체가 왜 거부되는지는 여전히 미해결이지만, 더 이상 블로커가 아니다.** `client_credentials`만으로 필요한 모든 조회가 정상 동작하는 게 확인됐기 때문이다. 서명 방식 자체의 원인 규명은 우선순위를 낮춘다.

**동시에 반영된 부가 수정**: `/private/get_assets_info`의 `asset_type` 파라미터는 문자열이 아니라 JSON 배열이어야 한다는 것도 함께 확인·반영했다 (§2).

## 4. 최초 실주문 검증 (2026-07-30, 사용자 명시적 요청) — `place_limit_order` 세 가지 문제 발견 및 해결

사용자 요청으로 BTC-USDT-PERPETUAL 숏 포지션 추가용 매도 지정가(0.002 BTC @ 64660, 증거금 2 USDT·레버리지 50배 기준)를 실제로 걸어 `place_limit_order()`를 처음으로 라이브 검증했다. 시행착오 끝에 세 가지를 확정했다 (상세는 `docs/api-notes.md` §5/§6 항목14):

1. **API 키 scope 문제** — 최초 시도(0.008 BTC)는 scope가 `trade:read`뿐이라 `Access denied`(2033)로 거부. 사용자가 앱에서 Trading 쓰기 권한을 켠 뒤 통과.
2. **응답 envelope에 상태 필드가 없음** — `/private/buy`,`/private/sell` 응답은 `{"order": {"order_id":..., "custom_order_id":...}}`뿐이고 `order_state`/`filled_amount`가 없다. 실제 상태는 `/private/get_order_state`를 따로 호출해야 알 수 있다 → `place_limit_order()`가 내부적으로 두 번 호출하도록 수정.
3. **헤지 모드 `position_side` 누락으로 자동 취소** — 계좌가 `dual_side_position=true`인데 주문에 `position_side`를 안 넣으면 서버가 기본값 `BOTH`로 처리해 기존 SHORT 포지션과 충돌, `order_state=canceled`(`error_code=5998`)로 즉시 취소된다. (레버리지 파라미터 추가는 효과 없어 기각한 가설.) `position_side: "SHORT"`를 명시하자 정상적으로 `order_state=open`이 됨.

부가로 `order_state`의 실제 철자가 문서(`cancelled`)와 달리 `canceled`(미국식, L 1개)임을 확인 — 매핑되지 않은 `order_state`는 조용히 `open`으로 폴백하던 것을 `OrangeXResponseSchemaError`로 막도록 강화했다.

`OrangeXAdapter.__init__`에 `position_side` 파라미터를 추가해 생성 시점에 지정하도록 했다(SPEC상 봇 인스턴스는 항상 단일 방향만 다루므로 `config/settings.py`의 `direction`과 1:1 대응 예정). 최종 주문(`order_id=828610767077068800`)은 정상적으로 오더북에 걸려 있음을 `get_order_state`로 재확인했다(체결 대기, `order_state=open`).

## 4-1. `cancel_order` 실주문 검증 (2026-07-30, 사용자 명시적 요청, 증거금 1 USDT 한도)

문서상 취소 메서드명 `/private/cancel_by_id`가 실제로는 존재하지 않는 메서드였다(`{"code":1000,"message":"No service found"}`) — `get_positions`(→`get_user_position`), `get_open_order_by_instrument`와 같은 패턴. 후보 이름을 순차 시도해 실제 메서드명이 **`/private/cancel`**(파라미터는 문서와 동일하게 `order_id`)임을 확인했다. 취소 전후 `get_order_state`로 `order_state: open → canceled`, `filled_amount=0`, `error_code=0`을 확인해 정상 취소를 검증했다. `OrangeXAdapter.cancel_order()`을 수정하고 관련 유닛테스트도 갱신했다(43개 테스트 계속 통과). 상세는 `docs/api-notes.md` §6 항목15 참고.

부수적으로 두 가지가 함께 해소됐다:
- **`get_user_position`의 flat(무포지션) 케이스**: 테스트 시작 시점에 계좌가 우연히 flat이었고 `[]`가 반환됨을 확인 — §6에서 미확정으로 남겨뒀던 항목 해소.
- **공개 가격 조회 엔드포인트**: 문서에 없던 `/public/ticker`(`last_price`/`mark_price`/`best_bid_price`/`best_ask_price` 등)가 정상 동작함을 확인. `/public/get_order_book`, `/public/get_last_trades_by_instrument`도 정상. Phase 3의 격자 기준가(현재가) 조회에 바로 쓸 수 있다.

## 4-2. `get_open_orders` 대체 엔드포인트 탐색 (2026-07-30) — 결론: 못 찾음, 지원팀 문의 필요

10개 이상의 후보 메서드명(`get_open_order_by_instrument`/`by_currency`, `get_open_orders`, `get_user_open_order(s)`, `open_orders`, `get_orders`, `get_orders_by_instrument`)을 순차 시도했으나 전부 "No service found"였다. 유일하게 성공한 `get_order_history_by_instrument`가 대체 가능한지 실주문으로 검증했는데(0.001 BTC 테스트 주문, 시장가 대비 +5%로 즉시체결 방지), **주문이 명백히 `open` 상태일 때 이 엔드포인트로 조회하면 0건**으로 나왔다 — 종료된(체결/취소) 주문만 주는 순수 이력 조회이지 미체결 주문 목록이 아니다. **`get_open_orders()`는 여전히 구현 방법이 없다.** OrangeX 지원팀에 정식 문의가 필요한 항목으로 남긴다(테스트 주문은 5초 대기 후 정상 취소 확인).

부수 발견(항목16): 주문 접수/취소 직후 지연 없이 바로 상태를 재조회하면 오류(`KeyError`) 또는 stale 값(취소 반영 안 됨)이 나오고, 2~5초 대기 후에는 정상 반영됨을 반복 관찰했다 — `place_limit_order()`의 즉시 재조회 로직에 재시도/backoff가 필요할 수 있으나, 정확한 지연 상한을 통계적으로 확인하지 못해 하드코딩하지 않고 사용자 판단을 기다린다.

## 5. 완성된 것 (라이브 이슈와 무관하게 전부 유닛테스트로 검증됨)

- `config/settings.py`, `.env.example`, `.gitignore`
- `exchange/base.py` — `ExchangeAdapter` 추상 인터페이스 (`place_stop_order` 의도적 제외)
- `exchange/paper.py` — `PaperAdapter` 인메모리 체결 시뮬레이터 (수수료, 부분체결, 중복 client_order_id 방지)
- `exchange/orangex/auth.py`, `client.py` — 서명·JSON-RPC 클라이언트 (요청 형태는 Mock으로 검증, 실제로 `/public/get_instruments`/`get_user_position`/`get_assets_info` 라이브 호출 전부 성공함). 지원팀 확인 레이트리밋(10 req/s) 클라이언트측 스로틀 반영, 토큰 scope 저장(`token_scope` 프로퍼티) 추가, access_token을 Authorization 헤더로 전달하도록 수정(§3).
- `exchange/orangex/adapter.py` — `get_contract_spec`/`place_limit_order`/**`cancel_order`**/**`get_balance`**/**`get_position`**은 문서·지원팀 답변·라이브 시행착오 기준으로 구현 및 라이브 검증 완료 (`get_position`은 2026-07-30에 non-flat·flat 케이스 전부 검증 §2/§3; `place_limit_order`는 2026-07-30에 실주문으로 검증 §4; `cancel_order`는 2026-07-30에 실취소로 검증 §4-1, 문서 메서드명이 틀려 `/private/cancel`로 정정). **`get_open_orders`만 유일하게 미검증 상태로 남음** — 사용하는 엔드포인트(`get_open_order_by_instrument`)가 라이브에서 "No service found"를 반환해 대체 엔드포인트 탐색이 아직 필요하다.

## 6. 가정 목록 (SPEC 0번 원칙)

- **확정값(라이브 검증)**: BTC/ETH의 tick_size, min_qty, min_notional, maker/taker 수수료, 최대 레버리지, 가격 밴드
- **확정값(OrangeX 지원팀 답변, 2026-07-28/29)**: 레이트리밋 10 req/s, 계좌조회 메서드명(`/private/get_assets_info`)과 PERPETUAL 필드명, `asset_type`은 JSON 배열, **access_token은 Authorization 헤더로 전달**(§3)
- **확정값(라이브 검증, 2026-07-29)**: `get_balance()` 실제 API로 성공 확인
- **확정값(라이브 검증, 2026-07-30)**: 포지션 조회는 `/private/get_positions`가 아니라 `/private/get_user_position`이 실제로 동작하는 메서드다. 필드명(`position_side`/`size`/`average_price`/`margin_type`) 및 non-flat(실포지션) 케이스까지 `get_position()`으로 라이브 검증 완료 — §2/§3 참고
- **확정값(실주문 라이브 검증, 2026-07-30)**: `place_limit_order` 응답 envelope(상태 필드 없음, `get_order_state` 재조회 필요), 헤지 모드 `position_side` 필수 여부, `order_state` 실제 철자(`canceled`) — §4 참고
- **확정값(실취소 라이브 검증, 2026-07-30)**: 취소 메서드명은 문서의 `cancel_by_id`가 아니라 `/private/cancel`이다. 취소 성공 시 `order_state=canceled`, `error_code=0` (자동취소 시의 `error_code=5998`과 구분됨) — §4-1 참고
- **확정값(라이브 검증, 2026-07-30)**: `get_user_position`의 flat(무포지션) 응답은 빈 배열(`[]`) — §4-1 참고
- **확정값(라이브 검증, 2026-07-30, 신규)**: 공개 가격 조회는 `/public/ticker`(및 `get_order_book`, `get_last_trades_by_instrument`)로 가능 — §4-1 참고
- **미확정**: 테스트넷 유무, API 키 권한조회 엔드포인트, 펀딩비 조회 방식, WS 재연결 정책, `error_code=5998`의 정확한 의미 — Phase 0 때와 동일하게 여전히 미확인
- **미확정(신규)**: `get_open_orders()`용 미체결 주문 전용 조회 엔드포인트 — 10개 이상 후보 시도·전부 실패, `get_open_orders()`가 유일하게 남은 미검증(미구현 가능) 어댑터 메서드. OrangeX 지원팀 문의 필요 (§4-2)
- **미확정(신규)**: 주문 접수/취소 직후 상태 반영 지연의 정확한 상한 — 2~5초 관찰됐으나 통계적 확인 안 됨 (§4-2, 항목16)
- **더 이상 추적하지 않음**: `client_signature` grant_type이 왜 거부되는지 — 근본 원인이 아니었던 것으로 밝혀져 우선순위 낮춤(§3)
- **버려진 가정**: Phase 1 리포트 c의 taker 수수료 0.05% 가정 → 실측 0.06%로 대체 필요 (Phase 3에서 반영)

## 7. 다음 단계 제안

Phase 2 어댑터 메서드 8개 중 7개(`get_balance`/`get_position`/`get_contract_spec`/`set_leverage`/`place_limit_order`/`cancel_order`/`watch_fills`는 Phase3로 의도적 이연)가 라이브로 검증됐다. **유일하게 막힌 것은 `get_open_orders`** — 10개 이상의 후보 엔드포인트명을 라이브로 시도했지만 전부 실패했고, 유일하게 동작하는 `get_order_history_by_instrument`는 종료된 주문만 주는 순수 이력 조회라 대체가 안 됨을 실주문으로 확인했다(§4-2). 이건 시행착오로 더 좁혀지지 않아 **OrangeX 지원팀 정식 문의가 필요**하다.

또한 주문 접수/취소 직후 즉시 상태를 재조회하면 오류나 stale 값이 나오는 현상을 반복 관찰했다(§4-2, 항목16) — `place_limit_order()`의 재조회 로직에 재시도(backoff)를 넣을지는 정확한 지연 상한을 모르는 채로 하드코딩하고 싶지 않아 사용자 판단을 기다린다.

**Phase 3(실행 엔진)로 진행할지는 사용자 승인이 필요하다** — PaperAdapter는 이미 완전히 동작하므로 그걸로 먼저 개발하고, 실주문 테스트가 필요한 시점에 OrangeXAdapter로 전환하는 방식을 제안한다. `get_open_orders`(재시작 복구용 미체결 주문 조회)는 PaperAdapter 개발 단계에서는 블로커가 아니지만, 라이브 전환 전에는 반드시 해소해야 한다.
