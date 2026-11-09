"""Normalized feed records; wire parsing follows Muse's feed UI schema."""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import json
import ipaddress
import math
import re
from urllib.parse import quote, unquote, urlencode, urlsplit


_MEDIA_PATH = re.compile(r"/(?:api/)?feed/units/([^/]+)/media/(.+)")


def _image_url(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if (not url.scheme and not url.netloc and _MEDIA_PATH.fullmatch(url.path)):
            return value
        if url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password:
            if url.hostname.lower().rstrip(".") in {"localhost", "localhost.localdomain"}:
                return None
            try:
                if not ipaddress.ip_address(url.hostname).is_global:
                    return None
            except ValueError:
                pass
            return value
    except ValueError:
        pass
    return None


def _unwrap(data):
    """Strip the transport envelope ``{"ok": true, "result": {...}}`` if present."""
    if isinstance(data, dict) and "ok" in data and "result" in data:
        if data["ok"] is not True:
            raise ValueError("Muse feed request was not accepted")
        return data["result"]
    return data


def parse_feed(data):
    """Parse the source-verified days/editions/units feed.list response.

    No speculative fallback to chat events, widget HTML or synthetic stream units.
    Unknown media shapes are skipped rather than sent as file paths.
    """
    data = _unwrap(data)
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        raise ValueError("Malformed Muse feed response")
    posts = []
    for day in data["days"]:
        for edition in day.get("editions", []) if isinstance(day, dict) else []:
            for unit in edition.get("units", []) if isinstance(edition, dict) else []:
                if not isinstance(unit, dict):
                    continue
                uid, stamp = unit.get("unit_id"), unit.get("created_at_ms")
                if (not isinstance(uid, str) or not uid or type(stamp) not in (int, float)
                        or not math.isfinite(stamp) or stamp <= 0):
                    continue
                title = unit.get("title") if isinstance(unit.get("title"), str) else ""
                body = unit.get("body_md") if isinstance(unit.get("body_md"), str) else ""
                text = "\n\n".join(part.strip() for part in (title, body) if part.strip())
                attachment = unit.get("attachment")
                attachment = attachment if isinstance(attachment, dict) else {}
                kind = attachment.get("kind")
                images = []
                if kind == "hero_image":
                    images = [attachment.get("hero_image_url")]
                elif kind == "carousel":
                    images = attachment.get("image_urls", [])
                    media = attachment.get("media_items")
                    if isinstance(images, list) and isinstance(media, list) and len(media) == len(images):
                        images = [(item.get("poster_url") if item.get("kind") == "video"
                                   else item.get("url") or fallback)
                                  if isinstance(item, dict) else fallback
                                  for item, fallback in zip(media, images)]
                elif kind == "social_embed":
                    media = attachment.get("social_media_items")
                    if isinstance(media, list):
                        images = [item.get("media_url") for item in media
                                  if isinstance(item, dict) and item.get("kind") == "image"]
                    if not images:
                        images = [attachment.get("social_thumbnail_url")]
                if not isinstance(images, list):
                    images = []
                images = list(dict.fromkeys(url for image in images if (url := _image_url(image))))[:9]
                if not text and images:
                    text = "【Muse 动态】"
                if text:
                    posts.append(FeedPost(uid, stamp / 1000, text, images))
    return posts


@dataclass(frozen=True)
class FeedPost:
    id: str
    created_at: float
    text: str
    images: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FeedPage:
    source_id: str
    posts: list[FeedPost]


def fetch_feed(client, limit):
    """Read at most ten existing feed pages, never request generation."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("feed limit must be between 1 and 100")
    params = {"timezone": client.timezone}
    posts, seen_ids, seen_cursors = [], set(), set()
    prompt_id = None
    for _ in range(10):
        raw = client.request("GET", "/api/feed?" + urlencode(params),
                             include_content_type=False, service="daemon")
        data = _unwrap(json.loads(raw))
        parsed = parse_feed(data)
        for post in parsed:
            if post.id not in seen_ids:
                seen_ids.add(post.id)
                posts.append(post)
        if prompt_id is None:
            prompt_id = data.get("prompt_id")
            if prompt_id is not None and not isinstance(prompt_id, str):
                raise ValueError("Malformed feed prompt_id")
        elif data.get("prompt_id") != prompt_id:
            raise ValueError("Muse feed prompt changed during pagination")
        pagination = data.get("pagination")
        pagination = pagination if isinstance(pagination, dict) else {}
        cursor = pagination.get("next_cursor")
        if len(posts) >= limit or not cursor:
            break
        if not isinstance(cursor, str) or cursor in seen_cursors:
            raise ValueError("Invalid or repeated Muse feed cursor")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
        if prompt_id:
            params["prompt_id"] = prompt_id
    posts.sort(key=lambda post: (post.created_at, post.id), reverse=True)
    return FeedPage(f"{client.host}:{client.vm_id}:{prompt_id or ''}", posts[:limit])


def resolve_feed_images(client, post, max_bytes):
    """Resolve authenticated unit media locally; never give Noise URLs to OneBot.

    Direct HTTP(S) image sources stay URLs for the backend's upload helper.
    File paths, data URLs, widgets and video bodies are not forwarded.
    """
    images = []
    total = 0
    for source in post.images:
        if _image_url(source) is None:
            raise ValueError("Unsupported Muse feed image source")
        parsed = urlsplit(source)
        match = _MEDIA_PATH.fullmatch(parsed.path)
        if not match:
            images.append(source)
            continue
        unit_id, handle = (unquote(part) for part in match.groups())
        if unit_id != post.id or not handle or any(part in {".", ".."} for part in handle.split("/")):
            raise ValueError("Invalid Muse feed media path")
        path = "/api/feed/units/" + quote(unit_id, safe="") + "/media/" + quote(handle, safe="")
        if parsed.query:
            path += "?" + parsed.query
        raw = client.request("GET", path, include_content_type=False, service="daemon")
        total += len(raw)
        if len(raw) > max_bytes or total > max_bytes * 9:
            raise ValueError("Muse feed image exceeds configured image limit")
        # Media route returns bytes. Reject HTML/JSON error bodies masquerading as images.
        if not (raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM"))
                or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")):
            raise ValueError("Muse feed media did not return a supported image")
        images.append("base64://" + base64.b64encode(raw).decode("ascii"))
    return images
