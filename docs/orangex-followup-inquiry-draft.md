# OrangeX 후속 문의 초안 (client_signature 미해결) — [해결됨, 발송 불필요]

**2026-07-29 업데이트: 이 문의는 더 이상 보낼 필요가 없다.** 지원팀 2차 답변에서 진짜
근본 원인(access_token을 params가 아니라 `Authorization: bearer {token}` 헤더로 보내야
함, `asset_type`은 배열이어야 함)을 확인해줬고, 반영 후 `get_balance`/`get_positions`가
전부 라이브로 성공했다. `client_signature`가 왜 거부되는지는 여전히 안 풀렸지만
`client_credentials`로 모든 기능이 정상 동작하므로 더 이상 블로커가 아니다.
(`docs/api-notes.md` §6 항목9/12, `docs/phase2-report.md` §3 참고.) 아래 초안은 과거
기록으로만 남겨둔다.

---

지원팀 1차 답변(2026-07-28)에서 안내한 조건(ms 타임스탬프, hex lowercase, nonce 포함)은
우리가 이미 시도한 "변형 A"와 동일해서 문제가 풀리지 않았다. 아래 초안으로 후속 문의를
보내면 된다. **API_KEY만 채우고 API_SECRET은 절대 넣지 말 것.**

---

## 한국어 버전

안녕하세요, 답변 감사합니다.

말씀해주신 조건(타임스탬프 밀리초 단위, nonce 포함, HMAC-SHA256 hex 소문자)은 저희가
처음 문의드리기 전에 이미 시도했던 조합과 동일합니다 (문의에 "변형 A"로 표기했던 것). 즉
안내해주신 조건을 그대로 만족한 요청도 여전히 `{"code": 10000, "message": "Authentication Failure"}`를
반환합니다.

client_id: [여기에 API_KEY 입력]

로그 조회에 도움이 되도록, 저희가 방금 다시 한 번 client_signature로 인증을 시도했습니다.
- 시도 시각(UTC): [실행 직후 스크립트 출력의 UTC 시각을 여기에 적을 것]
- grant_type: client_signature
- StringToSign 형식: `{client_id}\n{timestamp}\n{nonce}\n`
- signature: HMAC-SHA256(key=client_secret, message=StringToSign)의 hex 소문자 인코딩

위 client_id와 시각으로 서버 로그를 확인하셔서, 정확히 어떤 이유로 거절되는지
(서명값 불일치인지, 이 키에 서명 인증이 별도로 활성화되어 있지 않은지, IP 제한 등
문서에 없는 추가 조건인지) 알려주실 수 있을까요?

추가로 한 가지 더 확인 부탁드립니다. client_credentials 방식으로는 토큰 발급 자체는
성공하는데(scope는 `account:read block_trade:undefined trade:read wallet:read`로
정상 발급됨), 이 토큰으로 아래 두 private 메서드를 호출하면 전부 실패합니다.

- `/private/get_positions`: 파라미터를 비워도, `instrument_name`/`currency`/`kind` 등
  여러 조합으로 시도해도 항상 `{"code": 10000, "message": "Authentication Failure"}`
- `/private/get_assets_info`: 파라미터가 없으면 역시 `Authentication Failure`(10000)인데,
  `asset_type` 파라미터를 추가하면 (값을 PERPETUAL/ALL/FUTURES/SWAP/숫자 0~4 등 여러
  가지로 바꿔봐도 전부) `{"code": 1001, "message": "Bad requested"}`로 바뀝니다.

client_credentials로 발급받은 토큰으로는 지금까지 어떤 `/private/*` 호출도 성공한 적이
없습니다. 혹시 **client_credentials 토큰은 client_signature 토큰과 권한이 다르게
설계되어 있어서 애초에 트레이딩 계열 private 메서드에는 쓸 수 없는 건가요?** 그렇다면
client_signature 인증 실패를 해결하는 게 저희에게는 최우선 과제가 됩니다.

감사합니다.

---

## English version

Hi, thank you for the previous response.

The conditions you confirmed (millisecond timestamp, nonce included, HMAC-SHA256 hex
lowercase) are exactly what we already tested as "Variant A" before our first inquiry.
A request that satisfies all of these conditions still returns
`{"code": 10000, "message": "Authentication Failure"}`.

client_id: a9c24335

To help you locate it in your logs, we just made another client_signature attempt:
- Attempt time (UTC): timestamp≈1785300369363, UTC≈2026-07-29T04:46:09.363000+00:00
- grant_type: client_signature
- StringToSign format: `{client_id}\n{timestamp}\n{nonce}\n`
- signature: hex-lowercase HMAC-SHA256(key=client_secret, message=StringToSign)

Could you check your server logs for this client_id around the time above and let us
know the specific reason for rejection — a signature mismatch, signature auth not being
enabled for this key, an IP restriction, or some other undocumented requirement?

One more thing we'd like to confirm. Using client_credentials, token issuance itself
succeeds (scope comes back as
`account:read block_trade:undefined trade:read wallet:read`), but every private method
we call with that token fails:

- `/private/get_positions`: fails with `{"code": 10000, "message": "Authentication Failure"}`
  regardless of parameters (empty, `instrument_name`, `currency`, `kind` — all tried)
- `/private/get_assets_info`: also `Authentication Failure` (10000) with no params, but
  switches to `{"code": 1001, "message": "Bad requested"}` as soon as an `asset_type`
  parameter is added — regardless of its value (tried PERPETUAL/ALL/FUTURES/SWAP/integers
  0-4, etc.)

We have not been able to get a single `/private/*` call to succeed with a
client_credentials-issued token. **Is it possible that client_credentials tokens are
scoped differently from client_signature tokens and simply cannot be used for trading-
related private methods?** If so, resolving the client_signature authentication failure
becomes our top priority.

Thank you.

---

## 문의 전에 할 일 (선택)

"방금 시도한 시각"을 실제로 채우려면, 문의 보내기 직전에 아래를 실행해서 콘솔에 뜨는
UTC 타임스탬프를 그대로 옮겨 적으면 된다 (읽기전용, 인증 시도만 함):

```
python scripts/orangex_auth_diagnostic.py
```

`client_signature` 시도 블록이 실행되는 실제 시각(밀리초 타임스탬프)을 문의에 반영하면
지원팀이 로그를 더 빨리 찾을 수 있다.
