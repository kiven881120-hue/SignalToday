import logging
import os
import re


SENSITIVE_NAMES = (
    "AI_API_KEY",
    "DATABASE_URL",
    "DJANGO_SECRET_KEY",
    "EMAIL_API_KEY",
    "LLM_API_KEY",
    "POSTGRES_PASSWORD",
)
SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)((?:cookie|session|api[_-]?key|token|password)\s*[:=]\s*)([^\s,;]+)"),
)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        for name in SENSITIVE_NAMES:
            value = os.getenv(name)
            if value:
                message = message.replace(value, "[REDACTED]")
        for pattern in SENSITIVE_PATTERNS:
            message = pattern.sub(r"\1[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True
