"""Source-derived feed fixtures: all transport calls are offline mocks."""
import base64
import json
import unittest
from unittest.mock import MagicMock, Mock
from urllib.parse import parse_qs, urlsplit

from muse_bridge.muse.feed import FeedPost, fetch_feed, parse_feed, resolve_feed_images
from muse_bridge.muse.service import MuseService


def unit(key="u1", **kwargs):
    return {"unit_id": key, "created_at_ms": 1000000,
            "title": "标题", "body_md": "正文", **kwargs}


def page(*units, cursor=None, prompt="p1"):
    return {"prompt_id": prompt, "days": [{"local_date": "2026-11-09",
            "editions": [{"generated_at_ms": 1000000, "units": list(units)}]}],
            "pagination": {"next_cursor": cursor, "prev_cursor": None}}


class TestMuseFeed(unittest.TestCase):
    def test_title_body_and_timestamp(self):
        self.assertEqual(parse_feed(page(unit())), [FeedPost("u1", 1000, "标题\n\n正文", [])])

    def test_media_shapes_dedup_and_image_only(self):
        cases = [
            ({"kind": "hero_image", "hero_image_url": "https://cdn.example/a.png"},
             ["https://cdn.example/a.png"]),
            ({"kind": "carousel", "image_urls": ["https://cdn.example/a.png"] * 2},
             ["https://cdn.example/a.png"]),
            ({"kind": "social_embed", "social_media_items": [
                {"kind": "image", "media_url": "https://cdn.example/a.png"},
                {"kind": "video", "media_url": "https://cdn.example/a.mp4"}]},
             ["https://cdn.example/a.png"]),
            ({"kind": "social_embed", "social_thumbnail_url": "https://cdn.example/t.jpg"},
             ["https://cdn.example/t.jpg"]),
            ({"kind": "carousel", "image_urls": ["https://cdn.example/a.mp4"],
              "media_items": [{"kind": "video", "url": "https://cdn.example/a.mp4",
                               "poster_url": "https://cdn.example/p.jpg"}]},
             ["https://cdn.example/p.jpg"]),
            ({"kind": "widget", "widget_html": '<img src="file:///secret">'}, []),
        ]
        for attachment, expected in cases:
            with self.subTest(attachment=attachment):
                posts = parse_feed(page(unit(title="", body_md="", attachment=attachment)))
                self.assertEqual(posts[0].images if posts else [], expected)
                if expected:
                    self.assertEqual(posts[0].text, "【Muse 动态】")

    def test_transport_envelope_is_unwrapped_and_rejection_fails(self):
        wrapped = {"ok": True, "result": page(unit())}
        self.assertEqual([p.id for p in parse_feed(wrapped)], ["u1"])
        with self.assertRaises(ValueError):
            parse_feed({"ok": False, "result": page(unit())})
        client = self.client(wrapped)
        self.assertEqual([p.id for p in fetch_feed(client, 5).posts], ["u1"])

    def test_unknown_timestamps_and_shapes_are_not_publishable(self):
        for value in (0, -1, True, None, "1000", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(parse_feed(page(unit(created_at_ms=value))), [])
        with self.assertRaises(ValueError):
            parse_feed({"events": []})

    def test_unsafe_sources_are_not_images(self):
        bad = ["file:///secret", "base64://data", "javascript:alert(1)",
               "http://127.0.0.1/a.png", "http://localhost/a.png", "http://10.0.0.1/a.png",
               "https://user:secret@example.com/a.png", "//example.com/a.png", {}, None]
        posts = parse_feed(page(unit(attachment={"kind": "carousel", "image_urls": bad})))
        self.assertEqual(posts[0].images, [])

    def test_relative_authenticated_media_is_preserved(self):
        source = "/api/feed/units/u1/media/abc?size=large"
        posts = parse_feed(page(unit(attachment={"kind": "hero_image", "hero_image_url": source})))
        self.assertEqual(posts[0].images, [source])

    def client(self, *pages):
        client = Mock(host="hatch.metaaivm.com", vm_id="vm1", timezone="Asia/Shanghai")
        client.request.side_effect = [json.dumps(p).encode() for p in pages]
        return client

    def test_bounded_pagination_prompt_and_cursor_encoding(self):
        client = self.client(page(unit(), cursor="a +&"), page(unit("u2", created_at_ms=2000000)))
        result = fetch_feed(client, 2)
        self.assertEqual([p.id for p in result.posts], ["u2", "u1"])
        self.assertEqual(result.source_id, "hatch.metaaivm.com:vm1:p1")
        method, path = client.request.call_args_list[1].args
        self.assertEqual(method, "GET")
        self.assertEqual(urlsplit(path).path, "/api/feed")
        self.assertEqual(parse_qs(urlsplit(path).query), {
            "timezone": ["Asia/Shanghai"], "cursor": ["a +&"], "prompt_id": ["p1"]})
        self.assertEqual(client.request.call_args.kwargs,
                         {"include_content_type": False, "service": "daemon"})

    def test_page_limit_duplicate_ids_and_no_generation(self):
        client = self.client(page(unit(), unit(), unit("u2"), cursor="next"))
        self.assertEqual(len(fetch_feed(client, 1).posts), 1)
        client.request.assert_called_once()
        self.assertNotIn("infinite", client.request.call_args.args[1])
        for bad in (0, 101, True, "1"):
            with self.assertRaises(ValueError):
                fetch_feed(client, bad)

    def test_repeated_cursor_and_prompt_changes_fail(self):
        for pages in ((page(cursor="repeat"), page(cursor="repeat")),
                      (page(cursor="next"), page(prompt="other"))):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                fetch_feed(self.client(*pages), 50)

    def test_absolute_and_relative_private_media_become_base64(self):
        for source in ("/api/feed/units/u1/media/opaque?size=full",
                       "https://muse.ai/feed/units/u1/media/opaque?size=full"):
            with self.subTest(source=source):
                client = Mock()
                raw = b"\x89PNG\r\n\x1a\nfixture"
                client.request.return_value = raw
                result = resolve_feed_images(client, FeedPost("u1", 1, "t", [source]), 100)
                self.assertEqual(result, ["base64://" + base64.b64encode(raw).decode()])
                client.request.assert_called_once_with(
                    "GET", "/api/feed/units/u1/media/opaque?size=full",
                    include_content_type=False, service="daemon")

    def test_media_size_type_and_cross_unit_rejected(self):
        for source, response, cap in (("/feed/units/u1/media/a", b"<html>error</html>", 100),
                                      ("/feed/units/u1/media/a", b"\x89PNG\r\n\x1a\nlarge", 5),
                                      ("/feed/units/u2/media/a", b"", 100),
                                      ("/feed/units/u1/media/../secret", b"", 100)):
            with self.subTest(source=source, cap=cap), self.assertRaises(ValueError):
                client = Mock()
                client.request.return_value = response
                resolve_feed_images(client, FeedPost("u1", 1, "t", [source]), cap)

    def test_public_images_do_not_use_authenticated_transport(self):
        client = Mock()
        source = "https://cdn.example/image.jpg"
        self.assertEqual(resolve_feed_images(client, FeedPost("u1", 1, "t", [source]), 100), [source])
        client.request.assert_not_called()

    def test_service_uses_fresh_client_and_closes_on_failure(self):
        service = MuseService.__new__(MuseService)
        client = MagicMock()
        client.__enter__.return_value = client
        client.host, client.vm_id, client.timezone = "host", "vm", "UTC"
        service.create_client = Mock(return_value=client)
        client.request.return_value = json.dumps(page(unit())).encode()
        self.assertEqual(len(service.list_feed(1).posts), 1)
        client.connect.assert_called_once()
        client.__exit__.assert_called_once()
        client.request.side_effect = TimeoutError("unavailable")
        with self.assertRaises(TimeoutError):
            service.list_feed(1)
        self.assertEqual(client.__exit__.call_count, 2)


if __name__ == "__main__":
    unittest.main()
