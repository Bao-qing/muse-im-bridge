# Worker 传输验证记录

验证日期：2026-11-17。对照基线为未修改的 `HEAD`（`ef379a2`），通过 `git archive HEAD` 解出到全新临时目录运行，未切换当前分支、未覆盖工作文件。

## Worker 阶段结果

以下为加入事件驱动审批前的 Worker 传输验证记录。后续审批改造的测试与联调结果单独记录在 [事件驱动审批说明](approval-event-driven.md)。

| 检查 | 结果 |
| --- | --- |
| 新增 Python `test_worker_routing.py` | 16 项全部通过 |
| Node `workers/cfproxy.test.mjs` | 82 项全部通过 |
| HEAD 原始完整 Python 套件 | 716 项，12 failures、1 error |
| 本次变更后完整 Python 套件 | 732 项，12 failures、1 error |
| 对比两次失败/错误用例名称（包含子用例 mode） | 完全相同 |
| `git diff --check` | 通过（Git 另提示部分文件将按现有设置转换 CRLF） |

完整套件的真实输出摘要：

```text
BASELINE: Ran 716 tests in 32.957s
FAILED (failures=12, errors=1)

FINAL: Ran 732 tests in 38.445s
FAILED (failures=12, errors=1)

Identical failure/error names: True
```

因此：新增测试全过，没有新增已观察到的回归失败；但“现有测试套件全过”这一验收项**未达到**，不能报告为全绿。本任务没有修改既有测试断言或审批业务逻辑来掩盖基线问题。

## 基线与最终结果中相同的失败

错误（1）：

- `test_approval_queue.QueueCase.test_ordinary_command_and_batch_refresh_failures_report_unconfirmed`，子用例 `mode='batch'`：`KeyError: 'command'`，审批测试 mock 查找旧条目失败。

失败（12）：

- `test_approval_queue.QueueCase.test_ordinary_command_and_batch_refresh_failures_report_unconfirmed`，子用例 `mode='command'`
- `test_approval_queue.QueueCase.test_quoted_refresh_failure_reports_unconfirmed_without_post`
- `test_approval_queue.QueueCase.test_quoted_timed_out_card_has_feedback_without_post`
- `test_background_approvals.BackgroundCase.test_quoted_card_invalid_choice_stays_in_explicit_quote_flow`
- `test_connector_keys.ConnectorChatCase.test_command_multiline_rejected_then_single_line_succeeds`
- `test_connector_keys.ConnectorChatCase.test_quoted_selection_command_is_never_submitted_as_key`
- `test_interactions.TestQQInteractions.test_saved_cookie_rejected_by_server_requires_login`
- `test_login_fallback.TestLoginFallback.test_inbound_post_message_auth_failure_triggers_login_fallback`
- `test_onebot_adapter.TestOneBotInbound.test_chinese_gateway_and_interaction_commands`
- `test_onebot_adapter.TestOneBotInbound.test_send_failure_reply_is_explained`
- `test_outbound_event_driven.TestOutboundEventDriven.test_widget_option_and_card_rendering`
- `test_secret_credentials.SecretReplyCase.test_legacy_password_card_renders_and_records_real_quote_reference`

这些失败的断言涉及现有提示语，例如：

```text
AssertionError: '结果暂未确认' not found in '审批结果未能确认，请重新查看审批。…'
AssertionError: '单行' not found in '请发送密钥（一行即可），无需填写账号。'
AssertionError: '未自动重发' not found in '连接暂时不可用，消息可能已发送，请稍后核对。'
AssertionError: '【请选择】：' not found in '【请选择】\n1. 帮我启动机器测通 (已选)\n2. 先别启动，保持暂停'
```

## 未验证与未执行

- 未部署或更新远程 Worker。
- 未用真实 Muse Cookie、邮箱验证码或 token 做联调。
- 未做 WS 长时间挂机、真实 Cloudflare 运行时及真实 Cookie 重定向链验证。
- `origin` 参数、空 `client_ip` 接受程度、`permessage-deflate` 兼容性仍是待实测项，开关见 [启用说明](worker-transport.md)。
- 未修改生产 `config.json`、Cookie 或 mint 文件；未提交、未推送 Git。
