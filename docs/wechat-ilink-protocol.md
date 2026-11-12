# 微信 iLink 接线与协议边界

本通道使用微信 iLink 的 HTTP 登录、长轮询和发送接口，不要求第三方网关。它不是腾讯官方 SDK，也不保证平台协议长期稳定；仅用于自己有权操作的账号。实现不绕过扫码确认、验证或账号限制。

## 本地登录与检查

```bash
python main.py --config config.json --wechat-login
```

```bash
python main.py --config config.json --wechat-status
```

两个命令在完整 `ConfigLoader.load()` 之前分流，使用 `ConfigLoader.load_wechat()`：只读取配置和 `const.json`，不初始化 Muse、不读取 Muse Cookie、不探测出口 IP。配置仍采用工作目录中的 `config.json` / `credentials.json` 默认候选及 `const.json` 底层合并规则；显式配置覆盖 const，顶层 overrides 优先。不会自动启动登录。`--login` 保持 Muse 登录用途，三个登录/状态开关互斥。

扫码展示将 auth 提供的字符串直接编码为终端 ASCII 二维码，不下载二维码图片或访问展示 URL；依赖 `qrcode>=8.0`，不需要 Pillow。验证输入使用 `getpass` 隐藏回显；auth 状态回调只显示固定提示，不输出票据、验证地址或原始服务端 payload。成功后调用 `WeChatState.save_account(credentials)` 保存并激活账号。失败和取消不会打印认证详情。

`--wechat-status` 只消费 `WeChatState.status()` 的脱敏摘要，不输出 credentials 或 token。它不连接微信，不证明 token 远端有效。`--check` 同时显示此本地摘要；完整配置校验保留已有 Muse 出口 IP 探测语义。没有本地凭据时，运行适配器会提示显式执行 `--wechat-login`，不在后台自动重扫码。

## 配置与账号隔离

`WeChatConfig(ChatConfig)` 默认关闭，`state_dir=data/wechat`、`channel_version=2.4.6`、`route_tag=""`、`http_timeout_s=35`、`polling_timeout_ms=35000`、`typing_enabled=false`。`chunk_chars=4000` 且必须是 1–4000 的整数，`max_chunks` 必须是正整数。超时、限额不能用 bool 或字符串冒充数字，不能使用 NaN/Infinity；整数参数不接受浮点数。开关必须是真正 JSON 布尔值。

默认 `admin_only=false`、`allow_all_private=false`、`unbound_push_to_admin=false`。当前扫码 owner 由适配器动态允许聊天，不自动成为管理员，也不改写配置。只有本地 `admin_id` 显式授予管理员身份。向导通过本地字段确认授权，不使用微信首条消息自动认领身份。群配置（`allow_all_group=true`、群白名单、`group:` 绑定）拒绝；会话预设仅允许 `private:<user_id>`。

陌生用户的普通消息不建立主动发送上下文、不下载媒体。明确的 `/申请` 会保存有界、账号隔离、最长七天的申请上下文，以便重启后批准时发送开通通知；批准前它不是普通 proactive 上下文，批准后也不会覆盖该用户更新的已授权 token。同一批轮询中先申请通过、再发送聊天，后续消息会重新检查授权。

`config.for_account(bot_id)` 深复制配置，不修改全局。账号目录使用：

```text
state_dir/accounts/<sha256(bot_id 的 UTF-8 字节)[:24]>/
```

网关运行文件强制派生到该目录：`delivered.jsonl`、`session_map.json`、`push_state.json`、`routes.json`、`access.local.json`；引用索引等通过对应运行路径派生，同样隔离。配置中的旧路径不能使这些文件逃逸到其他账号目录。显式配置的管理员及 `session_bindings` 不随账号切换自动清空或推断；如不希望多个账号共享预设，请使用不同配置文件。

## 上下文、回复与能力

- **即时 reply**：接收到一条消息时固定当前上下文快照，命令等即时回复不应因随后收到的新消息而被覆盖。
- **Muse 异步回复 / proactive**：按 bot + user 查找 latest 上下文，不把异步内容绑定到早先入站的旧 context token。切换账号或用户不得复用别人的上下文。
- reply/latest 是发送上下文，不等价于原生消息引用。微信通道不保证精确原生引用某条用户消息；不能声称与 QQ message_reference 或 OneBot reply 段完全相同。
- 不支持群、按钮、编辑或语音发送。审批、凭证等走共享网关文本交互与管理员权限检查，不凭多行内容自动认领凭证。

## 失效、游标与不确定结果

- `-14` 表示会话失效处理路径：冷却一小时，保留 token，不删除凭据，不自动扫码，不通过密集重试绕过服务端限制。
- 长轮询 cursor 为 best-effort 持久化；本地状态并不是服务端 exactly-once 消费确认，崩溃、网络或落盘失败可能造成重放或漏收。
- 对消息入站、分段和发送的不确定状态保守处理。请求超时/断开后，不知道服务端是否已接收时不得盲目重发；可能漏发比重复发送更可接受。不承诺跨进程原子消费和远端发送事务。
- 使用单实例；不要让两个进程同时操作相同账号的状态目录。状态损坏应报告并人工处理，不自动清空、重新扫码或回放消息。

## 网络与媒体边界

认证 API 当前仅允许已核实的 `ilinkai.weixin.qq.com`，CDN 仅允许 `novac2c.cdn.weixin.qq.com`。扫码状态机理解 redirect/baseurl，但未知区域节点或新 CDN 域名会安全拒绝，不能据此宣称任意区域节点可用；扩展域名之前应独立核实服务归属。`binded_redirect` 若没有能唯一匹配本地账号的身份信息，同样拒绝猜测凭据，可明确重新绑定而不是选取任意 token。

生产请求校验公网 DNS 并固定到已验证的 IP，保留 Host、TLS SNI 和证书验证；不自动跟随 HTTP 重定向。外部 Muse 媒体源允许 HTTPS 公网下载，但该通道没有 bot_token、Cookie 或业务鉴权头。API/CDN URL、媒体密钥和配对票据不进入普通请求日志。

图片、文件、视频按文档使用 AES-128-ECB/PKCS7 上传；下载兼容两种 base64 密钥编码，图片无密钥时可按明文下载。媒体有本地资源限额，不代表腾讯承诺的服务端额度。`typing_enabled` 默认关闭；启用时仅覆盖可确定的本地处理期，缓存 ticket、失败退避并在取消时停止，不猜测 Muse 整轮任务终态。

本实现没有在真实微信服务验证扫码、区域跳转、CDN 或 TLS 握手；自动测试使用离线替身。网络策略失败不会关闭 TLS 校验或扩大域名白名单来绕过。

## 凭据与数据保护

运行凭据为 JSON 明文，含 `bot_token`、`baseurl`、`bot_id`、`user_id`，不是加密保险箱。账号目录还可能含上下文、游标、去重与路由状态，应视作敏感数据。限制目录 ACL/文件权限，保护备份，不提交到 Git、不上传 Issue，不分享二维码、验证输入或完整凭据。终端登录本身会访问微信；自动化测试必须 mock state/auth/client，只使用临时文件，不读取真实账号、不发送真实流量。
