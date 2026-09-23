"""Shared static/JSON HTTP support; no server runtime or container dependencies."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path, PurePosixPath
from typing import Any

STATIC_ROOT = Path(__file__).with_name("catalog_static")
STATIC_CONTENT_TYPES_BY_SUFFIX = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}


def safe_static_path(asset_path: str) -> Path | None:
    normalized = PurePosixPath(asset_path)
    if normalized.is_absolute() or ".." in normalized.parts or not normalized.parts:
        return None
    return STATIC_ROOT.joinpath(*normalized.parts)


def static_content_type(asset_path: str) -> str | None:
    return STATIC_CONTENT_TYPES_BY_SUFFIX.get(PurePosixPath(asset_path).suffix)


def read_static_asset(asset_path: str) -> tuple[bytes, str] | None:
    path = safe_static_path(asset_path)
    content_type = static_content_type(asset_path)
    if path is None or content_type is None:
        return None
    try:
        return path.read_bytes(), content_type
    except FileNotFoundError:
        return None


def vite_public_root_asset(request_path: str) -> str | None:
    if not request_path.startswith("/"):
        return None
    normalized = PurePosixPath(request_path.removeprefix("/"))
    if len(normalized.parts) != 1:
        return None
    asset_path = normalized.as_posix()
    if asset_path in {"", ".", "index.html"} or static_content_type(asset_path) is None:
        return None
    return asset_path


class StaticHandler(BaseHTTPRequestHandler):
    """Subclasses supply handle_request and their own action routes."""

    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def write_json(self, payload: dict[str, Any], *, send_body: bool, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def write_static(self, filename: str, *, send_body: bool) -> bool:
        asset = read_static_asset(filename)
        if asset is None:
            return False
        body, content_type = asset
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)
        return True

    def write_not_found(self, *, send_body: bool) -> None:
        self.send_response(404)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        if send_body:
            self.wfile.write(b"not found\n")

    def write_text(self, body: str, *, content_type: str, send_body: bool) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", f"{content_type}; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if send_body:
            self.wfile.write(payload)
