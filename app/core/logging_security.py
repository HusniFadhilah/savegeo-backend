"""Redact credentials from application log messages and exception traces."""
from __future__ import annotations

import logging
import re
import traceback


_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s:]+(?::[^/@\s]*)?@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|signature|client[_-]?secret|x-amz-signature)=)[^&#\s]*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|token|signature)\b(\s*[:=]\s*)(['\"]?)[^\s,'\";&]+"), r"\1\2[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_ACCESS_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
)


def redact_log_text(value: str) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


class SecretRedactingFilter(logging.Filter):
    """Sanitize formatted message arguments and traceback text before handlers emit them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_log_text(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 - a logging safeguard must never disrupt request handling
            record.msg = "Log message omitted because it could not be sanitized"
            record.args = ()
        if record.exc_info:
            try:
                record.exc_text = redact_log_text("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
            except Exception:  # noqa: BLE001
                record.exc_text = "Exception details omitted because they could not be sanitized"
                record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_log_text(record.stack_info)
        return True


def install_secret_redaction() -> None:
    """Attach the filter to configured handlers, including Uvicorn's handlers."""
    filter_instance = SecretRedactingFilter()
    root = logging.getLogger()
    loggers = [root]
    loggers.extend(
        value for value in logging.Logger.manager.loggerDict.values()
        if isinstance(value, logging.Logger)
    )
    seen: set[int] = set()
    for logger in loggers:
        for handler in logger.handlers:
            if id(handler) not in seen:
                handler.addFilter(filter_instance)
                seen.add(id(handler))

