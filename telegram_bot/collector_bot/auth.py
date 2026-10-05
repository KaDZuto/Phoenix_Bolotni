"""HMAC token signing and validation for secure audio streaming proxy.

Prevents exposing Telegram Bot API token to web clients while allowing
one-time, time-limited access to stream audio in the Mini App web player.
"""
import hashlib
import hmac
import time
from typing import Tuple


def generate_audio_token(submission_id: int, secret: str, ttl_seconds: int = 900) -> str:
    """Generate time-limited signature: <expiry_ts>:<hmac>"""
    expiry_ts = int(time.time()) + ttl_seconds
    message = f"{submission_id}:{expiry_ts}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{expiry_ts}:{sig}"


def verify_audio_token(submission_id: int, token: str, secret: str) -> bool:
    """Verify that signature is authentic and has not expired."""
    try:
        parts = token.split(":")
        if len(parts) != 2:
            return False
        expiry_ts = int(parts[0])
        given_sig = parts[1]

        # Check expiration
        if time.time() > expiry_ts:
            return False

        message = f"{submission_id}:{expiry_ts}".encode("utf-8")
        expected_sig = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given_sig, expected_sig)
    except Exception:
        return False
