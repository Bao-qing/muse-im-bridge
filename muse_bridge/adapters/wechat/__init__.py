"""WeChat iLink adapter (HTTP long polling, no third-party gateway)."""

__all__ = ["WeChatAdapter"]


def __getattr__(name):
    if name == "WeChatAdapter":
        from .adapter import WeChatAdapter
        return WeChatAdapter
    raise AttributeError(name)
