"""JSON-lines logging to stdout for the API and worker.

Callers pass ids and codes through ``extra``; never secrets, cookies, tokens,
request bodies, names or private reasons.
"""
from datetime import datetime, timezone
import json
import logging
import os
import re
import sys

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
_CALLBACK_QUERY = re.compile(r'(/(?:auth|api/email/microsoft)/callback)\?[^\s"\']*')


def _redact_callback(value):
    return _CALLBACK_QUERY.sub(r'\1?[redacted]', value) if isinstance(value, str) else value


class CallbackSecretsFilter(logging.Filter):
    def filter(self, record):
        # Keep uvicorn's five access-log arguments intact for its own formatter.
        record.msg = _redact_callback(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_callback(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_callback(value) for key, value in record.args.items()}
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                 "level": record.levelname, "logger": record.name, "message": _redact_callback(record.getMessage())}
        entry.update({k: v for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_")})
        if record.exc_info:
            entry["stack"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging():
    """Idempotent: one stdout JSON handler on the root logger, level from ROTA_LOG_LEVEL (default INFO)."""
    root = logging.getLogger()
    root.setLevel(os.getenv("ROTA_LOG_LEVEL", "INFO").upper())
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    # httpx logs full URLs at INFO (OIDC calls); keep only its warnings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in ("uvicorn.access", "httpx"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, CallbackSecretsFilter) for item in logger.filters):
            logger.addFilter(CallbackSecretsFilter())
