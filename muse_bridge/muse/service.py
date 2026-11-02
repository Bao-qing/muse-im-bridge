"""Decoupled Muse Service layer.

Provides a unified facade for chat turns, attachment handling,
workspace file reads, and event stream subscriptions.
Can be shared across multiple adapters (QQ, OneBot, WeChat).
"""

from __future__ import annotations

import base64
import time
from typing import Any, Callable, Dict, List, Optional

from .client import MuseClient, TurnResult
from .client.errors import HandshakeError, TurnCancelled, LoginRequired, is_runtime_auth_error

from ..config.models import MuseConfig
from ..core.interactions import InteractionManager
from ..core.message import Attachment
from ..observability import get_logger, trace_event
from .token_manager import MuseTokenManager
from .interactions import ApprovalBlocked, MuseInteractions


logger = get_logger(__name__)


class MuseService:
    def __init__(self, config: MuseConfig, token_manager: MuseTokenManager):
        self.config = config
        self.token_manager = token_manager
        self.interactions = InteractionManager()
        self.interaction_protocol = MuseInteractions(self)
        if config.session_id:
            self.interactions.managed_sessions.add(config.session_id)

    def format_approval(self, item):
        return self.interaction_protocol.format_approval(item)

    async def submit_approval(self, item, choice, **kwargs):
        return await self.interaction_protocol.submit_approval(item, choice, **kwargs)

    def capture_credential(self, item, secret):
        return self.interaction_protocol.capture_credential(item, secret)

    def check_approval_gate(self, session_id):
        self.interactions.managed_sessions.add(session_id)
        try:
            self.interaction_protocol.fetch_approvals()
        except Exception:
            raise ApprovalBlocked("审批状态暂不可确认，此消息未转发、未缓存；请稍后重新发送。") from None
        pending = self.interaction_protocol.approval_queue(session_id)
        if pending:
            raise ApprovalBlocked(self.format_approval(pending[0]))

    def create_client(self) -> MuseClient:
        """Create a new MuseClient instance with auto-refreshing credentials."""
        token, notary_token, vm_id = self.token_manager.get_credentials()

        client = MuseClient(
            token=token,
            notary_token=notary_token,
            vm_id=vm_id,
            host=self.config.host,
            client_ip=self.config.client_ip,
            timezone=self.config.timezone,
            proxy_url=self.config.proxy_url,
            direct=self.config.direct,
            reply_mode=self.config.reply_mode,
        )

        orig_connect = client.connect

        def connect_with_retry():
            try:
                orig_connect()
            except HandshakeError as exc:
                client.close()
                # Network/Noise failure is retryable, but is not evidence of rejected mint.
                if exc.status not in (401, 403):
                    raise
                self.token_manager.invalidate_mint(client.token, client.notary_token)
                new_t, new_n, new_vm = self.token_manager.refresh_from_cookies(False, rejected_token=client.token, rejected_notary=client.notary_token)
                client.token, client.notary_token, client.vm_id = new_t, new_n, new_vm
                try:
                    orig_connect()
                except Exception:
                    client.close()
                    raise

        orig_request = client.request

        def request_without_replay(*args, **kwargs):
            try:
                return orig_request(*args, **kwargs)
            except Exception as exc:
                if is_runtime_auth_error(exc):
                    self.token_manager.invalidate_mint(client.token, client.notary_token)
                raise

        client.request = request_without_replay
        client.connect = connect_with_retry
        client.credentials_current = lambda: self.token_manager.credentials_current(client.token, client.notary_token, client.vm_id)
        return client

    def verify_connection(self) -> bool:
        """Verify Muse credentials by attempting a connection.
        Returns False only for missing/rejected cookies; temporary errors propagate.
        """
        if not self.token_manager.has_credentials():
            return False
        try:
            with self.create_client() as client:
                client.connect()
                return True
        except LoginRequired:
            return False
        # A temporary failure is not a negative authentication verdict.
        # Let startup distinguish it from a missing/rejected cookie session.

    def process_attachments(
        self,
        client: MuseClient,
        attachments: List[Attachment],
        turn_text: str,
        threshold_bytes: int = 2 * 1024 * 1024,
    ) -> tuple[List[dict], List[dict], List[dict], str]:
        """Classify attachments into inline images, inline files, and file_refs."""
        inline_images: List[dict] = []
        inline_files: List[dict] = []
        file_refs: List[dict] = []
        appended_text = turn_text

        for att in attachments:
            data = att.data
            if not data:
                continue

            # 1. Large files & large images -> upload first and send as file_ref
            if len(data) > threshold_bytes:
                filename = att.filename or ("image.jpg" if att.type == "image" else "file.dat")
                try:
                    uploaded_path = client.upload_file(filename, data)
                    file_refs.append({
                        "mime_type": att.content_type or ("image/jpeg" if att.type == "image" else "application/octet-stream"),
                        "filename": filename,
                        "path": uploaded_path,
                    })
                except Exception:
                    label = f"[{'图片' if att.type == 'image' else '文件'}：{filename}（上传失败）]"
                    appended_text = f"{appended_text}\n{label}" if appended_text else label
                continue

            # 2. Small images -> inline base64
            if att.type == "image":
                inline_images.append({
                    "mime_type": att.content_type or "image/jpeg",
                    "filename": att.filename or "image.jpg",
                    "data": data,
                })
            # 3. Small files -> inline file item
            else:
                inline_files.append({
                    "mime_type": att.content_type or "application/octet-stream",
                    "filename": att.filename or "file.dat",
                    "data": data,
                })

        return inline_images, inline_files, file_refs, appended_text

    def post_message(
        self,
        session_id: str,
        text: str,
        attachments: Optional[List[Attachment]] = None,
        threshold_bytes: int = 2 * 1024 * 1024,
        in_reply_to: Optional[str] = None,
        reply_to_target: Optional[dict] = None,
    ) -> str:
        """Post a user message to Muse and return the message_id immediately without waiting."""
        try:
            with self.create_client() as client:
                client.connect()
                atts = attachments or []
                inline_images, inline_files, file_refs, processed_text = self.process_attachments(
                    client, atts, text, threshold_bytes
                )
                self.check_approval_gate(session_id)
                sent_mid = client.send_message(
                    session_id=session_id,
                    text=processed_text,
                    images=inline_images or None,
                    files=inline_files or None,
                    file_refs=file_refs or None,
                    **({"in_reply_to": in_reply_to} if in_reply_to else {}),
                    **({"reply_to_target": reply_to_target} if reply_to_target else {}),
                )
                logger.debug("Message posted to Muse session_id=%s sent_mid=%s", session_id, sent_mid)
                trace_event("muse.service", "message.posted", session_id=session_id, sent_mid=sent_mid,
                            attachment_count=len(atts))
                return sent_mid
        except Exception as e:
            if is_runtime_auth_error(e) and "client" in locals():
                self.token_manager.invalidate_mint(client.token, client.notary_token)
            # Never replay a possibly accepted POST; recover for the next operation.
            raise

    def send_turn(
        self,
        session_id: str,
        text: str,
        attachments: Optional[List[Attachment]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        threshold_bytes: int = 2 * 1024 * 1024,
        on_sent: Optional[Callable[[str], None]] = None,
        in_reply_to: Optional[str] = None,
        reply_to_target: Optional[dict] = None,
    ) -> TurnResult:
        """Send a turn to Muse, wait for reply, and mark seen."""
        try:
            with self.create_client() as client:
                client.connect()
                atts = attachments or []
                inline_images, inline_files, file_refs, processed_text = self.process_attachments(
                    client, atts, text, threshold_bytes
                )

                # Send message
                self.check_approval_gate(session_id)
                sent_mid = client.send_message(
                    session_id=session_id,
                    text=processed_text,
                    images=inline_images or None,
                    files=inline_files or None,
                    file_refs=file_refs or None,
                    **({"in_reply_to": in_reply_to} if in_reply_to else {}),
                    **({"reply_to_target": reply_to_target} if reply_to_target else {}),
                )
                logger.debug("Message posted to Muse session_id=%s sent_mid=%s", session_id, sent_mid)
                trace_event("muse.service", "message.posted", session_id=session_id, sent_mid=sent_mid,
                            attachment_count=len(atts))

                if on_sent and sent_mid:
                    try:
                        on_sent(sent_mid)
                    except Exception:
                        pass

                if should_cancel and should_cancel():
                    raise TurnCancelled("turn cancelled before wait")

                # Wait for turn completion
                if self.config.reply_mode == "push":
                    try:
                        sub = client.subscribe(session_id, timeout=20)
                        turn = client.wait_push(sub, session_id, processed_text, sent_mid, timeout=120)
                    except Exception:
                        # Fallback to polling
                        turn = client.wait_turn(session_id, processed_text, sent_mid=sent_mid, timeout=180, should_cancel=should_cancel)
                else:
                    turn = client.wait_turn(session_id, processed_text, sent_mid=sent_mid, timeout=180, should_cancel=should_cancel)

                client.mark_seen(session_id)
                return turn
        except Exception as e:
            if is_runtime_auth_error(e) and "client" in locals():
                self.token_manager.invalidate_mint(client.token, client.notary_token)
            # Never replay a possibly accepted POST; recover for the next operation.
            raise

    def read_workspace_bytes(self, paths: List[str], cap: int = 25 * 1024 * 1024) -> Dict[str, Optional[bytes]]:
        """Read files/images from the Muse workspace."""
        out: Dict[str, Optional[bytes]] = {}
        if not paths:
            return out
        try:
            with self.create_client() as client:
                client.connect()
                for p in paths:
                    try:
                        data = client.read_file(p)
                        if data and len(data) <= cap:
                            out[p] = data
                        else:
                            out[p] = None
                    except Exception:
                        out[p] = None
        except Exception:
            pass
        return out

    def list_sessions(self) -> List[dict]:
        with self.create_client() as client:
            client.connect()
            return client.list_sessions()
