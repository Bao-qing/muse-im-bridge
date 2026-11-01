"""Compatibility alias: whitelist applications moved to ``muse_bridge.gateway.access``."""

from ...gateway.access import AccessRequestStore, ChatAccessRequests, valid_id

QQAccessRequests = ChatAccessRequests

__all__ = ["AccessRequestStore", "QQAccessRequests", "valid_id"]
