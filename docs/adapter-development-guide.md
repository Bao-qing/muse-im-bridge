# 统一网关适配器开发指南

本指南面向在 `UnifiedGateway` 架构下新增或维护 IM 平台适配器（Platform Driver）的开发者。`muse_bridge/adapters/` 下可直接对照参考的真实实现：`qq`（标准 OpenAPI + WS）、`onebot`（NapCat 正向 WS + HTTP + 原生空间动态 + 输入态）、`wechat`（无管理员白名单的个人 iLink 长轮询渠道 + 分段投递账本）、`telegram`（官方 Bot API 长轮询 + 原生反应 + Markdown 转消息实体）。

---

## 1. 架构红线与职责边界

重构后的核心原则是：**适配器只做平台协议翻译与 I/O，网关拥有全部业务状态。**

### 适配器必须负责的职责

1. **平台连接与保活**：长连接 / WebSocket / 长轮询的建立、鉴权、心跳、退避重连，以及收到取消信号时的干净关闭。
2. **事件解析与事实标准化**：把平台原始事件解析为标准的 `InboundMessage`（包含是否被 @ `mentioned`、引用关系 `references`、自身外部索引 `own_references`、附件元数据 `Attachment`）。
3. **可信账号身份**：通过 `account_identity()` 和 `prepare()` 提供当前实际登录的账号 ID（严禁把 HTTP URL、配置路径或随机值当账号身份）。
4. **媒体下载与解密**：实现 `download_attachment(attachment, max_bytes)`，负责平台签名、CDN 解密与字节上限校验。
5. **纯出站投递**：实现 `deliver(DeliveryEnvelope) -> DeliveryResult`，只负责按网关给定目标与内容调用平台发信 API，准确返回四态结果与原生回执 `DeliveryReceipt`。
6. **可选能力**：平台原生的输入状态信号（`activity_started` / `activity`）与原生动态广场发表（`supports_native_feed` / `publish_feed`）。

### 适配器绝对禁止的职责（有 AST 测试强制守卫）

在 `tests/test_unified_gateway.py` 中，`test_platform_modules_do_not_import_business_services` 会解析 `muse_bridge/adapters/` 下**所有 `.py` 文件的 AST**，强制禁止导入：
- `muse` / `muse.*`（严禁直接持有或调用 `MuseService`、`MuseClient`）
- `gateway.chat` / `gateway.outbound` / `gateway.feed`
- `core.session_map` / `core.interactions`

因此，适配器代码里**不得出现**：
- 权限 / 白名单检查、管理员判定、群触发策略过滤（`all`/`at`/`prefix` 均由 `InboundPipeline` 决定）
- 斜杠命令解析、凭证卡或审批卡路由
- 会话映射（`SessionMapper`）、回复路由（`RouteManager`）或因果猜测
- `MuseEventPump` 或对 Muse 历史流的主动订阅
- 结果不明（`UNKNOWN`）或部分完成（`PARTIAL`）后的自动重发

---

## 2. 目录结构约定

新增平台 `<platform>`（例如 `telegram`、`discord`）建议按如下结构组织：

```text
muse_bridge/adapters/<platform>/
├── __init__.py       # 导出 <Platform>Adapter, <Platform>Client, <Platform>Gateway, <Platform>Sender
├── adapter.py        # 继承 BaseAdapter：生命周期、prepare、事件解析 -> emit、download_attachment、activity/feed 钩子
├── client.py         # 平台 HTTP/WS API 底层封装（鉴权、收发、上传、下载）
├── gateway.py        # 平台入站事件源（长连接 / 轮询循环 + 重连 + 取消退出处理）
└── outbound.py       # 纯发送器 <Platform>Sender：deliver(DeliveryEnvelope) -> DeliveryResult
```

---

## 3. 核心数据契约

### 3.1 入站消息模型（`muse_bridge/core/message.py` & `identity.py`）

适配器在收到合法消息并校验 ID（调用 `muse_bridge.core.identity.valid_id(value)`：非空字符串、`<=256` 字符、非 `*`、不含空白字符）后，构造并提交 `InboundMessage`：

```python
InboundMessage(
    id=msg_id,                          # 平台消息唯一 ID（字符串；用于去重、被动回复与引用关联）
    source=MessageSource(
        platform=self.name,             # 必须与 adapter.name 完全一致，runtime 会强校验
        chat_type=kind,                 # 只能是 "private" 或 "group"
        target_id=target,               # private 时填对方用户 ID；group 时填群 ID（均保持字符串，勿转 int 丢失精度或前导零）
        sender_id=user,                 # 实际发送者 ID（private 时通常等于 target_id）
        sender_name=nickname,           # 展示昵称（可选，用于群消息发送者元信息）
        sender_card=card,               # 群名片（可选）
    ),
    text=raw_text,                      # 注意：不要预先 .strip() 掉首尾空白或换行！凭证/多行输入需要原样保留空白；网关会在非凭证流程下自行 strip
    attachments=attachments,            # list[Attachment(type="image"|"file"|"video", filename=..., content_type=..., url=..., file_info=...)]
    timestamp=ts,                       # 可选 Unix 时间戳（float）
    references=references,              # [("id", quoted_msg_id), ("ref", quoted_ref_idx), ...] 被引用消息的平台标识对
    own_references=own_references,      # [("ref", own_ref_idx), ...] 当前入站消息自身附带的额外平台引用键（如 QQ msg_idx）
    mentioned=mentioned,                # bool：本消息在群里是否 @ 了当前机器人自身
)
```

**关键注意事项**：
- **保留原始空白**：`OneBot` 与 `QQ` 在解析阶段都不做全局 `strip()`（例如 `parse_segments(..., strip_text=False)`），由 `InboundPipeline` 判断当前消息属于凭证输入还是普通消息后统一决定是否裁切空白。
- **不透传原始平台 `raw_event`**：规范化引用（`references` / `own_references`）和 `@` 状态（`mentioned`）后即可；不要依赖把整个平台 JSON 塞进 `raw_event` 让网关猜。
- **群消息防自身回环**：若平台事件会下发机器人自己发出的消息（如 OneBot），必须在 `handle_event` 中用可信 `self_id` 过滤 `user == self_id`。

### 3.2 投递信封与结果（`muse_bridge/core/delivery.py`）

```python
@dataclass
class DeliveryEnvelope:
    delivery_id: str                    # 网关生成的稳定幂等键（切勿篡改）
    target_type: str                    # "private" 或 "group"
    target_id: str                      # 目标用户或群 ID
    text: str = ""
    images: list[dict] = ...            # 元素含 {"data": bytes} 或 {"url": str}，及可选 "filename"
    files: list[dict] = ...
    videos: list[dict] = ...
    purpose: str = "proactive"          # "reply" | "proactive" | "notification"
    input_id: str | None = None         # 触发本回合的原生入站消息 ID
    passive_id: str | None = None       # 可用于平台被动回复配额的入站消息 ID
    quote: NativeQuote | None = None    # 平台原生引用：quote.references 为 (("id", ...), ("ref", ...))
    quote_fallback: str = ""            # 无法原生引用时的纯文本引用前缀（由发送器决定在无原生引用时拼接）
    plain_text: bool = False            # True 时不走 Markdown 封装
    fallback_target: str | None = None  # 仅当网关政策明确授权时非空；发送器不得自行发明私聊备选目标
```

你的 `deliver(envelope)` 必须返回 `DeliveryResult(status, receipts, reason)`：

| `DeliveryStatus` | 语义 | 网关侧后续行为 |
|---|---|---|
| `CONFIRMED` | 本次信封中所有内容已收到平台明确成功响应 | 账本记为 `confirmed`，登记 `receipts` 用于后续引用与审批卡匹配，标记消息已送达 |
| `REJECTED` | 平台明确拒绝，或参数/媒体校验在发包前失败，**确定未发出任何部分** | 账本记为 `rejected`，允许后续业务层在条件允许时重试（若是媒体超限会触发提示兜底） |
| `PARTIAL` | 多段消息/媒体中有部分已确认发出，后续段被拒绝或准备失败 | 保留已发出的 `receipts`，**网关账本永久抑制整包重放**，防止重复刷屏 |
| `UNKNOWN` | 网络超时、连接断开、响应畸形、5xx 服务端异常或发送中途协程被取消 | **禁止再次请求，禁止切换 `fallback_target`**；持久化为 `unknown` 抑制一切重放 |

**`DeliveryReceipt` 回执规范**：
- 每成功发出一条平台消息或文件，追加一条 `DeliveryReceipt(target_type, target_id, (("id", native_msg_id), ...))`。
- `DeliveryReceipt.from_body(kind, target, body)` 可直接从常见 `{"id": ..., "message_id": ..., "ext_info": {"ref_idx": ...}}` 结构中提取干净的引用对。
- 注意区分 `PARTIAL` 与多段循环中的 `UNKNOWN`：前面已经发成功几段，后一段遇到 `UNKNOWN` 时，**务必立刻返回 `DeliveryResult(DeliveryStatus.UNKNOWN, receipts, reason)`**，把已经成功的 `receipts` 一并带回给网关登记引用。

---

## 4. 实现 `BaseAdapter` 完整接口

所有接口定义在 [muse_bridge/adapters/base.py](../muse_bridge/adapters/base.py)：

### 4.1 必需属性与方法

| 成员 | 类型/签名 | 实现要点 |
|---|---|---|
| `name` | `@property def name(self) -> str` | 返回固定小写平台标识（如 `"telegram"`），与 `Config` 及 `MessageSource.platform` 一致 |
| `is_enabled` | `@property def is_enabled(self) -> bool` | 不仅检查 `config.enabled`，还应检查必须凭据（如 token / app_id）已就绪 |
| `account_identity()` | `def account_identity(self) -> str` | 返回当前已登录账号的稳定字符串 ID；在未取得可信账号前返回 `""`（不要返回 API URL） |
| `prepare()` | `async def prepare(self) -> None` | 在网关开启订阅与事件循环前执行；若账号 ID 需联网获取（如 `get_login_info`），在此获取并调用 `self.host.identity_changed(self)` |
| `start()` | `async def start(self) -> None` | 若 `not self.is_enabled` 直接 `return`；否则启动平台事件源并保持运行，直到被停止或取消 |
| `stop()` | `async def stop(self) -> None` | 停止平台事件源、取消内部输入态任务并等待结束、关闭底层 HTTP/WS 客户端 |
| `download_attachment()` | `async def download_attachment(self, att: Attachment, max_bytes: int) -> bytes` | 下载或解密媒体字节并强制执行 `max_bytes` 限制；超限或不可用时抛出异常 |
| `deliver()` | `async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryResult` | 委托给纯 `<Platform>Sender.deliver(envelope)` |

### 4.2 可选钩子（按平台能力按需实现）

1. **`notification_target(self) -> str | None`**
   - 仅**个人号型平台**（无配置型管理员列表、非 `ChatConfig`）需要覆盖，例如微信返回当前扫码登录者 `user_id`。
   - **社区/群聊型平台**（继承 `ChatConfig`）不要覆盖此方法，`ChannelContext` 会自动读取 `config.admin_target()`。

2. **输入状态指示（Typing / Activity 生命周期）**
   ```python
   def activity_started(self, target: str, token: str, *, kind: str = "private") -> None
   async def activity(self, target: str, token: str, state: str, *, kind: str = "private") -> None
   ```
   - `InboundPipeline._forward` 在收到非斜杠消息时**同步**调用 `activity_started(target, "intake:<msg_id>", kind=target_type)`，失败/阻塞拦截则在 `finally` 调 `activity(..., "finished")`；
   - 成功提交 Muse 后，`track_session` 会再调 `activity_started(target, session_id, kind=kind)` 覆盖 `intake:` 占位令牌；
   - `MuseEventPump` 会把 `on_turn_finished(session_id)` 排在对应上下文投递队列的尾部，确保回复实际发出后才调 `activity(target, session_id, "finished", kind=kind)`。
   - **实现注意**：
     - 如果平台仅支持私聊输入态，开头判断 `if kind != "private"` 直接返回（参照 [onebot/adapter.py:120](../muse_bridge/adapters/onebot/adapter.py:120)）。
     - 若输入消息先到后才由 `session_id` 覆盖 `token`，须防止旧 `intake:` token 覆盖掉已升级的 `session_id` token（`if token.startswith("intake:"): self._typing_sessions.setdefault(target, token)`）。
     - `activity_started` 是同步方法，应在其中启动异步保活任务并设置最大兜底超时（如 600s）及失败指数退避。

3. **原生动态广场投递（Feed Surface）**
   ```python
   def supports_native_feed(self) -> bool
   async def publish_feed(self, feed: FeedEnvelope) -> DeliveryResult
   ```
   - 仅当平台拥有独立动态广场（类似 QQ 空间、朋友圈动态墙）时返回 `True` 并实现 `publish_feed(feed)`；
   - 不支持原生广场的平台无需任何代码，`GatewayFeedCoordinator` 在 `auto` / `chat` 模式下会自动把动态渲染为图文消息，直接调用你的 `deliver()` 推给管理员/登录者私聊。

4. **反应（Reaction）双向契约**
   ```python
   # 入站：平台事件 -> 网关（独立于消息管道）
   await self.emit_reaction(InboundReaction(event_id, source, message_ref, old_reactions, new_reactions))

   # 出站：网关 -> 平台
   def supports_native_reactions(self) -> bool
   async def deliver_reaction(self, envelope: ReactionEnvelope) -> DeliveryResult
   ```
   - **入站**：只有在你确实能解析出「谁、在哪条平台消息上加/减了什么」时才调用 `emit_reaction()`；`message_ref` 使用与引用一致的 `("id", "<chat_id>:<message_id>")` 形式。网关会自行做白名单校验、会话归属与 Muse 消息映射，找不到归属时返回 `"ignored"`，**适配器不要自己判断权限或猜测会话**。
   - **出站**：`ReactionEnvelope.reactions` 是**完整目标状态**（空表示清除）。`supports_native_reactions()` 返回 `True` 时网关会走原生反应；否则网关调用你的 `deliver_reaction()`，你可以直接用 `reaction_as_delivery(envelope)` 生成「引用原消息的纯文本通知」交给 `deliver()`（`qq` / `onebot` / `wechat` 即如此）。
   - **部分支持也算不支持**：若平台只接受固定表情列表（如 Telegram Bot API 仅 73 种），必须在 `deliver_reaction()` 内先判断，列表外就走引用文本兜底，不能静默丢弃（参照 [telegram/outbound.py](../muse_bridge/adapters/telegram/outbound.py) 的 `supports_native_reaction`）。
   - **不要回显自触发**：把用户反应同步到上游后，上游可能回推同一条反应；网关已用 `reaction_lock` + 本地快照抑制这种回环，适配器无需自行去重。

---

## 5. 身份绑定、并发与生命周期核心细节

### 5.1 动态账号身份与分片隔离

- 如果你的平台在 `__init__` 时就知道账号（如 `config.bot_token` 中解析出 bot id 或显式配置了 `app_id`），在 `gateway.mount(adapter)` 时就会立刻创建 `ChannelContext`。
- 如果像 OneBot 一样必须在联网后才知道实际登录账号：
  1. `account_identity()` 初始返回 `""`。
  2. 在 `prepare()` 和每条 `handle_event()` 中更新真实 `self_id`，一旦发现变化立即调用 `self.host.identity_changed(self)`。
  3. 网关会自动按 `(platform, account)` 创建隔离的 `ChannelContext` 和状态分片文件（`.identity.local.json`），新账号**不会继承**旧账号的会话绑定与白名单授权。
  4. **重要**：调用 `await self.emit(msg)` 前，如果还没有调用过 `identity_changed`，`accept()` 也会触发一次 `identity_changed(self)`；若账号身份仍为空，`accept()` 会返回 `"retryable"`。

### 5.2 锁顺序与 `emit()` 返回值约定

- `await self.emit(message)` 会阻塞直到该聊天（以及共享同一 `session_id` 的所有聊天）排队完成**附件下载 + 命令执行 + 提交 Muse `post_message`**（但不等 Muse AI 生成完整回复）。因此，如果你的平台轮询一次拉回一批消息（如微信），只需顺序 `await self.emit(msg)` 即可天然保序；如果是 WebSocket，对同一连接上顺序收到的消息建议直接按序或交由网关 `ChannelKey` 锁排队，不要无序乱发 `post_message`。
- `emit(message)` 返回 `"handled"`、`"ignored"`、`"unknown"` 或 `"retryable"`：
  - 对于有服务端游标/ACK 机制的平台（如微信）：在调 `emit()` 前先把本地消息标为 `unknown`，只有明确返回 `"retryable"` 时才保留游标以便重拉；返回 `"unknown"` 或 `"handled"` 时绝不重投给业务管道。

### 5.3 取消退出与异常安全（极易踩坑！）

- **切勿吞掉 `asyncio.CancelledError`**：参考 QQ 曾经的教训（见 [qq/gateway.py:86](../muse_bridge/adapters/qq/gateway.py:86)），事件循环重连 `while self._running:` 必须显式区分 `except asyncio.CancelledError:` 与普通断线异常——收到 `CancelledError` 时关闭连接并 `raise`，否则整个网关 `Ctrl-C` 或 `stop()` 会挂死在无限重连中。
- **Sender 内的中途中止防护**：参考 `QQSender` / `OneBotSender` 的 `deliver()` 入口：持目标锁后先把当前 `key` 在内存 `_uncertain` 字典里占位为 `UNKNOWN`，`await _deliver_locked` 成功或明确 `REJECTED`/`PARTIAL` 时再覆盖或弹出。这样即使协程在网络请求途中被 `CancelledError` 打断，网关和发送器都不会误重放。

### 5.4 超限媒体诊断规范

在 `<Platform>Sender` 中下载/检查发往平台的媒体大小时：
- 按网关 [delivery.py:25](../muse_bridge/gateway/delivery.py:25) 约定的格式构造 `DeliveryResult.reason`：
  - 已知实际字节：`f"<Platform> {label} exceeds upload limit ({len(data)} > {cap} bytes)"`（`label` 为 `"image"` / `"video"` / `"file"`）
  - 下载阶段截断未知精确字节：`f"<Platform> {label} exceeds upload limit (>{cap} bytes)"`
- **绝对不要**在 `reason` 中包含文件名、URL、正文或凭据，网关会自动识别该格式给用户补发中文提示文字并记录脱敏日志。

---

## 6. 接入配置、引擎与工具的完整清单

新增一个平台适配器时，按以下清单逐一接线：

1. **配置模型（[muse_bridge/config/models.py](../muse_bridge/config/models.py)）**：
   - 若平台有群聊、管理员与白名单需求，配置 dataclass 继承 `ChatConfig`；
   - 若为纯个人私聊渠道（无管理员/白名单体系），配置 dataclass 继承 `ConversationConfig`；
   - 在 `AppConfig` 添加字段，在 `ConfigLoader.load()` 中解析（同时更新 `GatewayFeedConfig.__post_init__` 中允许的 `targets` 平台集合及 `config.example.json`）。
   - 确保每个挂载账号拥有独立的可变状态路径（`routes_file`、`session_map_file`、`delivered_file`、`push_state_file`、`access_requests.state_file`），`UnifiedGateway.mount()` 会检查所有路径不允许与其它已挂载适配器重叠。
2. **组合根（[muse_bridge/engine.py](../muse_bridge/engine.py)）**：
   - 在 `BridgeEngine.__init__` 中按 `if config.<platform>.enabled:` 挂载：
     ```python
     self.adapters.append(self.gateway.mount(<Platform>Adapter(config.<platform>)))
     ```
   - 保持 `BridgeEngine` 仅做装配，不向适配器注入 `MuseService` 或 `SessionMapper`。
3. **CLI 检查与配置向导（[main.py](../main.py)、[init_config.py](../init_config.py)）**：
   - 在 `main.py --check` 输出中补充新适配器的脱敏摘要；
   - 按需在 `init_config.py` 中补充交互式编辑菜单。

---

## 7. 新适配器测试标准与验收清单

提交新适配器前，以下 4 类离线测试必须全部通过：

1. **入站解析与策略交接测试（参考 `tests/test_onebot_adapter.py`）**：
   - 校验消息解析出的 `InboundMessage` 保留了凭证空白、`references`、`own_references`、`mentioned`、附件和合法 ID 过滤；
   - 挂载到真实 `UnifiedGateway`（可参考 `tests/gateway_helpers.py`）验证群触发、白名单/申请、共享会话隔离与多渠道不串线；
   - 验证开启 `typing_enabled` 时输入态在普通转发时启动、队列回复发出后停止、`/停止` 或异常时立即结束、群聊不误发。
2. **纯发送器契约测试（参考 `tests/test_platform_delivery.py`）**：
   - 使用固定测试桩覆盖：`CONFIRMED` 回执提取、明确拒绝返回 `REJECTED`、网络异常/格式畸形返回 `UNKNOWN`、多段部分发送失败返回 `PARTIAL`（且带已发 `receipts`）、超限媒体按规范输出 `reason` 且不泄漏文件名。
3. **生命周期与边界测试**：
   - 模拟在连接中 `task.cancel()`，验证 `start()` 迅速关闭连接并抛出 `CancelledError`，不再循环重连（参考 `tests/test_qq_gateway_lifecycle.py`）。
   - 若支持动态身份变化，验证在 `account_identity` 变更后 `identity_changed()` 自动切换到隔离分片。
   - **全目录 AST 边界测试**：运行 `python -m unittest tests/test_unified_gateway.py`，确保你的 `adapters/<platform>/*.py` 没有违规导入任何网关业务或 Muse 模块。
4. **完整回归**：
   ```bash
   python -m compileall -q muse_bridge
   python -m unittest discover -s tests -q
   ```
