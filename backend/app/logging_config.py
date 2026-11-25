"""Structured logging with credential redaction."""

from __future__ import annotations

import logging
from collections.abc import Iterable

REDACTED = "***"


class SecretRedactionFilter(logging.Filter):
    """Replaces known secret values in any log record with ``***``."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, REDACTED)
        record.msg = message
        record.args = ()
        return True


def configure_logging(level: str, secrets: Iterable[str]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SecretRedactionFilter(secrets))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
