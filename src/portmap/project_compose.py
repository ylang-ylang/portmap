from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError


ENDPOINT_CONFIG = ".portmap/endpoints.toml"
COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)


def load_project_config(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read project config {path}: {exc}") from exc


def find_compose_file(
    project_directory: Path,
    compose_file: Path | None = None,
    *,
    config_file: Path | None = None,
) -> Path | None:
    root = project_directory.resolve()
    field = "--compose-file"
    if compose_file is None:
        config = config_file if config_file is not None else root / ENDPOINT_CONFIG
        if not config.is_absolute():
            config = root / config
        if config.exists():
            payload = load_project_config(config)
            if "compose" in payload:
                compose = payload["compose"]
                if not isinstance(compose, dict):
                    raise ConfigError(f"{config}: [compose] must be a table")
                if "file" in compose:
                    value = compose["file"]
                    if not isinstance(value, str) or not value.strip():
                        raise ConfigError(f"{config}: [compose].file must be a non-empty string (project root: {root})")
                    compose_file = Path(value)
                    field = f"{config}: [compose].file"
    if compose_file is not None:
        resolved = (root / compose_file).resolve()
        if not resolved.is_file():
            raise ConfigError(f"{field}: compose file is missing or not a regular file: {resolved}")
        return resolved
    for name in COMPOSE_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def require_compose_file(
    project_directory: Path,
    compose_file: Path | None = None,
    *,
    config_file: Path | None = None,
) -> Path:
    selected = find_compose_file(project_directory, compose_file, config_file=config_file)
    if selected is None:
        raise ConfigError(f"missing compose file in project root: {project_directory.resolve()}")
    return selected


# Overlapping matches also expose values inside inline tables. Comparing each
# parsed candidate accepts only the intended Compose-selection change, without
# rewriting endpoint declarations, unrelated inline fields, or comments.
_TOML_VALUE = re.compile(
    r'(?=("""(?:[^"\\]|\\.|"(?!""))*"""|\'\'\'[\s\S]*?\'\'\'|"(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\''
    r'|\[[^\]]*\]|\{[^}]*\}|[^\s,=\[\]{}#]+))',
    re.DOTALL,
)


def compose_config_text(text: str, value: str) -> str:
    payload = tomllib.loads(text)
    compose = payload.get("compose", {})
    replace_table = not isinstance(compose, dict)
    if replace_table:
        compose = {}
    expected = {**payload, "compose": {**compose, "file": value}}
    encoded = json.dumps(value, ensure_ascii=False)
    if compose.get("file") == value:
        return text

    def matches(candidate: str) -> bool:
        try:
            return tomllib.loads(candidate) == expected
        except tomllib.TOMLDecodeError:
            return False

    if replace_table or "file" in compose:
        replacement = f"{{ file = {encoded} }}" if replace_table else encoded
        for token in _TOML_VALUE.finditer(text):
            candidate = text[:token.start(1)] + replacement + text[token.end(1):]
            if matches(candidate):
                return candidate
    else:
        candidate = text.rstrip() + f"\n\n[compose]\nfile = {encoded}\n"
        if matches(candidate):
            return candidate
        candidate = f"compose.file = {encoded}\n" + text
        if matches(candidate):
            return candidate
        # Existing explicit, quoted, dotted, or inline compose tables keep
        # their original spelling and all unrelated comments and declarations.
        offsets = [0, *(match.end() for match in re.finditer(r"\n", text))]
        for offset in offsets:
            candidate = text[:offset] + f"file = {encoded}\n" + text[offset:]
            if matches(candidate):
                return candidate
        for match in re.finditer(r"\{", text):
            offset = match.end()
            candidate = text[:offset] + f"file = {encoded}, " + text[offset:]
            if matches(candidate):
                return candidate
    raise ConfigError("cannot persist [compose].file without changing other project configuration")


def persist_compose_file(
    project_directory: Path,
    compose_file: Path,
    *,
    config_file: Path,
    allow_empty_endpoints: bool = False,
) -> None:
    """Keep CLI selection in the existing endpoint contract, not a new store."""
    root = project_directory.resolve()
    canonical = root / ENDPOINT_CONFIG
    value = Path(os.path.relpath(compose_file.resolve(), root)).as_posix()
    paths = list(dict.fromkeys((config_file, canonical)))
    updates: list[tuple[Path, str]] = []
    for path in paths:
        source = path if path.exists() else config_file
        payload = load_project_config(source)
        endpoints = payload.get("endpoints")
        if (endpoints is not None and not isinstance(endpoints, dict)) or (
            not allow_empty_endpoints and not endpoints
        ):
            raise ConfigError(f"{source}: config must contain [endpoints.<name>] tables before persisting [compose].file")
        text = source.read_text(encoding="utf-8")
        updates.append((path, compose_config_text(text, value)))
    for path, text in updates:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
