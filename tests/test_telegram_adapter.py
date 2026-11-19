"""Offline Bot API tests. Every request is served by MockTransport."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from muse_bridge.adapters.telegram import TelegramAdapter
from muse_bridge.adapters.telegram.client import TelegramAPIError
from muse_bridge.adapters.telegram.markdown import chunk_formatted, parse_markdown, utf16_len
from muse_bridge.config.models import AccessRequestsConfig, ConfigLoader, MuseConfig, TelegramConfig
from muse_bridge.core.delivery import DeliveryEnvelope, DeliveryStatus, NativeQuote, ReactionEnvelope
from muse_bridge.core.message import Attachment, InboundReaction, MessageSource
from muse_bridge.gateway.runtime import UnifiedGateway
from muse_bridge.muse.service import MuseService


BOT_ID = 321
USER_ID = 123
GROUP_ID = -100100
TOKEN = f"{BOT_ID}:offline-fixture"


def ok(value):
    return httpx.Response(200, json={"ok": True, "result": value})


def reject(status, **extras):
    return httpx.Response(status, json={"ok": False, "error_code": status, **extras})


def update(uid, mid, text=None, *, chat_id=USER_ID, sender=USER_ID, **fields):
    message = {
        "message_id": mid, "date": 1234,
        "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
        "from": {"id": sender, "is_bot": False, "first_name": "Test"},
        **fields,
    }
    if text is not None:
        message["text"] = text
    return {"update_id": uid, "message": message}


def reaction_update(uid, old, new, *, chat_id=USER_ID, **fields):
    return {"update_id": uid, "message_reaction": {
        "message_id": 42, "date": 1234,
        "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
        "user": {"id": USER_ID, "is_bot": False},
        "old_reaction": [{"type": "emoji", "emoji": emoji} for emoji in old],
        "new_reaction": [{"type": "emoji", "emoji": emoji} for emoji in new],
        **fields,
    }}


class TelegramConfigTests(unittest.TestCase):
    def test_loader_reads_env_and_uses_closed_defaults(self):
        raw = {"adapters": {"telegram": {"enabled": True, "admin_id": [USER_ID],
                                        "whitelist": {"private": [USER_ID]}}}}
        with patch.object(ConfigLoader, "_read_raw", return_value=raw), patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}):
            cfg = ConfigLoader.load()
        self.assertEqual(cfg.telegram.bot_token, TOKEN)
        self.assertNotIn(TOKEN, repr(cfg))
        self.assertEqual(cfg.telegram.admin_id, [str(USER_ID)])
        self.assertEqual(cfg.telegram.whitelist["private"], [str(USER_ID)])
        self.assertFalse(cfg.telegram.allow_all_private)
        self.assertFalse(cfg.telegram.allow_all_group)
        self.assertTrue(cfg.telegram.credentials_in_place)
        self.assertFalse(cfg.telegram.seed_main_session)
        self.assertTrue(cfg.qq.seed_main_session)
        self.assertTrue(cfg.onebot.seed_main_session)

    def test_credentials_in_place_can_be_disabled_explicitly(self):
        raw = {"adapters": {"telegram": {"credentials_in_place": False}}}
        with patch.object(ConfigLoader, "_read_raw", return_value=raw):
            cfg = ConfigLoader.load()
        self.assertFalse(cfg.telegram.credentials_in_place)

    def test_enabled_adapter_requires_token_without_echoing_bad_input(self):
        with self.assertRaisesRegex(ValueError, "valid bot_token") as cm:
            TelegramConfig(enabled=True, bot_token="not-a-token-secret")
        self.assertNotIn("not-a-token-secret", str(cm.exception))


class TelegramMarkdownTests(unittest.TestCase):
    def test_utf16_headings_code_blocks_links_and_nesting_rules(self):
        md = (
            "# **标题**\n\n"
            "🦊 **加粗** *斜体* ~~删除~~ ||隐藏||\n\n"
            "标识符 foo_bar_baz 与 **含 `code` 的粗体** 和 ***粗斜***\n\n"
            "[*链接*](https://example.invalid/a_(b)) 与 [危险](javascript:alert(1))\n\n"
            "```python\n**not bold**\n```\n\n"
            "> 引用 *内联* 与 `行内代码` [link](https://example.invalid/)"
        )
        plain, entities = parse_markdown(md)
        self.assertIn("🦊 加粗 斜体 删除 隐藏", plain)
        self.assertIn("foo_bar_baz", plain)
        self.assertNotIn("javascript:", [e.get("url", "") for e in entities])
        self.assertIn("[危险](javascript:alert(1))", plain)

        def slice_u16(entity):
            raw = plain.encode("utf-16-le")
            return raw[entity["offset"] * 2 : (entity["offset"] + entity["length"]) * 2].decode("utf-16-le")

        by_type = {}
        for e in entities:
            by_type.setdefault(e["type"], []).append((slice_u16(e), e))

        # Heading should emit a single bold entity (no duplicate bold inside bold)
        self.assertEqual([text for text, _ in by_type["bold"]].count("标题"), 1)
        self.assertIn("加粗", [text for text, _ in by_type["bold"]])
        self.assertIn("含 ", [text for text, _ in by_type["bold"]])
        self.assertIn(" 的粗体", [text for text, _ in by_type["bold"]])
        self.assertIn("粗斜", [text for text, _ in by_type["bold"]])
        self.assertIn("粗斜", [text for text, _ in by_type["italic"]])
        self.assertEqual([text for text, _ in by_type["strikethrough"]], ["删除"])
        self.assertEqual([text for text, _ in by_type["spoiler"]], ["隐藏"])
        self.assertEqual([text for text, _ in by_type["code"]], ["code"])
        self.assertEqual(by_type["pre"][0][0], "**not bold**")
        self.assertEqual(by_type["pre"][0][1]["language"], "python")
        self.assertEqual(by_type["text_link"][0][0], "链接")
        self.assertEqual(by_type["text_link"][0][1]["url"], "https://example.invalid/a_(b)")
        self.assertEqual(by_type["blockquote"][0][0], "引用 内联 与 行内代码 link (https://example.invalid/)")

        # Verify Telegram nesting rule: bold/italic/etc. never overlap code or pre
        code_ranges = [(e["offset"], e["offset"] + e["length"]) for e in entities if e["type"] in {"code", "pre"}]
        for e in entities:
            if e["type"] in {"bold", "italic", "strikethrough", "spoiler", "text_link"}:
                s, t = e["offset"], e["offset"] + e["length"]
                for cs, ct in code_ranges:
                    self.assertTrue(t <= cs or s >= ct, f"{e} overlaps code/pre {(cs, ct)}")

        # Verify blockquote never contains code, pre, or text_link
        for bq in [e for e in entities if e["type"] == "blockquote"]:
            bs, bt = bq["offset"], bq["offset"] + bq["length"]
            for e in entities:
                if e["type"] in {"code", "pre", "text_link"}:
                    s, t = e["offset"], e["offset"] + e["length"]
                    self.assertTrue(t <= bs or s >= bt, f"{e} nested inside blockquote")

    def test_tables_lists_and_unmatched_delimiters(self):
        md = (
            "| 名称 | 值 |\n"
            "| --- | --- |\n"
            "| `k` | **v** |\n\n"
            "- [x] 完成\n"
            "- [ ] 待办\n"
            "  - 子项 *斜*\n"
            "1. 第一\n\n"
            "**含单个 * 的粗体** 与 2 * 3 = 6"
        )
        plain, entities = parse_markdown(md)
        self.assertIn("名称 | 值\nk | v", plain)
        self.assertIn("☑ 完成\n☐ 待办\n  • 子项 斜\n1. 第一", plain)
        self.assertIn("含单个 * 的粗体 与 2 * 3 = 6", plain)

    def test_chunk_formatted_preserves_surrogates_and_rebased_entities(self):
        plain, entities = parse_markdown("🦊 **加粗段一**\n**加粗段二**")
        chunks = chunk_formatted(plain, entities, limit=9, max_chunks=4)
        self.assertEqual([t for t, _ in chunks], ["🦊 加粗段一", "加粗段二"])
        self.assertEqual(chunks[0][1], [{"type": "bold", "offset": 3, "length": 4}])
        self.assertEqual(chunks[1][1], [{"type": "bold", "offset": 0, "length": 4}])
        for chunk_text, chunk_entities in chunks:
            u16_cap = utf16_len(chunk_text)
            for e in chunk_entities:
                self.assertGreaterEqual(e["offset"], 0)
                self.assertGreater(e["length"], 0)
                self.assertLessEqual(e["offset"] + e["length"], u16_cap)


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.requests = []
        self.updates = []
        self.responses = {}
        self.next_message = 900
        self.transport = httpx.MockTransport(self.handle_request)

    async def handle_request(self, request):
        self.assertEqual(request.url.host, "api.telegram.org")
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content) if "application/json" in request.headers.get("content-type", "") else request.content
        self.requests.append((method, payload))
        if method in self.responses and self.responses[method]:
            override = self.responses[method].pop(0)
            if isinstance(override, BaseException):
                raise override
            return override
        if method == "getMe":
            return ok({"id": BOT_ID, "is_bot": True, "username": "MuseTestBot"})
        if method == "getUpdates":
            return ok([row for row in self.updates if row["update_id"] >= payload.get("offset", 0)])
        if method == "getFile":
            return ok({"file_path": "photos/file_1.jpg", "file_size": 4})
        if method == "file_1.jpg":
            return httpx.Response(200, content=b"data")
        if method == "sendMessage":
            self.next_message += 1
            return ok({"message_id": self.next_message, "chat": {"id": payload["chat_id"]}})
        self.fail(f"Unexpected Bot API method: {method}")

    def adapter(self, **overrides):
        fields = {key: str(self.root / key) for key in
                  ("routes_file", "delivered_file", "session_map_file", "push_state_file")}
        fields.update(enabled=True, bot_token=TOKEN, admin_id=[str(USER_ID)],
                      whitelist={"private": [str(USER_ID)], str(GROUP_ID): "*"},
                      access_requests=AccessRequestsConfig(state_file=str(self.root / "access.json")))
        fields.update(overrides)
        adapter = TelegramAdapter(TelegramConfig(**fields), transport=self.transport)
        self.addAsyncCleanup(adapter.stop)
        return adapter

    async def mounted(self, **overrides):
        token = MagicMock()
        token.has_credentials.return_value = True
        svc = MuseService(MuseConfig(session_id="production-main", cookies_file=str(self.root / "cookies")), token)
        svc.interactions.approvals_ready = True
        svc.post_message = MagicMock(side_effect=lambda *args, **kw: f"m{svc.post_message.call_count}")
        gateway = UnifiedGateway(svc)
        self.addAsyncCleanup(gateway.stop)
        adapter = gateway.mount(self.adapter(**overrides))
        await adapter.prepare()
        ctx = gateway.contexts[adapter]
        ctx.outbound_pusher.ensure_baseline = AsyncMock()
        return adapter, ctx, svc

    async def test_long_poll_through_gateway_with_plain_command_reply_and_restart(self):
        adapter, ctx, svc = await self.mounted()
        self.assertEqual(adapter.account_identity(), str(BOT_ID))
        self.assertEqual(ctx.session_mapper.current("private", str(USER_ID)), "")
        self.updates = [update(22, 6, "Can you see this?"), update(21, 5, "/help"),
                        update(23, 1, "blocked", chat_id=999, sender=999)]
        await adapter._poller.poll_once()
        self.assertEqual(svc.post_message.call_count, 1)
        sid = ctx.session_mapper.current("private", str(USER_ID))
        self.assertNotEqual(sid, "production-main")
        self.assertEqual(svc.post_message.call_args.args[:2], (sid, "Can you see this?"))
        _, data = next(req for req in self.requests if req[0] == "sendMessage")
        self.assertNotIn("reply_parameters", data)
        self.assertEqual(data["chat_id"], USER_ID)
        restarted = self.adapter()
        host = MagicMock()
        host.accept = AsyncMock(return_value="handled")
        restarted.mount(host)
        await restarted.prepare()
        await restarted._poller.poll_once()
        host.accept.assert_not_awaited()
        self.assertEqual(self.requests[-1], ("getUpdates", {
            "timeout": 30, "allowed_updates": ["message", "message_reaction"], "offset": 24,
        }))

    async def test_reaction_without_session_is_ignored_then_processes_chat(self):
        adapter, _, svc = await self.mounted()
        self.updates = [reaction_update(1, [], ["👍"]), update(2, 5, "ordinary chat")]
        self.assertTrue(await adapter._poller.poll_once())
        self.assertEqual(adapter._poller._state["offset"], 3)
        svc.post_message.assert_called_once()
        self.assertEqual(svc.post_message.call_args.args[1], "ordinary chat")

    async def test_reaction_handoff_preserves_add_replace_and_clear(self):
        adapter = self.adapter()
        host = MagicMock()
        host.accept = AsyncMock()
        host.accept_reaction = AsyncMock(return_value="handled")
        adapter.mount(host)
        await adapter.prepare()
        self.updates = [reaction_update(3, ["❤️"], []), reaction_update(1, [], ["👍"]),
                        reaction_update(2, ["👍"], ["❤️"]), reaction_update(4, [], ["👍"], chat_id=GROUP_ID)]
        self.assertTrue(await adapter._poller.poll_once())
        host.accept.assert_not_awaited()
        self.assertEqual([call.args for call in host.accept_reaction.await_args_list], [
            (adapter, InboundReaction(str(uid), MessageSource("telegram", kind, str(target), str(USER_ID)),
                                      ("id", f"{target}:42"), old, new, 1234))
            for uid, kind, target, old, new in (
                (1, "private", USER_ID, (), ("👍",)), (2, "private", USER_ID, ("👍",), ("❤️",)),
                (3, "private", USER_ID, ("❤️",), ()), (4, "group", GROUP_ID, (), ("👍",)),
            )
        ])
        self.assertEqual(adapter._poller._state["offset"], 5)

    async def test_malformed_or_unsupported_reactions_do_not_clear_state(self):
        adapter = self.adapter()
        host = MagicMock()
        host.accept_reaction = AsyncMock()
        adapter.mount(host)
        await adapter.prepare()
        for overrides in (
            {"new_reaction": None}, {"new_reaction": [{"type": "paid"}]},
            {"new_reaction": [{"type": "custom_emoji", "custom_emoji_id": "42"}]},
            {"new_reaction": [{"type": "emoji", "emoji": ""}]}, {"old_reaction": None},
            {"actor_chat": {"id": GROUP_ID}}, {"user": None}, {"user": {"id": USER_ID, "is_bot": True}},
            {"message_id": 0},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(await adapter._handle_update(reaction_update(1, ["👍"], [], **overrides)), "ignored")
        host.accept_reaction.assert_not_awaited()

    async def test_group_mentions_foreign_commands_and_topic_events(self):
        adapter, ctx, svc = await self.mounted(group_sender_info=False)
        at = {"type": "mention", "offset": 3, "length": len("@MuseTestBot")}
        command = {"type": "bot_command", "offset": 0, "length": len("/help@MuseTestBot")}
        foreign = {**command, "length": len("/help@OtherBot")}
        self.updates = [update(1, 4, "plain", chat_id=GROUP_ID),
                        update(2, 5, "🧪 @MuseTestBot hello", chat_id=GROUP_ID, entities=[at]),
                        update(3, 6, "/help@OtherBot", chat_id=GROUP_ID, entities=[foreign]),
                        update(4, 7, "/help@MuseTestBot", chat_id=GROUP_ID, entities=[command]),
                        update(5, 8, "/help", chat_id=GROUP_ID, is_topic_message=True)]
        await adapter._poller.poll_once()
        self.assertEqual(svc.post_message.call_count, 1)
        self.assertEqual(svc.post_message.call_args.args[1], "🧪  hello")
        sent = [body for method, body in self.requests if method == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], GROUP_ID)
        self.assertNotIn("reply_parameters", sent[0])

    async def test_media_and_quote_preserve_original_input_until_gateway(self):
        adapter = self.adapter()
        host = MagicMock()
        host.accept = AsyncMock(return_value="handled")
        adapter.mount(host)
        await adapter.prepare()
        self.updates = [update(1, 6, caption="  user\npass  ",
                               photo=[{"file_id": "small"}, {"file_id": "large"}],
                               reply_to_message={"message_id": 5, "chat": {"id": USER_ID}}),
                        update(2, 7, "cross chat", reply_to_message={"message_id": 5, "chat": {"id": GROUP_ID}})]
        await adapter._poller.poll_once()
        msg, other = [call.args[1] for call in host.accept.await_args_list]
        self.assertEqual(msg.text, "  user\npass  ")
        self.assertEqual(msg.references, [("id", "123:5")])
        self.assertEqual(other.references, [])
        self.assertEqual(msg.attachments, [Attachment("image", "photo.jpg", "image/jpeg", file_info="large")])
        self.assertEqual(await adapter.download_attachment(msg.attachments[0], 4), b"data")
        with self.assertRaisesRegex(ValueError, "limit"):
            await adapter.download_attachment(msg.attachments[0], 3)
        self.responses["getFile"] = [ok({"file_path": "../getUpdates"})]
        with self.assertRaisesRegex(ValueError, "file path"):
            await adapter.download_attachment(msg.attachments[0], 10)
        self.responses["getFile"] = [ok({"file_path": "photos/file_1.jpg"})]
        self.responses["file_1.jpg"] = [httpx.Response(200, content=b"xxxx", headers={"Content-Length": "1"})]
        with self.assertRaisesRegex(ValueError, "limit"):
            await adapter.download_attachment(msg.attachments[0], 3)

    async def test_retryable_holds_batch_and_cancelled_update_is_not_replayed(self):
        self.updates = [update(1, 5, "first"), update(2, 6, "second")]
        adapter = self.adapter()
        host = MagicMock()
        host.accept = AsyncMock(side_effect=["retryable", asyncio.CancelledError()])
        adapter.mount(host)
        await adapter.prepare()
        self.assertFalse(await adapter._poller.poll_once())
        self.assertEqual(host.accept.await_count, 1)
        with self.assertRaises(asyncio.CancelledError):
            await adapter._poller.poll_once()
        self.assertEqual(host.accept.await_count, 2)
        restarted = self.adapter()
        host.accept = AsyncMock(return_value="handled")
        restarted.mount(host)
        await restarted.prepare()
        await restarted._poller.poll_once()
        host.accept.assert_awaited_once()
        self.assertEqual(host.accept.await_args.args[1].text, "second")

    async def test_stale_offset_is_not_sent(self):
        adapter = self.adapter()
        await adapter.prepare()
        adapter._poller._save(9999)
        adapter._poller._state["saved_at"] = int(time.time()) - 6 * 86400
        self.updates = [update(1, 5, "smaller new ID")]
        host = MagicMock()
        host.accept = AsyncMock(return_value="handled")
        adapter.mount(host)
        await adapter._poller.poll_once()
        self.assertNotIn("offset", self.requests[-1][1])
        host.accept.assert_awaited_once()

    async def test_stop_cancels_in_flight_long_poll(self):
        adapter = self.adapter()
        await adapter.prepare()
        entered = asyncio.Event()

        async def blocking_poll(*args):
            entered.set()
            await asyncio.Future()

        adapter.client.updates = blocking_poll
        running = asyncio.create_task(adapter.start())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(adapter.stop(), 1)
        self.assertTrue(running.cancelled())

    async def test_conflict_is_reported_without_deleting_webhook(self):
        adapter = self.adapter()
        await adapter.prepare()
        self.responses["getUpdates"] = [reject(409, description="webhook is active")]
        with self.assertRaisesRegex(RuntimeError, "no webhook or other poller"):
            await adapter.start()
        self.assertEqual([method for method, _ in self.requests], ["getMe", "getUpdates"])

    async def test_sender_does_not_infer_quotes_from_routing_ids(self):
        adapter = self.adapter()
        for route in ({"input_id": "123:5"}, {"passive_id": "123:6"}):
            with self.subTest(route=route):
                env = DeliveryEnvelope(next(iter(route)), "private", str(USER_ID), "plain reply",
                                       purpose="reply", **route)
                self.assertTrue((await adapter.deliver(env)).confirmed)
                self.assertEqual(self.requests[-1], ("sendMessage", {
                    "chat_id": USER_ID, "text": "plain reply",
                }))

    async def test_sender_rejection_and_unknown_keep_prior_receipts(self):
        adapter = self.adapter(chunk_chars=5)
        delivery = DeliveryEnvelope("key", "private", str(USER_ID), "hello!",
                                    input_id="123:5", passive_id="123:6",
                                    quote=NativeQuote((("id", "123:77"),)))
        self.responses["sendMessage"] = [ok({"message_id": 1, "chat": {"id": USER_ID}}), reject(403)]
        result = await adapter.deliver(delivery)
        self.assertEqual(result.status, DeliveryStatus.PARTIAL)
        self.assertEqual(result.receipts[0].references, (("id", "123:1"),))
        self.assertEqual(self.requests[0][1]["reply_parameters"]["message_id"], 77)
        self.assertNotIn("reply_parameters", self.requests[1][1])
        self.responses["sendMessage"] = [ok({"message_id": 2, "chat": {"id": USER_ID}}), httpx.ReadTimeout(TOKEN)]
        result = await adapter.deliver(delivery)
        self.assertEqual(result.status, DeliveryStatus.UNKNOWN)
        self.assertNotIn(TOKEN, result.reason)
        self.assertEqual(result.receipts[0].references, (("id", "123:2"),))
        self.assertEqual(await adapter.deliver(delivery), result)
        self.assertEqual(len(self.requests), 4)

    async def test_multipart_and_cross_chat_reference(self):
        adapter = self.adapter()
        self.responses["sendPhoto"] = [ok({"message_id": 4, "chat": {"id": USER_ID}})]
        env = DeliveryEnvelope("key", "private", str(USER_ID), images=[{"data": b"png"}],
                               quote=NativeQuote((("id", "-100100:42"),)))
        self.assertTrue((await adapter.deliver(env)).confirmed)
        method, body = self.requests[-1]
        self.assertEqual(method, "sendPhoto")
        self.assertIn(b'name="photo"', body)
        self.assertNotIn(b"reply_parameters", body)
        env.delivery_id = "new"
        env.quote = NativeQuote((("id", "123:42"),))
        self.responses["sendPhoto"] = [ok({"message_id": 5, "chat": {"id": USER_ID}})]
        self.assertTrue((await adapter.deliver(env)).confirmed)
        self.assertIn(b'"message_id": 42', self.requests[-1][1])

    async def test_native_reaction_sets_or_clears_without_sending_messages(self):
        adapter = self.adapter()
        self.responses["setMessageReaction"] = [ok(True), ok(True)]
        for delivery_id, reactions in (("add", ("👍",)), ("clear", ())):
            env = ReactionEnvelope(delivery_id, "private", str(USER_ID), ("id", "123:42"), reactions)
            result = await adapter.deliver_reaction(env)
            self.assertTrue(result.confirmed)
            self.assertEqual(result.receipts, [])
        self.assertEqual(self.requests, [
            ("setMessageReaction", {"chat_id": USER_ID, "message_id": 42,
                                    "reaction": [{"type": "emoji", "emoji": "👍"}]}),
            ("setMessageReaction", {"chat_id": USER_ID, "message_id": 42, "reaction": []}),
        ])

    async def test_unsupported_emoji_falls_back_according_to_reaction_quote(self):
        # Default (reaction_quote=False): send plain emoji without quoting original message.
        adapter = self.adapter()
        env = ReactionEnvelope("fallback-unquoted", "private", str(USER_ID), ("id", "123:42"),
                               ("😮",), added=("😮",))
        self.assertTrue((await adapter.deliver_reaction(env)).confirmed)
        self.assertEqual(self.requests[-1], ("sendMessage", {
            "chat_id": USER_ID, "text": "😮",
        }))
        self.assertNotIn("setMessageReaction", [method for method, _ in self.requests])

        # Opt-in (reaction_quote=True): quote original message when falling back.
        quoted_adapter = self.adapter(reaction_quote=True)
        env_q = ReactionEnvelope("fallback-quoted", "private", str(USER_ID), ("id", "123:42"),
                                 ("😮",), added=("😮",))
        self.assertTrue((await quoted_adapter.deliver_reaction(env_q)).confirmed)
        self.assertEqual(self.requests[-1], ("sendMessage", {
            "chat_id": USER_ID, "text": "😮",
            "reply_parameters": {"message_id": 42, "allow_sending_without_reply": True},
        }))
        self.assertNotIn("setMessageReaction", [method for method, _ in self.requests])

        # Native reactions remain unaffected regardless of reaction_quote.
        self.responses["setMessageReaction"] = [ok(True)]
        native_env = ReactionEnvelope("native-quoted", "private", str(USER_ID), ("id", "123:42"), ("👍",))
        self.assertTrue((await quoted_adapter.deliver_reaction(native_env)).confirmed)
        self.assertEqual(self.requests[-1], ("setMessageReaction", {
            "chat_id": USER_ID, "message_id": 42,
            "reaction": [{"type": "emoji", "emoji": "👍"}],
        }))

    async def test_supported_emoji_forms_use_native_reaction(self):
        adapter = self.adapter()
        self.responses["setMessageReaction"] = [ok(True), ok(True), ok(True)]
        for emoji in ("❤", "❤️", "❤️‍🔥"):
            env = ReactionEnvelope(f"native-{len(emoji)}", "private", str(USER_ID), ("id", "123:42"), (emoji,))
            self.assertTrue((await adapter.deliver_reaction(env)).confirmed)
        self.assertEqual([p["reaction"] for m, p in self.requests if m == "setMessageReaction"],
                         [[{"type": "emoji", "emoji": "❤"}],
                          [{"type": "emoji", "emoji": "❤️"}],
                          [{"type": "emoji", "emoji": "❤️‍🔥"}]])

    async def test_clearing_reaction_stays_native_even_for_empty_state(self):
        adapter = self.adapter()
        self.responses["setMessageReaction"] = [ok(True)]
        env = ReactionEnvelope("clear", "private", str(USER_ID), ("id", "123:42"), ())
        self.assertTrue((await adapter.deliver_reaction(env)).confirmed)
        self.assertEqual(self.requests[-1], ("setMessageReaction", {
            "chat_id": USER_ID, "message_id": 42, "reaction": [],
        }))

    async def test_native_reaction_rejects_invalid_targets_or_states(self):
        adapter = self.adapter()
        for ref, reactions in (("-100100:42", ("👍",)), ("123:0", ("👍",)),
                               ("123:42", ("👍", "❤️")), ("123:42", ("",))):
            env = ReactionEnvelope("bad", "private", str(USER_ID), ("id", ref), reactions)
            self.assertEqual((await adapter.deliver_reaction(env)).status, DeliveryStatus.REJECTED)
        self.assertEqual(self.requests, [])

    async def test_native_reaction_failure_never_falls_back_or_retries_unknown(self):
        adapter = self.adapter()
        self.responses["setMessageReaction"] = [reject(400), ok(False), httpx.ReadTimeout(TOKEN)]
        for key, status in (("reject", DeliveryStatus.REJECTED), ("false", DeliveryStatus.UNKNOWN),
                            ("timeout", DeliveryStatus.UNKNOWN)):
            env = ReactionEnvelope(key, "private", str(USER_ID), ("id", "123:42"), ("👍",))
            result = await adapter.deliver_reaction(env)
            self.assertEqual(result.status, status)
            self.assertNotIn(TOKEN, result.reason)
            if status == DeliveryStatus.UNKNOWN:
                self.assertEqual(await adapter.deliver_reaction(env), result)
        self.assertEqual([method for method, _ in self.requests], ["setMessageReaction"] * 3)

    async def test_markdown_renders_muse_text_and_respects_plain_text(self):
        adapter = self.adapter()
        text = "**粗体** *斜体*\n\n- 列表 1\n- 列表 2\n\n`代码`\n\n> 引用"
        expected_plain = "粗体 斜体\n\n• 列表 1\n• 列表 2\n\n代码\n\n引用"
        expected_entities = [
            {"type": "bold", "offset": 0, "length": 2},
            {"type": "italic", "offset": 3, "length": 2},
            {"type": "code", "offset": 22, "length": 2},
            {"type": "blockquote", "offset": 26, "length": 2},
        ]
        env = DeliveryEnvelope("formatted", "private", str(USER_ID), text)
        self.assertTrue((await adapter.deliver(env)).confirmed)
        self.assertEqual(self.requests[-1], ("sendMessage", {
            "chat_id": USER_ID, "text": expected_plain, "entities": expected_entities,
        }))

        plain_env = DeliveryEnvelope("plain", "private", str(USER_ID), text, plain_text=True)
        self.assertTrue((await adapter.deliver(plain_env)).confirmed)
        self.assertEqual(self.requests[-1], ("sendMessage", {"chat_id": USER_ID, "text": text}))

        prefix = "【引用】*literal*\n"
        quote_env = DeliveryEnvelope("literal-quote", "private", str(USER_ID), text, quote_fallback=prefix)
        self.assertTrue((await adapter.deliver(quote_env)).confirmed)
        shift = utf16_len(prefix)
        shifted_entities = [{**e, "offset": e["offset"] + shift} for e in expected_entities]
        self.assertEqual(self.requests[-1], ("sendMessage", {
            "chat_id": USER_ID, "text": prefix + expected_plain, "entities": shifted_entities,
        }))

    async def test_markdown_entity_rejection_falls_back_only_for_current_segment(self):
        adapter = self.adapter(chunk_chars=8)
        env = DeliveryEnvelope("chunks", "private", str(USER_ID), "**first**!\n**second**!\n**third**?",
                               quote=NativeQuote((("id", "123:42"),)))
        self.responses["sendMessage"] = [
            ok({"message_id": 1, "chat": {"id": USER_ID}}),
            reject(400, description="Bad Request: can't parse entities: invalid entity"),
        ]
        result = await adapter.deliver(env)
        self.assertTrue(result.confirmed)
        self.assertEqual(len(result.receipts), 3)
        sends = [params for _, params in self.requests]
        self.assertEqual([params["text"] for params in sends], ["first!", "second!", "second!", "third?"])
        self.assertTrue("entities" in sends[0])
        self.assertTrue("entities" in sends[1])
        self.assertNotIn("entities", sends[2])
        self.assertTrue("entities" in sends[3])
        self.assertEqual(sends[0]["reply_parameters"]["message_id"], 42)
        self.assertTrue(all("reply_parameters" not in params for params in sends[1:]))

    async def test_markdown_other_failures_do_not_retry_as_plain_text(self):
        adapter = self.adapter()
        for key, failure, status in (
            ("bad-chat", reject(400, description="Bad Request: chat not found"), DeliveryStatus.REJECTED),
            ("rate-limit", reject(429), DeliveryStatus.REJECTED),
            ("ambiguous", httpx.ReadTimeout(TOKEN), DeliveryStatus.UNKNOWN),
        ):
            self.responses["sendMessage"] = [failure]
            before = len(self.requests)
            env = DeliveryEnvelope(key, "private", str(USER_ID), "**text**")
            self.assertEqual((await adapter.deliver(env)).status, status)
            self.assertEqual(len(self.requests), before + 1)

    async def test_markdown_truncation_notice_keeps_chunk_limits(self):
        adapter = self.adapter(chunk_chars=12, max_chunks=2)
        env = DeliveryEnvelope("short", "private", str(USER_ID), "**" + "a" * 80 + "**")
        self.assertTrue((await adapter.deliver(env)).confirmed)
        sends = [params for _, params in self.requests]
        self.assertEqual(len(sends), 2)
        self.assertTrue(all(len(params["text"]) <= 12 for params in sends))
        self.assertTrue(sends[-1]["text"].endswith("[内容已截断]"))
        self.assertEqual(sends[-1]["entities"], [{"type": "bold", "offset": 0, "length": 3}])

    async def test_ambiguous_api_error_does_not_expose_response_body(self):
        adapter = self.adapter()
        self.responses["getMe"] = [httpx.Response(503, json={"ok": False, "description": TOKEN})]
        with self.assertRaises(TelegramAPIError) as cm:
            await adapter.client.call("getMe")
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertFalse(cm.exception.rejected)

    async def test_bidirectional_reaction_gateway_roundtrip_and_no_echo(self):
        adapter, ctx, svc = await self.mounted()
        toggled = []

        def fake_toggle(mid, emoji):
            toggled.append((mid, emoji))
            if emoji == "👍" and toggled.count((mid, "👍")) == 1:
                return ["👍"]
            if emoji == "❤️":
                return ["❤️"]
            return []

        svc.toggle_reaction = MagicMock(side_effect=fake_toggle)
        self.updates = [update(1, 50, "hello")]
        await adapter._poller.poll_once()
        sid = ctx.session_mapper.current("private", str(USER_ID))
        self.assertTrue(sid)

        # User reacts on Telegram message 50 -> gateway toggles Muse and suppresses push echo.
        self.updates = [
            {"update_id": 2, "message_reaction": {
                "message_id": 50, "date": 1235,
                "chat": {"id": USER_ID, "type": "private"},
                "user": {"id": USER_ID, "is_bot": False},
                "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            }}
        ]
        await adapter._poller.poll_once()
        self.assertEqual(toggled, [("m1", "👍")])

        # Muse echoes back reactions.updated ["👍"] -> suppressed by gateway state.
        await ctx.outbound_pusher._scan_and_dispatch(
            [{"event": "reactions.updated", "seq": 10, "payload": {"message_id": "m1", "reactions": ["👍"]}}],
            sid,
        )
        self.assertFalse(any(m == "setMessageReaction" for m, _ in self.requests))

        # User switches 👍 -> ❤️ then clears on Telegram.
        self.updates = [
            {"update_id": 3, "message_reaction": {
                "message_id": 50, "date": 1236,
                "chat": {"id": USER_ID, "type": "private"},
                "user": {"id": USER_ID, "is_bot": False},
                "old_reaction": [{"type": "emoji", "emoji": "👍"}],
                "new_reaction": [{"type": "emoji", "emoji": "❤️"}],
            }},
            {"update_id": 4, "message_reaction": {
                "message_id": 50, "date": 1237,
                "chat": {"id": USER_ID, "type": "private"},
                "user": {"id": USER_ID, "is_bot": False},
                "old_reaction": [{"type": "emoji", "emoji": "❤️"}],
                "new_reaction": [],
            }},
        ]
        await adapter._poller.poll_once()
        self.assertEqual(toggled, [("m1", "👍"), ("m1", "👍"), ("m1", "❤️"), ("m1", "❤️")])

        # Muse sets native reaction on a delivered assistant message and clears it.
        ctx.outbound_pusher.reference_store.remember(sid, "private", str(USER_ID), "assistant-msg-9", [("id", "123:77")])
        self.responses["setMessageReaction"] = [ok(True), ok(True)]
        await ctx.outbound_pusher._scan_and_dispatch(
            [{"event": "reactions.updated", "seq": 11,
              "payload": {"message_id": "assistant-msg-9", "reactions": ["👍"]}}],
            sid,
        )
        await ctx.outbound_pusher._scan_and_dispatch(
            [{"event": "reactions.updated", "seq": 12,
              "payload": {"message_id": "assistant-msg-9", "reactions": []}}],
            sid,
        )
        reaction_calls = [p for m, p in self.requests if m == "setMessageReaction"]
        self.assertEqual(reaction_calls, [
            {"chat_id": USER_ID, "message_id": 77, "reaction": [{"type": "emoji", "emoji": "👍"}]},
            {"chat_id": USER_ID, "message_id": 77, "reaction": []},
        ])
        self.assertFalse(any(m == "sendMessage" for m, _ in self.requests))

    async def test_gateway_ignores_unauthorized_or_cross_session_reaction_and_handles_failure(self):
        adapter, ctx, svc = await self.mounted()
        svc.toggle_reaction = MagicMock(return_value=["👍"])
        self.updates = [update(1, 50, "hello")]
        await adapter._poller.poll_once()
        old_sid = ctx.session_mapper.current("private", str(USER_ID))

        # Unauthorized private chat 999 is ignored.
        self.updates = [
            {"update_id": 2, "message_reaction": {
                "message_id": 50, "date": 1235,
                "chat": {"id": 999, "type": "private"},
                "user": {"id": 999, "is_bot": False},
                "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            }}
        ]
        await adapter._poller.poll_once()
        svc.toggle_reaction.assert_not_called()

        # Reaction to message from an old session after /new is ignored.
        ctx.session_mapper.bind("private", str(USER_ID), "new-session")
        self.assertNotEqual(old_sid, "new-session")
        self.updates = [
            {"update_id": 3, "message_reaction": {
                "message_id": 50, "date": 1236,
                "chat": {"id": USER_ID, "type": "private"},
                "user": {"id": USER_ID, "is_bot": False},
                "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            }}
        ]
        await adapter._poller.poll_once()
        svc.toggle_reaction.assert_not_called()

        # Switch back; toggle_reaction error returns unknown and advances offset without replaying.
        ctx.session_mapper.bind("private", str(USER_ID), old_sid)
        svc.toggle_reaction.side_effect = RuntimeError("rpc timeout")
        self.updates = [
            {"update_id": 4, "message_reaction": {
                "message_id": 50, "date": 1237,
                "chat": {"id": USER_ID, "type": "private"},
                "user": {"id": USER_ID, "is_bot": False},
                "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            }}
        ]
        await adapter._poller.poll_once()
        self.assertEqual(svc.toggle_reaction.call_count, 1)
        self.assertEqual(adapter._poller._state["offset"], 5)
        await adapter._poller.poll_once()
        self.assertEqual(svc.toggle_reaction.call_count, 1)

    async def test_native_reaction_skips_missing_anchor_without_fallback_text(self):
        adapter, ctx, _ = await self.mounted()
        self.updates = [update(1, 50, "hello")]
        await adapter._poller.poll_once()
        sid = ctx.session_mapper.current("private", str(USER_ID))
        ctx.outbound_pusher._seen_ids.add("unseen-muse-mid")
        await ctx.outbound_pusher._scan_and_dispatch(
            [{"event": "reactions.updated", "seq": 10,
              "payload": {"message_id": "unseen-muse-mid", "reactions": ["👍"]}}],
            sid,
        )
        self.assertEqual(self.requests[-1][0], "getUpdates")
        self.assertEqual(ctx.outbound_pusher.reference_store.reaction_state(sid, "unseen-muse-mid"), (10, ["👍"]))


class MuseClientReactionTests(unittest.TestCase):
    def test_toggle_reaction_normalizes_response_and_aliases(self):
        from muse_bridge.muse.client.client import MuseClient

        client = MuseClient("tok", "notary", "vm", "192.0.2.1")
        client.request = MagicMock(side_effect=[
            json.dumps({"reactions": [{"emoji": "hatch:facebook_thumbs_up_v1"}]}).encode(),
            json.dumps({"result": {"reactions": ["❤️", "👍", "❤️"]}}).encode(),
        ])
        self.assertEqual(client.toggle_reaction("mid-1", "👍"), ["👍"])
        self.assertEqual(client.request.call_args_list[0].args[:2], ("POST", "/reactions/toggle"))
        self.assertEqual(json.loads(client.request.call_args_list[0].args[2]),
                         {"message_id": "mid-1", "emoji": "👍"})
        self.assertEqual(client.toggle_reaction("mid-1", "❤️"), ["❤️", "👍"])
