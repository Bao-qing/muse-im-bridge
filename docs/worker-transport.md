# 统一网络代理与 Cloudflare Worker 传输

所有外发 HTTP 与 WebSocket 传输统一由 `muse_bridge/network.py` 收拢管理，所有客户端均强制关闭隐式系统环境变量代理（`trust_env=False` / 显式空 `ProxyHandler({})` / `proxy=None`），仅按配置路由。

## 顶层 `network` 配置

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

- `network.proxy_url`：全局 HTTP(S) 代理地址（如 `http://127.0.0.1:7890`），供选择 `"http"` 模式的模块使用。
- `network.worker.base`：自有 Cloudflare Worker 的 HTTPS origin（443 端口、无路径/query/凭据）。
- `network.worker.ws` / `network.worker.http`：默认均为 `true`，分别控制 Worker 模式下 WebSocket 与 HTTP（含 Muse 登录流与 Telegram Bot API）是否经 Worker 转发。

## 模块 `proxy` 模式

`muse` 与四个适配器（`qq`、`onebot`、`wechat`、`telegram`）均提供 `proxy` 字段，合法值：

| 模式 | 行为 |
| --- | --- |
| `"none"` | 严格直连，忽略系统环境变量代理 |
| `"http"` | 走 `network.proxy_url` 指定的 HTTP(S) 代理（未配置时报错） |
| `"worker"` | 经 `network.worker` 改写为 `{base}/{scheme}/{host}/...` 转发（未配置 `base` 时报错） |

默认值：
- `muse.proxy`：`"worker"`
- `adapters.telegram.proxy`：`"worker"`
- `adapters.qq.proxy` / `adapters.onebot.proxy` / `adapters.wechat.proxy`：`"none"`

说明：
- `muse.client_ip` 留空时，不再在启动加载配置阶段连接第三方公网探测服务，而是在首次向 Muse 发消息时按当前 `muse.proxy` 路由异步请求同源 `GET https://muse.ai/api/client-ip`（3 秒超时并内存缓存，未就绪或失败时省略 `client_ip` 字段）。
- QQ 适配器特有的 `download_proxy`（腾讯附件 CDN URL 前缀改写）属于适配器自身逻辑，不进入 `network` 层。
- Worker 转发不会在失败时静默回退到直连。

## 离线测试

```powershell
python -m unittest discover -s tests -p test_worker_routing.py
```

```powershell
python -m unittest discover -s tests
```
