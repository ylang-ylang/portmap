from __future__ import annotations

import re


def slugify(value: str, *, fallback: str = "item") -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def route_id(*parts: str) -> str:
    return "-".join(slugify(part) for part in parts if part)

