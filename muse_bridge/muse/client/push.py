"""Push-route helpers: pure logic over /chat/subscribe events.

No network here, no client state — just event classification, so it is
trivially unit-testable offline. The network half (opening the
subscribe stream, reading its frames) lives in client.py.

Turn completion (from the live probe 2026-10-24):
- an assistant message finishes: event "delta.message_done";
- the whole turn finishes: event "task.status" with status "completed"
  for the ROOT task (task_id == root_agent_id, falling back to
  task_id == agent_id when no root id is present).
A turn is done only when BOTH have been seen for this session. Events
for other sessions (the subscribe stream also replays agent-level
snapshots) are ignored.

Presentation exception (official web client, read 2026-10-26):
the web client's delta.presentation handler inserts the
presentation immediately with isTextStreaming false — it never
waits for message_done or task completion. Presentation turns
therefore never send the signals above, and a tracker waiting
for them times out (the relay's starry turn took 133s, via the
polling fallback). So a delta.presentation of kind image or
file also completes the turn, UNLESS text is currently
streaming (a message_start/text_append seen without a
message_done yet): media arriving mid-text-stream must not
cut the text off. A turn whose text arrives only after the
media is still safe: the relay records just the media id,
and the later text message is unrecorded, so the subscriber
pushes it as fallback.

The subscribe ACK itself is not an "event": it is a body_chunk JSON of
the form {"id": "http-subscribe-...", "payload": {"subscribed": true}}.
A missing session answers {"ok": false, "error": {"code": "not_found"}}.
parse_subscribe_chunk() classifies one such chunk: "subscribed",
"not_found", or None (anything else, including pushed events).
"""
import json


def parse_subscribe_chunk(data: bytes):
    """Classify one /chat/subscribe body_chunk's data."""
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    payload = obj.get("payload") or {}
    if payload.get("subscribed") is True:
        return "subscribed"
    if obj.get("ok") is False:
        return "not_found"
    return None


class PushTurnTracker:
    """Detect turn completion from subscribe events for one session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.seen_message_done = False
        self.seen_root_task_completed = False
        self.text_streaming = False
        self.seen_presentation_media = False

    @staticmethod
    def _event_session(event: dict):
        if not isinstance(event, dict):
            return None
        payload = event.get("payload") or {}
        if isinstance(payload, dict):
            sid = payload.get("session_id") or payload.get("thread_id")
            if sid:
                return sid
            ctx = payload.get("chat_context") or {}
            if isinstance(ctx, dict) and ctx.get("chat_id"):
                return ctx.get("chat_id")
        return event.get("session_id") or event.get("thread_id")

    def feed(self, event: dict) -> bool:
        """Feed one pushed event dict. Returns True once the turn is done."""
        if not isinstance(event, dict):
            return False
        if self._event_session(event) != self.session_id:
            return False
        name = event.get("event")
        payload = event.get("payload") or {}
        if name in ("delta.message_start", "delta.text_append"):
            self.text_streaming = True
        elif name == "delta.message_done":
            self.seen_message_done = True
            self.text_streaming = False
        elif name == "delta.presentation":
            # Web handler: data.type ?? kind; voice-call and
            # browser-task presentations are not media turns.
            data = payload.get("data") or {}
            kind = data.get("type") or payload.get("kind")
            if kind in ("image", "file", "video") \
                    and not self.text_streaming:
                self.seen_presentation_media = True
        elif name == "task.status" and payload.get("status") == "completed":
            tid = payload.get("task_id")
            root = payload.get("root_agent_id")
            agent = payload.get("agent_id")
            if root:
                is_root = (tid == root)
            elif agent:
                is_root = (tid == agent)
            else:
                is_root = True
            if is_root:
                self.seen_root_task_completed = True
        return self.done

    @property
    def done(self) -> bool:
        return ((self.seen_message_done and self.seen_root_task_completed)
                or self.seen_presentation_media)
