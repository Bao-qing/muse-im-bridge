# 网页审批推送运行时验证

日期：2026-11-17。网页采证仅使用 js-reverse MCP，未使用 Magpie；未修改生产配置。网页采证阶段未修改桥接代码，后续已实现事件驱动改造，见 [实现说明](approval-event-driven.md)。

## 实际操作

1. 初始化 WS 帧捕获后重载 Muse 网页。
2. 按用户明确指令在网页发送一次 SSH 测试请求。
3. 网页出现对应 SSH 网络审批卡，状态为 pending，并提示审批前暂停发送。
4. 没有点击允许一次、始终允许、拒绝，也没有输入 SSH 凭据。
5. 在 `approvals.snapshot` 消费回调设置临时断点，重载页面，捕获重连后服务端下发的现有待审批快照。
6. 检查同一次暂停的调用栈：当前回调 payload 中包含对应 SSH 审批；上层订阅分发帧中 method 为 `chat.subscribe`、service 为 `daemon`、mode 为 `subscription`。
7. 移除本次临时断点并恢复页面执行。

## 已实际观察到的运行时结构

从订阅分发帧读取的摘要（没有保存 token、Cookie、部署地址、完整历史或完整审批 scope）：

```json
{
  "subscription": {
    "method": "chat.subscribe",
    "service": "daemon",
    "mode": "subscription"
  },
  "event": {
    "type": "event",
    "event": "approvals.snapshot",
    "eventKeys": ["event", "payload", "seq", "ts_ms", "type"],
    "payloadKeys": [
      "emitted_at_ms",
      "pending_approvals",
      "recent_approvals",
      "schema_version"
    ]
  }
}
```

事件 payload 中存在刚才触发的审批：`status=pending`、`payload.type=network`、SSH 协议、22 端口、前台审批展示与 `pre_connect` 恢复阶段。实际目标地址和用户关联 ID 不写入本公共说明。

这证明至少网页当前这条 `chat.subscribe` 订阅会收到审批快照。它不是独立的普通 HTTPS `/approvals` 响应，也不是仅凭源码中出现事件名做推断。

## 定时任务后台审批：实时触发验证

同日再次测试，仍只使用 js-reverse。按用户提供原文发送“一分钟后发测试邮件”的定时任务请求，在任务创建前预先设置带内容匹配条件的 `approvals.snapshot` 回调断点。任务创建后保持当前连接，**没有通过刷新或重连补抓事件**。

到点后的运行时调用栈确认：

```json
{
  "subscription": {
    "method": "chat.subscribe",
    "service": "daemon",
    "mode": "subscription"
  },
  "event": {
    "type": "event",
    "event": "approvals.snapshot",
    "seq": 2193,
    "payload": {
      "schema_version": 1,
      "pending_approvals": [
        {
          "status": "pending",
          "payload": {"type": "connector"},
          "display": {"attention_mode": "background"},
          "scope": {
            "requester_source": "scheduler.cron",
            "workload": "cron.scheduler.worker.execute",
            "resume_kind": "connector_authorization"
          }
        }
      ]
    }
  }
}
```

匹配审批的连接器方法为 `users.messages.send`，邮件主题、正文均为用户指定测试内容。实际邮箱、连接器账号、审批 ID、任务 ID 和完整 scope 未保存在本记录中。

- 审批 `requested_at_ms` 对应 **2026-11-17T12:39:39.644Z**。
- 快照 `emitted_at_ms`／事件 `ts_ms` 对应 **2026-11-17T12:39:39.703Z**。
- 两个服务端时间戳相差 **59 ms**；这不是测得的浏览器端到端到达延迟。
- `runtime_request_trace.request_origin` 为 `internal.scheduler_cron`，并携带计划任务标识和计划执行时间。
- scope 包含 `scope_type`、`schedule_id`、`cron_run_id` 等字段；这个样本没有前台 `thread_id` 字段，不应拿它的 `session_scope_id` 强行当作当前聊天所有权。
- 恢复页面后，网页显示“一项任务需要你的批准／一项任务需要审核”，而不是前台 SSH 卡那种阻止当前聊天发送的展示。

这次已验证：**计划任务新产生的后台审批，也能通过同一类型的 `daemon/chat.subscribe` 订阅实时推送 `approvals.snapshot`**。不只支持前台审批，也不只是重连初始化快照。

临时断点已移除，页面恢复执行；没有批准、拒绝或取消任务。没有查询已发送邮件，因此不声称已验证邮件实际投递结果。Muse 在任务描述与审批预览中自行将用户给出的收件地址增加了一个字母；输入原文未改，故该测试邮件不应被自动批准。

## 网页源码交叉证据

已保存的网页源码（本地 `.live/reference-chat.js`）：

- 7626–7639：监听 `approvals.snapshot`，取消列表查询，提交快照并记录连接代次。
- 7653–7659：快照可用性要求连接 ready，且连接代次、target token 和 gateway scope 匹配；不是永久缓存。
- 4008–4034：发送前读取本地 activity 中的任务状态、待审批记录和作用域，并以 `requireSnapshotMatch=true` 进行门禁判断。
- 2041–2044：已有当前连接权威快照时，非强制审批查询返回 `superseded_by_committed_authority`。

网页模型是“当前连接的事件快照驱动本地门禁，缺失/失效时查询协调”，不是每次发送前无条件全量拉取。

## 桥接改进边界

可以据此接入快照驱动的门禁，避免每次消息前全量 GET `/approvals`。但不能简单删掉检查或永久信任旧缓存：

- 启动、断线、重连或切换凭据/VM/会话时，旧快照必须失效。
- 当前连接收到并验证快照之后，才允许用它做本地门禁。
- 无有效快照时使用明确的 GET 补偿，或阻止发送等待同步；失败不放行。
- 前台审批按会话/任务作用域阻塞，后台审批仍保留独立通知与输入所有权规则。
- 审批提交前的刷新/一致性处理不应未经验证一并移除。
- 需要核对桥侧实际 `chat.subscribe` 参数、全局/会话流的覆盖范围、事件顺序与重连恢复，不能仅凭本次一个网页订阅样本断言桥侧现有所有订阅都自动覆盖所有审批。

## 前台审批挂起时，桥客户端直接发送的返回

同日按用户授权，临时绕过本地 `MuseService.check_approval_gate()`：使用桥现有配置、当前有效凭据及 `MuseService.create_client()` 得到真实客户端，直接调用 `client.send_message()`。没有修改正式代码；没有重试 POST；没有批准、拒绝或取消审批。

先通过 Sentinel `GET /approvals` 确认存在唯一 `foreground`、`pending` 网络审批，并从 `scope.thread_id` 和 trace 中确认对应聊天会话。该会话与桥配置默认会话不同，因此本次明确使用审批所属会话，避免跨会话测试。

测试消息：

```text
[前台审批发送测试] 请只回复“收到”，不要执行其他操作。
```

实际路径：桥 → 当前配置的 Worker WS → Noise → `daemon` 的 `POST /chat/stream`。单次发送返回用时 **289.662 ms**，响应状态 **200**。原始响应的结构如下，标识值使用占位符，不公开实际会话 ID：

```json
{
  "channel": "main",
  "is_thread": false,
  "message_id": "<new-message-id>",
  "reply_to_message_id": "<same-new-message-id>",
  "session_id": "<foreground-approval-chat-session>"
}
```

发送后立刻再次查询，原审批仍为 `pending`、`foreground`。所以本次实测中，前台审批未让发送 API 返回拒绝；它给出了成功接收形式的确认。

但不能据此声称新消息已执行：网页仍显示“发送功能会暂停”的本地门禁，新测试消息在当前 DOM 中未出现；随后对同一 session 的 `/chat/history` 分别读取 12 条和 100 条窗口，均没有匹配测试文本或新 message_id，也没有确认到“收到”的回复。暂时无法区分排队等待、执行调度、历史可见性或会话映射的后续行为。

结论严格限定为：**这次在前台审批 pending 时直接 POST，服务端返回 200 与 message_id，而不是审批阻塞错误；接收确认不等于已执行或已落入可见历史。**不据此删除正式门禁，也不据此承诺服务端会自动拒收所有审批期间的新消息。

## 未验证

SSH 样本抓到的是触发后重连送达的快照；后续定时邮件样本已在新后台审批产生时命中实时回调。两次均未执行批准或拒绝，审批解决事件、跨客户端解决、超时和其他更新时序仍未实测。

没有承诺性能改善数值。网页采证后，桥侧已实现事件驱动审批；未提交或推送。

## 桥客户端全局订阅：只读联调

同日完成 WebSocket 短超时分帧／pong 处理修复后，使用现有配置与 `MuseService.create_client()`，连接并调用不带会话参数的 `client.subscribe()`。不发送聊天消息、不提交审批决定、不启用自动策略、不修改生产配置。

实际结果：

```json
{
  "account_wide_subscribe": "ACK",
  "snapshot_received": true,
  "accepted_by_bridge": true,
  "schema_version": 1,
  "pending_count": 1,
  "recent_count": 10,
  "foreground_count": 0,
  "background_count": 1
}
```

收到快照后将原始事件交给新的 `accept_approval_event()`，校验通过，再关闭本次连接。没有保存实际审批 ID、完整 scope、Token、Cookie 或部署地址。本次确认了桥的账号级订阅可接收并消费真实后台审批快照；当时没有待处理前台审批，因此桥侧这次未直接验证前台样本，前台来源证据仍为前面的网页运行时捕获。此前桥侧探针只拿到 ACK，随后解密失败，不能与此次成功结果混为一谈。
