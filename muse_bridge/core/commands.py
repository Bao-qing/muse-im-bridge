"""Slash command layer for session management and bot controls."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, List, Optional, Tuple

GATEWAY_COMMANDS = frozenset(["status", "compact", "clear", "usage", "subagents", "stop"])
LOCAL_COMMANDS = frozenset([
    "sessions", "switch", "new", "current", "delete", "rename",
    "pin", "unpin", "archive", "unarchive", "help", "login"
])

HELP_TEXT = (
    "命令（/ 开头）：\n"
    "/申请 [全员] — 申请使用；全员表示开放当前群\n"
    "/申请审批 [编号 同意|拒绝] — 管理员私聊审批白名单\n"
    "/sessions [页] — 列出会话\n"
    "/switch <序号|id> — 切换会话\n"
    "/new — 新开会话\n"
    "/current — 当前会话\n"
    "/login [邮箱] — 显式登录 Muse（管理员私聊专用）\n"
    "/凭证 [编号] [保存|一次] — 填写凭证，也可回复凭证卡\n"
    "账号和密码可分两行发送，保存后自动继续聊天\n"
    "/审批 [编号] [选项数字] — 查看或选择 Muse 原生审批选项\n"
    "/cancel — 结束本地输入（不会取消远端审批）\n"
    "/chat — 显式退出已结束的输入流程，重新发送聊天消息\n"
    "/delete <序号|id> — 删除会话（主会话不能删）\n"
    "/rename <序号|id> <新标题> — 改名\n"
    "/pin /unpin <序号|id> — 置顶 / 取消置顶\n"
    "/archive /unarchive <序号|id> — 归档 / 取消归档\n"
    "/status — 会话状态与用量\n"
    "/usage on|off — 开关用量显示\n"
    "/clear — 下一句话起用全新上下文\n"
    "/compact — 压缩当前会话上下文\n"
    "/subagents — 列出子代理\n"
    "/help — 显示本帮助"
)

_PER_PAGE = 20


class CommandError(Exception):
    """Safe error message to return to user."""
    pass


class CommandHandler:
    @staticmethod
    def parse_command(text: str) -> Optional[Tuple[str, str]]:
        if not text:
            return None
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        parts = stripped[1:].split(None, 1) or [""]
        name = (parts[0] or "").lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        if name in GATEWAY_COMMANDS:
            if name == "usage" and args and args.lower() not in ("on", "off"):
                return ("__usage_hint__", "")
            return None  # Pass through to Muse
        if name in LOCAL_COMMANDS:
            return (name, args)
        return ("__help__", "")

    @staticmethod
    def sort_sessions(sessions: List[dict]) -> List[dict]:
        return sorted(
            (s for s in sessions if isinstance(s, dict)),
            key=lambda s: (-(s.get("updated_at_ms") or 0), s.get("session_id") or ""),
        )

    @staticmethod
    def resolve_session(arg: str, sessions: List[dict]) -> str:
        if not arg:
            raise CommandError("请指定会话序号或 id")
        # Try 1-based index
        try:
            idx = int(arg)
            visible = [s for s in CommandHandler.sort_sessions(sessions) if not s.get("archived")]
            if 1 <= idx <= len(visible):
                sid = visible[idx - 1].get("session_id")
                if sid:
                    return sid
        except ValueError:
            pass

        # Match exact ID or prefix
        lower = arg.lower()
        matches = [s["session_id"] for s in sessions if s.get("session_id", "").lower().startswith(lower)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CommandError(f"匹配到多个会话，请输入更长的 id 前缀：{', '.join(matches[:3])}")
        raise CommandError(f"未找到匹配的会话：{arg}")

    @classmethod
    def execute(
        cls,
        name: str,
        args: str,
        client: Any,
        active_session_id: str,
        session_state_file: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Execute a local slash command.

        Returns (reply_text, new_active_session_id).
        """
        if name in ("help", "__help__"):
            return HELP_TEXT, active_session_id
        if name == "__usage_hint__":
            return "用法：/usage on 或 /usage off", active_session_id

        if name == "new":
            new_id = str(uuid.uuid4())
            if session_state_file:
                cls.save_session_id(session_state_file, new_id)
            return f"已开启新会话：{new_id}", new_id

        if name == "current":
            return f"当前会话：{active_session_id}", active_session_id

        if client is None:
            raise CommandError("Muse 客户端未连接，无法执行此指令")

        sessions = client.list_sessions()

        if name == "sessions":
            page = 1
            if args:
                try:
                    page = max(1, int(args))
                except ValueError:
                    page = 1
            visible = [s for s in cls.sort_sessions(sessions) if not s.get("archived")]
            total_pages = max(1, (len(visible) + _PER_PAGE - 1) // _PER_PAGE)
            start = (page - 1) * _PER_PAGE
            page_items = visible[start : start + _PER_PAGE]

            lines = [f"会话列表（第 {page}/{total_pages} 页，共 {len(visible)} 个）："]
            for i, s in enumerate(page_items, start=start + 1):
                sid = s.get("session_id", "")
                title = s.get("title") or "未命名会话"
                prefix = "* " if sid == active_session_id else "  "
                pin = "[顶] " if s.get("pinned") else ""
                lines.append(f"{prefix}{i}. {pin}{title} ({sid[:8]}...)")
            return "\n".join(lines), active_session_id

        sid = cls.resolve_session(args.split()[0] if args else "", sessions)

        if name == "switch":
            if session_state_file:
                cls.save_session_id(session_state_file, sid)
            return f"已切换至会话：{sid}", sid

        if name == "delete":
            client.delete_session(sid)
            return f"已删除会话：{sid}", active_session_id

        if name == "pin":
            client.pin_session(sid)
            return f"已置顶会话：{sid}", active_session_id

        if name == "unpin":
            client.unpin_session(sid)
            return f"已取消置顶：{sid}", active_session_id

        if name == "archive":
            client.archive_session(sid)
            return f"已归档会话：{sid}", active_session_id

        if name == "unarchive":
            client.unarchive_session(sid)
            return f"已取消归档：{sid}", active_session_id

        if name == "rename":
            parts = args.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                raise CommandError("用法：/rename <序号|id> <新标题>")
            new_title = parts[1].strip()
            client.rename_session(sid, new_title)
            return f"会话已重命名为：{new_title}", active_session_id

        return HELP_TEXT, active_session_id

    @staticmethod
    def load_session_id(path: str, default: str = "") -> str:
        if not path or not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                sid = data.get("session_id")
                return str(sid).strip() if sid else default
        except Exception:
            return default

    @staticmethod
    def save_session_id(path: str, sid: str) -> None:
        if not path:
            return
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"session_id": sid}, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
