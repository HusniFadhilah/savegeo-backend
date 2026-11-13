"""RFC 6238 TOTP primitive; enrollment/service layers enforce replay and limits."""
import base64
import hmac
import struct


def code(secret: str, timestamp: int, digits: int = 6) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    mac = hmac.digest(key, struct.pack(">Q", timestamp // 30), "sha1")
    offset = mac[-1] & 15
    number = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % (10 ** digits)).zfill(digits)


def matching_counter(secret: str, value: str, timestamp: int, previous: int = -1) -> int | None:
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        return None
    for counter in (timestamp // 30 - 1, timestamp // 30, timestamp // 30 + 1):
        if counter > previous and counter >= 0 and hmac.compare_digest(code(secret, counter * 30), value):
            return counter
    return None
