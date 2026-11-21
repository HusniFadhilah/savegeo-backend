import io
import logging

from app.core.logging_security import SecretRedactingFilter


def test_log_messages_redact_auth_headers_tokens_and_signed_urls():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(SecretRedactingFilter())
    logger = logging.getLogger("test.security.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.error("provider failed Authorization: Bearer abc.def.ghi api_key=%s", "secret-value")
    logger.error("upstream %s", "https://example.test/data?token=private&ok=1")

    rendered = output.getvalue()
    assert "abc.def.ghi" not in rendered
    assert "secret-value" not in rendered
    assert "token=private" not in rendered
    assert "[REDACTED]" in rendered


def test_exception_traceback_is_redacted_before_emission():
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(SecretRedactingFilter())
    logger = logging.getLogger("test.security.traceback")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)

    try:
        raise RuntimeError("database password=very-secret-value")
    except RuntimeError:
        logger.exception("request failed request_id=%s", "req-123")

    rendered = output.getvalue()
    assert "req-123" in rendered
    assert "very-secret-value" not in rendered
    assert "password=[REDACTED]" in rendered

