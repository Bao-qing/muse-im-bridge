"""Platform port. Adapters translate platform events; the host owns business state."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from ..core.delivery import DeliveryEnvelope, DeliveryResult, DeliveryStatus, FeedEnvelope, ReactionEnvelope
from ..core.message import Attachment, InboundMessage, InboundReaction


class AdapterHost(Protocol):
    async def accept(self, adapter: BaseAdapter, message: InboundMessage) -> str: ...
    def identity_changed(self, adapter: BaseAdapter) -> None: ...


class ReactionHost(Protocol):
    async def accept_reaction(self, adapter: BaseAdapter, event: InboundReaction) -> str: ...


class BaseAdapter(ABC):
    host: AdapterHost | None = None

    def mount(self, host: AdapterHost) -> None:
        if self.host is not None and self.host is not host:
            raise ValueError("Adapter already belongs to another gateway")
        self.host = host

    async def emit(self, message: InboundMessage) -> str:
        if self.host is None:
            raise RuntimeError("Adapter must be mounted before receiving messages")
        return await self.host.accept(self, message)

    async def emit_reaction(self, event: InboundReaction) -> str:
        """Hand off a reaction independently of the ordinary message pipeline."""
        if self.host is None:
            raise RuntimeError("Adapter must be mounted before receiving reactions")
        accept = getattr(self.host, "accept_reaction", None)
        return await accept(self, event) if callable(accept) else "ignored"

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def is_enabled(self) -> bool: ...

    @abstractmethod
    def account_identity(self) -> str: ...

    def notification_target(self) -> str | None:
        return None

    async def prepare(self) -> None:
        """Resolve a trusted account identity before starting the event source."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def download_attachment(self, attachment: Attachment, max_bytes: int) -> bytes: ...

    @abstractmethod
    async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryResult: ...

    async def deliver_reaction(self, envelope: ReactionEnvelope) -> DeliveryResult:
        return DeliveryResult(DeliveryStatus.REJECTED, reason="Native reactions not supported")

    def supports_native_feed(self) -> bool:
        """Whether this platform provides a dedicated feed/timeline surface."""
        return False

    async def publish_feed(self, feed: FeedEnvelope) -> DeliveryResult:
        """Publish a resolved feed post to the platform's native feed surface."""
        return DeliveryResult(DeliveryStatus.REJECTED, reason="Native feed surface not supported")

    def activity_started(self, target: str, token: str, *, kind: str = "private") -> None:
        """Record activity synchronously before the intake awaitable returns."""

    async def activity(self, target: str, token: str, state: str, *, kind: str = "private") -> None:
        """Optional platform typing signal; token is opaque to the adapter."""
