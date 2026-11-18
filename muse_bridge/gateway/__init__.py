"""Unified business gateway. Platform adapters implement ports, not gateway subclasses."""

__all__ = ["UnifiedGateway"]


def __getattr__(name):
    if name == "UnifiedGateway":
        from .runtime import UnifiedGateway
        return UnifiedGateway
    raise AttributeError(name)
