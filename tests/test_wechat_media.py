"""Offline AES vectors, media shape, signed URL and byte-boundary tests."""
import base64
import hashlib
import json
import unittest
from unittest import mock

import httpx

from muse_bridge.adapters.wechat.client import WeChatClient, WeChatError
from muse_bridge.adapters.wechat.media import WeChatMedia, decrypt, encrypt, parse_key
from muse_bridge.adapters.wechat.security import DEFAULT_CDN, WeChatSecurityError

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")


class TestWeChatAES(unittest.TestCase):
    def test_known_aes128_ecb_vectors_with_pkcs7(self):
        plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
        encrypted = encrypt(plain, KEY)
        self.assertEqual(encrypted[:16].hex(), "3ad77bb40d7a3660a89ecaf32466ef97")
        self.assertEqual(len(encrypted), 32)
        self.assertEqual(decrypt(encrypted, KEY), plain)
        self.assertEqual(encrypt(b"", bytes(16)).hex(), "0143db63ee66b0cdff9f69917680151e")
        self.assertEqual(decrypt(encrypt(b"", KEY), KEY), b"")
        for size in (1, 15, 16, 17, 31, 32, 65):
            plain = bytes(range(size))
            self.assertEqual(len(encrypt(plain, KEY)), (size // 16 + 1) * 16)
            self.assertEqual(decrypt(encrypt(plain, KEY), KEY), plain)

    def test_raw_hex_and_both_base64_key_encodings(self):
        for value in (KEY, KEY.hex(), KEY.hex().upper(),
                      base64.b64encode(KEY).decode(), base64.b64encode(KEY.hex().encode()).decode()):
            self.assertEqual(parse_key(value), KEY)

    def test_invalid_keys_padding_and_truncation_are_sanitized(self):
        for value in (None, {}, "secret", "z" * 32, base64.b64encode(bytes(15)).decode(),
                      base64.b64encode(b"g" * 32).decode()):
            with self.subTest(value=value), self.assertRaises(WeChatSecurityError) as caught:
                parse_key(value)
            self.assertNotIn("secret", str(caught.exception))
        for value in (b"", b"short", bytes(16)):
            with self.assertRaises(WeChatSecurityError):
                decrypt(value, bytes(16))


class TestWeChatMedia(unittest.IsolatedAsyncioTestCase):
    def media(self, handler):
        self.client = WeChatClient(token="bot-secret", route_tag="private", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.client.aclose)
        return WeChatMedia(self.client)

    async def test_upload_each_kind_presigned_flow_and_enum_mapping(self):
        for kind, upload_type, item_type in (("image", 1, 2), ("video", 2, 5), ("file", 3, 4)):
            requests = []
            def handler(request):
                requests.append(request)
                if request.url.path.endswith("getuploadurl"):
                    return httpx.Response(200, json={"upload_param": "upload+secret/="})
                return httpx.Response(200, headers={"x-encrypted-param": "download-secret"})
            media = self.media(handler)
            with mock.patch("muse_bridge.adapters.wechat.media.secrets.token_bytes", return_value=KEY), \
                 mock.patch("muse_bridge.adapters.wechat.media.secrets.token_hex", return_value="a" * 32):
                item = await media.upload(b"hello world", "user", kind, filename="hello.txt")
            body = json.loads(requests[0].content)
            self.assertEqual(body["media_type"], upload_type)
            self.assertEqual(body["rawsize"], 11)
            self.assertEqual(body["filesize"], 16)
            self.assertEqual(body["rawfilemd5"], hashlib.md5(b"hello world").hexdigest())
            self.assertEqual(body["aeskey"], KEY.hex())
            self.assertTrue(body["no_need_thumb"])
            self.assertEqual(requests[1].url.params["encrypted_query_param"], "upload+secret/=")
            self.assertEqual(requests[1].url.params["filekey"], "a" * 32)
            self.assertEqual(decrypt(requests[1].content, KEY), b"hello world")
            self.assertEqual(requests[1].headers["content-type"], "application/octet-stream")
            for name in ("authorization", "authorizationtype", "x-wechat-uin", "ilink-app-id", "skrouteTag"):
                self.assertNotIn(name, requests[1].headers)
            self.assertEqual(item["type"], item_type)
            descriptor = item[kind + "_item"]
            reference = descriptor["media"]
            self.assertEqual(reference["encrypt_query_param"], "download-secret")
            self.assertEqual(base64.b64decode(reference["aes_key"]), KEY.hex().encode())
            self.assertEqual(reference["encrypt_type"], 1)
            if kind == "file":
                self.assertEqual(descriptor["file_name"], "hello.txt")
                self.assertEqual(descriptor["len"], "11")
            elif kind == "video":
                self.assertEqual(descriptor["video_size"], 16)
            else:
                self.assertEqual(descriptor["mid_size"], 16)

    async def test_full_upload_url_has_priority(self):
        requests = []
        url = DEFAULT_CDN + "/custom-upload?signature=secret"
        def handler(request):
            requests.append(request)
            if request.url.path.endswith("getuploadurl"):
                return httpx.Response(200, json={"upload_full_url": url, "upload_param": "ignored"})
            return httpx.Response(200, headers={"x-encrypted-param": "result"})
        await self.media(handler).upload(b"x", "user", "file")
        self.assertEqual(str(requests[1].url), url)

    async def test_encrypted_download_atom_and_both_encodings(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=encrypt(b"voice/video/file", KEY))
        media = self.media(handler)
        for value in (base64.b64encode(KEY).decode(), base64.b64encode(KEY.hex().encode()).decode()):
            result = await media.download({"media": {"aes_key": value, "encrypt_query_param": "q+/secret"}}, 16)
            self.assertEqual(result, b"voice/video/file")
        self.assertEqual(requests[0].url.params["encrypted_query_param"], "q+/secret")

    async def test_image_hex_key_precedes_media_key_and_full_url_precedes_param(self):
        requests = []
        url = DEFAULT_CDN + "/full?signature=secret"
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=encrypt(b"image", KEY))
        media = self.media(handler)
        result = await media.download({"aeskey": KEY.hex(), "media": {
            "aes_key": "invalid-ignored-key", "full_url": url, "encrypt_query_param": "ignored",
        }}, 5, image=True)
        self.assertEqual(result, b"image")
        self.assertEqual(str(requests[0].url), url)

    async def test_plain_image_without_key(self):
        media = self.media(lambda r: httpx.Response(200, content=b"image"))
        for descriptor in ({"media": {"encrypt_query_param": "q"}}, {"url": DEFAULT_CDN + "/plain"}):
            self.assertEqual(await media.download(descriptor, 5, image=True), b"image")
        with self.assertRaises(WeChatSecurityError):
            await media.download({"media": {"encrypt_query_param": "q"}}, 5)

    async def test_encrypted_plaintext_limit_and_wire_limit(self):
        descriptor = {"media": {"encrypt_query_param": "q", "aes_key": base64.b64encode(KEY).decode()}}
        for data, limit in ((b"123456", 5), (b"x" * 17, 3)):
            media = self.media(lambda r: httpx.Response(200, content=encrypt(data, KEY)))
            with self.assertRaises(WeChatSecurityError):
                await media.download(descriptor, limit)
        media = self.media(lambda r: httpx.Response(200, content=encrypt(b"", KEY)))
        self.assertEqual(await media.download(descriptor, 0), b"")

    async def test_bad_descriptors_limits_and_upload_inputs_fail_offline(self):
        handler = mock.Mock()
        media = self.media(handler)
        for descriptor, limit, image in (({}, 10, False), ({"media": []}, 10, False),
                                         ({}, -1, True), ({}, True, True),
                                         ({"aeskey": "secret", "url": DEFAULT_CDN + "/plain"}, 5, True)):
            with self.assertRaises(WeChatSecurityError):
                await media.download(descriptor, limit, image=image)
        for args in (("not-bytes", "user", "file"), (b"x", "user", "voice"), (b"x", "", "image")):
            with self.assertRaises(WeChatSecurityError):
                await media.upload(*args)
        with mock.patch("muse_bridge.adapters.wechat.media.MAX_UPLOAD_BYTES", 3):
            with self.assertRaises(WeChatSecurityError):
                await media.upload(b"1234", "user", "file")
        handler.assert_not_called()

    async def test_untrusted_full_urls_are_not_replaced_by_fallback(self):
        handler = mock.Mock()
        media = self.media(handler)
        for url in ("https://evil.qq.com/secret", "http://novac2c.cdn.weixin.qq.com/x",
                    "https://user:secret@novac2c.cdn.weixin.qq.com/x"):
            with self.assertRaises(WeChatSecurityError) as caught:
                await media.download({"media": {"full_url": url, "encrypt_query_param": "valid"}}, 5, image=True)
            self.assertNotIn("secret", repr(caught.exception))
        handler.assert_not_called()
        media = self.media(lambda r: httpx.Response(200, json={
            "upload_full_url": "https://evil.qq.com/secret", "upload_param": "fallback",
        }))
        with self.assertRaises(WeChatSecurityError):
            await media.upload(b"x", "user", "image")

    async def test_upload_requires_encrypted_param_and_download_rejects_bad_padding(self):
        media = self.media(lambda r: httpx.Response(200, json={"upload_param": "p"}))
        with self.assertRaises(WeChatError):
            await media.upload(b"x", "user", "file")
        media = self.media(lambda r: httpx.Response(200, content=bytes(16)))
        with self.assertRaises(WeChatSecurityError):
            await media.download({"media": {"encrypt_query_param": "p", "aes_key": base64.b64encode(bytes(16)).decode()}}, 100)


if __name__ == "__main__":
    unittest.main()
