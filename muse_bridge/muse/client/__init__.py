"""muse_client: a platform-agnostic Muse gateway client library.

Adapters (QQ today, WeChat later) use MuseClient to talk to Muse and
render.py to turn Muse turns into plain text. The library never imports
adapter code, never reads adapter state, and never touches a platform
API: tokens and session ids come in as arguments, TurnResult objects
come out.

Quick start (one connection per turn):

    from muse_client import MuseClient
    with MuseClient.from_mint_file("path/to/.mint.json") as client:
        turn = client.send_and_wait(session_id, "hello")
        print(turn.text)
"""
from .client import MuseClient, PushSubscription, TurnMessage, TurnResult
from .errors import (
    ConnectionLost,
    HandshakeError,
    MuseError,
    PushFailed,
    ReplyTimeout,
    RPCError,
    SubscribeError,
)
from .push import PushTurnTracker, parse_subscribe_chunk
from .render import collect_turn_events, join_turn, render_assistant_event

__version__ = "0.2.0"
__all__ = [
    "MuseClient",
    "PushSubscription",
    "TurnMessage",
    "TurnResult",
    "MuseError",
    "HandshakeError",
    "RPCError",
    "ReplyTimeout",
    "ConnectionLost",
    "SubscribeError",
    "PushFailed",
    "PushTurnTracker",
    "parse_subscribe_chunk",
    "render_assistant_event",
    "collect_turn_events",
    "join_turn",
]
