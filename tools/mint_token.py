#!/usr/bin/env python3
"""从 Muse 网页端换取 .mint.json 里的 token / notary_token。

网页端登录后，浏览器发往 muse.ai 的会话 cookie 就是唯一凭证，网页端
自己也是这么换的（2026-10-27 从网页 JS 里确认）：

    POST https://muse.ai/api/hatch/token
    {"vmAddress": "wss://<vm_id>.metaaivm.com/", "vmName": "<vm_id>"}
    -> {"token": "s0:...", "notary_token": "endorsement.v1..."}

个别情况下响应里没有 notary_token，脚本会再打一次：
    POST https://muse.ai/api/hatch/noise-notary-token
    {"vmName": "<vm_id>"} -> {"notaryToken": "endorsement.v1..."}

cookie 是 HttpOnly，document.cookie 读不到，从 DevTools 复制整行即可：

  1. 打开 https://muse.ai（保持登录），F12 → Network
  2. 点任意一个 muse.ai 的请求 → Request Headers → Cookie
  3. 整行复制，然后：

     python tools/mint_token.py --cookie "hatch_sess=...; hatch_gw=..."
     python tools/mint_token.py --cookie-file cookies.txt
     MUSE_COOKIE="..." python tools/mint_token.py

vm_id 的来源优先级：--vm-id > 现有 --out 文件里 token 的 env_id > const.json。
脚本绝不打印 token 本身，只打印写入路径、长度和 notary 过期时间。
"""
import argparse
import base64
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://muse.ai"
TOKEN_PATH = "/api/hatch/token"
NOTARY_PATH = "/api/hatch/noise-notary-token"


def vm_id_from_token(token):
    """从 mint token（s0:<jwt>）的 env_id claim 取 vm_id，失败返回 ''。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        decoded = base64.urlsafe_b64decode(
            parts[1] + "=" * (-len(parts[1]) % 4))
        obj = json.loads(decoded.decode("utf-8"))
        env_id = obj.get("env_id") if isinstance(obj, dict) else None
        return env_id if isinstance(env_id, str) else ""
    except Exception:
        return ""


def notary_expiry(notary):
    """读 notary token 的 timeout 限制（unix 秒），失败返回 None。

    结构：endorsement.v1.<payload>.<sig>.<claims>.<sig>，payload 里的
    restrictions 形如 ["timeout:1793083350", "hostname:...", ...]。
    """
    try:
        parts = notary.split(".")
        if len(parts) < 3:
            return None
        payload = parts[2] + "=" * (-len(parts[2]) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        restrictions = obj.get("restrictions")
        if not isinstance(restrictions, list):
            return None
        for item in restrictions:
            if isinstance(item, str) and item.startswith("timeout:"):
                ts = int(item.split(":", 1)[1].strip())
                return ts if ts > 0 else None
    except Exception:
        return None
    return None


def cookie_from_file(path):
    """读 Netscape cookies.txt 或一行裸 Cookie 头，只保留 muse.ai 的项。"""
    pairs = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("cookie:"):
                line = line.split(":", 1)[1].strip()
            if "\t" in line:
                # Netscape: domain \t flag \t path \t secure \t expiry \t name \t value
                fields = line.split("\t")
                if len(fields) >= 7 and "muse.ai" in fields[0] and fields[5]:
                    pairs.append(f"{fields[5]}={fields[6]}")
            elif "=" in line:
                pairs.append(line)
    return "; ".join(pairs)


def post_json(url, payload, cookie, timeout):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Cookie": cookie,
        "Accept": "application/json",
        "Origin": "https://muse.ai",
        "Referer": "https://muse.ai/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise SystemExit(f"{url} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"{url} -> 连不上：{e.reason}")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise SystemExit(f"{url} -> 返回的不是 JSON：{raw[:200]!r}")


def resolve_vm_id(args):
    if args.vm_id:
        return args.vm_id.strip()
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                derived = vm_id_from_token(json.load(fh).get("token") or "")
            if derived:
                return derived
        except Exception:
            pass
    const_path = args.const
    if os.path.exists(const_path):
        try:
            with open(const_path, encoding="utf-8") as fh:
                vm = (json.load(fh).get("muse") or {}).get("vm_id") or ""
            if vm.strip():
                return vm.strip()
        except Exception:
            pass
    raise SystemExit(
        "无法确定 vm_id：用 --vm-id 指定，或让 --out 指向已有的 "
        f"{args.out}，或提供含 muse.vm_id 的 {const_path}")


def write_atomic(path, obj):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mint-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    ap = argparse.ArgumentParser(
        description="用网页端会话 cookie 换 Muse mint token（token + notary_token）")
    ap.add_argument("--cookie", default=os.environ.get("MUSE_COOKIE", ""),
                    help="Cookie 请求头整行（也可用环境变量 MUSE_COOKIE）")
    ap.add_argument("--cookie-file",
                    help="Netscape cookies.txt 或一行裸 Cookie 头")
    ap.add_argument("--vm-id", help="Muse VM id（默认从现有 mint/const 文件推导）")
    ap.add_argument("--base-url", default=os.environ.get("MUSE_BASE_URL", DEFAULT_BASE),
                    help=f"网页端地址（默认 {DEFAULT_BASE}）")
    ap.add_argument("--out", default=".mint.json",
                    help="输出文件（默认当前目录 .mint.json）")
    ap.add_argument("--const", default="const.json",
                    help="vm_id 兜底来源（默认 const.json）")
    ap.add_argument("--force-refresh", action="store_true",
                    help="带 force_refresh，让服务端作废旧 token")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="把 JSON 打到 stdout 而不写文件（会包含敏感值）")
    args = ap.parse_args()

    cookie = (args.cookie or "").strip()
    if not cookie and args.cookie_file:
        cookie = cookie_from_file(args.cookie_file)
    if not cookie:
        raise SystemExit(
            "缺少 cookie：用 --cookie / --cookie-file / MUSE_COOKIE；"
            "也可以在 muse.ai 控制台直接跑 tools/mint_token.js")

    vm_id = resolve_vm_id(args)
    vm_address = f"wss://{vm_id}.metaaivm.com/"
    base = args.base_url.rstrip("/")

    payload = {"vmAddress": vm_address, "vmName": vm_id}
    if args.force_refresh:
        payload["force_refresh"] = True
    data = post_json(base + TOKEN_PATH, payload, cookie, args.timeout)

    token = (data.get("token") or "").strip()
    notary = (data.get("notary_token") or "").strip()
    if not token:
        raise SystemExit(f"{TOKEN_PATH} 没有返回 token：{str(data)[:200]}")

    if not notary:
        extra = post_json(base + NOTARY_PATH, {"vmName": vm_id},
                          cookie, args.timeout)
        notary = (extra.get("notaryToken") or "").strip()
    if not notary:
        raise SystemExit("没拿到 notary_token（换不到就又得回网页端看一次）")

    result = {"token": token, "notary_token": notary}

    if args.print_only:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        print("警告：上面是活的凭证，别贴到公开的地方。", file=sys.stderr)
        return

    write_atomic(args.out, result)

    expiry = notary_expiry(notary)
    lines = [
        f"已写入 {os.path.abspath(args.out)}",
        f"  vm_id        : {vm_id}",
        f"  token        : {len(token)} 字符（{token[:3]}…）",
        f"  notary_token : {len(notary)} 字符（{notary[:14]}…）",
    ]
    if expiry:
        remaining = expiry - time.time()
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry))
        lines.append(f"  notary 到期  : {when}（还有 {remaining / 3600:.1f} 小时）")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
