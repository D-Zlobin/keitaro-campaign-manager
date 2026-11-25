import io
import logging

from app.logging_config import SecretRedactionFilter


def test_secret_values_are_redacted_from_log_records() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter(["super-secret-key"]))
    logger = logging.getLogger("test.redaction")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    logger.info("calling keitaro with key=%s done", "super-secret-key")

    output = stream.getvalue()
    assert "super-secret-key" not in output
    assert "***" in output
