"""One account-wide approval subscription, independent of adapter sessions."""
from __future__ import annotations

import contextlib
import threading
import time

from .client.errors import LoginRequired
from ..observability import get_logger, safe_error

logger = get_logger(__name__)


class ApprovalEventStream:
    """Blocking worker: one owner thread for the non-thread-safe MuseClient."""

    def __init__(self, protocol):
        self.protocol = protocol
        self.stop = threading.Event()

    def run(self):
        p = self.protocol
        while not self.stop.is_set():
            client = None
            generation = None
            try:
                if not p.service.token_manager.has_credentials():
                    p.invalidate_approval_state()
                    self.stop.wait(3)
                    continue
                generation = p.begin_approval_stream()
                client = p.service.create_client()
                client.connect()
                # The web client receives foreground and scheduler snapshots
                # through the account-wide daemon chat.subscribe stream.
                subscription = client.subscribe()
                last_activity = time.monotonic()
                while not self.stop.is_set():
                    if client.credentials_current() is False:
                        break
                    events = subscription.read_events(timeout=1.0)
                    # A credential rotation during a blocking read invalidates
                    # these events before they can re-open any local gate.
                    if client.credentials_current() is False:
                        break
                    for event in events:
                        p.accept_approval_event(event, generation)
                    now = time.monotonic()
                    if events:
                        last_activity = now
                    elif now - last_activity >= 30:
                        client.ping(presence="active", timeout=10)
                        last_activity = time.monotonic()
            except LoginRequired:
                logger.debug("Approval event stream paused: Muse login required.")
            except Exception as exc:
                if not self.stop.is_set():
                    logger.warning("Approval event stream disconnected; resynchronizing. error=%s", safe_error(exc))
            finally:
                if generation is not None:
                    p.end_approval_stream(generation)
                if client is not None:
                    with contextlib.suppress(Exception):
                        client.close()
            self.stop.wait(3)
