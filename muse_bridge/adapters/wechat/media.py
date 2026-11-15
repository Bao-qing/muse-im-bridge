"""Bounded CDN media transfer and the iLink AES-128-ECB wire format."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
from collections.abc import Mapping
from urllib.parse import urlencode

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .client import WeChatClient
from .security import DEFAULT_CDN, WeChatSecurityError, validate_url

# A local resource guard, not a claim about the server's upload-size policy.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_HEX_KEY = re.compile(rb"[0-9a-fA-F]{32}\Z")


def parse_key(value: str | bytes) -> bytes:
    """Accept raw 16-byte keys, hex, base64(raw), or base64(ASCII hex)."""
    try:
        if isinstance(value, bytes) and len(value) == 16:
            return value
        raw = value.encode("ascii") if isinstance(value, str) else value
        if not isinstance(raw, bytes):
            raise ValueError
        if _HEX_KEY.fullmatch(raw):
            return bytes.fromhex(raw.decode("ascii"))
        decoded = base64.b64decode(raw, validate=True)
        if len(decoded) == 16:
            return decoded
        if _HEX_KEY.fullmatch(decoded):
            return bytes.fromhex(decoded.decode("ascii"))
    except (ValueError, UnicodeError, binascii.Error):
        pass
    raise WeChatSecurityError("Invalid WeChat AES-128 key")


def encrypt(data: bytes, key: str | bytes) -> bytes:
    """AES-128-ECB with PKCS7 padding, including an extra full aligned block."""
    if not isinstance(data, bytes):
        raise WeChatSecurityError("Invalid WeChat plaintext bytes")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    cipher = Cipher(algorithms.AES(parse_key(key)), modes.ECB()).encryptor()
    return cipher.update(padded) + cipher.finalize()


def decrypt(data: bytes, key: str | bytes) -> bytes:
    """Reject truncated or incorrectly padded ciphertext without echoing it."""
    if not isinstance(data, bytes) or not data or len(data) % 16:
        raise WeChatSecurityError("Invalid WeChat AES ciphertext size")
    parsed = parse_key(key)
    try:
        cipher = Cipher(algorithms.AES(parsed), modes.ECB()).decryptor()
        padded = cipher.update(data) + cipher.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise WeChatSecurityError("Invalid WeChat AES padding") from None


class WeChatMedia:
    def __init__(self, client: WeChatClient) -> None:
        self.client = client

    async def download(self, descriptor: Mapping, max_bytes: int, *, image: bool = False) -> bytes:
        """Download an atomic image_item/voice_item/file_item/video_item dict.

        max_bytes limits final plaintext, and also bounds ciphertext overhead.
        Only images explicitly allow missing keys (plain CDN media).
        """
        if type(max_bytes) is not int or max_bytes < 0 or not isinstance(descriptor, Mapping):
            raise WeChatSecurityError("Invalid WeChat media download arguments")
        media = descriptor.get("media", {})
        if not isinstance(media, Mapping):
            raise WeChatSecurityError("Invalid WeChat media descriptor")
        image_key = descriptor.get("aeskey") if image else None
        value = image_key if image_key not in (None, "") else media.get("aes_key")
        key = parse_key(value) if value not in (None, "") else None
        if key is None and not image:
            raise WeChatSecurityError("WeChat encrypted media omitted AES key")
        url = media.get("full_url") or descriptor.get("full_url") or (descriptor.get("url") if image else None)
        if not url:
            param = media.get("encrypt_query_param")
            if not isinstance(param, str) or not param:
                raise WeChatSecurityError("WeChat media omitted CDN reference")
            url = DEFAULT_CDN + "/download?" + urlencode({"encrypted_query_param": param})
        validate_url(url, cdn=True)
        wire_limit = (max_bytes // 16 + 1) * 16 if key is not None else max_bytes
        data = await self.client.cdn_download(url, wire_limit)
        plain = decrypt(data, key) if key is not None else data
        if len(plain) > max_bytes:
            raise WeChatSecurityError("WeChat media exceeds plaintext byte limit")
        return plain

    async def upload(self, data: bytes, user_id: str, kind: str, filename: str = "") -> dict:
        """Return an image/file/video MessageItem; sending remains caller-owned."""
        if not isinstance(data, bytes) or len(data) > MAX_UPLOAD_BYTES:
            raise WeChatSecurityError("WeChat upload exceeds byte limit or is not bytes")
        if not isinstance(kind, str) or kind not in {"image", "file", "video"}:
            raise WeChatSecurityError("Unsupported WeChat upload kind")
        if not isinstance(filename, str) or not isinstance(user_id, str) or not user_id:
            raise WeChatSecurityError("Invalid WeChat upload arguments")
        key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
        encrypted = encrypt(data, key)
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        response = await self.client.get_upload_url({
            "filekey": filekey, "media_type": {"image": 1, "video": 2, "file": 3}[kind],
            "to_user_id": user_id, "rawsize": len(data), "rawfilemd5": md5,
            "filesize": len(encrypted), "no_need_thumb": True, "aeskey": key.hex(),
        })
        url = response.get("upload_full_url")
        if not url:
            param = response.get("upload_param")
            if not isinstance(param, str) or not param:
                raise WeChatSecurityError("WeChat upload omitted signed CDN reference")
            url = DEFAULT_CDN + "/upload?" + urlencode({
                "encrypted_query_param": param, "filekey": filekey,
            })
        validate_url(url, cdn=True)
        param = await self.client.cdn_upload(url, encrypted)
        media = {
            "encrypt_query_param": param,
            "aes_key": base64.b64encode(key.hex().encode("ascii")).decode("ascii"),
            "encrypt_type": 1,
        }
        if kind == "image":
            return {"type": 2, "image_item": {"media": media, "mid_size": len(encrypted)}}
        if kind == "video":
            return {"type": 5, "video_item": {
                "media": media, "video_size": len(encrypted), "video_md5": md5,
            }}
        return {"type": 4, "file_item": {
            "media": media, "file_name": filename or "file", "len": str(len(data)), "md5": md5,
        }}
