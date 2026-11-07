# Muse im Bridge

Muse im Bridge 是一 Muse 助理消息桥接系统。支持将 Muse 虚拟助理接入到 **QQ 官方机器人**、**OneBot (推荐 NapCat )**，并将在未来接入 **微信 OpenClaw** 。

> **非官方项目**：本项目与 Muse、腾讯 QQ 无隶属关系，接入方式依赖对网页端协议的逆向分析，可能违反目标平台的服务条款，也可能随服务端更新而失效，使用风险自负。

---

## 目录结构

```
muse_bridge/
├── main.py                   # 主程序入口（加载配置、初始化引擎、优雅启停）
├── config.example.json       # 用户配置文件模板（无敏感信息，复制为 config.json）
├── muse_bridge/              # 核心框架包
│   ├── config/               # 配置系统（分清用户凭据与程序默认值，支持覆盖）
│   │   ├── constants.py      # 程序默认常量（超时、限制、时区、域名）
│   │   └── models.py         # 配置模型与深层合并加载器
│   ├── core/                 # 领域核心
│   │   ├── message.py        # 统一消息定义（Inbound/Outbound/Attachment/Markdown分块）
│   │   ├── deduplicator.py   # 3状态消息去重
│   │   └── commands.py       # 通用斜杠指令层（/sessions, /switch, /new 等）
│   ├── muse/                 # Muse 独立业务服务层
│   │   ├── client/           # 底层 Noise_XX 加密协议与 WebSocket 传输客户端
│   │   ├── token_manager.py  # 自动凭证推导与静默续期（cookies -> vm_id -> mint）
│   │   └── service.py        # 会话交互、附件处理、工作区文件读写、主动事件流订阅
│   ├── gateway/              # 与平台无关的 Gateway 层（对接 Muse）
│   │   ├── chat.py           # ChatGateway：会话映射、斜杠命令、登录/凭证/审批输入、入站流水线
│   │   ├── outbound.py       # OutboundDispatcher：订阅 Muse 事件、路由、审批卡投递（平台只实现 _deliver）
│   │   ├── interactions.py   # 登录、凭证、审批数字回复的输入路由
│   │   ├── access.py         # 白名单申请与本地审批
│   │   └── routes.py         # Muse 消息 ID -> 聊天目的地
│   ├── adapters/             # 平台适配器（只负责解析事件与调用平台 API）
│   │   ├── base.py           # BaseAdapter 抽象基类
│   │   ├── qq/               # QQ 官方机器人适配器
│   │   │   ├── adapter.py    # QQAdapter：解析 QQ 事件、被动/主动回复
│   │   │   ├── client.py     # QQ OpenAPI 客户端（鉴权、发消息、上传富媒体）
│   │   │   ├── gateway.py    # QQ WebSocket 事件源（心跳与事件分发）
│   │   │   ├── outbound.py   # QQOutboundPusher：被动回复、主动配额、富媒体上传
│   │   │   └── ws_transport.py # 兼容 QQ 101 Connection:close 的底层 WS 客户端
│   │   └── onebot/           # OneBot v11 (NapCat) 适配器
│   │       ├── adapter.py    # OneBotAdapter：消息段解析、群触发策略、自身/重复过滤
│   │       ├── client.py     # NapCat HTTP API 客户端（Array 消息段、retcode 校验）
│   │       ├── gateway.py    # NapCat 正向 WebSocket 事件源（退避重连）
│   │       └── outbound.py   # OneBotOutboundPusher：文本分段、base64 图片/视频、文件上传
│   └── engine.py             # BridgeEngine 协调调度引擎
├── tools/
│   └── export_cookies.js     # 网页端 Cookie 一键导出辅助工具
└── tests/
    └── test_bridge.py       
```

---

## 核心设计与特性


---

## 快速开始

环境要求：Python 3.11 或更高版本。

### 1. 安装依赖

```bash
pip install -e .
```

### 2. 初始化配置

**方式一：交互式首次配置向导（推荐）**

直接运行向导脚本，按提示输入各渠道基础凭据（支持 QQ / OneBot），随后在任一渠道发送一条消息，脚本将自动捕获您的身份将其设为管理员、将该渠道设为默认渠道，并自动生成最小 `config.json`：

```bash
python init_config.py
# 或
python main.py --setup
```

**方式二：手动从模板复制**

从模板复制并手动填写凭据：

```bash
cp config.example.json config.json
```

```json
{
  "muse": {
    "cookies_file": "cookies.json",
    "session_id": "YOUR_MUSE_SESSION_ID"
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
      }
    },
    "onebot": {
      "enabled": false,
      "base_url": "https://onebot.example.com",
      "access_token": "YOUR_ONEBOT_TOKEN",
      "ws_url": "wss://onebot.example.com",
      "admin_id": ["YOUR_ADMIN_QQ"],
      "whitelist": {
        "private": ["YOUR_ADMIN_QQ"],
        "YOUR_GROUP_ID": "*"
      }
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
    "session_id": "YOUR_MUSE_SESSION_ID",          // [可选] 主会话 UUID，仅用于其审批卡推送给管理员；QQ 与 OneBot 都按聊天自动建会话，不使用此项
    "vm_id": "",                                   // [可选] 虚拟机 ID，留空则自动从 cookies 或 mint token 解析
    "host": "hatch.metaaivm.com",                  // [可选] Muse 网关域名 (默认: "hatch.metaaivm.com")
    "client_ip": "",                               // [可选] 出口公网 IP，留空则启动时自动探测
    "timezone": "Asia/Shanghai",                   // [可选] 时区 (默认: "Asia/Shanghai")
    "proxy_url": null,                             // [可选] 网络代理 URL，例如 "http://127.0.0.1:7890" (默认: null)
    "direct": true,                                // [可选] 是否直连 (默认: true)
    "approval": {                                // [可选] 原生审批策略，不改变长期权限规则
      "mode": "manual",                         // manual / allow_all / deny_all
      "notify": true                              // 自动决定后通知，默认开启
    }
  },
  "adapters": {
    "qq": {
      "enabled": true,                             // [必填] 是否启用 QQ 官方机器人适配器
      "app_id": "YOUR_QQ_APP_ID",                  // [必填] QQ 机器人 AppID
      "app_secret": "YOUR_QQ_APP_SECRET",          // [必填] QQ 机器人 AppSecret
      "admin_id": [                                // [可选] 管理员 OpenID 列表
        "YOUR_ADMIN_OPENID"
      ],
      "admin_only": true,                          // [可选] 会话管理指令的管理员限制 (默认: true)
      "credentials_in_place": true,                // [可选] 允许管理员在凭证所在群填写；群成员可见输入，设 false 则只能私聊填写 (默认: true)
      "approvals_in_place": true,                  // [可选] 审批卡只推送到会话所在聊天，不另发管理员 (默认: true)
      "allow_all_private": false,                  // [可选] 是否放开所有私聊访问（默认: false）
      "allow_all_group": false,                    // [可选] 是否放开所有群聊互动（默认: false）
      "whitelist": {                               // [必填] 权限白名单
        "private": [
          "YOUR_C2C_OPENID"                        // 允许私聊机器人的用户 OpenID 列表
        ],
        "YOUR_GROUP_OPENID": "*"                   // 允许互动的群 OpenID: "*" 表示群员全员开放，或配置指定成员 OpenID 列表
      },
      "session_map_file": "data/qq_session_map.json", // [可选] 群/私聊 -> Muse 会话映射文件 (默认: "data/qq_session_map.json")
      "session_bindings": {},                      // [可选] 预置绑定，如 {"group:<group_openid>": "<session_id>"}
      "delivered_file": "data/qq_delivered.jsonl", // [可选] 已投递消息去重记录文件路径 (默认: "data/qq_delivered.jsonl")
      "push_state_file": "data/qq_push_state.json", // [可选] 推送水位持久化文件路径 (默认: "data/qq_push_state.json")
      "catch_up_enabled": false,                   // [可选] 启动时是否补推离线期间产生的未推送消息 (默认: false，全量标为已读)
      "catch_up_window_s": 21600,                  // [可选] 补推允许的最大离线时间窗口 (秒，默认: 21600 = 6小时)
      "catch_up_max_messages": 5,                  // [可选] 单次补推的最大消息条数 (默认: 5，防止刷屏)
      "recheck_sleep_s": 5,                        // [可选] 会话空闲时的出站复查间隔 (秒，默认: 5)
      "in_flight_ttl_s": 300,                      // [可选] 发送中消息的去重锁有效期 (秒，默认: 300)
      "processing_ttl_s": 300,                     // [可选] 处理中消息的去重锁有效期 (秒，默认: 300)
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
      "debug": false                               // [可选] 旧兼容开关，仅在 logging.level 未指定时选用 DEBUG (默认: false)
    },
    "onebot": {
      "enabled": false,                            // [可选] 是否启用 OneBot (NapCat v11) 适配器 (默认: false)
      "base_url": "https://onebot.example.com",    // [必填-若启用] OneBot HTTP API 地址
      "access_token": "YOUR_ONEBOT_TOKEN",         // [必填-若启用] OneBot API Bearer Token
      "ws_url": "wss://onebot.example.com",        // [必填-若启用] OneBot 正向 WebSocket 事件流地址
                                                   // access_token / base_url / ws_url 也可用环境变量
                                                   // ONEBOT_ACCESS_TOKEN / ONEBOT_BASE_URL / ONEBOT_WS_URL 提供；ws_url 留空时由 base_url 推出
      "admin_id": ["YOUR_ADMIN_QQ"],               // [可选] 管理员 QQ 号（审批、凭证、/login、管理指令）
      "group_trigger": "all",                      // [可选] 群消息触发：all 全部 / at 需 @机器人或 / 指令 / prefix 仅 / 指令 (默认: all)
      "whitelist": {                               // [必填-若启用] 与 QQ 同格式；为空时拒绝所有聊天
        "private": ["YOUR_ADMIN_QQ"],              // 允许私聊的 QQ 号
        "YOUR_GROUP_ID": "*"                       // 群号: "*" 全员开放，或成员 QQ 号列表；旧写法 "groups": [群号] 仍兼容
      }
      // 其余字段与 adapters.qq 相同：admin_only、approvals_in_place、credentials_in_place、
      // unbound_push_to_admin、access_requests、session_bindings、catch_up_*、chunk_chars、max_*_bytes 等；
      // 状态文件默认 data/onebot_*.json，与 QQ 分开
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

## 日志与 JSONL 追踪

在配置文件顶层增加 `logging`（完整示例见 `config.example.json`）：
警告！：开启追踪会生成大量日志，可能影响性能和磁盘空间，日志已脱敏，但仍包含QQ消息等隐私信息，请妥善管理，谨慎使用！
```json
{
  "logging": {
    "level": "INFO",
    "console": true,
    "file": "logs/bridge.log",
    "trace": false,                 // [可选] 是否开启 JSONL 追踪 (默认: false)
    "trace_file": "logs/trace.jsonl",
    "payload_limit": 65536,
    "max_bytes": 10485760,
    "backup_count": 5
  }
}
```

- **等级优先级**：显式 `logging.level` 优先，支持 `TRACE` / `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` / `OFF`（大小写不敏感）。未指定等级时，旧顶层 `debug`、QQ `debug` 或环境变量 `QQ_DEBUG` 开启则使用 `DEBUG`，否则 `INFO`；旧字段保留。显式 `INFO` 或 `OFF` 不会被旧 debug 标志覆盖。
- **目的地**：`console` 控制 stderr 运行日志，`file` 控制文本运行日志；`file: ""` 或 `null` 禁用文件输出。`trace: true` 单独开启 JSONL 追踪，不会因 `DEBUG` / `TRACE` 等级自动打开；`OFF` 同时关闭运行日志与追踪。开启追踪时 `trace_file` 必须非空且不能与 `file` 指向同一路径；未开启时可设为空或 `null`。相对路径基于进程工作目录。
- **JSONL 模式**：每行一个 JSON 对象，包含 schema 版本、UTC 时间、单调时钟、运行 ID、顺序号、组件/事件，以及可用的关联 ID、耗时、状态与脱敏 payload。用于串联诊断事件与 HTTP 请求/响应，不改变请求、重试或业务行为，也不主动消费尚未读完的流。运行日志与 JSONL 不混写。
- **大小与保留**：`payload_limit` 为单事件脱敏后 payload 的 UTF-8 字节限制，默认 65536；超出时记录截断预览、长度与摘要。`0` 表示不限，**不是关闭脱敏**；大消息会增加隐私暴露面、磁盘占用和写入压力，通常不建议长期使用。`max_bytes` 必须为正整数（默认 10 MiB），`backup_count` 为至少 1 的整数（默认 5）；两个文件分别按大小轮转，保留当前文件与最多 5 个备份（`.1` 至 `.5`），不是按天保留，单条大记录可能超过阈值。开关必须为 JSON 布尔值，数字字段不接受字符串、浮点数或布尔值。
- **隐私与重放限制**：认证请求、Token、Cookie、Authorization、密码、验证码和凭证授权等会被遮蔽，低层交互敏感输入/消息正文也受保护；二进制或无法安全解释的内容仅保留省略标记、长度或摘要。日志不是完整抓包，不能依靠它原样重放认证、凭证、敏感输入或二进制请求。脱敏不能保证识别任意业务字段中的个人信息；即使截断/脱敏后也应限制访问、及时清理，分享前人工检查。不要记录完整配置或真实凭据，也不要提交日志；默认目录及轮转文件已加入 `.gitignore`。

`main.py` 在加载配置前先启用不创建文件的安全默认日志，加载完成后再应用完整配置；加载中的出口 IP 探测行为保持不变。`--check` 的业务输出、CLI 用户提示和交互 `input()` 不受日志开关影响。独立登录工具同样读取日志配置，原有 `--email` / `--output` 行为不变：

```bash
python tools/login.py --config config.json --output cookies.json
```

临时诊断可将 `trace` 改为 `true`，排查后关闭并按需删除本地日志；日志目录不可写等日志故障不应阻止认证或消息投递。不要为排查而把真实 Cookie、Token、验证码或密码写进示例配置、日志消息或测试。

## 白名单与权限管理

系统支持细粒度白名单、动态申请审批，以及全局放开模式（QQ 与 OneBot 通用）：

### 全局放开选项

在对应适配器（`adapters.qq` 或 `adapters.onebot`）中配置：

- `allow_all_private: true`：允许任意用户通过私聊与机器人互动，跳过私聊白名单检查。
- `allow_all_group: true`：允许任意群聊及其全体成员与机器人互动，跳过群白名单检查。
- 两项默认均为 `false`，此时严格执行静态 `whitelist` 及动态申请授权。

### 白名单申请

新私聊或新群无需先加入白名单即可向机器人发送申请；普通消息仍不会转发。

- `/申请`：申请当前用户的私聊访问，或在当前群内仅允许申请人使用。
- `/申请 全员`：申请将当前群对所有成员开放，不影响其他群或私聊。已能使用的成员也可申请全员升级。
- 管理员收到私聊通知后，发送 `/申请审批 编号 同意` 或 `/申请审批 编号 拒绝`；单独发送 `/申请审批` 查看待处理申请。只允许配置中的管理员在私聊审批。
- 批准后立即生效并通知原聊天；拒绝不改变已有权限。不会授予管理员权限，也不会补发此前被拦截的消息。

在 `adapters.qq` 中配置：

```json
"access_requests": {
  "enabled": true,
  "auto_approve": false,
  "state_file": "data/qq_access.local.json"
}
```

默认人工审批。显式设置 `auto_approve: true` 后，新申请会直接通过并加入有效白名单，包括 `/申请 全员` 的整群开放；这意味着任何能向机器人发送申请的新群都可自行开通。开启前请确认这是期望的访问策略。切换自动模式不会自动批准已有待办。`enabled: false` 停止接收新申请，已有授权和管理员处理待办仍可用。

动态授权与申请记录原子保存到 `state_file`，与配置中的静态白名单合并生效，不改写含密钥的配置文件。删除状态文件会丢失动态授权；修改静态白名单不会撤销已批准的动态授权。文件损坏或不属于当前 QQ 应用时拒绝启动，避免静默重置权限。单实例使用该文件，不支持多个进程同时写入。运行状态文件无需提交。

### 运行状态文件（`data/`）

以下运行状态文件默认写入项目根目录的 `data/`（可各自在配置中改写路径），目录在首次写入时自动创建：

| 文件 | 内容 | 配置项 |
| :--- | :--- | :--- |
| `data/qq_access.local.json` | 动态白名单授权与申请记录 | `adapters.qq.access_requests.state_file` |
| `data/qq_delivered.jsonl` | 已投递消息去重记录（追加写入） | `adapters.qq.delivered_file` |
| `data/qq_push_state.json` | 主动推送水位与已见消息 | `adapters.qq.push_state_file` |
| `data/qq_routes.json` | 消息 ID -> QQ 目标路由（重启后仍可被动回复） | `adapters.qq.routes_file` |
| `data/qq_session_map.json` | 群/私聊到 Muse 会话的映射 | `adapters.qq.session_map_file` |
| `data/onebot_*.json(l)` | OneBot 的同类文件（access、delivered、push_state、routes、session_map） | `adapters.onebot.*_file` |

`data/` 已在 `.gitignore` 中忽略。从旧版升级时，将根目录下的同名文件移入 `data/` 即可保留原有状态（不移动则按空状态重新开始）。

重复待办合并；每人新申请间隔至少一分钟，已处理申请重提需等待十分钟，待办七天过期。管理员通知失败时保留待办，可稍后重发申请或由管理员查询；通知和授权结果不等同 QQ 一定送达。

申请只开通聊天访问；获批的群/私聊同样拥有独立的 Muse 会话。登录、凭证、Muse 原生审批仍沿用各自管理员权限。

## 登录、凭证与审批交互

三类交互共用临时状态与身份校验，但不混用协议：

- **登录**：管理员私聊发送 `/login` 或 `/login 邮箱`，再输入验证码。直接发送邮箱不再触发登录。`admin_only: false` 不会放开登录、凭证或审批的管理员权限。
- **凭证**：回复凭证卡即可填写；账号和密码可一次分两行发送，信息不足时只询问缺少的一项。也支持 `/凭证 编号 [保存|一次]` 后连续输入。仅有一张可用卡时可直接发送多行；有多张时先回复对应卡。验证码卡只需回复验证码。单字段密钥卡（创建密码、API key、SSH 私钥、OTP 种子等）只需引用卡片发送完整内容，无需虚构账号，支持整段多行私钥；也可先 `/凭证 编号 [保存|一次]` 再发送。账号/密码卡可在第一行发送账号、后续各行发送多行密钥。保存成功后自动返回聊天，无需 `/chat`。第三方登录仍在 Muse 网页完成。
- **原处填写**：`credentials_in_place`（QQ 与 OneBot 均默认 `true`）允许管理员在凭证所在群内填写，回复凭证卡分两行发账号和密码即可；设为 `false` 则群内提示「请私聊填写此凭证」，需管理员私聊填写。群内输入对群成员可见。不会放开普通成员权限，也不能填写其他群的凭证。
- **引用识别**：仅关联机器人实际发送成功的消息；不凭引用文字猜测卡片。服务重启后旧消息的引用关联不保留，可发送 `/凭证` 查看当前可填写的卡片，再按编号填写。
- **审批**：按当前卡实际提供的原生选项逐项展示，保留长期允许范围及顺序；可直接回复数字，或使用 `/审批 编号 选项数字`。管理员私聊可查看管理中的所有会话；群聊只能操作本群绑定会话。
- **审批排队**：Muse 同时发起多个审批时，每个会话按请求时间排队，一次只推送一张卡，卡头显示 `【审批 编号】序号/总数`；处理完当前卡后，结果与下一张卡合并为一条消息发出，全部完成后提示可继续聊天。回复 `数字 all`（或 `/审批 all 数字`）对当前已显示队列全部使用该选项：按决定类型与长期范围匹配各卡的同义选项，没有相同选项的卡保留待审批；之后新到的审批不受影响。管理员私聊同时有多个会话待审批时，需用 `/审批 编号 数字` 指定。
- **原位推送审批**：默认 `adapters.qq.approvals_in_place: true`，审批卡只推送到会话所在的聊天，不再另发管理员私聊；找不到所在聊天或发送失败时才退回发给管理员。设为 `false` 则会话聊天与管理员私聊都会收到。
- **门禁**：对应 Muse 会话未结束的审批会拦截 QQ、OneBot 的普通发送；消息直接丢弃，不排队、不在审批后补发。QQ 再次返回审批卡。审批内容/版本变化后必须重新选择。
- **退出**：`/cancel`（流程进行中也可直接发“取消”/“退出”）结束本地登录或凭证输入并清除临时秘密，不会取消远端审批；登录完成、凭证保存成功会自动退出。临时输入约五分钟过期，过期后五分钟内的第一条消息会被拦截一次（防止迟到的密码或验证码被转发），之后恢复正常转发。
- **转发规则**：只有以下情况才进入本地处理：斜杠命令、正在进行的登录/凭证输入、引用凭证卡、仅有一张待填凭证卡时的多行消息、当前会话有待审批。其余消息（包括六位数字、“取消”等普通文字）一律直接转发给 Muse。

IM 私聊不等同网页安全输入：密码、验证码和私钥等秘密仍经过 IM 链路，群内发送对群成员可见；敏感密钥建议管理员私聊填写。交互正文在日志前消费；不持久化临时密码、验证码、私钥或卡片授权。凭证提交使用固定幂等键；结果不确定时仅接受相同输入重试。审批提交结果不确定时先查询远端，不自动重复决定。

在 `muse` 中配置：

```json
"approval": {
  "mode": "manual",
  "notify": true
}
```

- `manual`：用户选择；`allow_all`：对每个待审批请求选择单次允许；`deny_all`：逐个拒绝。自动模式不创建长期允许/拒绝规则。
- `notify` 控制自动决定后的通知，默认开启。实际生产配置不会随示例或测试自动切换模式。
- 原生 `decision_options` 存在时（即使为空）为权威列表，不从标签、域名或排他标记猜测范围。仅在该字段缺失且请求类型受支持时，采用网页源码已验证的旧版回退规则；无法验证选项时保留门禁并引导网页处理。若只提供长期权限选项，自动策略不擅自创建长期规则，而是交回人工选择。
- 单字段卡按明确的 capture 类型提交受保护字段，不根据标题猜测字段类型；未知类型、字段与卡片 URL 冲突时仍回退网页。旧版 `create_password` 卡允许不带 `capture_authorization`（与已验证的 Authd 行为一致），其他卡仍要求授权；所有提交都带所属会话 ID。
- 凭证 capture 返回有效 ID 才记录完成；网页成功回调也只更新本地展示，不额外猜测远端卡片完成接口。桥接在 `cookies_file + .captures.local.json` 中原子保存目标/会话/卡片/授权上下文的 SHA-256 回执，以防重启重开已成功提交的同一卡片。文件不含账号、密码、验证码、授权原文或 capture ID；删除该回执文件会丢失本地完成去重，损坏或不可读时拒绝启动。远端 `credential_capture_completed` 状态仍以服务器事件为准。
- 审批使用独立 Sentinel 状态协调（约三秒一次，发送前再次核对）；聊天出站仍为事件驱动。尚未把未证实的 `approvals.snapshot` 订阅来源写死。重启从远端恢复待审批，状态查询失败时不放行普通消息。

## 会话隔离

QQ 与 OneBot 适配器中每个群、每个私聊用户各自绑定一个 Muse 会话，互不可见（两个平台的映射文件分开，互不串号）。需要多个聊天共用同一会话时，在各聊天中 `/switch` 到同一个会话 ID，或用 `session_bindings` 预置。

相关选项：

- `session_map_file`（默认 `data/qq_session_map.json`）：`群/私聊 -> Muse session_id` 映射，重启后保持。
- `session_bindings`：预置绑定，键为 `group:<group_openid>` 或 `private:<user_openid>`，值为已有的 Muse `session_id`。
- `session_hot_window_s` / `session_sweep_s`：出站检测参数（见下）。
- `unbound_push_to_admin`（默认 `true`）：会话没有绑定聊天时，主动消息是否私聊发给第一个管理员。设为 `false` 后这类消息直接丢弃（审批卡、凭证卡等仍会发给管理员，因为需要管理员操作）。

出站消息的投递顺序：

1. 有对应提问的回复，被动回复到提问所在的群/私聊。
2. 提醒等无对应提问的主动消息，推到该会话绑定的群/私聊（多个聊天共用时取映射中的第一个）。群主动消息被 QQ 拒绝时改发管理员私聊。
3. 会话没有绑定任何聊天时，私聊发给第一个管理员（`admin_id[0]`）。
4. 未配置管理员，或设置了 `unbound_push_to_admin: false` 时丢弃并记录 warning 日志，不会发给白名单中的其他用户。

行为说明：

- 首次收到某个群/私聊的消息时自动新建会话（新 UUID）；`/new`、`/switch` 只改变发指令的这个群/私聊。
- 出站不会轮询所有会话历史：每轮只调用一次 `/api/session/list`，按 `updated_at_ms` 变化判断哪个会话有新活动（等价于网页端的 `sessions.updated` 事件），只拉取有变化或刚发过消息的会话历史。
- 运行中切换到已有会话时，会先把该会话旧消息标记为已读，不会回放历史。

## 双向消息引用与 Muse 反应

- **渠道 → Muse**：引用已关联的消息时，桥接发送 Muse 原生 `in_reply_to`；引用展示卡时发送 `reply_to_target`。账号、群/私聊及当前 Muse 会话分别隔离，切换会话后不会误引用旧会话。登录和凭证输入先由本地交互处理，不进入普通消息引用索引。
- **Muse → 渠道**：官方 QQ 使用 `message_reference.message_id`，优先选择不透明的 `REFIDX`；OneBot 使用 Array `reply` 段。原生引用与 QQ 被动回复授权 `msg_id` / `msg_seq` 分开处理。每个成功发送的文本分段、图片和视频都记录消息 ID。
- **仅原消息查不到时降级**：当前聊天没有对应原消息时，用文字标明「原消息在此聊天不可用」，不会拿另一个群或会话的文本代替。原生引用 API 报错不会改发文字引用；引用导致 Muse 返回查无消息时，也不会自动重建会话或重发。
- **文件**：OneBot 的 `upload_*_file` 不返回可引用的聊天消息 ID，因此附发一条可原生引用的「【文件】文件名」说明。回复该说明可关联到原来的 Muse 展示卡；不把文件 ID 冒充消息 ID。
- **点赞及其他反应**：同时处理 Muse 历史的 `reaction_emoji` 和实时 `reactions.updated`，将新增/取消反应转换为引用原消息的「【Muse 反应】」/「【Muse 取消反应】」消息。反应独立于原消息投递去重；启动基线不重放旧反应，重复和乱序的实时更新不重复推送。
- **持久化**：引用索引和反应状态保存在 `<routes_file>.references.local.json`，最多保留各 4096 条记录及每条 240 字符的普通消息摘要。官方 QQ 按应用隔离，OneBot 按接口地址及登录账号隔离；文件不保存凭证输入或完整原始事件，应与其他本地状态一样妥善保管，不提交仓库。

## OneBot (NapCat) 适配器

OneBot 与 QQ 走同一个 Gateway 层（`muse_bridge/gateway/`），会话隔离、斜杠命令、`admin_only`、白名单申请、登录/凭证/审批、主动推送的行为与 QQ 一致；适配器只负责把 NapCat 事件转成统一消息、调用 NapCat API 发出。

- **连接**：HTTP API（`base_url`）发消息，正向 WebSocket（`ws_url`，留空则由 `base_url` 推出）收事件。WS 断开后按 1s、2s、4s…最长 30s 退避重连。证书不校验（兼容自签证书）。请求带自定义 User-Agent，Cloudflare 会拒绝 Python 默认 UA（错误 1010）。
- **消息格式**：按 Array 消息段解析 `text`、`at`、`reply`、`image`、`video`、`file`、`record`；`message_format: string`（CQ 码）只取文本、@ 与引用。
- **过滤**：丢弃机器人自己发的消息（`self_id`）与 `message_sent` 事件；按 `message_id` 去重。
- **群触发**：`group_trigger` 为 `all`（默认，群内所有消息）、`at`（需 @机器人，或以 `/` 开头的指令）、`prefix`（只处理 `/` 指令）。私聊不受影响。
- **回复与推送**：OneBot 没有被动回复窗口，回复和主动推送都直接发送，投递顺序同上（提问所在聊天 → 会话绑定的聊天 → 管理员 → 丢弃）。长文本按 `chunk_chars` / `max_chunks` 分段；图片、视频以 `base64://` 消息段发送，文件走 `upload_group_file` / `upload_private_file`。群发送被 NapCat 拒绝（`retcode != 0`，如被禁言、被移出群）时，加「【群消息提醒】」前缀改发第一个管理员私聊；网络超时不会改发，避免重复。
- **收图与文件**：图片段自带可下载的 QQ CDN 地址（含 rkey），直接下载。文件段只有 `file_id`，依次尝试 `get_file`（base64 或 http 地址）与 `get_group_file_url`；NapCat 只给出它本机路径时无法读取，Muse 收到的消息会附「[file下载失败：NapCat 未提供可下载地址]」。
- **凭证引用**：回复（引用）凭证提示消息即可选中该凭证，等同 QQ 的引用回复。
- **白名单**：与 QQ 同格式，QQ 号、群号写成字符串或数字均可；为空时拒绝所有聊天（旧版 OneBot 原型为空即放行，已改）。旧写法 `"groups": [群号]` 等价于这些群 `"*"`。
- **审批卡与登录**：审批卡只推送到拥有该会话的适配器；没有任何聊天绑定的会话（如 `muse.session_id`）才推送到所有适配器的管理员。`gateway.login_through_adapter` 设为 `"onebot"` 时，凭据失效的登录提醒发给 OneBot 管理员。

## 安全与免责声明

- 本项目为非官方工具，与 Muse、腾讯 QQ 官方无关。Muse 接入依赖对网页端协议的逆向分析，可能随服务端更新而失效，也可能违反其服务条款；请只使用自己的账号与机器人，并自行承担使用风险。
- 仓库不包含任何真实凭据。`config.json`、`cookies.json`、`.mint.json`、`.env*`、`data/`、`logs/` 等均已加入 `.gitignore`。提交前请再次确认没有把 Cookie、Token、OpenID、QQ 号或私有服务器地址带入版本库。
- 运行日志与 `data/` 下的状态文件可能包含聊天摘要和消息 ID，请按敏感数据处理，不要公开上传。
- 发现安全问题时请通过 GitHub Security Advisories 私下报告，不要在公开 Issue 中附带凭据或可复现的账号信息。

## 贡献

欢迎提交 Issue 和 Pull Request。提交前请确保 `python -m unittest discover tests` 全部通过，并确认改动没有引入真实凭据或个人数据。

## 许可证

本项目基于 [MIT License](LICENSE) 开源。
