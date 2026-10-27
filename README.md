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
├── muse_client/              # 底层 Noise 加密与 WebSocket 传输库
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
        "target_id": null
      }
    },
    "onebot": {
      "enabled": false,
      "base_url": "https://bot.baoqing.eu.org:81",
      "access_token": "YOUR_TOKEN",
      "ws_url": "wss://bot.baoqing.eu.org:81",
      "target_group_id": 836401578
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
