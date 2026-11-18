# 可选 cfproxy Worker 传输

三个开关互相独立，默认全关。不修改生产配置；示例仅供手动合并到**测试实例**。

`https://your-worker.example.com` 是占位地址，启用前须在你自己的本地配置中填写自有 Worker 地址；不要把实际部署地址写入公共示例或程序默认值。

## 通道范围

| 开关 | 实际流量 | 关闭后 |
| --- | --- | --- |
| `muse.worker.ws` | `hatch.metaaivm.com` 的 WS + 原样 Noise 数据，包括聊天、当前实现中的 Feed/审批 RPC、媒体/文件 RPC | 原有 `direct` / `proxy_url` 路由 |
| `muse.worker.http` | `token_manager` 的 Muse 首页 HTML（取 vm_id）、`/api/hatch/token`、`/api/hatch/noise-notary-token` | 原有 httpx 路由 |
| `muse.worker.login` | `auth.muse.ai` SSO、Muse 原生邮箱 OTP API 与登录重定向链 | 原有登录路由 |

当前 `feed.py` 和审批协议通过 `MuseClient.request()` 在 Noise 中发送虚拟 HTTP 请求，**并不建立单独的 muse.ai httpx 连接**，所以随 `ws` 开关而不是 `http` 开关切换。不改变这部分业务或审批网关。

`vmAddress: wss://{vm_id}.metaaivm.com/` 只是铸造凭据的 payload 字段，保持原值，不加 Worker 前缀，也不为该域建立连接。

## 示例一：只开 WS

```json
{
  "muse": {
    "worker": {
      "base": "https://your-worker.example.com",
      "ws": true,
      "http": false,
      "login": false
    }
  }
}
```

拨号变成 `wss://your-worker.example.com/wss/hatch.metaaivm.com/v1/noise?...`，原 query 字节与顺序保留。实际拨号、TLS SNI、Host 均为 Worker；本地 CONNECT 代理不参与此通道。Noise 握手与帧协议不变。

## 示例二：三个通道全开

```json
{
  "muse": {
    "client_ip": "",
    "worker": {
      "base": "https://your-worker.example.com",
      "ws": true,
      "http": true,
      "login": true,
      "login_origin": "real",
      "ws_permessage_deflate": true
    }
  }
}
```

`base` 必须是 HTTPS origin（无路径、query、用户名或密码；443 端口）。后两个字段可省略，默认值如上。

任意一个 Worker 开关开启时，空 `client_ip` **不会触发 api.ipify.org 等第三方出口探测**。可显式配置 `muse.client_ip` 测试指定值。Worker HTTP/login 使用 `trust_env=False`，不依赖环境中的本地代理。

## 分阶段启用与退回

1. 在测试实例保持现有 Cookie 与凭据文件，只开启 `ws`。挂机观察连接保持、订阅推送、重连、聊天和 Feed/审批；确认所有失败均明确上报，不会偷偷换回原网关。
2. WS 稳定后单独打开 `http`，观察首页取 vm_id、mint/notary 续期。已有缓存可能延迟触发这些请求；测试时使用独立凭据文件，勿删除生产 mint。
3. 打开 `login` **前**，由用户自行更新 Worker 为 `workers/cfproxy.js`；本仓库只交付源码，不自动部署。旧 Worker 不改写 Location/Set-Cookie，不足以支持完整登录流。
4. 再单独打开 `login`，完成测试账号的邮箱验证码登录、Cookie 保存、续期及重启验证。也可用 `tools/login.py --config <测试配置> --output <测试Cookie路径>`；QQ / OneBot / 微信共享的邮箱登录入口同样接收此配置。
5. 要退回旧路由，把 `ws/http/login` 全设为 `false` 后重启实例。若旧配置是 `direct=true`，即恢复直连；若旧配置使用 CONNECT 代理，恢复该代理。不要在操作失败时自动回落。

开关在实例启动时加载，不是热切换。不要删除 `cookies.json`，也不需要因切换模式重新登录。

## Worker 配套实现

- HTTP fetch 使用 `redirect: "manual"`，将 Muse/auth 的绝对、协议相对、相对 Location 改写为 Worker 前缀。桥侧仍保留 `follow_redirects=True`，且在请求发出前拦截逃离 Worker 的跳转。
- 多条 `Set-Cookie` 独立保留，正确处理 Expires 中的逗号。显式 Domain 改为 Worker 主机；Path 映射到对应上游前缀。`.muse.ai; Path=/` 的共享 Cookie 使用 `/https/`，使两个登录前缀都能收到。
- 桥侧域 helper 只接受 Muse 正确域边界，以及 Worker 模式下精确匹配的代理主机；不会误接受 `muse.ai.evil.test`。Worker 登录保存时保留旧 `cookie` 字符串，并附加域统一为 `muse.ai`、路径恢复为上游路径的 `cookies` 记录，直连/Worker 都能读取。
- Worker 转发原始方法/body，WS 文本与二进制不改写；保留子协议选择，传递关闭/错误。不改 Noise。
- 新源码限制目标为 `muse.ai`、`auth.muse.ai`、`hatch.metaaivm.com`；不支持其他上游或 `{vm_id}.metaaivm.com`。跳往其他登录域会明确失败，需另行核实而非直连。
- `__Host-` Cookie 要求无 Domain 且 `Path=/`，与按上游前缀隔离 Cookie 的模型不能等价表达；新 Worker 如遇此类 Cookie 会明确返回 502，不静默丢弃。此情况没有做真实 Cloudflare 联调。

## 待实测确认点

以下都**没有**用本次离线测试替代真实服务端结论：

1. **SSO 的 origin 参数**：默认 `worker.login_origin="real"` 使用真实 `https://muse.ai`；可切为 `"worker"`，使用 `https://your-worker.example.com/https/muse.ai`。代码明确标注待实测。HTTP Origin/Referer 头仍使用真实 Muse origin，避免把带路径的代理 URL 当成 HTTP origin。
2. **空 client_ip 是否接受**：默认保留空值且禁止出口探测；用 `muse.client_ip` 显式指定来对照测试。不推测 Cloudflare 出口 IP、不用本机 IP冒充 Worker 出口。
3. **permessage-deflate 头是否需要摘掉**：默认 `worker.ws_permessage_deflate=true` 保持原头。若 Worker fetch Upgrade 握手异常，先切为 `false`；只影响 Worker WS，直连仍保持原请求头。WS 类仅新增此可选头开关，握手校验与 Noise 未变。

## 离线测试

在仓库根目录运行：

```bash
python -m unittest discover -s tests -p test_worker_routing.py
```

```bash
node --test workers/cfproxy.test.mjs
```

```bash
python -m unittest discover -s tests
```

Python 测试覆盖三开关组合、WS URL/query、TLS SNI/Host、扩展头切换、auth 两种 origin、域接受/拒绝、Cookie 跨模式读取、重定向跟随/逃逸拒绝、Worker 失败无直连重试、禁止出口探测与配置接线。Node 测试覆盖 Location/Set-Cookie 重写、HTTP fetch、WebSocketPair 透传与错误路径。它们不发送真实 Muse/Cloudflare 请求，未自动化真实邮箱 OTP、Cloudflare 运行时或长时间挂机。

**日志提醒**：WS URL 中仍含完整 `auth_token` / `notary_token`。它们会经过用户自有 Cloudflare 账号边缘；Worker 侧请求日志可能记录完整 URL。这是使用该模式已接受的前提，本改造不修改协议或 token query。
