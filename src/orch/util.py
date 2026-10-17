"""Small shared helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any


def now() -> str:
    """Current UTC time as an ISO-8601 string with a Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def slugify(text: str, max_len: int = 63) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "project"
