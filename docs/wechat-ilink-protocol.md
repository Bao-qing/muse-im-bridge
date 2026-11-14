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

`WeChatConfig(ConversationConfig)` 默认关闭，只复用会话、消息和媒体设置，不继承 QQ / OneBot 的管理员、白名单、访问申请和群聊策略。`state_dir=data/wechat`、`channel_version=2.4.6`、`route_tag=""`、`http_timeout_s=35`、`polling_timeout_ms=35000`、`typing_enabled=false`。`chunk_chars=4000` 且必须是 1–4000 的整数，`max_chunks` 必须是正整数。超时、限额不能用 bool 或字符串冒充数字，不能使用 NaN/Infinity；整数参数不接受浮点数。开关必须是真正 JSON 布尔值。

微信仅做个人私信，收到的有效个人消息直接进入对话，没有另设的管理员、白名单或访问申请。Muse 登录、会话管理、审批与凭证仍走共享文本交互；这些是 Muse 的交互功能，不再附加微信管理员检查。协议必需的扫码登录、bot token 和上下文仍保留。会话预设仅允许 `private:<user_id>`。

交互配置菜单按项修改：进入后直接选择扫码登录或查看本地状态，也可单独修改某个参数。返回不改配置，登录不要求重新填写参数；Muse 登录通知发给当前扫码用户。旧版微信权限配置键读取时忽略，编辑保存后不再写回；QQ / OneBot 策略保持原样。

`config.for_account(bot_id)` 深复制配置，不修改全局。账号目录使用：

```text
state_dir/accounts/<sha256(bot_id 的 UTF-8 字节)[:24]>/
```

网关运行文件强制派生到该目录：`delivered.jsonl`、`session_map.json`、`push_state.json`、`routes.json`；引用索引等通过对应运行路径派生，同样隔离。不再创建 `access.local.json` 或保存待批准申请上下文。配置中的旧路径不能使这些文件逃逸到其他账号目录。`session_bindings` 不随账号切换自动清空或推断；如不希望多个账号共享预设，请使用不同配置文件。

## 上下文、回复与能力

- **即时 reply**：接收到一条消息时固定当前上下文快照，命令等即时回复不应因随后收到的新消息而被覆盖。
- **Muse 异步回复 / proactive**：按 bot + user 查找 latest 上下文，不把异步内容绑定到早先入站的旧 context token。切换账号或用户不得复用别人的上下文。
- **入站引用与发送回执**：实测 `sendmessage` 成功后会返回服务端 `message_id`，入站引用消息则在 `item.ref_msg.message_item.msg_id` 携带被引用的 `message_id`。适配器记录发信 `message_id` 并提取入站引用 ID 交给共享网关，用于引用凭证卡、审批卡及转成 Muse 原生 `in_reply_to`。iLink `sendmessage` 本身不渲染出站引用气泡，Muse 发往微信的 `reply_to_id` 在微信出站适配器中丢弃。
- 不支持群、按钮、编辑或语音发送。审批、凭证等走共享网关文本交互，不凭多行内容自动认领凭证，不要求微信管理员身份。

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

## 手动协议测试终端

仓库根目录运行，不启动 Muse、不读取 Muse Cookie：

```bash
python tests/wechat_cli.py --config config.json
```

默认先选择复用已保存登录或扫码登录；显式重新扫码可直接运行：

```bash
python tests/wechat_cli.py --config config.json --login
```

控制台复用微信 client/auth/state/gateway/media/sender，直接长轮询并打印收到的个人消息。普通文字发送到当前目标；默认扫码用户，`/to <用户ID>` 和 `--to` 可更换目标，`/peers` 查看最近收到消息的联系人。发送前需已有该用户的 context，缺失时提示对方先发来一条消息。

`/image <路径>`、`/file <路径>`、`/video <路径>` 发送媒体，路径可带空格和引号，保留 Windows 反斜杠；`/status` 只显示脱敏状态，`/help` 查看命令，`/quit`、EOF 或 Ctrl+C 停止轮询、关闭连接并退出。`/login` 只提示退出后用 `--login` 重新启动，不在运行中切换账号或创建第二个长轮询。

接收图片、文件、视频和原始语音自动下载至 `data/wechat_cli/downloads/`，有转写的语音也保留音频。`--download-dir` 可指定保存目录。文件名安全化并使用唯一名称、防止路径穿越和覆盖；下载受资源限额约束，失败仅输出安全摘要并记录为不确定结果，推进游标，避免反复下载。需要重新获取的失败附件应由对方重发。

**不可与同账号网桥同时运行。** 控制台使用同一份账号状态和游标，消费后的消息不再转给 Muse；已确认收到的消息不因重启重复下载。发送未知结果继续保守记录，不自动重发；手动重复输入是一次新的发送。自动测试只用合成事件、替身、临时目录，未进行真实扫码或远端传输验证。

## 凭据与数据保护

运行凭据为 JSON 明文，含 `bot_token`、`baseurl`、`bot_id`、`user_id`，不是加密保险箱。账号目录还可能含上下文、游标、去重与路由状态，应视作敏感数据。限制目录 ACL/文件权限，保护备份，不提交到 Git、不上传 Issue，不分享二维码、验证输入或完整凭据。终端登录本身会访问微信；自动化测试必须 mock state/auth/client，只使用临时文件，不读取真实账号、不发送真实流量。
