"""Remove secrets from text before it is stored or shown (command output, diffs, executor replies)."""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), MASK),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), MASK),
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)[^\s'\"]+"), r"\1" + MASK),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1" + MASK + "@"),
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|token)[A-Z0-9_]*)"
            r"(\s*[=:]\s*)(['\"]?)([^\s'\"]{4,})\3"
        ),
        r"\1\2\3" + MASK + r"\3",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate_middle(text: str, limit: int) -> tuple[str, bool]:
    """Keep the start and (mostly) the end, where test failures usually are."""
    if len(text) <= limit:
        return text, False
    head = limit // 4
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n\n[... {cut} characters omitted ...]\n\n{text[-tail:]}", True
