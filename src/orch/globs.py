"""Path globs for task scopes and policy rules.

Semantics (gitignore-like, always on POSIX-style relative paths):
- ``*`` matches within one path segment, ``?`` one character in a segment.
- ``**`` matches across segments; ``**/`` matches zero or more directories.
- A pattern without ``/`` matches the file or directory name at any depth
  (``.env*`` matches ``config/.env.local``).
- A pattern containing ``/`` is anchored at the repository root.
- A trailing ``/`` means "everything under this directory".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

UNBOUNDED = frozenset({"*", "**", "**/*", "./**", "./*"})


def normalize_path(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def validate_pattern(pattern: str) -> str | None:
    """Return an error message, or None if the pattern is acceptable as a scope glob."""
    if not pattern or not pattern.strip():
        return "empty pattern"
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:", pattern):
        return f"absolute path not allowed: {pattern!r}"
    if ".." in pattern.replace("\\", "/").split("/"):
        return f"'..' not allowed: {pattern!r}"
    return None


def is_unbounded(pattern: str) -> bool:
    return pattern.strip() in UNBOUNDED


def _translate(pattern: str) -> str:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                j = i + 2
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                else:
                    out.append(".*")
                    i = j
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@lru_cache(maxsize=2048)
def compile_glob(pattern: str) -> re.Pattern[str]:
    p = normalize_path(pattern.strip())
    anchored = "/" in p.rstrip("/")
    if p.endswith("/"):
        p += "**"
    regex = _translate(p)
    if not anchored:
        regex = "(?:.*/)?" + regex
    return re.compile("^" + regex + "$")


def match(path: str, pattern: str) -> bool:
    return compile_glob(pattern).match(normalize_path(path)) is not None


def match_any(path: str, patterns: Iterable[str]) -> bool:
    return any(match(path, p) for p in patterns)


def matching(paths: Iterable[str], patterns: Iterable[str]) -> list[str]:
    pats = list(patterns)
    return [p for p in paths if match_any(p, pats)]
