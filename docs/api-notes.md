# OrangeX API 조사 노트 (Phase 0)

조사 대상: https://openapi-docs.orangex.com/
조사일: 2026-07-24
조사 방법: 문서 페이지를 여러 각도로 조회해 정리. **문서에 명시되지 않은 값은 추측하지 않고 "미확인 항목"으로 별도 목록화했다.**

---

## 1. 전체 아키텍처

- 프로토콜: **JSON-RPC 2.0**, Deribit 계열 API와 동일한 설계.
- HTTP base: `https://api.orangex.com/api/v1`
- WebSocket: `wss://api.orangex.com/ws/api/v1`
- 메서드 네임스페이스: `/public/*`(공개), `/private/*`(인증 필요)
- 응답 포맷: `{ id, jsonrpc, result }` 또는 `{ id, jsonrpc, error }`. 타이밍 메타데이터로 `usIn`, `usOut`, `usDiff` 포함.
- 문서 구성: 단일 페이지(앵커 기반 목차) — Introduction / JSON-RPC / Authentication / Account / Wallet / Trading / MarketData / SubscriptionManagement / Subscriptions / CopyTrade / Convert / CMC & CoinGecko.

---

## 2. 인증

엔드포인트: `/public/auth`

### grant_type 종류

| grant_type | 용도 | 파라미터 |
|---|---|---|
| `client_credentials` | API 키/시크릿 평문 전달 | `client_id`, `client_secret` |
| `client_signature` | 서명 기반 (권장) | `client_id`, `signature`, `timestamp`, `nonce`(선택) |
| `refresh_token` | 토큰 갱신 | `refresh_token` |

### 서명 알고리즘 (`client_signature`)

```
StringToSign = clientId + "\n" + Timestamp + "\n" + Nonce + "\n"
Signature = HEX_STRING( HMAC_SHA256( key=ClientSecret, data=StringToSign ) )
```

- `Timestamp`: 밀리초 단위 UNIX epoch.
- `Nonce`: 사용자 생성 값, 선택 사항.

### 요청/응답 예시 (client_credentials)

```json
// Request
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "/public/auth",
  "params": {
    "grant_type": "client_credentials",
    "client_id": "qpdskdfnaowpfewq",
    "client_secret": "oqentgrfnfdwnveaef"
  }
}

// Response
{
  "id": "1",
  "jsonrpc": "2.0",
  "result": {
    "access_token": "ba3avFNzfE",
    "token_type": "bearer",
    "refresh_token": "X9VtGUvsWvzPAjx63jzZnY+yTPTC8Ip8GYHSK29j0teOXcjsA=",
    "expires_in": 43199,
    "scope": "account:read_write trade:read_write wallet:read_write"
  }
}
```

### WebSocket 인증

동일하게 `/public/auth`를 WS 커넥션 위에서 호출해 `access_token`을 얻고, 이후 `/private/*` 메서드 및 `/private/subscribe` 호출 시 params에 `access_token`을 포함시켜 사용한다. (WS 방식은 미검증 — 아래 REST 정정 참고.)

### ⚠️ REST 정정 (2026-07-29, OrangeX 지원팀 확인): access_token은 params가 아니라 HTTP 헤더로 보낸다

문서 예시(위)는 body에 뭘 넣는지만 보여주고 인증된 `/private/*` 호출에서 `access_token`을 어떻게 전달하는지는 명시하지 않는다. Phase 2 초기에 "params에 access_token 포함"으로 추측 구현했는데, `get_positions`/`get_assets_info` 등 **모든** `/private/*` 호출이 예외 없이 `Authentication Failure`(10000)를 반환했다. 지원팀 답변으로 정정됨:

```
Authorization: bearer {access_token}
```

를 HTTP 헤더로 보내야 한다 (params에는 넣지 않음). `exchange/orangex/client.py`의 `call()`을 이 방식으로 수정한 뒤 라이브로 즉시 `get_positions`/`get_assets_info` 성공을 확인했다 — 지금까지의 모든 `/private/*` 인증 실패(항목9, 11, 12 참고)는 사실 이 헤더 누락 하나가 원인이었다.

---

## 3. 필요 엔드포인트 매핑 (SPEC 요구사항 기준)

| SPEC 요구 | 문서상 메서드 | 비고 |
|---|---|---|
| 심볼/계약 스펙 조회 | `/public/get_instruments`, `/public/get_perpetual_instrument_config` | §4 참고 |
| 잔고 조회 | `/private/get_current_account_information` (Account 섹션) | 정확한 응답 필드는 미확인 (§6) |
| 포지션 조회 | ~~`/private/get_positions`~~ → **`/private/get_user_position`** (2026-07-30 정정, §6 항목13) | `liquid_price`, `margin_type`, `stop_loss_price`, `take_profit_price`, `risk_level` 포함 |
| 포지션 청산 | `/private/close_position` | |
| 지정가/시장가 주문 | `/private/buy`, `/private/sell` | §5 참고 |
| 주문 취소 | ~~`/private/cancel_by_id`~~ → **`/private/cancel`**(`order_id`) (2026-07-30 정정, §6 항목15). `/private/cancel_by_currency`, `/private/cancel_by_instrument`는 미검증 | |
| 주문 조회 | `/private/get_order_state`, `/private/get_open_order_by_instrument`, `/private/get_open_order_by_currency`, `/private/get_order_history_by_instrument` | |
| 레버리지 설정 | `/private/modify_perpetual_instrument_leverage` | 파라미터: `instrument_name`, `leverage` |
| 스톱 주문(SL/TP) 등록 | buy/sell 파라미터 내 조건부 필드 | §5, 메커니즘 불확실 (§6) |
| 체결 스트림 (WS) | `/private/subscribe` → `user.orders.{instrument_name}.raw`, `user.trades.{instrument_name}.{interval}`, `user.changes.{kind}.{currency}.{interval}` | |
| 하트비트 | `/public/ping` | 5초 간격 권장, 동시 연결 10개 미만 |

### 구독 관리

- 공개: `/public/subscribe`, `/public/unsubscribe`
- 사설(인증 필요): `/private/subscribe`, `/private/unsubscribe`
- 공통 파라미터: `channels` (array, 구독할 채널 목록)

---

## 4. 계약 스펙 필드

`/public/get_instruments` 응답에서 확인된 필드 (문서가 인스트루먼트 종류별로 균일하게 문서화되어 있지 않음 — 예시값만 제공):

| 필드 | 의미 |
|---|---|
| `min_trade_amount` | 최소 거래 수량 스텝 |
| `min_qty` | 최소 거래 수량 |
| `contract_size` | 계약 크기 |
| `tick_size` | 최소 가격 변동 단위(가격 틱) |
| `min_notional` | 최소 명목가치 (기준통화) |
| `instr_multiple` | 배수 (예시값 `"0.01"`만 제공, 정식 설명 없음) |
| `maker_commission` | 메이커 수수료율 |
| `taker_commission` | 테이커 수수료율 |

`/public/get_perpetual_instrument_config` 응답: `margin_type`(`cross`/`isolated`), `leverage`(현재 사용 가능 레버리지) 정도만 확인됨 — 이 엔드포인트가 계약 스펙 전체를 주는지는 불확실.

**주의**: BTC-USDT-PERPETUAL, ETH-USDT-PERPETUAL 각각의 실제 수치(틱 사이즈, 최소 수량 등)는 문서 예시값이 아니라 **라이브 API 호출로 재검증 필요** (Phase 2에서 수행).

---

## 5. 주문 파라미터 (`/private/buy`, `/private/sell`)

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `instrument_name` | O | 예: `BTC-USDT-PERPETUAL` |
| `amount` | O | 주문 수량 |
| `type` | X | `limit`(기본) 또는 `market` |
| `price` | limit일 때 O | 지정가 |
| `time_in_force` | X | `good_til_cancelled`(기본), `fill_or_kill`, `immediate_or_cancel` |
| `post_only` | X | 기본 false |
| `reduce_only` | X | 기본 false |
| `custom_order_id` | X | 클라이언트 주문 ID. 정규식 `^[\.A-Z\:/a-z0-9_-]{1,36}$`. 주문 조회 시 그대로 반환 → **SPEC의 client_order_id(UUID) 요구사항과 매핑 가능** — 단, 2026-07-30 라이브 확인 결과 `/private/buy`,`/private/sell`의 **응답**에는 이 값이 그대로 반환되지 않고 빈 문자열로 옴(§6 항목14) |
| `position_side` | **헤지 모드 계좌에서는 사실상 필수** | 문서 표에는 없던 필드. `LONG`/`SHORT`. 생략하면 서버가 기본값 `BOTH`(원웨이 모드용)로 처리해 기존 포지션과 충돌 → 주문이 즉시 자동 취소됨(§6 항목14, 2026-07-30 라이브 확인) |

### 조건부 주문 (파생상품 전용)

| 파라미터 | 설명 |
|---|---|
| `condition_type` | `NORMAL`, `STOP`, `TRAILING`, `IF_TOUCHED` — **자체 order_id를 가진 별도(조건부) 주문**을 생성 |
| `trigger_price` | STOP/IF_TOUCHED 트리거 가격 |
| `trigger_price_type` | `1`=mark price, `2`=last price |
| `trail_price` | TRAILING용 |
| `stop_loss_price` / `take_profit_price` / `stop_loss_type` / `take_profit_type` | **진입 주문(`buy`/`sell`)에 붙는 필드.** "Only for perpetuals". 값 0 = 미설정. `get_open_order_by_instrument` 응답에서 이 필드들은 **진입 주문과 같은 order_id 안에** 나타남 (독립 주문 아님) |

### ✅ [부분 해결, 2026-07-30] 조건부 주문(`condition_type=STOP`)이 실제로 트리거되어 체결됨을 라이브로 확인

Phase 2까지 "SL/TP 메커니즘 불확실"로 남겨두고 스코프에서 제외했었으나, Phase 3 설계 중 사용자 요청으로 실제로 라이브 검증했다 (`scripts/orangex_test_stop_order.py`, `scripts/orangex_test_stop_order_cancel.py`):

- BTC-USDT-PERPETUAL LONG 포지션(size=0.026) 대비 `condition_type=STOP`, `trigger_price_type=2`(last price), reduce_only=True인 매도 조건부 주문을 현재가에서 1틱 떨어진 trigger_price로 걸자, 다음 폴링(약 3초 후)에서 `order_state=open→filled`, `filled_amount=0.001`로 실제 체결됐고 포지션도 0.026→0.025로 실제 감소했다. **주의**: 최초 시도에서 trigger_price를 현재가에서 5틱 떨어뜨려 "이미 조건 충족" 상태로 걸었을 때는 24초 동안 전혀 트리거되지 않았다 — level-trigger(조건이 이미 참이면 즉시 체결)가 아니라 **crossing-trigger**(주문 이후 실제 가격이 트리거를 가로질러야 발동)로 추정된다. 실제 구현 시 이 특성을 반영해야 한다(트리거가는 항상 주문 시점 가격보다 "아직 도달 안 한" 방향으로 설정).
- 트리거 전(현재가 대비 5% 이격) 상태의 STOP 주문을 `/private/cancel`로 취소하자 `order_state: open→canceled`가 정상 반영됐다 — 기존 지정가 주문과 동일한 취소 메커니즘을 그대로 사용할 수 있음을 확인.

**결론: `condition_type=STOP`은 진짜로 동작하는 거래소 등록형 SL이다.** Phase 3 실행 엔진은 4~5차 진입 시 이 방식으로 실제 SL을 등록하고, 평단 변경 시 취소 후 재등록하는 SPEC 원래 설계를 그대로 따를 수 있다 (`docs/phase3-plan.md` 참고). 다만 아래는 여전히 미확인:
- trigger_price_type=1(mark price) 동작은 테스트 안 함(last price만 확인)
- 트리거 판정이 정확히 몇 초 간격으로 갱신되는지(폴링 간격 3초에서 처음엔 미반영, 두 번째에 반영됨)
- 대량 물량이나 슬리피지가 큰 상황에서 트리거 후 실제 체결가가 trigger_price와 얼마나 벌어질 수 있는지(시장가 체결이므로 슬리피지 가능성 있음)

### ⚠️ [상위 항목으로 대체됨] SL/TP 메커니즘 불확실 — Phase 0에서 가장 크리티컬한 미확인 항목이었음

문서상 SL/TP를 거는 방법이 **최소 3개 층위**로 나뉘어 있고, 서로 어떻게 상호작용하는지 확정할 수 없었다:

1. **주문 레벨 필드** — `buy`/`sell` 호출 시 `stop_loss_price`/`take_profit_price`를 진입 주문에 동봉. 이 진입 주문이 체결되면 별도 order_id 없이 그 주문에 종속된 SL/TP로 등록되는 것으로 보이나, 체결 후 트리거 방식(자동으로 반대 시장가 주문이 나가는지, 별도 대기 주문이 새로 생기는지)은 문서에 없음.
2. **조건부 주문(STOP/IF_TOUCHED)** — 동일한 `/private/buy`, `/private/sell`로 걸지만 `condition_type`을 지정하면 **독자적인 order_id를 가진 별도 주문**이 생성됨. 이건 취소(`cancel_by_id`)·조회가 명확히 가능.
3. **포지션 레벨 필드** — `get_positions` 응답에도 별도로 `stop_loss_price`/`take_profit_price`가 존재. 이게 (1)과 같은 값을 반영하는 read-only 뷰인지, 포지션에 독립적으로 설정 가능한 필드인지 불명확.

**결정적 공백: 일반 거래(non-copy)에는 "기존 포지션/주문의 SL/TP를 나중에 수정"하는 전용 엔드포인트가 없다.** (`(Copy) Order TP/SL`은 CopyTrade 전용으로만 문서화됨.)

이게 SPEC과 직접 충돌하는 지점: 이 봇은 격자 체결마다 평단가가 바뀌고, 그때마다 SL을 최신 평단 기준으로 재등록해야 한다(SPEC §Phase3 "체결될 때마다... 기존 TP 주문 취소 후 재등록"). 그런데:
- SL이 진입 주문에 종속된 필드라면 → 평단이 바뀔 때 "무엇을 취소하고 무엇을 다시 걸어야 하는지" 자체가 불명확 (진입 주문을 재발주해야 하나?)
- 전용 수정 API가 없다면 → 매번 새 조건부 주문(`condition_type=STOP`)을 걸고 이전 것을 취소하는 방식이 유일한 선택지일 가능성이 높지만, 이걸 문서가 명시적으로 보장하지 않음 → 취소-등록 사이 레이스 컨디션에서 포지션이 무방비 상태로 노출될 위험

→ **Phase 2(거래소 어댑터) 착수 전 반드시 라이브(또는 사용자 확인)로 해소해야 함.** 잘못 가정하면 "SL을 걸었다고 봇이 믿지만 실제로 걸려있지 않은" 상태가 발생할 수 있고, 이는 SPEC 0번 절대원칙(실제 돈을 잃을 수 있다) 위반 시나리오와 정확히 일치한다.

### 주문 상태 조회 (`/private/get_order_state`)

응답 필드: `order_id`, `order_state`(`open`, `filled`, `rejected`, `cancelled`), `instrument_name`, `direction`(`buy`/`sell`), `amount`, `price`, `filled_amount`, `average_price`, `condition_type`, `trigger_price`

**⚠️ 2026-07-30 라이브 정정**: 문서와 달리 실제 값은 `cancelled`(영국식, L 2개)가 아니라 **`canceled`(미국식, L 1개)**다. 취소된 주문에는 `error_code`(예: `5998` — 의미 미확인)도 함께 온다. `position_side`(`LONG`/`SHORT`/`BOTH`) 필드도 응답에 포함된다.

---

## 6. 문서에서 확인 불가능했던 항목 (추측하지 않고 목록화)

1. **[해결됨, 2026-07-30] SL/TP 등록·수정 메커니즘** — §5 상세 참고. Phase 2까지는 사용자가 SL 자동화를 스코프에서 제외해 우회했었으나, Phase 3 설계 중 `condition_type=STOP` 조건부 주문을 라이브로 검증해 실제 거래소 등록형 SL로 사용 가능함을 확인했다(§5 신규 항목, `docs/phase3-plan.md`). 트리거는 level-trigger가 아니라 crossing-trigger로 추정된다는 점이 구현 시 반영해야 할 특성으로 남는다.
2. ~~레이트 리밋 수치~~ **→ OrangeX 지원팀 답변(2026-07-28)으로 확정: 10 req/s.** 응답 헤더 등 서버측 실측 근거는 없고 지원팀 확인값을 그대로 신뢰 — `exchange/orangex/client.py`에 클라이언트측 스로틀(`MAX_REQUESTS_PER_SECOND=10`)로 반영함.
3. **테스트넷/데모 환경** — 존재 여부, URL 모두 문서에서 찾지 못함. **여전히 미확인.**
4. **API 키 권한(출금 권한 여부) 조회 전용 엔드포인트** 존재 여부 — SPEC Phase 4 "API 키 권한 확인" 가드에 필요하나 문서에서 못 찾음. **여전히 미확인.**
5. **펀딩비 조회 엔드포인트/정산 주기 산정 방식** — Phase 1의 손익분기 계산과 Phase 5 백테스트에 필요. **여전히 미확인.**
6. **WebSocket 재연결/타임아웃 정책** — 하트비트(`/public/ping`) 실패 시 서버가 몇 초 후 연결을 끊는지 명시 없음. **여전히 미확인.**
7. ~~계약 스펙 실측값~~ **→ Phase 2에서 라이브로 확인 완료.** `docs/phase2-report.md` §1 참고 (BTC/ETH의 실제 tick_size, min_qty, min_notional, maker/taker 수수료).
8. ~~Account/잔고 조회 응답의 정확한 필드 스키마~~ **→ 해결됨 (2026-07-29).** 메서드명 `/private/get_assets_info`, 필드명 `available_funds`/`wallet_balance`/`total_margin_balance`는 지원팀 답변대로였고, **`asset_type` 파라미터는 일반 문자열이 아니라 JSON 배열이어야 한다**는 것도 지원팀이 추가로 확인해줌 (예: `{"asset_type": ["PERPETUAL"]}`). 헤더 정정(위 §2, 항목12)과 배열 형식 수정을 함께 반영한 뒤 라이브로 실제 잔고 데이터를 성공적으로 받았다 (`exchange/orangex/adapter.py`의 `get_balance()`). 응답 envelope은 `{"PERPETUAL": {...}}` 형태(중첩)로 확인됨.
9. **`client_signature` 인증 방식 자체는 여전히 미해결이지만 더 이상 블로커가 아님** — 문서의 서명 공식을 그대로 구현하고 6가지 변형을 시도했으나 전부 실패했던 건 그대로 사실이다. 하지만 항목12에서 **모든 `/private/*` 실패의 진짜 원인이 access_token 전달 위치(헤더 vs params)였음이 확인**되어, `client_credentials`만으로도 모든 필요한 조회가 정상 동작한다. `client_signature`가 왜 거부되는지는 여전히 궁금하지만, 우선순위는 낮춘다 (선택: 계속 궁금하면 후속 문의, 아니면 `client_credentials`로 계속 진행).
10. ~~`/public/get_perpetual_instrument_config`가 라이브에서 존재하지 않음~~ **→ 확인됨.** `{"code": 1000, "message": "No service found"}`. `/public/get_open_order_by_instrument`도 헤더 정정 이후에도 동일하게 "No service found"를 반환 — 파라미터/인증 문제가 아니라 URL/메서드명 자체가 실제 서버에 없는 것으로 최종 확인됨. `/public/get_instruments`만으로 계약 스펙에 필요한 값을 전부 얻으므로 문제 없음. **주문 조회(`get_open_order_by_instrument`)는 대체 엔드포인트(`get_open_order_by_currency` 등)를 Phase 3 전에 확인해야 함 — 아직 미확인.**
11. ~~`/private/get_positions`의 인증 실패~~ **→ 해결됨 (2026-07-29), 그러나 항목13에서 재조사됨.** 1차 가설(스코프 부족)은 기각됐었고, 인증 실패의 진짜 원인은 항목12의 헤더 문제였다. 헤더 정정 후 파라미터 8가지 조합 전부 "성공"(에러 없음)했지만, 그때 반환된 `[]`는 사실 계좌가 무포지션이라서가 아니라 **이 메서드 자체가 이 계좌에서 포지션이 있을 때도 항상 빈 배열만 반환하는 문제**였던 것으로 나중에 밝혀짐 (§6 항목13, 2026-07-30).
12. **[해결] 근본 원인 확정: access_token을 params가 아니라 `Authorization: bearer {token}` HTTP 헤더로 보내야 했다** — OrangeX 지원팀 답변(2026-07-29)으로 확인. 지금까지 `get_positions`/`get_assets_info` 등 모든 `/private/*` 호출이 예외 없이 실패했던 것(항목9/11에서 각각 다른 가설로 접근했던 것)은 사실 이 하나의 원인이었다. `exchange/orangex/client.py`의 `call()`을 헤더 방식으로 수정한 뒤 `get_positions`/`get_assets_info` 라이브 호출 전부 성공. `client_credentials`로 발급한 토큰은 정상적으로 트레이딩 계열 엔드포인트에 쓸 수 있다 — "client_credentials 토큰이 애초에 막혀있다"는 이전 가설은 틀렸었다.
13. **[해결됨, 2026-07-30] `get_position()`의 실제(비-flat) 포지션 필드 매핑 + 근본적인 메서드명 오류 발견** — 사용자 계좌에 실제 포지션(BTC-USDT-PERPETUAL, cross, short)이 생긴 뒤 확인해보니, 문서(§3)에 명시된 `/private/get_positions`는 이 계좌에서 파라미터를 20가지 이상(빈 값/instrument_name/currency×kind 전 조합/kind=swap·linear·option·spot/margin_type/position_side/subaccount_id 등) 바꿔봐도 **항상 빈 배열만 반환했다** — 원인 불명, 서버 버그로 추정. 반면 `get_assets_info`에는 `total_upl_cross`/`total_initial_margin_cross` 등이 0이 아니어서 서버에는 분명히 포지션이 있었다. 시행착오로 실제 포지션을 반환하는 메서드가 `/private/get_user_position`(문서에 없는 이름)임을 찾았고, 응답 필드도 확정했다: `position_side`(`LONG`/`SHORT`), `size`(부호 있는 수량, 음수=short), `average_price`, `margin_type`, `leverage`, `floating_profit_loss` 등. `exchange/orangex/adapter.py`의 `get_position()`을 이 메서드로 전환해 라이브로 검증 완료 (`scripts/orangex_position_diagnostic.py`, `scripts/orangex_get_position_live_check.py`). **주의(2026-07-30 해소)**: flat(무포지션) 상태는 이전엔 `/private/get_positions`로만 검증됐었으나, 항목15의 cancel_order 테스트 시작 시점에 계좌가 마침 flat 상태가 되어 `/private/get_user_position`도 빈 배열(`[]`)을 반환함을 라이브로 확인했다 — 기존 가정이 맞았음이 확정됨.
14. **[해결됨, 2026-07-30] 사용자 요청으로 실행한 최초 실주문에서 `place_limit_order`의 세 가지 문제 발견.** BTC-USDT-PERPETUAL 숏 추가용 매도 지정가(0.002 BTC @ 64660)를 실제로 걸어본 결과:
    - **1차 시도(scope 문제)**: API 키 scope가 `trade:read`뿐이라 `Access denied`(code 2033)로 거부됨 — 사용자가 앱에서 Trading 쓰기 권한을 켠 뒤 통과.
    - **2차 시도**: API 호출 자체는 성공(order_id 발급)했지만 `get_order_state`로 확인해보니 `order_state=canceled`(`error_code=5998`, 체결 0)였다. `/private/buy`,`/private/sell` **응답에는 `order_id`/`custom_order_id`만 오고 `order_state`/`filled_amount` 등 상태 필드가 전혀 없다** — 상태를 알려면 `get_order_state`를 별도로 호출해야 함(§5 참고). 참고로 이때 `custom_order_id`는 우리가 보낸 값이 아니라 빈 문자열로 돌아옴.
    - **3차 시도(레버리지 가설, 기각)**: `get_order_state` 응답에 `"leverage": 25`가 찍혀 앱에서 확인한 실제 설정(50배)과 달라 레버리지 파라미터를 명시적으로 추가(`leverage: "50"`)해 재시도했으나 응답은 여전히 25, 효과 없음 — 기각.
    - **4차 시도(성공)**: `get_order_state` 응답에 `"position_side": "BOTH"`가 눈에 띄어, 계좌가 헤지 모드(`dual_side_position=true`)인데 주문에 `position_side`를 안 넘겨서 서버가 기본값 `BOTH`(원웨이 모드 규약)로 처리 → 기존 SHORT 포지션과 충돌해 자동 취소된 것으로 판단. `position_side: "SHORT"`를 명시하자 즉시 `order_state=open`, `leverage=50`으로 정상 반영되며 성공.
    - **부가 발견**: `order_state`의 실제 철자는 문서(`cancelled`, 영국식)와 달리 **`canceled`(미국식, L 1개)**다. `exchange/orangex/adapter.py`의 `_ORDER_STATE_TO_STATUS` 매핑에 두 철자를 모두 반영했고, 매핑에 없는 미확인 `order_state`는 조용히 "open"으로 폴백하지 않고 `OrangeXResponseSchemaError`를 던지도록 강화했다.
    - `OrangeXAdapter`는 이제 생성 시 `position_side`(SPEC상 봇 인스턴스당 단일 방향, `config/settings.py`의 `direction`과 연동 예정)를 받아 모든 주문에 태깅하고, `place_limit_order()` 내부에서 주문 접수 후 `get_order_state`를 자동으로 재조회하도록 수정됨. (`scripts/orangex_place_first_live_order.py`에 시행착오 전체 기록.)
15. **[해결됨, 2026-07-30] `cancel_order` 실주문 검증 — 문서 메서드명 `cancel_by_id`가 실제로는 존재하지 않음.** 사용자 요청(증거금 1 USDT 한도)으로 BTC-USDT-PERPETUAL 매도 지정가(0.001 BTC @ 67304.5, 현재가 대비 +5%로 즉시체결 방지)를 걸고 취소까지 검증했다:
    - 주문 접수 및 `get_order_state`로 `order_state=open` 확인까지는 정상.
    - `/private/cancel_by_id`(`order_id`)를 호출하자 `{"code": 1000, "message": "No service found"}` — `get_positions`(→`get_user_position`), `get_open_order_by_instrument`(§6 항목10)와 동일한 패턴으로, **문서화된 메서드명 자체가 서버에 없다.**
    - 후보 메서드명을 순차 시도(`cancel`, `cancel_order`, `cancel_by_order_id`, `order_cancel`, `cancel_order_by_id`, `user_cancel_order`, `cancel_all_by_instrument`)한 끝에 **`/private/cancel`**(파라미터는 문서와 동일하게 `order_id`)이 정상 동작함을 확인. 호출 후 `get_order_state`로 재조회하니 `order_state`가 `open → canceled`로 바뀌었고 `filled_amount=0`, `error_code=0`(§5의 자동취소 케이스 `error_code=5998`과 구분됨)이었다.
    - `exchange/orangex/adapter.py`의 `cancel_order()`을 `/private/cancel`로 수정하고 `tests/test_orangex_adapter.py`도 갱신함 (43개 테스트 전부 통과 유지).
    - **부수 발견 1**: 이 테스트 시작 시점에 계좌가 우연히 flat 상태였다 — `/private/get_user_position`이 `[]`를 반환하는 것을 확인해, 항목13에서 미확인으로 남겼던 **flat 케이스도 이번에 검증 완료**(`/private/get_positions`로 검증했던 예전 flat 동작과 동일하게 빈 배열).
    - **부수 발견 2**: 계좌가 flat이라 포지션 기반으로 현재가를 못 구해 공개 가격 조회 엔드포인트를 탐색했고, **`/public/ticker`**(파라미터: `instrument_name`)가 `last_price`/`mark_price`/`best_bid_price`/`best_ask_price`/`stats` 등을 반환함을 확인했다. `/public/get_order_book`, `/public/get_last_trades_by_instrument`도 정상 동작. (`/public/get_ticker`, `/public/get_index_price`, `/public/get_mark_price`는 "No service found".) 이 문서에는 원래 ticker류 엔드포인트가 전혀 없었는데, Phase 3의 격자 가격 기준(현재가)을 얻는 데 바로 쓸 수 있다.
    - **미해결로 남은 것**: `get_open_orders()`가 쓰는 `/private/get_open_order_by_instrument`는 여전히 "No service found" 상태 그대로다(§6 항목10) — 이번 테스트는 취소 확인에 집중하느라 대체 엔드포인트 탐색까지는 못 갔다. 별도로 진행 필요.
16. **[해결됨, 2026-07-30] 주문 접수/취소 직후 즉시 조회 시 지연·오류가 발생함 — "즉시 재조회"가 안전하지 않다.** 2026-07-30 네 차례 실주문 테스트에서 반복 관찰됨:
    - **주문 접수 직후** `get_order_state`를 지연 없이 바로 호출하면 두 번 다 `data`에 `"result"`도 `"error"`도 없는 응답으로 `KeyError: 'result'`가 발생했다(raw body 미확보, 재현은 됐지만 정확한 응답 형태는 못 잡음). **2초 대기 후 호출하니 정상적으로 `order_state=open`을 반환**했다 — 서버측 인덱싱 지연으로 추정(확정은 아님).
    - **취소 직후**도 마찬가지 패턴: `/private/cancel` 호출은 즉시 성공 응답(`{"order_id": ...}`)을 주지만, 그 직후 `get_order_state`를 호출하면 `order_state`가 여전히 `open`으로 나온다(두 차례 재현). **5초 대기 후에는 정상적으로 `canceled`로 반영됐다.**
    - **세 번째 재현(2026-07-30, `place_market_order` 라이브 검증 중)**: 시장가 진입 주문이 실제로 체결됐는데(`get_user_position`으로 교차 확인됨) 직후 `get_order_state` 호출이 다시 `KeyError: 'result'`로 죽어서 스크립트가 청산 단계 전에 중단, 포지션이 잠깐 미청산 상태로 남았다(수동으로 정리, `scripts/orangex_cleanup_open_long.py`). 재시도 없이 5초 대기 후 재조회하니 정상적으로 `filled`가 반영됨 — 기존 관찰과 일치.
    - **해결**: 세 번째 재현으로 재시도 정책을 미룰 이유가 없어져 `OrangeXAdapter._get_order_state_with_retry()`로 구현했다 — 즉시 1회 시도 후 실패하면 2/3/5초 간격으로 최대 3회 재시도(총 대기 10초), 그래도 실패하면 `OrangeXResponseSchemaError`. `place_limit_order`/`place_stop_order`/`place_market_order` 전부 적용.
17. **[해결됨, 2026-07-30] `get_open_orders()`용 "현재 미체결 주문만" 조회하는 엔드포인트를 결국 찾았다 — 원인은 단/복수 표기 차이였다.** 1차 시도(10개 이상 후보: `get_open_order_by_instrument`, `get_open_order_by_currency`, `get_open_orders`(파라미터 유무 둘 다), `get_user_open_order(s)`, `open_orders`, `get_orders`, `get_orders_by_instrument`)는 전부 "No service found"였다. 유일하게 성공했던 `get_order_history_by_instrument`는 종료된(체결/취소/거부) 주문만 주는 순수 이력 조회라 대체가 안 됐다. **2차 시도(`scripts/orangex_find_open_orders_endpoint_v2.py`)에서 `/private/get_open_orders_by_instrument`(복수형 "orders" — 기존에 시도했던 건 전부 단수형 "order"였다)가 성공**함을 확인했다. `get_positions`처럼 "성공은 하지만 있어도 항상 빈 배열"인 함정일 가능성이 있어 `scripts/orangex_verify_get_open_orders.py`로 실제 미체결 주문 하나를 걸고(현재가 대비 20% 낮은 지정가, 즉시체결 방지) 조회 → 목록에 실제로 나타남 확인 → 취소 → 목록에서 사라짐까지 전부 교차 검증했다. `exchange/orangex/adapter.py`의 `get_open_orders()`를 이 메서드로 전환 완료 — **`engine/restart_recovery.py`의 라이브 블로커가 해소됐다.**
18. **[해결됨, 2026-07-30] `condition_type=STOP` 조건부 주문이 실제 거래소 등록형 SL로 동작함을 라이브로 확인.** Phase 3 실행 엔진 설계 중 사용자 요청으로 검증(`scripts/orangex_test_stop_order.py`, `scripts/orangex_test_stop_order_cancel.py`):
    - **1차 시도(실패)**: BTC-USDT-PERPETUAL LONG 포지션(size=0.026) 대비 매도 STOP 주문을 현재가에서 5틱 떨어진 trigger_price(현재가보다 높게 설정 — "이미 조건 충족" 상태를 의도)로 걸었으나, 24초(8회 폴링) 동안 `order_state=open`, `filled_amount=0`으로 전혀 트리거되지 않았다.
    - **2차 시도(성공)**: trigger_price를 현재가에서 1틱만 떨어뜨리자, 다음 폴링(약 3초 후)에 `order_state=open→filled`, `filled_amount=0.001`로 실제 체결됐고, `get_user_position` 재조회로 실제 포지션이 0.026→0.025 BTC로 감소했음을 확인 — 실제로 자금이 움직이는 진짜 체결임을 교차검증.
    - **결론**: OrangeX의 STOP 트리거는 **level-trigger가 아니라 crossing-trigger**로 추정된다 — 주문 시점에 조건이 이미 참이어도 트리거되지 않고, 주문 이후 실제 가격이 trigger_price를 "가로질러야" 발동하는 것으로 보인다. Phase 3 구현 시 trigger_price는 항상 "주문 시점 가격 기준 아직 도달하지 않은 방향"으로 설정해야 한다.
    - **취소 검증**: 트리거 전(현재가 대비 5% 이격) STOP 주문에 기존 `/private/cancel`(order_id)을 호출하자 `order_state: open→canceled`로 정상 취소됨 — 일반 지정가 주문과 동일한 취소 메커니즘을 그대로 재사용 가능.
    - **파라미터**: `condition_type: "STOP"`, `trigger_price`, `trigger_price_type: 2`(last price — mark price인 1은 미검증), 나머지는 일반 `/private/buy`,`/private/sell` 파라미터와 동일(`position_side` 헤지모드 필수 등 §5 기존 규칙 그대로 적용됨).
    - **미검증**: `trigger_price_type=1`(mark price), 트리거 판정 갱신 주기의 정확한 상한, 트리거 후 실제 체결가와 trigger_price 간 슬리피지 정도.
    - **Phase 3 영향**: SPEC 원래 설계(4~5차 진입 시 거래소 SL 필수 등록, 평단 변경마다 취소 후 재등록, 등록 실패 시 강제청산)를 그대로 구현 가능해짐 — 자세한 내용은 `docs/phase3-plan.md` 참고.
19. **[완전 확인, 2026-07-30] WebSocket 체결 스트림 연결/인증/구독/실제 체결 스키마까지 라이브 검증**:
    - 1차(`scripts/orangex_probe_ws_fills.py`, 읽기전용 — 주문 없음): `wss://api.orangex.com/ws/api/v1` 연결 성공.
    - **`/public/auth`는 REST와 동일하게 `client_credentials`로만 성공한다** — `client_signature`는 WS에서도 `Authentication Failure`(code 10000)로 실패 재현됨(§6 항목9의 REST 문제와 동일 패턴).
    - **WS 응답의 `id`는 요청과 달리 문자열로 echo된다**(REST는 그대로 정수) — 매칭 시 문자열 정규화 필요. `exchange/orangex/ws_client.py`에 반영됨.
    - **인증된 WS 호출은 문서 그대로 `access_token`을 params에 넣는 방식이 실제로 통한다** — REST와 다른 점(REST는 헤더 방식이어야 했다, §2 정정 참고). WS는 문서가 맞았다.
    - **구독 성공 확인된 채널**: `user.orders.{instrument}.raw`, `user.trades.{instrument}.raw`, `user.trades.{instrument}.100ms` 전부 정상 구독 응답(`result`에 채널명 echo). `user.changes.future.USDT.raw`(추측 시도)는 `channel regex not match`(code 3401)로 거부됨 — 문서에 없던 형식이라 예상대로 실패.
    - **2차(`scripts/orangex_observe_live_fill_ws.py`, 사용자 명시적 요청, 2026-07-30): 실제 체결 스키마 확인.** BTC-USDT-PERPETUAL 0.001 BTC LONG 진입 후 즉시 청산(순노출 원복, 최종 포지션 flat 확인)하며 `user.trades.{instrument}.raw` 알림을 실시간 캡처했다. 실제 payload 필드 전체:
      `instrId`, `direction`("buy"/"sell"), `amount`, `price`, `timestamp`, `role`("taker"), `rpl`(실현손익 추정), `posId`, `positionSide`(카멜케이스 — 주의: `get_user_position`의 `position_side`와 표기가 다름), `leverage`, `marginType`, **`fee`**, `feeCoupon`, `feeActual`, `feeReal`, `source`, `trade_id`, `order_id`, `instrument_name`, `show_name`, `order_type`, `fee_use_coupon`, `fee_coin_type`, `index_price`, `mark_price`, `self_trade`.
      **`fee` 필드명이 정확히 확정됐다** — `OrangeXAdapter._parse_trade_to_fill()`이 이미 이 이름으로 파싱하고 있었어서 코드 수정 불필요했다. **`custom_order_id`는 이 실제 payload에 아예 없었다** — `.get(..., "")` 방어 처리가 이미 있어 문제없이 빈 문자열로 처리됨(FillRouter는 order_id로만 매칭하므로 라우팅 영향 없음). 실제 payload는 `tests/test_orangex_adapter.py`의 `test_watch_fills_parses_real_live_trade_payload`에 그대로 고정해뒀다.
    - **여전히 미확인**: WS 하트비트/재연결 정책(§6 항목6과 동일), 장시간 연결 유지 시 토큰 만료 갱신 방식.

---

## 7. ccxt 지원 여부

**결론: ccxt는 orangex를 지원하지 않는다.**

- 로컬 환경에 ccxt가 설치되어 있지 않음 (`python -c "import ccxt"` → `ModuleNotFoundError`).
- 패키지를 설치하지 않고, GitHub `ccxt/ccxt` 저장소의 `python/ccxt/` 디렉터리 파일 목록을 직접 조회하여 확인 — `orangex.py` 파일이 존재하지 않음. binance.py, kraken.py, coinbase.py 등 다른 거래소는 존재.
- 참고: 사용자가 원한다면 `pip install ccxt` 후 SPEC이 지정한 다음 명령으로 재확인 가능하다 (현재는 소스 확인만으로 충분하다고 판단해 설치는 생략함):
  ```
  python -c "import ccxt; print('orangex' in ccxt.exchanges)"
  ```

**→ SPEC 3번 지침에 따라 OrangeX 전용 클라이언트를 직접 작성해야 한다 (Phase 2).**

---

## 8. 요약: 다음 Phase 전 확인이 필요한 것

Phase 1(계산 엔진)은 거래소와 무관하므로 이 API 조사 결과와 독립적으로 진행 가능하다. 단, **Phase 2(거래소 어댑터) 시작 전에는 §6의 미확인 항목, 특히 SL/TP 메커니즘·레이트 리밋·테스트넷 유무·계약 스펙 실측값을 확정해야 한다.**
