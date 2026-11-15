from .base import BaseAdapter

__all__ = ["BaseAdapter", "QQAdapter", "OneBotAdapter", "WeChatAdapter"]


def __getattr__(name):
    # Lazy: adapters build on muse_bridge.gateway, which imports .base from here.
    if name == "QQAdapter":
        from .qq import QQAdapter
        return QQAdapter
    if name == "OneBotAdapter":
        from .onebot import OneBotAdapter
        return OneBotAdapter
    if name == "WeChatAdapter":
        from .wechat.adapter import WeChatAdapter
        return WeChatAdapter
    raise AttributeError(name)
