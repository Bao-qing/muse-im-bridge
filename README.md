# Muse Bridge (面向对象架构)

Muse Bridge 是一个解耦、面向对象的通用虚拟助理消息桥接系统。支持将 Muse 虚拟助理接入到 **QQ 官方机器人**、**OneBot (NapCat v11)**，并为未来接入 **微信 OpenClaw** 预留了统一的架构扩展规范。

---

## 目录结构

```
muse_bridge/
├── main.py                   # 简洁的主程序入口（加载配置、初始化引擎、优雅启停）
├── config.example.json       # 用户配置文件模板（无敏感信息，复制为 config.json）
├── muse_bridge/              # 核心框架包
│   ├── config/               # 配置系统（分清用户凭据与程序默认值，支持覆盖）
│   │   ├── constants.py      # 程序默认常量（超时、限制、时区、域名）
│   │   └── models.py         # 配置模型与深层合并加载器
│   ├── core/                 # 领域核心
│   │   ├── message.py        # 统一消息定义（Inbound/Outbound/Attachment/Markdown分块）
│   │   ├── deduplicator.py   # 3状态消息去重器（防重防漏、支持故障回退）
│   │   └── commands.py       # 通用斜杠指令层（/sessions, /switch, /new 等）
│   ├── muse/                 # Muse 独立业务服务层
│   │   ├── client/           # 底层 Noise_XX 加密协议与 WebSocket 传输客户端
│   │   ├── token_manager.py  # 自动凭证推导与静默续期（cookies -> vm_id -> mint）
│   │   └── service.py        # 会话交互、附件处理、工作区文件读写、主动事件流订阅
│   ├── adapters/             # 适配器体系
│   │   ├── base.py           # BaseAdapter 抽象基类
│   │   ├── qq/               # QQ 官方机器人适配器
│   │   │   ├── adapter.py    # QQAdapter 总控（协调网关与主动推送子组件）
│   │   │   ├── client.py     # QQ OpenAPI 客户端（鉴权、发消息、上传富媒体）
│   │   │   ├── gateway.py    # QQ WebSocket 网关（心跳与事件分发）
│   │   │   ├── push.py       # QQPushService（主动推送子组件：默认推送到第一私聊）
│   │   │   └── ws_transport.py # 兼容 QQ 101 Connection:close 的底层 WS 客户端
│   │   └── onebot/           # OneBot v11 (NapCat) 适配器
│   │       ├── adapter.py    # OneBotAdapter 总控
│   │       ├── client.py     # NapCat HTTP API 客户端（标准 Array 消息段结构）
│   │       └── gateway.py    # NapCat WebSocket 实时事件监听
│   └── engine.py             # BridgeEngine 协调调度引擎
├── tools/
│   └── export_cookies.js     # 网页端 Cookie 一键导出辅助工具
└── tests/
    └── test_bridge.py        # 全新架构单元测试集
```

---

## 核心设计与特性

1. **面向对象领域模型**：
   - 彻底摆脱原有的全局状态字典传递，构建了清晰的 `BridgeEngine`、`MuseService`、`BaseAdapter`、`QQAdapter`、`OneBotAdapter`、`MessageDeduplicator` 等类层次。
2. **QQ Push 归入 QQ 子集统一管理**：
   - 主动推送（`QQPushService`）作为 `QQAdapter` 的子组件统一运行，共享 QQClient 鉴权（无需重复刷新 Token）与 Deduplicator。
   - **推送目标策略**：默认自动推送到白名单中的第一个私人会话（`whitelist.private[0]`），同时支持在配置中显式指定目标（私聊或群聊），并具备候选目标容错能力。
3. **配置文件设计（用户可改 vs 程序默认）**：
   - 用户只需在单一 `config.json`（或 `credentials.json`）中填写自己的凭证与白名单。
   - 所有内部运行参数（如分块字符数、每轮消息上限、图片/文件限制、超时时间等）均有内置默认常量，用户如需调整可在配置文件相应位置或 `overrides` 中覆盖。
   - 敏感信息（AppID, Secret, Token, Cookies, OpenID）绝不硬编码，示例模板均使用标准占位符。
4. **Muse 凭证静默续期**：
   - 配合 `cookies.json`，在启动和运行过程中自动推导 `vm_id` 并从接口换取 `.mint.json` 中的 `token` 与 `notary_token`，在临近过期或遭遇服务端 401/403 握手拒绝时全自动刷新并重连。
5. **消息不漏不重**：
   - 采用完善的 3 状态机模型（`Absent -> Processing -> Delivered / Rollback`），无论是被动回复还是主动推送，均做严格原子标记，杜绝重复回复，遇到错误立即回滚。

---

## 快速开始

### 1. 安装依赖

```bash
pip install -e .
```

### 2. 配置 `config.json`

从模板复制并填写凭据：

```bash
cp config.example.json config.json
```

```json
{
  "muse": {
    "cookies_file": "cookies.json",
    "session_id": "YOUR_MUSE_SESSION_ID",
    "reply_mode": "push"
  },
  "adapters": {
    "qq": {
      "enabled": true,
      "app_id": "YOUR_QQ_APP_ID",
      "app_secret": "YOUR_QQ_APP_SECRET",
      "whitelist": {
        "private": [
          "YOUR_PRIVATE_OPENID"
        ],
        "YOUR_GROUP_OPENID": "*"
      },
      "push": {
        "enabled": true,
        "target_type": "private",
        "target_id": null,
        "catch_up_enabled": false
      }
    },
    "onebot": {
      "enabled": false,
      "base_url": "https://onebot.example.com:81",
      "access_token": "YOUR_TOKEN",
      "ws_url": "wss://onebot.example.com:81",
      "target_group_id": 300000002
    }
  }
}
```

### 3. 配置检查

运行校验模式（仅检查凭证与配置可用性，不连接真实网关）：

```bash
python main.py --check
```

### 4. 启动服务

```bash
python main.py
```

### 5. 运行测试集

```bash
python -m unittest discover tests
```

---

## 全量配置参考 (`config.json`)

系统采用“**必须填写的用户凭据**”与“**有合理默认值的内部参数**”分层设计。以下是所有可用配置项的全量参考（可根据需求选择性配置，未填写的项将自动采用内置默认值）：

```json
{
  "muse": {
    "cookies_file": "cookies.json",                 // [可选] 网页 cookies 路径，用于自动提取 vm_id 和静默续期 token (默认: "cookies.json")
    "mint_file": ".mint.json",                     // [可选] 缓存的 mint token 路径 (默认: ".mint.json")
    "session_id": "YOUR_MUSE_SESSION_ID",          // [必填] Muse 目标会话 UUID
    "vm_id": "",                                   // [可选] 虚拟机 ID，留空则自动从 cookies 或 mint token 解析
    "host": "hatch.metaaivm.com",                  // [可选] Muse 网关域名 (默认: "hatch.metaaivm.com")
    "client_ip": "",                               // [可选] 出口公网 IP，留空则启动时自动探测
    "timezone": "Asia/Shanghai",                   // [可选] 时区 (默认: "Asia/Shanghai")
    "proxy_url": null,                             // [可选] 网络代理 URL，例如 "http://127.0.0.1:7890" (默认: null)
    "direct": true,                                // [可选] 是否直连 (默认: true)
    "reply_mode": "push"                           // [可选] 会话等待模式: "push" (事件流监听) 或 "poll" (历史轮询) (默认: "push")
  },
  "adapters": {
    "qq": {
      "enabled": true,                             // [必填] 是否启用 QQ 官方机器人适配器
      "app_id": "YOUR_QQ_APP_ID",                  // [必填] QQ 机器人 AppID
      "app_secret": "YOUR_QQ_APP_SECRET",          // [必填] QQ 机器人 AppSecret
      "admin_id": [                                // [可选] 管理员 OpenID 列表 (仅管理员可在私聊执行管理指令与 /login 登录)
        "YOUR_ADMIN_OPENID"
      ],
      "whitelist": {                               // [必填] 权限白名单
        "private": [
          "YOUR_C2C_OPENID"                        // 允许私聊机器人的用户 OpenID 列表
        ],
        "YOUR_GROUP_OPENID": "*"                   // 允许互动的群 OpenID: "*" 表示群员全员开放，或配置指定成员 OpenID 列表
      },
      "push": {                                    // [可选] 主动推送子组件配置
        "enabled": true,                           // 是否启用主动推送 (默认: true)
        "target_type": "private",                  // 推送目标类型: "private" (私聊) 或 "group" (群聊) (默认: "private")
        "target_id": null,                         // 推送目标 ID: 留空 null 则默认自动推送到管理员 (admin_id[0]) 或第一私聊
        "catch_up_enabled": false,                 // 启动时是否追赶离线期间产生的历史未推送消息 (默认: false，全量标为已读)
        "catch_up_window_s": 21600,                // 追赶允许的最大离线时间窗口 (秒，默认: 21600 = 6小时)
        "catch_up_max_messages": 5,                // 追赶单次补推的最大消息条数 (默认: 5，防止刷屏)
        "state_file": "qq_push_state.json",        // 推送水位持久化文件路径 (默认: "qq_push_state.json")
        "delivered_file": "qq_delivered.jsonl"     // 已投递消息去重记录文件路径 (默认: "qq_delivered.jsonl")
      },
      "ws_guid": "258EAFA5-E914-47DA-95CA-C5AB0DC85B11", // [可选] QQ Gateway 握手 GUID
      "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36", // [可选] 全局统一 HTTP/WS User-Agent
      "chunk_chars": 1200,                         // [可选] 单条消息文本最大切片字符数 (默认: 1200)
      "max_chunks": 8,                             // [可选] 单轮回复最大允许发送的切片数量 (默认: 8)
      "max_active_chunks": 2,                      // [可选] 被动回复额度耗尽后，允许消耗的主动消息配额条数 (默认: 2)
      "max_image_bytes": 8388608,                  // [可选] 图片下载/上传字节上限 (默认: 8MB)
      "max_file_bytes": 26214400,                  // [可选] 文件下载/上传字节上限 (默认: 25MB)
      "max_video_bytes": 104857600,                // [可选] 视频下载字节上限 (默认: 100MB)
      "max_video_upload_bytes": 10485760,          // [可选] QQ 视频接口单文件上传上限 (默认: 10MB)
      "file_ref_threshold_bytes": 2097152,         // [可选] 大文件切片上传阈值 (默认: 2MB，超量自动转为 file_ref 上传)
      "gateway_dedup_s": 120,                      // [可选] QQ 网关事件去重窗口秒数 (默认: 120)
      "download_timeout_s": 60,                    // [可选] 附件下载超时时间 (秒，默认: 60)
      "download_proxy": "",                        // [可选] 针对受阻域名的专用代理前缀，例如 "https://proxy.example.com/https/"
      "session_state_file": "qq_session.json",     // [可选] /switch 与 /new 命令持久化记录文件路径
      "debug": false                               // [可选] 是否开启极致详尽的调试日志输出 (默认: false)
    },
    "onebot": {
      "enabled": false,                            // [可选] 是否启用 OneBot (NapCat v11) 适配器 (默认: false)
      "base_url": "https://onebot.example.com:81",  // [必填-若启用] OneBot HTTP API 地址
      "access_token": "YOUR_ONEBOT_TOKEN",         // [必填-若启用] OneBot API Bearer Token
      "ws_url": "wss://onebot.example.com:81",     // [必填-若启用] OneBot 正向 WebSocket 事件流地址
      "target_group_id": 300000002,                // [可选] 默认交互/推送的 QQ 群号
      "session_id": "",                            // [可选] 独立绑定的 Muse 会话 ID，留空复用 muse.session_id
      "whitelist": {                               // [可选] OneBot 交互白名单
        "private": [],                             // 允许私聊的用户 QQ 号列表
        "groups": [300000002]                      // 允许群聊的群号列表 ("*" 表示全员开放)
      }
    }
  },
  "gateway": {                                     // [可选] 跨通道与网关协同配置
    "login_through_adapter": "qq"                  // 允许通过指定适配器进行登录认证 (如 "qq"，管理员可在私聊发送 /login <邮箱> 登录)
  },
  "overrides": {                                   // [可选] 全局顶层参数覆盖字典 (优先级最高)
    "chunk_chars": 1200,
    "timezone": "Asia/Shanghai"
  }
}
```
