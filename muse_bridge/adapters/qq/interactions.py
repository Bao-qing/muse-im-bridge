"""Compatibility alias: interaction routing moved to ``muse_bridge.gateway.interactions``."""

from ...gateway.interactions import APPROVAL_REPLY, LATE_INPUT_GRACE_S, ChatInteractions

QQInteractions = ChatInteractions

__all__ = ["APPROVAL_REPLY", "LATE_INPUT_GRACE_S", "QQInteractions"]
