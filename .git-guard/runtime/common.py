from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any

ZERO = "0" * 40

AGENT_REJECT_HINT = (
    "if you are an agent, read the contribution document and use the configured workflow; "
    "do not try to bypass this hook."
)

@dataclass(frozen=True)
class RefUpdate:
    old: str
    new: str
    ref: str

@dataclass(frozen=True)
class SourceCandidate:
    ref: str
    sha: str
    rule: dict[str, Any]

@dataclass(frozen=True)
class PushUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

@dataclass(frozen=True)
class SubmoduleGitlink:
    path: str
    sha: str

@dataclass(frozen=True)
class SubmoduleBranchTip:
    pattern: str
    branch: str
    ref: str
    sha: str

@dataclass(frozen=True)
class LocalPolicyTag:
    ref: str
    object_sha: str
    target_sha: str

@dataclass(frozen=True)
class BranchLogSettings:
    path: str
    force_diff_required: bool

class HookReject(RuntimeError):
    def __init__(self, code: str, **context: Any) -> None:
        self.code = code
        self.context = context
        super().__init__(format_reject(code, context))

def format_reject(code: str, context: dict[str, Any]) -> str:
    if not context:
        return code
    fields = [f"{key}={format_context_value(value)}" for key, value in context.items()]
    return " ".join([code, *fields])

def format_context_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return ",".join(format_context_value(item) for item in value)
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,-]+", text):
        return text
    return json.dumps(text, ensure_ascii=True)

def warn(code: str, **context: Any) -> None:
    print(f"git-guard: {format_reject(code, context)}", file=sys.stderr)

def append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)
