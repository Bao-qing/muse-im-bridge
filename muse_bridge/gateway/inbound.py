"""One shared inbound pipeline; the runtime supplies an identity-scoped context."""
from __future__ import annotations

import asyncio
import json

from ..config.models import ChatConfig
from ..core.commands import CHINESE_COMMANDS, GATEWAY_COMMANDS, CommandError, CommandHandler
from ..core.message import InboundMessage
from ..muse.client.errors import LoginRequired, is_runtime_auth_error
from ..muse.interactions import ApprovalBlocked
from ..observability import get_logger, safe_error, trace_event

logger = get_logger(__name__)

# Commands that change which session a chat uses, or the account itself.
ADMIN_COMMANDS = frozenset({"login", "switch", "delete", "rename", "archive", "unarchive", "new"})


class InboundPipeline:
    async def handle_inbound(self, ctx, msg: InboundMessage) -> str:
        """Process one parsed chat message (ids already validated, mentions stripped)."""
        kind = msg.source.chat_type
        target = msg.source.target_id
        user = msg.source.sender_id
        event = {"references": msg.references, "own_references": msg.own_references}
        msg_id = msg.id or None
        content = msg.text
        attachments = msg.attachments
        credential_input = ctx.interaction_router.is_credential_input((kind, target, user), content, event)
        if not credential_input:
            content = content.strip()
        sid = ctx.session_mapper.current(kind, target) or ""
        known_quote = ctx.outbound_pusher.reference_store.resolve_channel(sid, kind, target, msg.references)
        trigger = getattr(ctx.config, "group_trigger", "all")
        if (kind == "group" and trigger != "all" and not credential_input and not known_quote
                and not content.startswith("/") and not (trigger == "at" and msg.mentioned)):
            return "ignored"
        if (kind == "private" and user == target and isinstance(ctx.config, ChatConfig)
                and ctx.config.is_admin(user)
                and (content or attachments or ctx.interaction_router.message_references(event))):
            ctx.admin_private_activity.record(ctx._admin_activity_identity(), user, ctx.config.admin_id)

        if ctx.access_requests is not None:
            if ctx.access_requests.is_command(content):
                owner = ctx.interaction_owner(kind, target, user)
                async with ctx.muse_service.interactions.owner_lock(owner):
                    current = ctx.muse_service.interactions.current(owner)
                    if (current and not current.terminal) or ctx.interaction_router.message_references(event):
                        await ctx.send_reply(kind, target, "请先结束当前填写，再单独发送申请命令。", msg_id)
                        return "handled"
                    await ctx.access_requests.handle(kind, target, user, content, msg_id)
                return "handled"
            if not ctx.access_requests.allows(kind, target, user):
                logger.debug('Ignored %s message: sender not in whitelist', kind)
                return "handled"

        if not content and not attachments and not ctx.interaction_router.message_references(event):
            return "handled"

        target_type, target_id = kind, target
        can_manage = ctx.can_manage(user)
        owner = ctx.interaction_owner(target_type, target_id, user)
        session_id = ctx.session_mapper.current(target_type, target_id) or ""

        # Consume sensitive input before any raw message logging.
        if await ctx.interaction_router.handle(owner, content, msg_id, can_manage, session_id, event=event):
            logger.info('Local interaction from %s:%s: [redacted]', target_type, target_id)
            return "handled"

        available = await asyncio.to_thread(ctx.muse_service.token_manager.has_credentials)
        if available and ctx._in_login_fallback:
            ctx.exit_login_mode()
        needs_login = not available
        if needs_login and not content.startswith("/"):
            text = "Muse 登录已失效，" + ctx._login_instruction()
            await ctx.send_reply(target_type, target_id, text, msg_id)
            return "handled"

        logger.info("Inbound platform=%s target_type=%s target_id=%s msg_id=%s attachments=%s",
                    ctx.name, target_type, target_id, msg_id, len(attachments))

        # 1. Local Slash Commands
        parsed = CommandHandler.parse_command(content)
        if parsed is not None:
            await self._run_command(ctx, parsed, target_type, target_id, msg_id, can_manage)
            return "handled"

        # Muse understands its native English commands, not their Chinese labels.
        parts = content.split(None, 1)
        if parts and parts[0].startswith("/"):
            native = CHINESE_COMMANDS.get(parts[0][1:])
            if native in GATEWAY_COMMANDS:
                args = parts[1] if len(parts) > 1 else ""
                if native == "usage":
                    args = {"开启": "on", "关闭": "off"}.get(args, args)
                content = "/" + native + ((" " + args) if args else "")

        trace_event(f"{ctx.name}.adapter", "message.received", payload={"content": content},
                    session_id=session_id, target_type=target_type, target_id=target_id,
                    msg_id=msg_id, attachment_count=len(attachments))

        references = msg.references or ctx.interaction_router.message_references(event)
        if (kind == "group" and isinstance(ctx.config, ChatConfig)
                and ctx.config.group_sender_info and not content.startswith("/")):
            info = {"用户ID": user}
            for label, value in (("昵称", msg.source.sender_name), ("群名片", msg.source.sender_card)):
                if isinstance(value, str) and value.strip():
                    info[label] = value.strip()
            content = "【群消息发送者 " + json.dumps(info, ensure_ascii=False) + "】\n" + content
        try:
            return await self._forward(ctx, target_type, target_id, content, attachments, msg_id, can_manage, references, event)
        finally:
            # /stop is a native Muse command, not a local CommandHandler action.
            # Finish after forwarding so track_session cannot restart typing.
            if content.split(None, 1)[0:1] == ["/stop"]:
                sid = ctx.session_mapper.current(target_type, target_id)
                if sid:
                    await ctx.adapter.activity(target_id, sid, "finished", kind=target_type)

    async def _run_command(self, ctx, parsed, target_type, target_id, msg_id, can_manage) -> None:
        cmd_name, cmd_args = parsed

        # Check admin-only permission switch
        if ctx._admin_commands_only() and cmd_name in ADMIN_COMMANDS and not can_manage:
            await ctx.send_reply(
                target_type,
                target_id,
                "权限不足：管理命令仅限管理员在私聊中使用。",
                msg_id,
            )
            return

        loop = asyncio.get_running_loop()
        client = None
        # Session this chat is currently bound to.
        chat_session_id = ctx.session_mapper.current(target_type, target_id)
        if not chat_session_id:
            chat_session_id = "（尚未创建，发送第一条消息后自动建立）"
        try:
            if cmd_name not in ("help", "__help__", "__usage_hint__", "new"):
                client = await asyncio.to_thread(ctx.muse_service.create_client)
                await asyncio.to_thread(client.connect)
            # Persistence is handled by SessionMapper.bind
            reply, new_sid = await loop.run_in_executor(
                None,
                CommandHandler.execute,
                cmd_name,
                cmd_args,
                client,
                chat_session_id,
                ctx.muse_service.config.session_id,
            )
            if cmd_name in {"stop", "delete", "archive"} or new_sid != chat_session_id:
                await ctx.adapter.activity(target_id, chat_session_id, "finished", kind=target_type)
            if new_sid != chat_session_id:
                ctx.session_mapper.bind(target_type, target_id, new_sid)
                if cmd_name == "new":
                    ctx.outbound_pusher.track_session(new_sid, fresh=True)
                else:
                    # Bound to an existing session: never replay its old history.
                    await ctx.outbound_pusher.ensure_baseline(new_sid)
        except CommandError as e:
            reply = str(e)
        except ApprovalBlocked as e:
            await ctx.send_reply(target_type, target_id, str(e), msg_id)
            return
        except Exception as e:
            if isinstance(e, LoginRequired):
                ctx.enter_login_mode()
                reply = "Muse 登录已失效，" + ctx._login_instruction()
            else:
                if is_runtime_auth_error(e) and client is not None:
                    await asyncio.to_thread(ctx.muse_service.token_manager.invalidate_mint, client.token, client.notary_token)
                reply = "命令结果未能确认，请稍后核对，不会重复执行。"
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

        # Reply command output directly
        await ctx.send_command_output(target_type, target_id, reply, msg_id)

    async def _forward(self, ctx, target_type, target_id, content, attachments, msg_id, can_manage, references=(), event=None) -> str:
        """handled: consumed; unknown: POST may have run; retryable: no POST attempted."""
        post_started = False
        turn_tracked = False
        session_id = ""
        loop = asyncio.get_running_loop()
        quote = {}
        intake_token = f"intake:{msg_id or id(event)}"
        intake_started = not content.startswith("/")

        own_references = [("id", msg_id)] if msg_id else []
        own_references.extend((event or {}).get("own_references", ()))

        if intake_started:
            ctx.adapter.activity_started(target_id, intake_token, kind=target_type)
        try:
            # Download attachments if any
            for att in attachments:
                if (att.url or att.file_info) and not att.data:
                    max_cap = (
                        ctx.config.max_video_bytes if att.type == "video"
                        else ctx.config.max_file_bytes if att.type == "file"
                        else ctx.config.max_image_bytes
                    )
                    try:
                        att.data = await ctx.download_attachment(att, max_cap)
                    except Exception as e:
                        logger.warning('Attachment download failed: %s', safe_error(e))
                        content += f"\n[{att.type}下载失败]"

            try:
                # Preserve the legacy admin main-session binding, not every personal chat.
                if (ctx.seed_main and target_type == "private" and isinstance(ctx.config, ChatConfig)
                        and can_manage and ctx.muse_service.config.session_id):
                    if not ctx.session_mapper.current("private", target_id):
                        ctx.session_mapper.bind("private", target_id, ctx.muse_service.config.session_id)

                # Resolve which Muse session this chat talks to
                session_id, created = ctx.session_mapper.resolve(target_type, target_id)
                if created:
                    ctx.outbound_pusher.track_session(session_id, fresh=True)
                    logger.info('New isolated Muse session %s for %s:%s', session_id, target_type, target_id)
                else:
                    # Pre-bound / restored session: baseline once so old history is never replayed
                    await ctx.outbound_pusher.ensure_baseline(session_id)

                reference = ctx.outbound_pusher.reference_store.resolve_channel(
                    session_id, target_type, target_id, references)
                quote = {}
                if reference:
                    if reference.reply_target:
                        quote["reply_to_target"] = reference.reply_target
                    else:
                        quote["in_reply_to"] = reference.muse_id
                elif references:
                    content = "【引用消息未关联到当前会话】" + chr(10) + content

                # Post message to Muse in strict chronological order
                post_started = True
                sent_mid = await asyncio.to_thread(
                    ctx.muse_service.post_message, session_id, content, attachments,
                    ctx.config.file_ref_threshold_bytes, **quote)
                if sent_mid and msg_id:
                    ctx.outbound_pusher.reference_store.remember(
                        session_id, target_type, target_id, sent_mid, own_references, content)
                ctx.outbound_pusher.track_session(session_id)
                turn_tracked = True

                # Preserve causal ownership independently of passive route lifetime.
                ctx.outbound_pusher.remember_origin(session_id, sent_mid, target_type, target_id)
                ctx.route_manager.register(sent_mid, target_type, target_id, msg_id)
                logger.debug('Message delivered to Muse (mid=%s), route registered for %s:%s', sent_mid, target_type, target_id)
                trace_event(f"{ctx.name}.adapter", "route.registered", session_id=session_id, sent_mid=sent_mid,
                            target_type=target_type, target_id=target_id, msg_id=msg_id)
                return "handled" if sent_mid else "unknown"
            except ApprovalBlocked as e:
                await ctx.send_reply(target_type, target_id, str(e), msg_id)
                return "handled"
            except Exception as e:
                if isinstance(e, LoginRequired):
                    logger.warning('Cookie 会话不可用，需要重新登录。 error=%s', safe_error(e))
                    ctx.enter_login_mode()
                    target = ctx.notification_target()
                    if target_type == "private" and can_manage:
                        reply = "Muse 登录已失效，请发送 /登录 或 /登录 <邮箱> 重新登录。"
                    else:
                        reply = "Muse 登录已失效，已通知管理员重新登录。"
                        if target and (target_type != "private" or target_id != target):
                            loop.call_soon(asyncio.create_task, ctx._notify_admin_login(target))
                    await ctx.send_reply(target_type, target_id, reply, msg_id)
                    return "unknown" if post_started else "retryable"
                if post_started and not quote and ctx.can_recreate_session(e):
                    # Preserve legacy recovery only for an explicit session rejection.
                    # A failed recovery POST is never replayed again.
                    try:
                        new_sid = ctx.session_mapper.new_session(target_type, target_id)
                        ctx.outbound_pusher.track_session(new_sid, fresh=True)
                        sent_mid = await asyncio.to_thread(
                            ctx.muse_service.post_message, new_sid, content, attachments,
                            ctx.config.file_ref_threshold_bytes)
                        if sent_mid and msg_id:
                            ctx.outbound_pusher.reference_store.remember(
                                new_sid, target_type, target_id, sent_mid, own_references, content)
                        ctx.outbound_pusher.track_session(new_sid)
                        turn_tracked = True
                        ctx.outbound_pusher.remember_origin(new_sid, sent_mid, target_type, target_id)
                        ctx.route_manager.register(sent_mid, target_type, target_id, msg_id)
                        return "handled" if sent_mid else "unknown"
                    except Exception as recovery_error:
                        logger.error('Missing-session recovery unconfirmed: %s', safe_error(recovery_error))
                        await ctx.send_command_output(target_type, target_id,
                            "消息结果未能确认，没有重复发送，请稍后核对。", msg_id)
                        return "unknown"
                # Transport failures and unclassified errors never replay user input.
                logger.error('Failed to post message to Muse: %s', safe_error(e))
                await ctx.send_reply(target_type, target_id,
                    "连接暂时不可用，消息可能已发送，请稍后核对。" if post_started
                    else "连接暂时不可用，消息尚未发送，请稍后重试。", msg_id)
                return "unknown" if post_started else "retryable"
        finally:
            if intake_started and not turn_tracked:
                await ctx.adapter.activity(target_id, intake_token, "finished", kind=target_type)
