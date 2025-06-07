import json
import hmac
import hashlib
from typing import Any
from AG1_AetherBus.envelope import Envelope


def _canonical_bytes(env: Envelope) -> bytes:
    data = env.to_dict().copy()
    data.pop("auth_signature", None)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def sign_envelope(env: Envelope, secret: str) -> str:
    """Return HMAC-SHA256 hex digest for the envelope."""
    payload = _canonical_bytes(env)
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def apply_signature(env: Envelope, secret: str) -> None:
    env.auth_signature = sign_envelope(env, secret)


def verify_envelope(env: Envelope, secret: str) -> bool:
    if not env.auth_signature:
        return False
    expected = sign_envelope(env, secret)
    return hmac.compare_digest(expected, env.auth_signature)
