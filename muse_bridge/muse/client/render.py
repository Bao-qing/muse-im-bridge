"""Render Muse history events to plain text for platforms without
widget rendering (QQ, WeChat, ...).

Two conversions a plain display_text pass-through gets wrong:
- message.assistant events can embed [[hatch_widget:<id>]] tokens. The
  widget's real content rides in payload.widgets (same event), whose
  fallback_text is the intended plain-text rendering. Substitute it;
  drop the token when no matching widget is present, never forward it.
- delta.presentation events (e.g. browser-task cards) are not
  message.assistant at all, so a first-message-only reader drops them.
  Render a browser-task card as one line from its status + title.

These are pure functions over history event dicts: no network, no
platform code, trivially unit-testable. Any adapter (QQ today, WeChat
later) uses them unchanged.
"""
import re


def render_widget_to_text(widget: dict) -> str:
    """Render a single widget (like option choice box) to clean readable plain text."""
    if not isinstance(widget, dict):
        return ""
    kind = widget.get("kind") or ""
    data = widget.get("data") or {}
    state_data = (widget.get("state") or {}).get("data") or {}

    # 1. Option Choice Box (选择框)
    if kind == "option" or "options" in data:
        options = data.get("options") or []
        if isinstance(options, list) and options:
            lines = ["【请选择】："]
            selected = state_data.get("selectedText")
            for idx, opt in enumerate(options, 1):
                opt_str = str(opt).strip()
                if selected and opt_str == str(selected).strip():
                    lines.append(f"{idx}. {opt_str} (已选)")
                else:
                    lines.append(f"{idx}. {opt_str}")
            return "\n".join(lines)
        fb = widget.get("fallback_text") or widget.get("display_text") or ""
        return fb.strip()

    # 2. General widget fallback
    fb = widget.get("fallback_text") or widget.get("display_text") or ""
    return fb.strip() if fb else ""


def render_presentation_card_to_text(event: dict):
    """Render non-media presentation cards (browser cards, secret/credentials vaults, etc.)
    into concise, user-friendly notification text for IM channels.
    """
    if not isinstance(event, dict):
        return None
    name = event.get("event_name") or event.get("event")
    if name != "delta.presentation":
        return None
    payload = event.get("payload") or {}
    kind = payload.get("kind") or (payload.get("data") or {}).get("type") or ""
    data = payload.get("data") or {}

    # 1. Browser Card (浏览器卡片)
    if kind == "browser_task":
        status = data.get("display_status") or data.get("display_state") or data.get("status") or "进行中"
        tab = data.get("latest_tab") or {}
        site = data.get("current_site") or {}
        title = tab.get("title") or site.get("page_title") or payload.get("fallback_text") or event.get("display_text") or "网页浏览"
        url = tab.get("url") or site.get("url") or ""
        if url:
            return f"【浏览器任务：{status}】{title} ({url})"
        return f"【浏览器任务：{status}】{title}"

    # 2. Secret / Credentials Input Box (秘钥框 / 安全存储凭据输入)
    if kind == "internal_link_action_card" or data.get("type") == "credentials_vault" or "credentials" in str(kind).lower():
        sub = data.get("subtitle") or ""
        title = data.get("title") or payload.get("reply_to_text") or "安全存储"
        inner_data = data.get("data") or {}
        user_lbl = inner_data.get("username_label")
        pwd_lbl = inner_data.get("password_label")
        hint_parts = []
        if user_lbl and pwd_lbl:
            hint_parts.append(f"需要输入{user_lbl}/{pwd_lbl}")
        elif pwd_lbl:
            hint_parts.append(f"需要输入{pwd_lbl}")
        hint_str = "，".join(hint_parts)
        if hint_str:
            hint_str = f"（{hint_str}，请前往网页端完成安全输入）"
        else:
            hint_str = "（需要输入秘钥/授权凭据，请前往网页端完成安全输入）"
        if sub:
            return f"【{title}提示】{sub}{hint_str}"
        return f"【{title}提示】{hint_str}"

    # 3. Image Options (图片选择框)
    if kind == "image_options":
        cands = data.get("candidates") or []
        if isinstance(cands, list) and cands:
            lines = ["【图片选项】："]
            for idx, c in enumerate(cands, 1):
                lbl = c.get("label") or f"选项 {idx}"
                lines.append(f"{idx}. {lbl}")
            return "\n".join(lines)
        fb = payload.get("fallback_text") or event.get("display_text") or ""
        return f"【图片选项】：\n{fb}" if fb else "【图片选项】"

    # 4. Avatar Share / Action Cards (头像分享/卡片)
    if kind == "avatar_share":
        title = data.get("title") or "新形象已生成"
        sub = data.get("subtitle") or ""
        fb = data.get("fallback_text") or payload.get("fallback_text") or ""
        desc = sub or fb or ""
        return f"【卡片提示】{title}：{desc}".strip("：")

    # 5. Generic Card Fallback (其他卡片作为文本发送提示)
    title = data.get("title") or payload.get("reply_to_text") or ""
    sub = data.get("subtitle") or ""
    fb = payload.get("fallback_text") or event.get("display_text") or ""
    body = sub or fb
    if title and body and title != body:
        return f"【卡片提示】{title}：{body}"
    if title or body:
        return f"【卡片提示】{title or body}"
    return None


def render_assistant_event(event: dict):
    """Render one history event to plain text, or None to skip it."""
    name = event.get("event_name")
    payload = event.get("payload") or {}
    if name == "message.assistant":
        if not event.get("display_text_ready", True):
            return None
        text = event.get("display_text") or ""
        widgets = {}
        for w in payload.get("widgets") or []:
            if isinstance(w, dict) and w.get("widget_id"):
                widgets[w["widget_id"]] = w

        def sub(m):
            w = widgets.get(m.group(1))
            if not w:
                return ""
            return render_widget_to_text(w)

        text = re.sub(r"\[\[hatch_widget:([^\]]+)\]\]", sub, text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text or None
    if name == "delta.presentation":
        return render_presentation_card_to_text(event)
    return None


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def extract_images(event: dict):
    """Extract image descriptors from one history event, if any.

    NOTE: Muse's image-attachment fields have NOT been captured from the
    web page yet (browser capture is pending user confirmation), so this
    parser only handles the common attachment shapes (payload/event
    attachments, images, media lists with url + content_type). If Muse
    uses different field names, this returns [] instead of crashing, and
    the parser must be updated after capture. It never downloads: the
    adapter decides whether to fetch a URL (QQ upload needs bytes).
    Returns a list of {url, filename, content_type} dicts, url present.
    """
    payload = event.get("payload") or {}
    found = []
    for container in (payload, event):
        for key in ("attachments", "images", "media"):
            entries = container.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                url = (entry.get("url") or entry.get("src")
                       or entry.get("download_url") or "")
                if not url:
                    continue
                ctype = (entry.get("content_type")
                         or entry.get("mime_type")
                         or entry.get("type") or "")
                filename = entry.get("filename") or entry.get("name") or ""
                is_image = (isinstance(ctype, str)
                            and ctype.startswith("image"))
                if not is_image and isinstance(url, str):
                    is_image = url.split("?", 1)[0].lower().endswith(
                        _IMAGE_EXTS)
                if not is_image and entry.get("type") == "image":
                    is_image = True
                if is_image:
                    found.append({"url": url, "filename": filename,
                                  "content_type": ctype})
    # De-duplicate by URL, preserving order.
    seen = set()
    out = []
    for img in found:
        if img["url"] not in seen:
            seen.add(img["url"])
            out.append(img)
    return out


def extract_files(event: dict):
    """Extract non-image file descriptors from one history event, if
    any. Parallel to extract_images, but for attachments whose
    content_type does not start with "image" (or whose entry type is
    "file"/"file_ref"). URL-bearing only: a file_ref part with a
    media-library path and no URL cannot be downloaded by the adapter,
    so it is skipped here, not listed with a path the adapter cannot
    fetch. Returns a list of {url, filename, content_type} dicts, url
    present, de-duplicated by URL."""
    payload = event.get("payload") or {}
    found = []
    for container in (payload, event):
        for key in ("attachments", "images", "media"):
            entries = container.get(key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                url = (entry.get("url") or entry.get("src")
                       or entry.get("download_url") or "")
                if not url:
                    continue
                ctype = (entry.get("content_type")
                         or entry.get("mime_type")
                         or entry.get("type") or "")
                filename = entry.get("filename") or entry.get("name") or ""
                is_image = (isinstance(ctype, str)
                            and ctype.startswith("image"))
                if not is_image and isinstance(url, str):
                    is_image = url.split("?", 1)[0].lower().endswith(
                        _IMAGE_EXTS)
                if not is_image and entry.get("type") == "image":
                    is_image = True
                if is_image:
                    continue
                if (isinstance(ctype, str) and ctype.startswith("image")):
                    continue
                found.append({"url": url, "filename": filename,
                              "content_type": ctype})
    seen = set()
    out = []
    for img in found:
        if img["url"] not in seen:
            seen.add(img["url"])
            out.append(img)
    return out


def collect_turn_events(events, user_index):
    """Render every assistant event after events[user_index], keyed by
    message_id -> (seq, text). Skips non-assistant roles and events that
    render to nothing. Ordering is by seq, not by list position."""
    rendered = {}
    for event in events[user_index + 1:]:
        if event.get("role") != "assistant":
            continue
        mid = event.get("message_id") or ""
        text = render_assistant_event(event)
        if text and mid:
            rendered[mid] = (event.get("seq") or 0, text)
    return rendered


_WS_MIME = {".webp": "image/webp", ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp"}


def extract_workspace_images(event: dict):
    """Image descriptors for Muse file-store images, which carry no
    URL at all: delta.presentation kind=image events whose payload
    has image_path (e.g. workspace/imagine_media/....webp). Before
    this, extract_images skipped them (URL-bearing only), so a
    generated image reached adapters only as sandbox:// markdown
    text — and QQ rendered that unloadable link as a bare [title]
    bracket, which is exactly what the 2026-10-26 08:43 Nahida
    turn showed in the group. Returns [{workspace_path, filename,
    content_type}]; the adapter reads bytes via client.read_file.
    """
    payload = event.get("payload") or {}
    if (event.get("event_name") != "delta.presentation"
            or payload.get("kind") != "image"):
        return []
    path = payload.get("image_path") or ""
    if not path:
        return []
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    title = event.get("display_text") or ""
    name = title or path.rsplit("/", 1)[-1]
    return [{"workspace_path": path, "filename": name,
             "content_type": _WS_MIME.get(ext, "image/png")}]


def collect_turn_images(events, user_index):
    """Parallel to collect_turn_events, keyed by message_id ->
    (seq, [image descriptors]). An event with images but no text still
    appears here, so an image-only Muse reply is not silently dropped
    even though collect_turn_events skips text-less events.
    URL images and file-store (workspace_path) images are merged,
    de-duplicated by their address."""
    images = {}
    for event in events[user_index + 1:]:
        if event.get("role") != "assistant":
            continue
        mid = event.get("message_id") or ""
        found = extract_images(event) + extract_workspace_images(event)
        if found and mid:
            seen = set()
            deduped = []
            for img in found:
                key = img.get("url") or img.get("workspace_path")
                if key and key not in seen:
                    seen.add(key)
                    deduped.append(img)
            images[mid] = (event.get("seq") or 0, deduped)
    return images


def _strip_sandbox(path):
    if not isinstance(path, str):
        return ""
    path = path.strip()
    if path.startswith("sandbox://"):
        path = path[len("sandbox://"):]
    return path.lstrip("/")


def extract_workspace_files(event: dict):
    """File descriptors for Muse file-store files, mirroring
    extract_workspace_images: delta.presentation kind=file events
    whose payload.data.file carries path + label + mime (live
    shape 2026-10-26, seq 4016: sandbox://workspace/*.txt,
    byte_len 447, missing False). Falls back to the fallback_text
    sandbox markdown when data.file is absent. Returns
    [{workspace_path, filename, content_type}]; the adapter reads
    bytes via client.read_file, exactly like workspace images."""
    payload = event.get("payload") or {}
    if (event.get("event_name") != "delta.presentation"
            or payload.get("kind") != "file"):
        return []
    data = payload.get("data") or {}
    f = data.get("file") or {}
    path = _strip_sandbox(f.get("path") or "")
    label = f.get("label") or event.get("display_text") or ""
    mime = f.get("mime") or ""
    if path:
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        name = label + ext if label and ext and not label.endswith(ext) else (label or path.rsplit("/", 1)[-1])
        return [{"workspace_path": path, "filename": name,
                 "content_type": mime}]
    fb = payload.get("fallback_text") or ""
    m = re.search(r"!\[([^\]]*)\]\(sandbox://([^)]+)\)", fb)
    if m:
        p = _strip_sandbox("sandbox://" + m.group(2))
        if p:
            ext = "." + p.rsplit(".", 1)[-1] if "." in p else ""
            name = m.group(1) + ext if m.group(1) and ext else (m.group(1) or p.rsplit("/", 1)[-1])
            return [{"workspace_path": p, "filename": name,
                     "content_type": mime}]
    return []


_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v")


def extract_workspace_videos(event: dict):
    """Video descriptors for Muse file-store videos, mirroring
    extract_workspace_files. The video presentation shape is NOT
    yet captured from a live turn (no generated-video sample as
    of 2026-10-26): accept payload.video_path (mirroring the
    image shape), payload.data.file.path (mirroring the file
    shape), and a sandbox:// markdown fallback, so whichever
    shape Muse uses is covered. Returns [{workspace_path,
    filename, content_type}]; the adapter reads bytes via
    client.read_file, exactly like workspace images/files."""
    payload = event.get("payload") or {}
    if (event.get("event_name") != "delta.presentation"
            or payload.get("kind") != "video"):
        return []
    data = payload.get("data") or {}
    f = data.get("file") or {}
    path = _strip_sandbox(
        payload.get("video_path") or f.get("path") or "")
    label = (f.get("label") or event.get("display_text") or "")
    mime = f.get("mime") or ""
    if path:
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        name = (label + ext if label and ext
                and not label.endswith(ext)
                else (label or path.rsplit("/", 1)[-1]))
        return [{"workspace_path": path, "filename": name,
                 "content_type": mime or "video/mp4"}]
    fb = payload.get("fallback_text") or ""
    m = re.search(r"!\[([^\]]*)\]\(sandbox://([^)]+)\)", fb)
    if m:
        p = _strip_sandbox("sandbox://" + m.group(2))
        if p:
            ext = "." + p.rsplit(".", 1)[-1] if "." in p else ""
            name = (m.group(1) + ext if m.group(1) and ext
                    else (m.group(1) or p.rsplit("/", 1)[-1]))
            return [{"workspace_path": p, "filename": name,
                     "content_type": mime or "video/mp4"}]
    return []


def collect_turn_videos(events, user_index):
    """Parallel to collect_turn_files, for videos: keyed by
    message_id -> (seq, [video descriptors]). A video-only
    assistant event still appears here, so it is not silently
    dropped. Workspace videos only (URL videos already land in
    files via extract_files and are forwarded as files)."""
    videos = {}
    for event in events[user_index + 1:]:
        if event.get("role") != "assistant":
            continue
        mid = event.get("message_id") or ""
        found = extract_workspace_videos(event)
        if found and mid:
            seen = set()
            deduped = []
            for v in found:
                key = v.get("workspace_path")
                if key and key not in seen:
                    seen.add(key)
                    deduped.append(v)
            videos[mid] = (event.get("seq") or 0, deduped)
    return videos


def collect_turn_files(events, user_index):
    """Parallel to collect_turn_images, for non-image files: keyed by
    message_id -> (seq, [file descriptors]). A file-only assistant
    event still appears here, so it is not silently dropped.
    URL files and file-store (workspace_path) files are merged,
    de-duplicated by their address."""
    files = {}
    for event in events[user_index + 1:]:
        if event.get("role") != "assistant":
            continue
        mid = event.get("message_id") or ""
        found = extract_files(event) + extract_workspace_files(event)
        if found and mid:
            seen = set()
            deduped = []
            for f in found:
                key = f.get("url") or f.get("workspace_path")
                if key and key not in seen:
                    seen.add(key)
                    deduped.append(f)
            files[mid] = (event.get("seq") or 0, deduped)
    return files


def join_turn(rendered) -> str:
    """Join a collect_turn_events result into one text, ordered by seq."""
    return "\n\n".join(text for _, text in sorted(rendered.values(), key=lambda x: x[0]))
