"""OrangeX 서명 로직 유닛 테스트.

docs/api-notes.md의 client_signature 예시에는 실제 서버가 승인하는 서명값 예시가 없다
(client_credentials 예시만 있음). 따라서 이 테스트는 "문서에 적힌 공식을 정확히
구현했는가"를 hmac/hashlib로 독립적으로 재계산한 값과 비교해 검증한다.
실서버가 이 서명을 실제로 승인하는지는 별개 문제이며, Phase 2 §7 라이브 확인
단계에서 확인한다.
"""
from __future__ import annotations

import hashlib
import hmac

from exchange.orangex.auth import build_string_to_sign, sign


def test_build_string_to_sign_format():
    result = build_string_to_sign("my-client-id", "1700000000000", "abc123")
    assert result == "my-client-id\n1700000000000\nabc123\n"


def test_sign_matches_independent_hmac_sha256_hex():
    client_id = "my-client-id"
    client_secret = "my-client-secret"
    timestamp = "1700000000000"
    nonce = "abc123"

    expected = hmac.new(
        key=client_secret.encode("utf-8"),
        msg=f"{client_id}\n{timestamp}\n{nonce}\n".encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    assert sign(client_secret, client_id, timestamp, nonce) == expected


def test_sign_without_nonce_still_has_trailing_newline():
    client_id = "cid"
    client_secret = "secret"
    timestamp = "123"

    expected = hmac.new(
        key=client_secret.encode("utf-8"),
        msg=f"{client_id}\n{timestamp}\n\n".encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    assert sign(client_secret, client_id, timestamp) == expected


def test_sign_is_deterministic():
    args = ("secret", "cid", "123", "nonce")
    assert sign(*args) == sign(*args)


def test_different_secret_changes_signature():
    assert sign("secret-a", "cid", "123", "nonce") != sign("secret-b", "cid", "123", "nonce")
