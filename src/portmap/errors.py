from __future__ import annotations


class PortmapError(RuntimeError):
    """Base exception for user-facing failures."""


class ConfigError(PortmapError):
    """Invalid portmap configuration."""


class ComposeError(PortmapError):
    """Invalid or unsupported Docker Compose configuration."""


class PortAllocationError(PortmapError):
    """Unable to allocate requested host ports."""
