"""Errors raised by muse_client. Adapters catch these, never raw
transport exceptions, so a platform switch never changes error handling."""


class MuseError(Exception):
    """Base class for all muse_client errors."""


class HandshakeError(MuseError):
    """Noise handshake or WebSocket upgrade failed."""


class RPCError(MuseError):
    """A gateway RPC returned a non-200 status."""

    def __init__(self, method, path, status, body=b""):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        super().__init__(
            f"{method} {path} -> status {status}: {body[:200]!r}")


class ReplyTimeout(MuseError):
    """No renderable assistant message appeared before the deadline."""


class TurnCancelled(MuseError):
    """A turn's wait was cancelled by a newer message in the same chat.
    The message itself was already sent to Muse (send is never
    cancelled); only the wait for its reply, and the QQ reply that
    would have followed it, are dropped, so the newer turn's merged
    reply is the one delivered."""


class SubscribeError(MuseError):
    """Opening a /chat/subscribe stream failed (e.g. session not found)."""


class PushFailed(MuseError):
    """The push route failed; carries sent_mid (if the message was
    already sent) so the caller can fall back to polling without
    re-sending."""

    def __init__(self, message, sent_mid=None):
        self.sent_mid = sent_mid
        super().__init__(message)


class ConnectionLost(MuseError):
    """The gateway connection dropped mid-operation."""
