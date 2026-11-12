#!/usr/bin/env python3
"""Standalone manual iLink console (not a Muse bridge or an automated test).

From the repository root: python tests/wechat_cli.py --config config.json
Use --login to scan before entering, --to USER to choose a recipient. Relative
config/state/media paths follow the working directory, as in the bridge itself.
The default download directory is covered by the repository's data/ gitignore.

Best effort: reserve each incoming identity before printing/downloading it. A
failed attachment is reported and marked unknown, not automatically retried.
After a crash, accepted inputs become unknown; replay skips them. This avoids
repeated downloads but can leave an incomplete download. The shared state's
bounded message history is NOT an exactly-once guarantee. Never run this console
and the real bridge against the same account at the same time.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import stat
import sys
import threading
import time
import unicodedata

# Support the requested direct-script invocation without tests/__init__.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from muse_bridge.config import ConfigLoader
from muse_bridge.adapters.wechat.adapter import parse_event
from muse_bridge.adapters.wechat.auth import WeChatAuth
from muse_bridge.adapters.wechat.cli import display_qr, verify_code
from muse_bridge.adapters.wechat.client import WeChatClient
from muse_bridge.adapters.wechat.gateway import WeChatGateway
from muse_bridge.adapters.wechat.media import MAX_UPLOAD_BYTES, WeChatMedia
from muse_bridge.adapters.wechat.sender import WeChatSender
from muse_bridge.adapters.wechat.state import WeChatState

DEFAULT_DOWNLOAD_DIR = Path("data/wechat_cli/downloads")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³"
}
HELP = """普通行发送文本（不会发给 Muse）；目标必须已有 context，请先在微信发一条消息。
/to USER          切换接收人
/peers            列出本次运行最近接收人
/image PATH       发送本地图片
/file PATH        发送本地文件
/video PATH       发送本地视频
路径可包含空格；Windows 路径可写为 /file "C:\\my files\\a.txt"。
/status           脱敏本地状态（不保证远端会话有效）
/login 或 /登录   提示重启扫码；运行中不换号
/help             帮助
/quit             取消长轮询并退出；也可 EOF / Ctrl+C
媒体失败只报告一次，不自动重试；崩溃/取消中的消息可能丢失，非 exactly-once。
显示内容隐藏已知协议密钥并净化终端控制符，保留正文链接；附件本身不改写。"""


def output_line(text):
    print(text, flush=True)


def display_text(value, private=()):
    """Hide known protocol secrets and terminal escapes, preserving body links."""
    text = str(value)
    for secret in sorted({v for v in private if isinstance(v, str) and v}, key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C")
                   else f"\\u{ord(c):04x}" for c in text)


def error_summary(exc):
    # str(exc), repr(exc) and traceback locals can contain tokens/keys/URLs.
    parts = [type(exc).__name__]
    for key in ("code", "errno"):
        value = getattr(exc, key, None)
        if type(value) is int:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


def valid_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 256
            and not any(c.isspace() or unicodedata.category(c).startswith("C") for c in value))


def safe_filename(name):
    """One bounded cross-platform component, never a remote path/ADS/device."""
    name = name if isinstance(name, str) else "attachment.dat"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if c in '<>:"/\\|?*' or unicodedata.category(c).startswith("C")
                   else c for c in name)
    name = name.encode("utf-8", "ignore")[:160].decode("utf-8", "ignore").strip(" .")
    if not name:
        name = "attachment.dat"
    if name.split(".", 1)[0].upper() in _RESERVED:
        name = "_" + name
    return name


def save_attachment(directory, filename, data, cap):
    if not isinstance(data, bytes) or len(data) > cap:
        raise ValueError("Media exceeds byte limit or is not bytes")
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(8):
        path = directory / (secrets.token_hex(12) + "_" + safe_filename(filename))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    raise FileExistsError("Could not allocate a unique attachment name")


def path_argument(argument):
    # A command takes exactly ONE path, not shell words. shlex(posix=True) would
    # destroy Windows backslashes; keeping the remainder also accepts bare spaces.
    argument = argument.strip()
    if not argument:
        raise ValueError("Missing path")
    if argument[0] in "\"'":
        if len(argument) < 2 or argument[-1] != argument[0]:
            raise ValueError("Unclosed path quote")
        argument = argument[1:-1]
    if not argument or "\x00" in argument:
        raise ValueError("Invalid path")
    return Path(argument).expanduser()


def read_attachment(path, cap):
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Only regular local files are supported")
    with path.open("rb") as handle:
        if os.fstat(handle.fileno()).st_size > cap:
            raise ValueError("Outgoing media exceeds byte limit")
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise ValueError("Outgoing media exceeds byte limit")
    return data


@dataclass
class IncomingAttachment:
    kind: str
    filename: str
    descriptor: dict = field(repr=False)


@dataclass
class Incoming:
    sender: str
    message_id: str
    text: str
    attachments: list[IncomingAttachment]
    context: str | None = field(repr=False)


def parse_incoming(event, bot_id):
    """Reuse the platform parser; only supplement its transcribed-voice omission.

    Importing parse_event does not instantiate an adapter, gateway or Muse service.
    Normal untranscribed voice is already a file attachment in the public model.
    """
    parsed = parse_event(event, bot_id)
    if parsed is None:
        return None
    message, media, context = parsed
    attachments = [IncomingAttachment(att.type, safe_filename(att.filename), media[att.file_info][0])
                   for att in message.attachments]
    # The business parser emits transcription instead of an attachment. This
    # manual protocol test additionally downloads that original voice_item.
    for item in event.get("item_list", []):
        if not isinstance(item, dict) or type(item.get("type")) is not int or item["type"] != 3:
            continue
        descriptor = item.get("voice_item")
        if isinstance(descriptor, dict) and isinstance(descriptor.get("text"), str) and descriptor["text"]:
            attachments.append(IncomingAttachment("voice", safe_filename(descriptor.get("file_name") or "voice.audio"),
                                                   descriptor))
    return Incoming(message.source.sender_id, message.id, message.text, attachments, context)


class ConsoleInput:
    """Demand-driven daemon input worker, never asyncio's default executor.

    One outstanding read only; no prefetch, no input worker waiting for *another*
    line after /quit. A cancelled blocked read/getpass cannot be interrupted on
    every platform; its daemon dies with this standalone process. Do not embed
    this manual console in a longer-lived application's stdin session.
    """
    def __init__(self, output=output_line, stream=None):
        self.output, self.stream = output, stream if stream is not None else sys.stdin
        self._requests = queue.Queue()
        self._closed = threading.Event()
        self._thread = None
        self._pending = None

    @staticmethod
    def _deliver(future, value, failed):
        if not future.done():
            if failed:
                future.set_exception(OSError("Console input failed"))
            else:
                future.set_result(value)

    def _worker(self):
        while not self._closed.is_set():
            request = self._requests.get()
            if request is None or self._closed.is_set():
                break
            loop, future, hidden = request
            failed = False
            try:
                value = verify_code() if hidden else self.stream.readline()
                value = None if value == "" else value.rstrip("\r\n")
            except (EOFError, KeyboardInterrupt):
                value = None
            except Exception:
                value, failed = None, True
            if self._closed.is_set():
                break
            try:
                loop.call_soon_threadsafe(self._deliver, future, value, failed)
            except RuntimeError:  # Loop already closed after cancellation.
                break

    async def _read(self, prompt, hidden=False):
        if self._closed.is_set():
            return None
        if self._pending is not None:
            raise RuntimeError("Concurrent stdin reads are not supported")
        if prompt:
            self.output(prompt)
        future = asyncio.get_running_loop().create_future()
        self._pending = future
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="wechat-cli-input", daemon=True)
            self._thread.start()
        self._requests.put((asyncio.get_running_loop(), future, hidden))
        try:
            return await future
        finally:
            self._pending = None

    async def read_line(self, prompt="wechat>"):
        return await self._read(prompt)

    async def verify(self):
        return await self._read("", hidden=True)

    def close(self):
        self._closed.set()
        self._requests.put(None)
        if self._pending is not None:
            self._pending.cancel()


class PollWarnings(logging.Handler):
    def __init__(self, emit):
        super().__init__(logging.WARNING)
        self.emit_line = emit

    def emit(self, record):
        # Do not render arbitrary LogRecord args, exceptions or remote payloads.
        self.emit_line("[轮询/处理错误] 保留游标并退避重试；敏感错误详情已隐藏。可用 /status 检查。")


class WeChatConsole:
    def __init__(self, config, state, client, media, sender, download_dir, *, to=None, output=output_line):
        self.config, self.state, self.client = config, state, client
        self.media, self.sender = media, sender
        self.download_dir = Path(download_dir)
        self.target = to or (state.credentials or {}).get("user_id", "")
        if self.target and not valid_id(self.target):
            raise ValueError("Invalid recipient")
        self.output = output
        self._private = [(state.credentials or {}).get("bot_token"), config.route_tag]
        self.peers = {}
        self.gateway = WeChatGateway(client, state, self.handle_batch, config.polling_timeout_ms)

    def emit(self, text, private=()):
        self.output(display_text(text, (*self._private, *private)))

    def cap(self, kind, *, outgoing=False):
        field_name = {"image": "max_image_bytes", "file": "max_file_bytes", "voice": "max_file_bytes",
                      "video": "max_video_upload_bytes" if outgoing else "max_video_bytes"}[kind]
        return min(getattr(self.config, field_name), MAX_UPLOAD_BYTES)

    async def handle_batch(self, events):
        for event in events:
            incoming = parse_incoming(event, self.state.bot_id)
            if incoming is None:
                self.emit("[忽略] 非私聊、无效消息或其他账号事件（不显示原始内容）。")
                continue
            user, mid = incoming.sender, incoming.message_id
            if self.state.message_status(user, mid) is not None:
                continue
            # Persist BEFORE output/download. If interrupted, do not repeat effects.
            self.state.remember_message(user, mid, incoming.context)
            self.peers.pop(user, None)
            self.peers[user] = mid
            if len(self.peers) > 128:
                self.peers.pop(next(iter(self.peers)))
            if not self.target:
                self.target = user
            private = [incoming.context]
            for attachment in incoming.attachments:
                descriptor = attachment.descriptor
                media = descriptor.get("media")
                private.extend(descriptor.get(k) for k in ("aeskey", "full_url", "url"))
                if isinstance(media, dict):
                    private.extend(media.get(k) for k in ("aes_key", "encrypt_query_param", "full_url"))
            self.emit(f"[收到] sender={user} message_id={mid}\n{incoming.text or '[无文本]'}", private)
            failed = False
            for attachment in incoming.attachments:
                try:
                    data = await asyncio.wait_for(
                        self.media.download(attachment.descriptor, self.cap(attachment.kind),
                                            image=attachment.kind == "image"),
                        timeout=self.config.download_timeout_s)
                    path = save_attachment(self.download_dir, attachment.filename, data, self.cap(attachment.kind))
                    self.emit(f"[附件已保存] sender={user} message_id={mid} kind={attachment.kind} path={path}", private)
                except Exception as exc:
                    failed = True
                    self.emit(f"[附件下载/保存失败] sender={user} message_id={mid} kind={attachment.kind} "
                              f"error={error_summary(exc)}；本条不自动重试。", private)
            # Failures are terminal best-effort outcomes, allowing cursor advancement.
            self.state.finish_message(user, mid, "unknown" if failed else "handled")

    async def send(self, text="", *, kind=None, path=None):
        if not self.target:
            self.emit("[未发送] 请用 /to USER 选择目标，或先在微信发一条消息。")
            return
        if not self.state.context(self.target):
            self.emit(f"[未发送] user_id={self.target} 缺少 context，请先在微信向机器人发一条消息。")
            return
        if self.state.cooldown_until > time.time():
            self.emit("[未发送] 会话冷却中，请查看 /status；不要连续重试。")
            return
        try:
            kwargs = {}
            if kind:
                data = read_attachment(path, self.cap(kind, outgoing=True))
                kwargs[{"image": "images", "file": "files", "video": "videos"}[kind]] = [
                    {"data": data, "filename": safe_filename(path.name)}]
            ok = await self.sender.send(self.target, text, purpose="proactive", **kwargs)
        except Exception as exc:
            self.emit(f"[发送错误] user_id={self.target} error={error_summary(exc)}；不自动重发。")
            return
        if ok:
            self.emit(f"[已发送] user_id={self.target} kind={kind or 'text'}")
        else:
            self.emit("[发送未确认] 可能失败、部分成功或结果未知；查看 /status，不要盲目重发。")

    async def command(self, line):
        """Return False only for /quit; all other failures keep the REPL alive."""
        if not line.strip():
            return True
        if not line.startswith("/"):
            await self.send(line)
            return True
        words = line.split(maxsplit=1)
        command, argument = words[0].lower(), words[1] if len(words) == 2 else ""
        if command == "/quit" and not argument:
            return False
        if command == "/help":
            self.emit(HELP)
        elif command in {"/login", "/登录"}:
            self.emit("运行中不切换账号。请 /quit 后用 python tests/wechat_cli.py --config PATH --login 重启。")
        elif command == "/to":
            if not valid_id(argument):
                self.emit("用法: /to USER（非空、无空格的 user_id）")
            else:
                self.target = argument
                self.emit(f"[目标] user_id={self.target}")
        elif command == "/peers":
            self.emit("最近接收人（仅本次运行，最多128人）:")
            for user, mid in reversed(list(self.peers.items())):
                self.emit(f"user_id={user} message_id={mid} context={'有' if self.state.context(user) else '无'}"
                          + (" [当前目标]" if user == self.target else ""))
        elif command == "/status":
            status = self.state.status()
            # Explicit allowlist: never serialize credentials or arbitrary state.
            summary = {key: status[key] for key in (
                "logged_in", "bot_id", "user_id", "has_token", "has_cursor", "context_users",
                "uncertain_sends", "uncertain_inputs")}
            summary.update(target=self.target, poll=self.gateway.phase,
                           cooldown_remaining_s=max(0, int(self.state.cooldown_until - time.time())))
            self.emit(json.dumps(summary, ensure_ascii=False))
        elif command in {"/image", "/file", "/video"}:
            try:
                path = path_argument(argument)
            except ValueError:
                self.emit(f"用法: {command} PATH（含空格可用一对引号，Windows反斜杠保留）")
            else:
                await self.send(kind=command[1:], path=path)
        else:
            self.emit("未知命令；输入 /help 查看帮助。发送以 / 开头的文字请在前面加空格。")
        return True

    async def run(self, reader):
        """Own the poll/input tasks; async_main owns the transport and reader."""
        async def commands():
            while True:
                line = await reader.read_line()
                if line is None or not await self.command(line):
                    return

        self.emit(f"[控制台] bot_id={self.state.bot_id} user_id={self.target or '[等待收到消息]'}")
        self.emit(HELP)
        warnings = PollWarnings(self.emit)
        logger = logging.getLogger("muse_bridge.adapters.wechat.gateway")
        logger.addHandler(warnings)
        poll = asyncio.create_task(self.gateway.start(), name="wechat-cli-poll")
        inputs = asyncio.create_task(commands(), name="wechat-cli-commands")
        try:
            done, _ = await asyncio.wait((poll, inputs), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task  # Surface unexpected task failures, never silently detach.
        finally:
            poll.cancel()
            inputs.cancel()
            await self.gateway.stop()
            await asyncio.gather(poll, inputs, return_exceptions=True)
            logger.removeHandler(warnings)


async def select_account(config, state, reader, *, login=False, output=output_line,
                         client_factory=None, auth_factory=None, display=None):
    """Finish and close login before creating the authenticated polling client."""
    if not login:
        while True:
            choice = await reader.read_line("[1] 使用已保存登录  [2] 扫码登录  [0] 退出")
            if choice is None or choice.strip() == "0":
                return False
            if choice.strip() == "1":
                if state.credentials:
                    return True
                output("没有已保存登录，请选择 [2] 扫码登录。")
            elif choice.strip() == "2":
                break
            else:
                output("请选择 1、2 或 0。")
    client = (client_factory or WeChatClient)(channel_version=config.channel_version,
                                            route_tag=config.route_tag, timeout=config.http_timeout_s)
    try:
        credentials = await (auth_factory or WeChatAuth)(client).login(
            state.accounts(), display=display or display_qr, verify=reader.verify,
            on_status=lambda *_: output("微信登录状态已更新，请按微信提示继续（不显示凭据）。"))
        state.save_account(credentials)
    finally:
        await client.aclose()
    output("微信登录已保存；即将启动唯一长轮询。")
    return True


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="手动微信 iLink 协议控制台，不连接 Muse；不要与桥接并行运行。")
    parser.add_argument("--config", default="config.json", metavar="PATH")
    parser.add_argument("--login", action="store_true", help="显式先扫码登录，跳过启动菜单")
    parser.add_argument("--to", metavar="USER", help="初始接收人；未指定则用扫码者/首个发信人")
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR,
                        help="附件保存目录（默认 data/wechat_cli/downloads，已被 gitignore）")
    return parser.parse_args(argv)


async def async_main(args, *, reader=None, output=output_line, config_loader=None,
                     state_factory=None, client_factory=None, auth_factory=None):
    reader = reader if reader is not None else ConsoleInput(output)
    client = None
    try:
        output("警告：不要和真实桥接进程或另一控制台同时运行同一微信账号，否则会争抢长轮询/游标。")
        output("这里只测试微信协议，不连接 Muse；共享账号state/游标，已消费消息不会再进入 Muse。")
        config = (config_loader or ConfigLoader.load_wechat)(args.config)
        state = (state_factory or WeChatState)(config.state_dir)
        if args.to and not valid_id(args.to):
            raise ValueError("Invalid recipient")
        if not await select_account(config, state, reader, login=args.login, output=output,
                                    client_factory=client_factory, auth_factory=auth_factory):
            return 0
        state.recover_inputs()
        credentials = state.credentials
        client = (client_factory or WeChatClient)(
            credentials["bot_token"], credentials["baseurl"], channel_version=config.channel_version,
            route_tag=config.route_tag, timeout=config.http_timeout_s,
            cooling_until=lambda: state.cooldown_until, on_session_invalid=state.pause)
        media = WeChatMedia(client)
        sender = WeChatSender(client, state, config, media)
        console = WeChatConsole(config, state, client, media, sender, args.download_dir,
                                to=args.to, output=output)
        await console.run(reader)
        return 0
    except Exception as exc:
        output(f"[控制台错误] {error_summary(exc)}（敏感详情不显示；请检查配置、状态文件和本地路径）")
        return 1
    finally:
        reader.close()
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:
                output(f"[关闭连接错误] {error_summary(exc)}")


def main(argv=None):
    try:
        return asyncio.run(async_main(parse_args(argv)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        output_line("已取消；长轮询已停止。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
