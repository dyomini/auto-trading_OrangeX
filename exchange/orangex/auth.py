"""OrangeX `client_signature` 서명 (docs/api-notes.md §2).

StringToSign = clientId + "\\n" + Timestamp + "\\n" + Nonce + "\\n"
Signature    = HEX_STRING( HMAC_SHA256( key=ClientSecret, data=StringToSign ) )
"""
from __future__ import annotations

import hashlib
import hmac


def build_string_to_sign(client_id: str, timestamp: str, nonce: str) -> str:
    return f"{client_id}\n{timestamp}\n{nonce}\n"


def sign(client_secret: str, client_id: str, timestamp: str, nonce: str = "") -> str:
    string_to_sign = build_string_to_sign(client_id, timestamp, nonce)
    digest = hmac.new(
        key=client_secret.encode("utf-8"),
        msg=string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return digest.hex()
