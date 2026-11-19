# Cloudflare Worker 转发部署指南

本指南说明如何将仓库内置的 [`tools/cfproxy.js`](../tools/cfproxy.js) 部署到你自己的 Cloudflare Workers，并接入 Muse IM Bridge 的统一网络路由，用于在国内网络环境下稳定访问 **Muse**（HTTPS 与 `Noise_XX` WebSocket）和 **Telegram Bot API**。

> 协议路由与配置字段详解另见 [统一网络代理与 Cloudflare Worker 传输](worker-transport.md)。

---

## 工作原理

`tools/cfproxy.js` 是一个同时支持 **HTTP(S)** 与 **WebSocket（WS/WSS）** 的路径转发 Worker，请求格式为：

```text
https://<your-worker-domain>/{protocol}/{host}/{path}[?query]
wss://<your-worker-domain>/{protocol}/{host}/{path}[?query]
```

- **HTTP(S)**：将 `/https/api.example.com/foo?a=1` 转发至 `https://api.example.com/foo?a=1`，完整透传请求方法、请求头与请求体。
- **WebSocket**：收到 `Upgrade: websocket` 后，通过 `WebSocketPair` 建立双向中继，原样透传文本与二进制帧（支持 Muse 的 `Noise_XX` 加密长连接）及 `Sec-WebSocket-Protocol`。

---

## 前置准备

1. 一个 **Cloudflare 账号**（免费版即可）。
2. **一个托管在 Cloudflare DNS 下的自定义域名**（国内网络强烈建议）。
   - Cloudflare 默认分配的 `*.workers.dev` 二级域名在中国大陆通常无法直连，需要通过 **Custom Domain（自定义域）** 绑定自己的子域名（例如 `proxy.example.com`）。
   - 海外服务器如需使用，可直接绑定自定义域名或按网络情况测试 `*.workers.dev`。

---

## 方式一：Cloudflare 控制台网页部署（推荐）

无需本地安装 Node.js，全程浏览器完成：

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)。
2. 左侧菜单进入 **Compute (Workers) → Workers & Pages**，点击 **Create** → **Create Worker**。
3. 填写一个 Worker 名称（例如 `muse-cfproxy`），先点击 **Deploy** 创建占位 Worker。
4. 进入该 Worker 详情页，点击右上角 **Edit code**。
5. 清空编辑器中的默认代码，完整复制仓库中的 [`tools/cfproxy.js`](../tools/cfproxy.js) 粘贴进去，点击右上角 **Deploy** 保存发布。
6. 绑定自定义域名：
   - 返回 Worker 详情页，切换到 **Settings → Domains & Routes**；
   - 点击 **Add → Custom Domain**；
   - 输入一个托管在当前 Cloudflare 账号下的子域名（建议不易被猜测，例如 `cf-relay-x9k2.example.com`），点击 **Add domain**；
   - 等待状态变为 **Active**（通常 1–2 分钟内自动完成 DNS 解析与 SSL 证书签发）。

---

## 方式二：使用 Wrangler CLI 部署

适合习惯命令行或需要版本化管理部署的用户：

1. 安装并登录 Wrangler（需 Node.js 18+）：
   ```bash
   npm install -g wrangler
   wrangler login
   ```
2. 在本地新建临时或私有的 `wrangler.toml`（不要提交含个人域名的配置到公共仓库）：
   ```toml
   name = "muse-cfproxy"
   main = "tools/cfproxy.js"
   compatibility_date = "2025-01-01"

   # 绑定自定义域名（需已托管在 Cloudflare DNS）
   routes = [
     { pattern = "cf-relay-x9k2.example.com", custom_domain = true }
   ]
   ```
3. 在仓库根目录执行部署：
   ```bash
   wrangler deploy
   ```

---

## 验证部署是否生效

将下方命令中的 `https://your-worker.example.com` 替换为你绑定的 Worker 域名：

### 1. 检查 Worker 基础响应

```bash
curl -i https://your-worker.example.com/
```

预期返回 HTTP `400` 且响应体为：

```text
Invalid path format. Expected: /{protocol}/{host}/{path}
```

这表示 Worker 代码已正常运行且路径校验生效。

### 2. 检查 HTTPS 转发连通性

```bash
curl -i https://your-worker.example.com/https/muse.ai/api/client-ip
```

预期返回 HTTP `200` 及 JSON 响应。若要测试 Telegram API 可达性（无需填写真实 Token）：

```bash
curl -i https://your-worker.example.com/https/api.telegram.org/bot0:invalid/getMe
```

预期返回 Telegram 的 `401` JSON（`{"ok":false,"error_code":401,"description":"Unauthorized"}`），说明到 `api.telegram.org` 的链路已通。

---

## 接入 Muse IM Bridge

### 方式 A：通过控制台配置（推荐）

运行 `python manage.py`，进入网络设置填入 Worker 地址，并为需要经 Worker 转发的模块（如 `muse`、`telegram`）选择 `worker` 模式，随后使用内置连通性体检验证。

### 方式 B：手动编辑 `config.json`

在 `config.json` 顶层配置 `network.worker`，并按模块指定 `proxy` 模式：

```json
{
  "network": {
    "proxy_url": "",
    "worker": {
      "base": "https://your-worker.example.com",
      "ws": true,
      "http": true
    }
  },
  "muse": {
    "proxy": "worker"
  },
  "adapters": {
    "qq": { "proxy": "none" },
    "onebot": { "proxy": "none" },
    "wechat": { "proxy": "none" },
    "telegram": { "proxy": "worker" }
  }
}
```

配置规则说明：
- `network.worker.base` 必须是 **HTTPS 443 端口的纯 origin**（例如 `https://your-worker.example.com`），不能包含路径、查询参数、用户名密码或非 443 端口。
- `muse.proxy` 与 `adapters.telegram.proxy` 默认即为 `"worker"`；国内直连渠道（`qq`、`onebot`、`wechat`）默认 `"none"`。
- 修改后可先运行本地校验：
  ```bash
  python main.py --check
  ```

### 可选：复用于 QQ 官方机器人附件下载代理

若 QQ 官方机器人在下载 `multimedia.nt.qq.com.cn` 等受阻域名的附件时失败，可直接复用同一 Worker 配置 `adapters.qq.download_proxy`（**注意末尾需带 `/https/`**）：

```json
{
  "adapters": {
    "qq": {
      "download_proxy": "https://your-worker.example.com/https/"
    }
  }
}
```

---

## 安全加固建议（重要）

[`tools/cfproxy.js`](../tools/cfproxy.js) 默认是通用的开放路径转发脚本，**请务必做好访问保护**，防止域名泄露后被第三方扫描滥用导致额度耗尽或账号风控：

1. **禁止公开域名**：切勿将你部署的 Worker 域名写入公开仓库、Issue、日志截图或群聊。
2. **使用难以猜测的子域名**：例如结合随机后缀命名子域名，不要使用 `proxy.`、`cf.` 等常见前缀。
3. **强烈建议添加目标域名白名单**：在部署自己的 Worker 时，可在 `handleRequest` 解析出 `rest` 后加一段目标主机校验，仅放行本项目实际需要的上游域名：
   ```javascript
   const ALLOWED_HOSTS = new Set([
     'muse.ai',
     'auth.muse.ai',
     'hatch.metaaivm.com',
     'api.telegram.org',
     'multimedia.nt.qq.com.cn',
   ])

   let targetHost
   try {
     const parseScheme = protocol.startsWith('ws') ? 'https' : protocol
     targetHost = new URL(`${parseScheme}://${rest}`).hostname.toLowerCase()
   } catch {
     return new Response('Invalid target URL', { status: 400 })
   }
   if (
     !ALLOWED_HOSTS.has(targetHost) &&
     !targetHost.endsWith('.muse.ai') &&
     !targetHost.endsWith('.metaaivm.com')
   ) {
     return new Response('Forbidden target host', { status: 403 })
   }
   ```
   > 如后续使用了自定义 Muse 网关域名或需经 Worker 转发其他上游（例如外部图片源或 OneBot 服务），同步将对应域名加入 `ALLOWED_HOSTS` 即可。
4. **可配合 Cloudflare WAF 规则**：如网桥部署在固定公网 IP 的服务器上，可在 Cloudflare 安全规则（WAF Custom Rules）中限制仅允许你的服务器 IP 访问该 Worker 子域名。

---

## 常见问题排查

| 现象 / 报错 | 原因与处理 |
| :--- | :--- |
| `network.worker.base must be an HTTPS origin on port 443` | `base` 写成了 `http://`、带了路径（如 `/https`）或自定义端口；请改为纯 origin（`https://your-worker.example.com`）。 |
| 国内连接超时或 TLS 握手失败 | 使用了默认 `*.workers.dev` 域名或本地网络干扰；请绑定自有 Custom Domain 并确认 DNS 已生效。 |
| 返回 `502 Proxy error` | Worker 访问上游目标失败（目标域名拼写错误、上游拒绝连接或瞬时故障），可单独用 `curl` 验证目标路径。 |
| 返回 `400 WebSocket upgrade required` | 用浏览器或普通 HTTP 工具直接访问了 `/wss/...` 路径；`/ws/` 与 `/wss/` 仅接受 WebSocket 升级请求。 |
| 报错 `429` 或间歇断连 | 可能触发 Cloudflare Workers 免费版日请求配额（默认 10 万次/天）或并发连接限制；请在 Cloudflare Dashboard 查看 Workers 指标，并确认未遭外部滥用。 |
| 开启 Worker 后不想继续使用 | 将对应模块的 `proxy` 改为 `"none"`（直连）或 `"http"`（改走 `network.proxy_url`），Worker 失败时网桥不会静默回退直连。 |
