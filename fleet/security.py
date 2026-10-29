"""Samsara HMAC verification over the exact raw body."""
import base64
import hashlib
import hmac
import time


def verify_webhook(body, timestamp, signature, secret, now=None):
    now = time.time() if now is None else now
    try:
        if abs(now - int(timestamp)) > 300:
            return False
        key = base64.b64decode(secret, validate=True)
        if not key:
            return False
        message = b'v1:' + timestamp.encode('ascii') + b':' + body
        expected = 'v1=' + hmac.new(key, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return False
