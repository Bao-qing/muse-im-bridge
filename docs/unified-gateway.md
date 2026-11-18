# 统一网关

## 运行结构

```text
QQ / OneBot / WeChat events
        │  normalized InboundMessage
        ▼
UnifiedGateway ── InboundPipeline ── MuseService
        │                             │
        │                 one MuseEventPump (session union)
        │                             │
        └── ChannelContext ◀──────────┘
                 │ GatewayOutbound: routing + ledger + associations
                 ▼ DeliveryEnvelope
        platform Sender ── DeliveryResult + native receipts
```

`BridgeEngine` 只负责组装和启停。业务状态与上游轮询（包括 Muse 动态同步 `GatewayFeedCoordinator`）全部由网关拥有：账号上下文是仓库分片，不是独立网关，也不能启动 Muse 订阅。适配器不持有 Muse 服务、SessionMapper 或交互管理器，不继承业务类。

MuseService 保留 token、RPC、附件上传、工作区文件和登录/凭证/审批协议。单一订阅协调器不是一条物理连接：账户级审批协议仍可使用独立连接。

## 平台端口

- `mount(host)` / `emit(message)`：发布规范化入站事件，并等待业务接收结果。
- `account_identity()` / `prepare()`：可信实际账号身份；OneBot 不能把 API 地址当登录账号。
- `download_attachment(...)`：平台鉴权、媒体描述符解密/下载，受类型大小上限约束。
- `deliver(envelope)`：只发送网关已解析的目标和内容，返回状态与实际收件人/原生 ID。
- `supports_native_feed()` / `publish_feed(feed)`：可选的平台原生动态广场投递（如 OneBot QQ 空间）；无原生广场或配置为 `chat` 模式时，由网关统一渲染并经 `deliver` 推送到该账号的 `notification_target()`。
- `activity_started` / `activity(..., 'finished')`：平台输入状态，token 对适配器不透明。

QQSender 拥有被动序号和主动配额，序号在请求前持久化。OneBotSender 编码原生消息段和引用。WeChatSenderTransport 使用原有分段投递账本，明确区分本次输入 token 快照与异步输出的最新 token。

## 顺序与路由

入站锁按 `(platform, account, chat kind, target)` 隔离，从下载附件前开始覆盖命令和交互。已绑定的共享 Muse 会话还串行提交。锁只保证桥接提交顺序，不保证 Muse 模型为每条快速连发的输入分别生成完整回答。

MuseEventPump 对会话取并集，每个账号上下文有独立投递队列。慢平台不阻塞其他平台和上游读取；队列满不静默丢弃，而是进入重连同步。首次自动绑定主会话也先解析实际 sid，再获取提交锁。出站会等正在提交的会话完成路由注册，投递网络请求不持有提交锁。

共享 Muse 会话不代表广播。同一因果消息只交给其来源聊天；没有来源的主动事件选择单一会话拥有者。因果来源持久化保存在投递账本的 origins 中，独立于短期被动 route；route 退休或另一个 worker 晚扫描不能把历史回复重新解释为跨通道主动推送。外通道已抑制的消息也保存 seen。原生引用、因果来源和平台被动回复许可分别保存。群发失败只能使用网关显式授权的管理员备选目标，发送器不得自行选择私人收件人。

## 交互安全

卡片被推送或列出不等于进入输入流程。输入只由明确命令、已有 flow 或验证过的引用接管，不按数字、行数、文本形状猜测凭证。

前台凭证阻塞需要回合来源或服务端前台 attention 的明确证据；明确的后台标记优先。阻塞期间普通文字不上传 Muse，也不作为密码消费。后台凭证与后台审批不占用普通输入；后台审批沿用单一管理员私聊收件箱及多卡队列规则。

## 投递结果

`DeliveryStatus` 包括 `confirmed`、`rejected`、`partial`、`unknown`。只有确认成功为真；HTTP 接受不等于用户已读。网关投递账本在非幂等平台请求前记录 `sending`，确认后保存原生回执。崩溃、取消、网络不明和部分完成不自动整包重发。明确拒绝允许后续业务重试。

账本不保存文本、密码或完整平台响应。真实回执用于引用关联，不伪造平台消息 ID。

## 状态与升级

第一版不引入数据库或持久化入站队列。已有配置键继续使用，业务仓库仍按账号物理分片：会话、路由、已投递、推送水位、访问申请等路径保留。新增元数据包括：

- `routes_file + .identity.local.json`：网关账号身份边界。
- `routes_file + .deliveries.local.json`：业务投递结果与回执。
- QQ routes 路径替换扩展名后的 `.passive.local.json`：原生输入的下一个被动序号。
- 原有引用、管理员活跃和微信输入/分段发送状态继续保留。

停旧进程后再启动新版本，不允许两个版本同时写同一套状态。升级前备份配置、cookies 和全部本地状态；不要把真实配置、日志或凭证提交到仓库。不同挂载不能共用任何可变状态文件（不仅仅是 routes）。切换实际账号时使用独立命名空间，保留旧账号文件，不删除或覆盖，也不继承旧账号的配置 seed/main-session 绑定。

**OneBot 首次升级注意**：没有新 identity sidecar 的旧状态，只有旧 references 元数据明确匹配 API 地址及实际登录账号时才自动沿用。不能证明所有权时保留旧文件，并为实际账号使用新的分片、重新建立会话绑定；不会因为 NapCat URL 相同而继承旧账号上下文。

回退不是简单删除 sidecar：旧代码不能理解新投递账本，回退可能重发未知/部分完成的消息，也可能使用过期会话映射。必须停进程并核对运行期间的映射与投递结果，必要时恢复升级前整套状态并人工处理升级后消息；不要只还原其中一个文件。

## 验证范围

离线合同在 `tests/test_unified_gateway.py`、`tests/test_platform_delivery.py` 和原有回归测试中；QQ 官方机器人另外做私聊/群聊真机验证。OneBot/微信真机验证及真实登录验证由维护者执行，不能用共用业务代码或离线测试替代。
