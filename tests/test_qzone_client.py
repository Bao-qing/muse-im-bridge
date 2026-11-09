"""Offline contract tests for publishing Qzone messages."""

import unittest
from unittest import mock

from muse_bridge.adapters.onebot.client import OneBotAPIError, OneBotClient


class TestQzoneClient(unittest.TestCase):
    def setUp(self):
        self.client = OneBotClient("https://onebot.test", "token")
        self.data = {"tid": "post-123", "time": 1700000000}

    def test_text_only_uses_endpoint_and_returns_data(self):
        envelope = {"status": "ok", "retcode": 0, "data": self.data}
        with mock.patch.object(self.client, "_post", return_value=envelope) as post:
            result = self.client.send_qzone_msg(" hello 世界 ")
        post.assert_called_once_with(
            "/send_qzone_msg", {"content": " hello 世界 "}, timeout=30,
        )
        self.assertIs(result, self.data)

    def test_image_sources_and_custom_timeout(self):
        images = ["file:///tmp/image.png", "http://example.test/a.png",
                  "https://example.test/b.png", "base64://aGVsbG8="]
        with mock.patch.object(self.client, "call", return_value=self.data) as call:
            self.client.send_qzone_msg("photos", images=images, timeout=90)
        call.assert_called_once_with("send_qzone_msg", timeout=90, content="photos", images=images)

    def test_all_documented_permissions(self):
        for permission in (1, 4, 16, 64, 128):
            with self.subTest(permission=permission):
                with mock.patch.object(self.client, "call", return_value=self.data) as call:
                    self.client.send_qzone_msg("text", ugc_right=permission, target_uins=[123, 456])
                call.assert_called_once_with(
                    "send_qzone_msg", timeout=30, content="text",
                    ugc_right=permission, target_uins=[123, 456],
                )

    def test_explicit_empty_optional_lists_are_preserved(self):
        with mock.patch.object(self.client, "call", return_value=self.data) as call:
            self.client.send_qzone_msg("text", images=[], target_uins=[])
        call.assert_called_once_with(
            "send_qzone_msg", timeout=30, content="text", images=[], target_uins=[],
        )

    def test_rejected_envelopes_raise(self):
        envelopes = [
            {"status": "failed", "retcode": 1200, "message": "denied"},
            {"status": "failed", "retcode": 0, "data": self.data},
            {"status": "ok", "retcode": 1, "data": self.data},
            {"status": "ok", "data": self.data},
        ]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                with mock.patch.object(self.client, "_post", return_value=envelope):
                    with self.assertRaises(OneBotAPIError):
                        self.client.send_qzone_msg("text")

    def test_invalid_arguments_never_call_api(self):
        invalid = [
            {"content": value} for value in (None, "", " \n", 123, b"text")
        ]
        invalid += [{"images": value} for value in
                    ("http://example.test/a", ("image",), [""], ["  "], [None], [b"image"], [123])]
        invalid += [{"ugc_right": value} for value in (0, 2, 5, -1, True, 1.0, "1", [])]
        invalid += [{"target_uins": value} for value in
                    ("123", (123,), [0], [-1], [True], [1.5], ["123"], [None])]
        with mock.patch.object(self.client, "call") as call:
            for params in invalid:
                with self.subTest(params=params):
                    with self.assertRaises(ValueError):
                        self.client.send_qzone_msg(**{"content": "text", **params})
            call.assert_not_called()

    def test_malformed_success_data_raises(self):
        malformed = [None, [], "ok", {}, {"time": 123},
                     {"tid": "", "time": 123}, {"tid": "  ", "time": 123},
                     {"tid": 123, "time": 123}, {"tid": "post"},
                     {"tid": "post", "time": "123"}, {"tid": "post", "time": True},
                     {"tid": "post", "time": 1.5}, {"tid": "post", "time": None}]
        for data in malformed:
            with self.subTest(data=data):
                with mock.patch.object(self.client, "_post", return_value={
                    "status": "ok", "retcode": 0, "data": data,
                }):
                    with self.assertRaisesRegex(OneBotAPIError, "malformed success response"):
                        self.client.send_qzone_msg("text")


if __name__ == "__main__":
    unittest.main()
