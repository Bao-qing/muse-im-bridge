"""Offline fixtures for pre-split tests, backed by the real unified gateway.

The facade is test-only: platform parsing/delivery stays on a real adapter and
business operations are delegated to its mounted ChannelContext/pipeline.
"""
from functools import partial
from pathlib import Path
from unittest.mock import Mock

from muse_bridge.adapters.base import BaseAdapter
from muse_bridge.adapters.qq.adapter import QQAdapter as QQDriver
from muse_bridge.adapters.onebot.adapter import OneBotAdapter as OneBotDriver
from muse_bridge.adapters.wechat.adapter import WeChatAdapter as WeChatDriver
from muse_bridge.config.models import MuseConfig
from muse_bridge.core.delivery import DeliveryReceipt, DeliveryResult, DeliveryStatus
from muse_bridge.core.interactions import InteractionManager
from muse_bridge.gateway.delivery import GatewayOutbound
from muse_bridge.gateway.runtime import UnifiedGateway


ONEBOT_SELF = "300000001"


def _isolate_defaults(config):
    """Keep legacy fixtures' default state files beside their explicit temp files."""
    fields = ("routes_file", "session_map_file", "delivered_file", "push_state_file")
    paths = [getattr(config, name, None) for name in fields]
    access = getattr(config, "access_requests", None)
    paths.append(getattr(access, "state_file", None))
    root = next((Path(value).parent for value in paths if value and Path(value).is_absolute()), None)
    if root is None:
        raise AssertionError("Gateway fixtures must provide a temporary state path")
    for name in fields:
        value = getattr(config, name, None)
        if value and not Path(value).is_absolute():
            setattr(config, name, str(root / Path(value).name))
    if access is not None and access.state_file and not Path(access.state_file).is_absolute():
        access.state_file = str(root / Path(access.state_file).name)
    return root


def _service_defaults(service, root):
    # Mock service stubs still need an actual session seed and interaction store.
    if not isinstance(getattr(service.config, "session_id", None), str):
        service.config = MuseConfig(session_id="", cookies_file=str(root / "cookies.json"))
    if not isinstance(service.interactions, InteractionManager):
        service.interactions = InteractionManager()


class MountedAdapter:
    """Old test spelling only; never adds compatibility to production adapters."""
    def __init__(self, adapter, service, *, gateway=None, deduplicator=None, onebot_self_id=ONEBOT_SELF):
        root = _isolate_defaults(adapter.config)
        _service_defaults(service, root)
        object.__setattr__(self, "adapter", adapter)
        object.__setattr__(self, "runtime", gateway or UnifiedGateway(service))
        object.__setattr__(self, "_pipeline_originals", {})
        self.runtime.mount(adapter)
        if isinstance(adapter, OneBotDriver):
            # Trusted offline identity: no get_login_info/prepare/network call.
            adapter.self_id = onebot_self_id
            adapter.host.identity_changed(adapter)
        if deduplicator is not None:
            self.ctx.deduplicator = deduplicator
            self.ctx.outbound_pusher.deduplicator = deduplicator

    @property
    def ctx(self):
        return self.runtime.contexts[self.adapter]

    def __getattr__(self, name):
        if name == "gateway":
            return self.adapter.gateway
        if name == "handle_inbound":
            return partial(self.runtime.pipeline.handle_inbound, self.ctx)
        if name in {"_forward", "_run_command"}:
            return partial(getattr(self.runtime.pipeline, name), self.ctx)
        if hasattr(self.ctx, name):
            return getattr(self.ctx, name)
        return getattr(self.adapter, name)

    def __setattr__(self, name, value):
        if name in {"handle_inbound", "_forward", "_run_command"}:
            # Parser/intake tests patch the pipeline, not a dead adapter method.
            self._pipeline_originals.setdefault(name, getattr(self.runtime.pipeline, name))
            object.__setattr__(self, name, value)
            setattr(self.runtime.pipeline, name, lambda ctx, *a, **kw: value(*a, **kw))
        elif name == "client":
            self.adapter.client = value
            self.adapter.sender.client = value
        elif hasattr(self.ctx, name):
            setattr(self.ctx, name, value)
        else:
            setattr(self.adapter, name, value)

    def __delattr__(self, name):
        if name in self._pipeline_originals:
            setattr(self.runtime.pipeline, name, self._pipeline_originals.pop(name))
            self.__dict__.pop(name, None)
        else:
            object.__delattr__(self, name)

    def is_allowed(self, event_type, event):
        """Legacy policy assertion, evaluated by the mounted access component."""
        author = event.get("author") or {}
        user = author.get("user_openid") or author.get("member_openid") or author.get("id") or ""
        kind = "private" if event_type == "C2C_MESSAGE_CREATE" else "group"
        target = user if kind == "private" else event.get("group_openid", "")
        return self.ctx.access_requests.allows(kind, target, user)


def QQAdapter(config, muse_service, *, client=None, deduplicator=None, gateway=None):
    _isolate_defaults(config)
    return MountedAdapter(QQDriver(config, client=client or Mock()), muse_service,
                          deduplicator=deduplicator, gateway=gateway)


def OneBotAdapter(config, muse_service, *, client=None, gateway=None, self_id=ONEBOT_SELF):
    _isolate_defaults(config)
    return MountedAdapter(OneBotDriver(config, client=client or Mock()), muse_service,
                          gateway=gateway, onebot_self_id=self_id)


def WeChatAdapter(config, muse_service, *, state=None, client=None, gateway=None):
    return MountedAdapter(WeChatDriver(config, state=state, client=client), muse_service, gateway=gateway)


def make_qq_outbound(config, client, muse_service, deduplicator, route_manager,
                     session_mapper=None, on_login_required=None):
    mounted = QQAdapter(config, muse_service, client=client, deduplicator=deduplicator)
    ctx = mounted.ctx
    ctx.route_manager = route_manager
    if session_mapper is not None:
        ctx.session_mapper = session_mapper
        muse_service.interactions.managed_sessions.update(session_mapper.session_ids())
    # Rebuild after supplying the repositories so subscriptions and delivery both
    # consult exactly the state the test drives. The platform sender is unchanged.
    ctx.outbound_pusher = GatewayOutbound(ctx)
    ctx.outbound_pusher.client = client  # test handle; actual delivery uses adapter.sender
    if on_login_required is not None:
        ctx.outbound_pusher._on_login_required = on_login_required
    return ctx.outbound_pusher


class FakeAdapter(BaseAdapter):
    """Explicit in-memory platform port for platform-independent context tests."""
    name = "qq"
    is_enabled = True

    def __init__(self, config):
        self.config = config
        self.sent = []
        self.replies = []

    def account_identity(self):
        return self.config.app_id

    async def start(self):
        pass

    async def stop(self):
        pass

    async def download_attachment(self, attachment, max_bytes):
        raise AssertionError("No attachment fixture was provided")

    async def deliver(self, envelope):
        self.sent.append(envelope)
        self.replies.append(envelope.text)
        return DeliveryResult(DeliveryStatus.CONFIRMED, [
            DeliveryReceipt(envelope.target_type, envelope.target_id, (("id", "local-reply"),))])
