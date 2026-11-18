"""Approval choices remain bound across equivalent push/list/detail records."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from muse_bridge.config.models import MuseConfig
from muse_bridge.muse.interactions import MuseInteractions
from muse_bridge.muse.service import MuseService


BASE_PURPOSE = "助手想要连接到测试服务器。"
SOURCE_NOTE = '. 此任务来自旁聊“⁨测试会话⁩”。'


def snapshot():
    return {
        "approval_id": "test-approval", "status": "pending", "scope": {"thread_id": "s1"},
        "display": {"attention_mode": "foreground", "summary_title": "测试 SSH 连接",
                    "short_explanation": BASE_PURPOSE, "purpose_summary": BASE_PURPOSE,
                    "detail_rows": [{"label": "主机", "value": "192.0.2.42"}]},
        "payload": {"type": "network", "network_payload": {
            "host": "192.0.2.42", "port": 22, "scheme": "tcp", "method": "CONNECT",
            "path": "192.0.2.42:22", "policy_subject": "tool.exec"}},
        "decision_options": [
            {"kind": "allow_once", "label": {"kind": "allow_once"}, "label_text": "允许一次"},
            {"kind": "allow_always", "label": {"kind": "allow_always_scoped", "scope": "destination"},
             "label_text": "总是允许此站点"},
            {"kind": "deny", "label": {"kind": "deny_for_this_task"}, "label_text": "拒绝"}],
    }


class FingerprintTests(unittest.TestCase):
    def test_only_recognized_source_annotation_is_equivalent(self):
        base = snapshot()
        annotated = copy.deepcopy(base)
        annotated["display"]["purpose_summary"] += SOURCE_NOTE
        self.assertEqual(MuseInteractions.approval_fingerprint(base),
                         MuseInteractions.approval_fingerprint(annotated))
        for suffix in (" 还要上传文件。", ". 此任务来自旁聊“测试会话”。另外上传文件。",
                       '. 此任务来自旁聊“测试会话\n还要上传文件”。'):
            with self.subTest(suffix=suffix):
                changed = copy.deepcopy(base)
                changed["display"]["purpose_summary"] += suffix
                self.assertNotEqual(MuseInteractions.approval_fingerprint(base),
                                    MuseInteractions.approval_fingerprint(changed))

    def test_source_note_without_matching_base_explanation_is_not_stripped(self):
        base = snapshot()
        annotated = copy.deepcopy(base)
        annotated["display"]["purpose_summary"] += SOURCE_NOTE
        annotated["display"]["short_explanation"] = "另一项操作"
        self.assertNotEqual(MuseInteractions.approval_fingerprint(base),
                            MuseInteractions.approval_fingerprint(annotated))

    def test_missing_revision_matches_initial_zero_but_not_positive_revision(self):
        base = snapshot()
        zero = {**base, "message_revision": 0}
        self.assertEqual(MuseInteractions.approval_fingerprint(base),
                         MuseInteractions.approval_fingerprint(zero))
        self.assertNotEqual(MuseInteractions.approval_fingerprint(base),
                            MuseInteractions.approval_fingerprint({**base, "message_revision": 1}))


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = MuseService(MuseConfig(session_id="s1", cookies_file=str(
            Path(tmp.name) / "cookies.json")), MagicMock())
        self.addCleanup(self.service.interactions.close)
        self.p = self.service.interaction_protocol
        self.generation = self.p.begin_approval_stream()
        self.list_row = snapshot()
        self.push_row = copy.deepcopy(self.list_row)
        self.push_row["display"]["purpose_summary"] += SOURCE_NOTE
        self.detail = {"approval_id": "test-approval", "status": "pending", "message_revision": 0,
                       "decision_options": ["allow_once", "allow_session", "allow_today", "allow_always", "deny"]}
        self.accept(self.push_row)
        self.item = self.p.manager.by_remote("approval", "test-approval")
        self.shown = self.item.context["fingerprint"]
        self.item.context["notified"] = True
        self.posts = []
        def request(method, path, body=None, **kwargs):
            if path == "/approvals":
                return {"pending_approvals": [copy.deepcopy(self.list_row)], "recent_approvals": []}
            if method == "POST":
                self.posts.append(copy.deepcopy(body))
                return {**copy.deepcopy(self.detail), "status": "approved"}
            return copy.deepcopy(self.detail)
        self.p.request = MagicMock(side_effect=request)

    def accept(self, row, seq=1):
        self.assertTrue(self.p.accept_approval_event({"event": "approvals.snapshot", "seq": seq,
            "payload": {"schema_version": 1, "emitted_at_ms": seq,
                        "pending_approvals": [row], "recent_approvals": []}}, self.generation))

    async def assert_changed_without_post(self):
        result = await self.p.submit_approval(self.item, "1", expected_fingerprint=self.shown)
        self.assertIn("审批内容已变化", result)
        self.assertEqual(self.posts, [])
        self.assertFalse(self.item.terminal)

    async def test_first_choice_with_real_trace_shapes_posts_once(self):
        result = await self.p.submit_approval(self.item, "1", expected_fingerprint=self.shown)
        self.assertNotIn("审批内容已变化", result)
        self.assertEqual(self.item.state, "approved")
        self.assertEqual(self.posts, [{"decision": "allow_once", "notification_channel": "ui",
                                       "expected_message_revision": 0}])
        self.assertEqual(self.item.context["options"][1]["scope"], "destination")
        self.assertIn(SOURCE_NOTE, self.p.format_approval(self.item))

    def test_repeated_fetches_preserve_shown_fingerprint_and_notification(self):
        for _ in range(2):
            self.p.fetch_approvals()
            self.assertEqual(self.item.context["fingerprint"], self.shown)
            self.assertTrue(self.item.context["notified"])
            self.assertIn(SOURCE_NOTE, self.p.format_approval(self.item))

    def test_push_after_detail_does_not_discard_known_revision_or_flicker(self):
        self.detail["message_revision"] = 3
        self.p.fetch_approvals()
        fingerprint = self.item.context["fingerprint"]
        self.item.context["notified"] = True
        self.accept(self.push_row, seq=2)
        self.assertEqual(self.item.context["approval"]["message_revision"], 3)
        self.assertEqual(self.item.context["fingerprint"], fingerprint)
        self.assertTrue(self.item.context["notified"])

    async def test_new_positive_revision_requires_new_choice(self):
        self.detail["message_revision"] = 1
        await self.assert_changed_without_post()

    async def test_known_revision_increment_still_requires_new_choice(self):
        self.detail["message_revision"] = 3
        self.p.fetch_approvals()
        self.shown = self.item.context["fingerprint"]
        self.detail["message_revision"] = 4
        await self.assert_changed_without_post()

    async def test_changed_request_target_is_not_hidden_by_same_display(self):
        self.list_row["payload"]["network_payload"]["host"] = "192.0.2.43"
        await self.assert_changed_without_post()

    async def test_changed_scope_requires_new_choice(self):
        self.list_row["decision_options"][1]["label"]["scope"] = "destination_domain"
        await self.assert_changed_without_post()

    async def test_changed_option_order_requires_new_choice(self):
        self.list_row["decision_options"].reverse()
        await self.assert_changed_without_post()

    async def test_changed_preview_document_requires_new_choice(self):
        self.list_row["display"]["request_preview_document"] = {
            "blocks": [{"type": "text", "text": "改为执行其他命令"}]}
        await self.assert_changed_without_post()

    async def test_changed_actual_explanation_requires_new_choice(self):
        self.list_row["display"]["purpose_summary"] = "连接后还将上传文件。"
        self.list_row["display"]["short_explanation"] = "连接后还将上传文件。"
        await self.assert_changed_without_post()

    async def test_arbitrary_purpose_change_is_not_treated_as_source_annotation(self):
        self.list_row["display"]["purpose_summary"] += " 同时上传文件。"
        await self.assert_changed_without_post()


if __name__ == "__main__":
    unittest.main()
