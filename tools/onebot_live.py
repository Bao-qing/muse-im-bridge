"""Drive the OneBot test bots by hand (live testing only, never in unit tests).

Reads endpoints from the environment (see ``.env.onebot``, git-ignored):
  USER_OB_URL / USER_OB_TOKEN   the simulated user (sends test messages)
  MUSE_OB_URL / MUSE_OB_TOKEN   the bot the bridge serves (read-only here)
  MUSE_OB_SELF                  the bot's own QQ number
  MUSE_OB_TEST_GROUPS           comma-separated group ids this tool may message
  MUSE_OB_TEST_PRIVATE          comma-separated private ids this tool may message

Usage:
  python tools/onebot_live.py send group <group_id> <text>
  python tools/onebot_live.py send private <user_id> <text>
  python tools/onebot_live.py history group <group_id> [count] [--since <unix_ts>]
  python tools/onebot_live.py history private <user_id> [count] [--since <unix_ts>]
  python tools/onebot_live.py call user|muse <action> [json-params]
  python tools/onebot_live.py ask|wait <kind> <target> <wait_s> <text|since_ts>
  python tools/onebot_live.py image|file <kind> <target> <path> [text]

Git Bash rewrites arguments such as "/help" into Windows paths; run
``export MSYS_NO_PATHCONV=1`` first when sending slash commands.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request

def _ids(name: str) -> set:
    return {x.strip() for x in os.environ.get(name, "").split(",") if x.strip()}


ALLOWED_GROUPS = _ids("MUSE_OB_TEST_GROUPS")
ALLOWED_PRIVATE = _ids("MUSE_OB_TEST_PRIVATE")


def call(side: str, action: str, params: dict) -> dict:
    prefix = "USER_OB" if side == "user" else "MUSE_OB"
    url = os.environ[f"{prefix}_URL"].rstrip("/") + "/" + action
    req = urllib.request.Request(url, data=json.dumps(params).encode(), headers={
        "Authorization": "Bearer " + os.environ[f"{prefix}_TOKEN"],
        "Content-Type": "application/json", "User-Agent": "muse-im-bridge-live/0.1"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return json.loads(resp.read())


def render(msg: dict) -> str:
    parts = []
    for seg in msg.get("message") or []:
        t, d = seg.get("type"), seg.get("data") or {}
        if t == "text":
            parts.append(d.get("text", ""))
        elif t == "at":
            parts.append(f"@{d.get('qq')}")
        else:
            parts.append(f"[{t}:{d.get('file') or d.get('name') or d.get('id') or ''}]")
    return "".join(parts)


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "send":
        kind, target, text = argv[1], argv[2], " ".join(argv[3:]).replace("\\n", "\n")
        allowed = ALLOWED_GROUPS if kind == "group" else ALLOWED_PRIVATE
        if target not in allowed:
            raise SystemExit(f"refusing to message {kind}:{target} (add it to MUSE_OB_TEST_GROUPS / MUSE_OB_TEST_PRIVATE)")
        key = "group_id" if kind == "group" else "user_id"
        res = call("user", f"send_{kind}_msg", {key: int(target), "message": [{"type": "text", "data": {"text": text}}]})
        print(json.dumps({"retcode": res.get("retcode"), "message_id": (res.get("data") or {}).get("message_id"),
                          "ts": int(time.time())}))
        return 0 if res.get("retcode") == 0 else 1
    if cmd == "history":
        kind, target = argv[1], argv[2]
        rest = argv[3:]
        since = 0
        if "--since" in rest:
            i = rest.index("--since")
            since = int(rest[i + 1])
            rest = rest[:i] + rest[i + 2:]
        count = int(rest[0]) if rest else 20
        if kind == "group":
            res = call("user", "get_group_msg_history", {"group_id": int(target), "count": count})
        else:
            res = call("user", "get_friend_msg_history", {"user_id": int(target), "count": count})
        for m in (res.get("data") or {}).get("messages") or []:
            if m.get("time", 0) < since:
                continue
            who = (m.get("sender") or {}).get("nickname") or m.get("user_id")
            print(f"[{time.strftime('%H:%M:%S', time.localtime(m.get('time', 0)))}] {who}: {render(m)}")
        return 0
    if cmd in ("image", "file"):
        # image <kind> <target> <path> [text] | file <kind> <target> <path>
        import base64
        kind, target, path = argv[1], argv[2], argv[3]
        if target not in (ALLOWED_GROUPS if kind == "group" else ALLOWED_PRIVATE):
            raise SystemExit(f"refusing to message {kind}:{target} (add it to MUSE_OB_TEST_GROUPS / MUSE_OB_TEST_PRIVATE)")
        b64 = "base64://" + base64.b64encode(open(path, "rb").read()).decode()
        key = "group_id" if kind == "group" else "user_id"
        if cmd == "image":
            segs = ([{"type": "text", "data": {"text": " ".join(argv[4:])}}] if argv[4:] else [])
            segs.append({"type": "image", "data": {"file": b64}})
            res = call("user", f"send_{kind}_msg", {key: int(target), "message": segs})
        else:
            res = call("user", f"upload_{kind}_file", {key: int(target), "file": b64, "name": os.path.basename(path)})
        print(json.dumps({"retcode": res.get("retcode"), "ts": int(time.time())}))
        return 0 if res.get("retcode") == 0 else 1
    if cmd in ("ask", "wait"):
        # ask <kind> <target> <wait_s> <text>: send, then print bot messages until quiet.
        # wait <kind> <target> <wait_s> <since_ts>: only print bot messages since a time.
        kind, target, wait_s = argv[1], argv[2], float(argv[3])
        if cmd == "ask":
            if main(["send", kind, target] + argv[4:]) != 0:
                return 1
            since = int(time.time()) - 1
        else:
            since = int(argv[4])
        bot = os.environ.get("MUSE_OB_SELF", "")
        seen, last_new, deadline = set(), time.time(), time.time() + wait_s
        while time.time() < deadline:
            time.sleep(3)
            if kind == "group":
                res = call("user", "get_group_msg_history", {"group_id": int(target), "count": 30})
            else:
                res = call("user", "get_friend_msg_history", {"user_id": int(target), "count": 30})
            for m in (res.get("data") or {}).get("messages") or []:
                if m.get("time", 0) < since or str(m.get("user_id")) != bot or m.get("message_id") in seen:
                    continue
                seen.add(m.get("message_id"))
                last_new = time.time()
                print(f"[{time.strftime('%H:%M:%S', time.localtime(m.get('time', 0)))}] BOT: {render(m)}")
            # Stop once the bot has answered and then stayed quiet for 12s.
            if seen and time.time() - last_new > 12:
                break
        if not seen:
            print("(no bot message)")
        return 0
    if cmd == "call":
        side, action = argv[1], argv[2]
        params = json.loads(argv[3]) if len(argv) > 3 else {}
        print(json.dumps(call(side, action, params), ensure_ascii=False, indent=1)[:4000])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
