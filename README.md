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
   - 配合 `cookies.json`，在启动和运行过程中自动推导 `vm_id` 并从接口换取 `.mint.json` 中的 `token` 与 `notary_token`。按 JWT 的 `exp`（若有）与 notary 的 `timeout` 中较早时间提前续期，持久订阅也会轮换连接；JWT 没有 `exp` 时不猜测固定寿命。
   - 网关拒绝运行凭据时先用现有 Cookie 刷新；网络超时、5xx、普通 403、缓存写入失败不会把 Cookie 标为过期。`hatch_vm_auth_no_match` 会为自动解析的 VM 重新定位一次，不覆盖显式配置的 VM。
   - 只有 Cookie 缺失，或 Cookie 支持的 mint 接口确认会话失效（排除 VM 认证错误的 401）才要求管理员 `/login`；后台不会弹终端登录提示或自行发送验证码。替换 Cookie 内容后自动恢复尝试，无需删除 mint。
   - 临时刷新失败短暂退避后重试，并发恢复复用已更新的凭据；可能已发出的消息、凭证与审批不会因认证恢复而自动重放。
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
      "base_url": "https://onebot.example.com",
      "access_token": "YOUR_ONEBOT_TOKEN",
      "ws_url": "wss://onebot.example.com",
      "target_group_id": null
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
    "reply_mode": "push",                          // [可选] 会话等待模式: "push" (事件流监听) 或 "poll" (历史轮询) (默认: "push")
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
      "credentials_in_place": false,               // [可选] 允许管理员在凭证所在群填写；群成员可见输入 (默认: false)
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
        "state_file": "data/qq_push_state.json",        // 推送水位持久化文件路径 (默认: "data/qq_push_state.json")
        "delivered_file": "data/qq_delivered.jsonl"     // 已投递消息去重记录文件路径 (默认: "data/qq_delivered.jsonl")
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
      "session_state_file": "data/qq_session.json", // [可选] /switch 与 /new 命令持久化记录文件路径 (默认: "data/qq_session.json")
      "debug": false                               // [可选] 旧兼容开关，仅在 logging.level 未指定时选用 DEBUG (默认: false)
    },
    "onebot": {
      "enabled": false,                            // [可选] 是否启用 OneBot (NapCat v11) 适配器 (默认: false)
      "base_url": "https://onebot.example.com",    // [必填-若启用] OneBot HTTP API 地址
      "access_token": "YOUR_ONEBOT_TOKEN",         // [必填-若启用] OneBot API Bearer Token
      "ws_url": "wss://onebot.example.com",        // [必填-若启用] OneBot 正向 WebSocket 事件流地址
      "target_group_id": null,                     // [可选] 默认交互/推送的 QQ 群号
      "session_id": "",                            // [可选] 独立绑定的 Muse 会话 ID，留空复用 muse.session_id
      "whitelist": {                               // [可选] OneBot 交互白名单
        "private": [],                             // 允许私聊的用户 QQ 号列表
        "groups": []                               // 允许群聊的群号列表 ("*" 表示全员开放)
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

## 日志与 JSONL 追踪

在配置文件顶层增加 `logging`（完整示例见 `config.example.json`）：

```json
{
  "logging": {
    "level": "INFO",
    "console": true,
    "file": "logs/bridge.log",
    "trace": false,
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

## 白名单申请

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
| `data/qq_delivered.jsonl` | 已投递消息去重记录（追加写入） | `adapters.qq.delivered_file` / `adapters.qq.push.delivered_file` |
| `data/qq_push_state.json` | 主动推送水位与已见消息 | `adapters.qq.push.state_file` |
| `data/qq_routes.json` | 消息 ID -> QQ 目标路由（重启后仍可被动回复） | `RouteManager` 默认值 |
| `data/qq_session.json` | `global` 模式下的当前 Muse 会话 | `adapters.qq.session_state_file` |
| `data/qq_session_map.json` | `per_chat` 模式下群/私聊到会话的映射 | `adapters.qq.session_map_file` |

`data/` 已在 `.gitignore` 中忽略。从旧版升级时，将根目录下的同名文件移入 `data/` 即可保留原有状态（不移动则按空状态重新开始）。

重复待办合并；每人新申请间隔至少一分钟，已处理申请重提需等待十分钟，待办七天过期。管理员通知失败时保留待办，可稍后重发申请或由管理员查询；通知和授权结果不等同 QQ 一定送达。

申请只开通聊天访问，不改变 `session_mode`：`global` 仍共享 Muse 会话，需群间隔离时使用 `per_chat`。登录、凭证、Muse 原生审批仍沿用各自管理员权限。

## 登录、凭证与审批交互

三类交互共用临时状态与身份校验，但不混用协议：

- **登录**：管理员私聊发送 `/login` 或 `/login 邮箱`，再输入验证码。直接发送邮箱不再触发登录。`admin_only: false` 不会放开登录、凭证或审批的管理员权限。
- **凭证**：回复凭证卡即可填写；账号和密码可一次分两行发送，信息不足时只询问缺少的一项。也支持 `/凭证 编号 [保存|一次]` 后连续输入。仅有一张可用卡时可直接发送两行；有多张时先回复对应卡。验证码卡只需回复验证码。保存成功后自动返回聊天，无需 `/chat`。第三方登录仍在 Muse 网页完成。
- **原处填写**：`adapters.qq.credentials_in_place: true` 允许管理员在凭证所在群内填写；默认 `false`，需管理员私聊填写。群内输入对群成员可见。不会放开普通成员权限，也不能填写其他群的凭证。
- **引用识别**：仅关联机器人实际发送成功的消息；不凭引用文字猜测卡片。服务重启后旧消息的引用关联不保留，可发送 `/凭证` 查看当前可填写的卡片，再按编号填写。
- **审批**：按当前卡实际提供的原生选项逐项展示，保留长期允许范围及顺序；使用 `/审批 编号 选项数字`。单张当前会话审批也可直接回复数字。管理员私聊可查看管理中的所有会话；群聊只能操作本群绑定会话。
- **门禁**：对应 Muse 会话未结束的审批会拦截 QQ、OneBot 的普通发送；消息直接丢弃，不排队、不在审批后补发。QQ 再次返回审批卡。审批内容/版本变化后必须重新选择。
- **退出**：`/cancel`（流程进行中也可直接发“取消”/“退出”）结束本地登录或凭证输入并清除临时秘密，不会取消远端审批；登录完成、凭证保存成功会自动退出。临时输入约五分钟过期，过期后五分钟内的第一条消息会被拦截一次（防止迟到的密码或验证码被转发），之后恢复正常转发。
- **转发规则**：只有以下情况才进入本地处理：斜杠命令、正在进行的登录/凭证输入、引用凭证卡、仅有一张待填凭证卡时的两行消息、当前会话有待审批。其余消息（包括六位数字、“取消”等普通文字）一律直接转发给 Muse。

QQ 私聊不等同网页安全输入：密码和验证码仍经过 QQ 链路。交互正文在日志前消费；不持久化临时密码、验证码或卡片授权。凭证提交使用固定幂等键；结果不确定时仅接受相同输入重试。审批提交结果不确定时先查询远端，不自动重复决定。

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
- 凭证 capture 返回有效 ID 才记录完成；网页成功回调也只更新本地展示，不额外猜测远端卡片完成接口。桥接在 `cookies_file + .captures.local.json` 中原子保存目标/会话/卡片/授权上下文的 SHA-256 回执，以防重启重开已成功提交的同一卡片。文件不含账号、密码、验证码、授权原文或 capture ID；删除该回执文件会丢失本地完成去重，损坏或不可读时拒绝启动。远端 `credential_capture_completed` 状态仍以服务器事件为准。
- 审批使用独立 Sentinel 状态协调（约三秒一次，发送前再次核对）；聊天出站仍为事件驱动。尚未把未证实的 `approvals.snapshot` 订阅来源写死。重启从远端恢复待审批，状态查询失败时不放行普通消息。

## 会话隔离（`session_mode`）

QQ 适配器支持两种会话模式，通过 `adapters.qq.session_mode` 选择：

| 模式 | 行为 |
| :--- | :--- |
| `global`（默认） | 所有群聊/私聊共用同一个 Muse 会话（`data/qq_session.json` / `muse.session_id`），与旧版完全一致 |
| `per_chat` | 每个群、每个私聊用户各自独立一个 Muse 会话，互不可见 |

`per_chat` 相关选项：

- `session_map_file`（默认 `data/qq_session_map.json`）：`群/私聊 -> Muse session_id` 映射，重启后保持。
- `session_bindings`：预置绑定，键为 `group:<group_openid>` 或 `private:<user_openid>`，值为已有的 Muse `session_id`。
- `proactive_to_session_owner`（默认 `true`）：提醒等无对应提问的主动消息，推回该会话所属的群/私聊；设为 `false` 则仍推给管理员私聊。
- `session_hot_window_s` / `session_sweep_s`：出站检测参数（见下）。

行为说明：

- 首次收到某个群/私聊的消息时自动新建会话（新 UUID）；`/new`、`/switch` 只改变发指令的这个群/私聊。
- 出站不会轮询所有会话历史：每轮只调用一次 `/api/session/list`，按 `updated_at_ms` 变化判断哪个会话有新活动（等价于网页端的 `sessions.updated` 事件），只拉取有变化或刚发过消息的会话历史。
- 运行中切换到已有会话时，会先把该会话旧消息标记为已读，不会回放历史。
