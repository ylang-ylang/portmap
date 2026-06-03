from __future__ import annotations

from typing import Any


def dump_yaml(value: Any) -> str:
    lines = list(render(value, 0))
    return "\n".join(lines) + "\n"


def render(value: Any, indent: int) -> list[str]:
    if isinstance(value, dict):
        return render_dict(value, indent)
    if isinstance(value, list):
        return render_list(value, indent)
    return [" " * indent + scalar(value)]


def render_dict(value: dict, indent: int) -> list[str]:
    lines: list[str] = []
    for key, item in value.items():
        prefix = " " * indent + f"{key}:"
        if isinstance(item, dict):
            lines.append(prefix)
            lines.extend(render_dict(item, indent + 2))
        elif isinstance(item, list):
            if item:
                lines.append(prefix)
                lines.extend(render_list(item, indent + 2))
            else:
                lines.append(prefix + " []")
        elif item is None:
            lines.append(prefix + " null")
        else:
            lines.append(prefix + " " + scalar(item))
    return lines


def render_list(value: list, indent: int) -> list[str]:
    lines: list[str] = []
    for item in value:
        prefix = " " * indent + "-"
        if isinstance(item, dict):
            lines.append(prefix)
            lines.extend(render_dict(item, indent + 2))
        elif isinstance(item, list):
            lines.append(prefix)
            lines.extend(render_list(item, indent + 2))
        else:
            lines.append(prefix + " " + scalar(item))
    return lines


def scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "null"
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

