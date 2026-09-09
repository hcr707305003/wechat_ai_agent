# Agent Session Bridge

一个面向渠道和 Agent 的双向适配层。首版通过 `wechatauto-replica` 接入 Windows 微信，并将每个私聊或群聊映射为一个持久化 `UnifiedSession`。同一逻辑 session 可选择 Codex 或 Claude，底层原生 thread/session ID 和统一消息历史由 SQLite 保存。

## 环境要求

- Windows 10/11
- Python 3.10+
- 微信 4.1.12+，已登录
- Codex 或 Claude 已按其官方 SDK 要求完成本机认证

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[all,test]"
Copy-Item config.example.yaml config.yaml
```

编辑 `config.yaml`：

- `default_working_directory`：Agent 默认工作目录。
- `allowed_roots`：所有 session 工作目录必须位于这些目录下。
- `allowed_private_ids`：允许接入的私聊。可填写内部 `wxid_...`、可见微信号、唯一昵称或唯一备注名；`filehelper` 表示文件传输助手。
- `allowed_group_ids`：允许接入的群聊原始 ID，通常以 `@chatroom` 结尾。
- `session_bindings`：按私聊或群聊固定 Agent 原生 session；`conversation_id` 可填写内部 ID、微信号、唯一昵称或群名，`conversation_type` 可选 `private` / `group`。
- `group_controllers`：可在群内切换或绑定 Agent session 的成员 wxid。
- `companion.mode`：`docked` 表示跟随微信，`independent` 表示独立窗口。
- `companion.side`：跟随模式下可设为 `left`、`right`、`top` 或 `bottom`。
- `companion.width` / `height`：伴随窗口的基础宽高。
- `companion.follow_interval`：跟随微信位置的检测间隔，默认 `0.016` 秒（约 60 FPS）。
- `companion.theme`：`system` 跟随 Windows，也可强制为 `light` 或 `dark`。
- `sender.mode`：默认 `idle_uia`，只使用后台 UIA Pattern 发送文本；不会移动或点击鼠标。
- `sender.idle_seconds`：连续无键鼠输入多久后开始发送，默认 `1.5` 秒。
- `sender.max_queue_age_seconds`：回复最长排队时间，默认 `600` 秒，超时后不会补发旧回复。
- `sender.mouse_fallback`：`idle_uia` 模式必须为 `false`，配置为 `true` 会直接拒绝启动。
- `sender.foreground_fallback_default`：前台备用发送的首次默认值，默认 `false`；工作台中的账号级开关保存后优先使用保存值。
- `sender.foreground_driver`：默认 `wechat_mcp`，前台授权后优先使用 WeChatMCP 已验证的 `Ctrl+F` 搜索与回车发送流程；`uia` 可强制使用旧驱动。
- `sender.foreground_operation_timeout_seconds`：单次前台自动化的硬超时，默认 `8` 秒；超时会终止隔离子进程，并将本次结果标记为未知，避免重复发送。
- `sender.foreground_quote_timeout_seconds`：引用发送的独立硬超时，默认 `30` 秒；普通前台发送仍使用上述 8 秒限制。
- `sender.foreground_cooldown_seconds`：前台自动化超时后的熔断时间，默认 `60` 秒；熔断期间消息保留在队列中，不消耗重试次数。

## Windows 安装包与管理面板

从 [GitHub Releases](https://github.com/hcr707305003/wechat_ai_agent/releases) 下载 `AgentBridge-Setup-x64.exe`，校验同页 SHA-256 后运行安装。安装包未做代码签名，Windows 可能提示未知发布者；请确认下载来源和校验值后再决定是否安装。

Windows 10/11 64 位最终用户使用 `AgentBridge-Setup-x64.exe` 安装，不需要另外安装 Python。安装目录默认是 `C:\Program Files\Agent Bridge`，安装时可以修改。

双击安装目录中的 `AgentBridge.exe` 会打开管理面板。这个 EXE 依赖旁边的 `_internal` 目录，不能单独复制一个 EXE 运行；分发请使用安装包。首次使用仍需登录本机微信，并完成 Codex 或 Claude 的个人认证；安装包不包含作者的登录信息、个人配置或聊天记录。

安装完成后从桌面或开始菜单打开“Agent Bridge 管理面板”。管理面板提供：

- 工作台运行状态、启动、收起/打开和正常关闭。
- 基础运行、微信白名单、群聊触发、发送、引用、Codex、Claude 和工作台外观配置。
- 多个私聊或群聊的原生 Session 绑定表格。
- 保留未知字段的高级 YAML 编辑。
- 环境检查、工作台日志和系统托盘入口。

源码环境可直接预览和使用同一个管理面板：

```powershell
.\.venv\Scripts\python.exe -m agent_bridge.manager_main
```

源码运行时，管理面板默认读取并保存项目根目录的 `config.yaml`。安装版默认使用 `%LOCALAPPDATA%\AgentBridge\config.yaml`；需要覆盖路径时仍可传入 `--user-data-dir`。

关闭管理面板窗口只会隐藏到系统托盘，不会中断正在运行的工作台。选择“退出管理程序”也不会强制关闭工作台；需要停止监听时，请先使用“关闭工作台”。管理面板不会启动、停止或强杀微信、PHP 或其他业务服务。

工作台显示时，面板和托盘菜单的按钮为“收起工作台”；收起或最小化后变为“打开工作台”。收起只隐藏窗口，监听和回复继续运行，状态仍为“已开启”。贴靠模式收起后保留悬浮入口；独立窗口模式可从管理面板或托盘恢复。首次使用此功能需重新打开管理面板，并在工作台下次启动时加载新的显示状态接口。

在“Agent → 聊天调试”中可选择 Codex 或 Claude，点击“测试连接”发起一次真实回复请求，或输入消息连续聊天。回复以打字机效果流式显示，支持 Ctrl+Enter 发送、停止生成和新建对话。调试使用当前表单中的模型、权限和工作目录，校验但不保存配置；调试会话独立于微信会话，不向微信发送消息。切换 Agent、新建对话、取消或失败后不会继续使用原来的调试上下文。真实登录、模型可用性和网络问题会显示在调试结果中。

用户配置、聊天历史、日志和缓存保存在 `%LOCALAPPDATA%\AgentBridge`。升级和普通卸载默认保留这些数据；卸载时只有明确勾选数据删除选项才会一并删除。

### 构建安装包

构建机需要项目完整开发环境、PyInstaller 和 Inno Setup 7（也兼容 Inno Setup 6）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\scripts\build-installer.ps1
```

脚本会依次执行测试、Python 编译检查、PyInstaller 冻结构建、冻结程序冒烟测试和 Inno Setup 编译。最终文件位于 `dist\installer`：

```text
AgentBridge-Setup-x64.exe
AgentBridge-Setup-x64.exe.sha256
```

如果 Inno Setup 未安装，可以显式传入编译器：

```powershell
.\scripts\build-installer.ps1 -InnoCompiler "D:\Tools\Inno Setup 6\ISCC.exe"
```

不要把 API Key 或登录 Token 写入 YAML。SDK 使用环境变量或本机已有登录状态。

多个会话可以分别绑定：

```yaml
channels:
  wechat:
    session_bindings:
      - conversation_id: example_friend
        conversation_type: private
        provider: codex
        session_id: 00000000-0000-4000-8000-000000000001
      - conversation_id: example_group@chatroom
        conversation_type: group
        provider: claude
        session_id: 550e8400-e29b-41d4-a716-446655440000
```

同一个 Agent 原生 session 可以绑定多个微信会话；每个微信会话仍保留独立的逻辑历史和回复队列，但 Agent 看到的是共享的原生上下文。

## 运行

先进行不会启动微信监听、不会调用 Agent、不会操作鼠标的检查。微信已登录时，`doctor` 还会只读检查静默发送所需的 UIA Pattern：

```powershell
python -m agent_bridge --config config.yaml doctor
```

确认微信客户端已由你自己启动并登录后，再运行桥接进程：

```powershell
python -m agent_bridge --config config.yaml run
```

正常启动后会出现 PySide6 实现的“微信 Agent 工作台”伴随窗口，并看到类似日志：

```text
微信白名单监听已启动: account=... conversations=...
Agent bridge is running. Close the companion window or press Ctrl+C to stop.
```

静默发送能力正常时，`doctor` 会包含：

```text
[OK] wechat_idle_uia: SetValue + Select + Invoke
[OK] wechat_foreground_fallback: available; default=off
```

收到一条满足白名单和群聊触发规则的消息后，会看到：

```text
收到微信消息: conversation=... type=... sender=... message_id=...
Agent 任务已提交: job=... session=... provider=... conversation=...
Agent 任务开始执行: job=... session=... provider=...
```

监听器只注册 `allowed_private_ids` 和 `allowed_group_ids` 中的会话，不会扫描全部微信会话。左侧只显示这些白名单会话。每个会话的“开启回复”默认关闭：关闭期间仍在窗口显示实时消息，但不调用 Agent，也不会发送确认或回复，因此不会切换 PC 微信当前会话。

启动时会把可见微信号、昵称或备注名解析为微信内部会话 ID，并输出 `微信白名单已解析: 配置值 -> wxid_...`。如果昵称匹配多人，为避免接错私聊不会自动选择，请改用日志或微信数据库中的内部 `wxid_...`。

右侧“设置”可分别控制当前会话：

- “开启回复”：开启后仅处理之后收到的新消息，状态会持久化。
- “允许 Agent 发送图片”：默认关闭；开启后 Agent 可发送当前 Session 工作目录内的 PNG、JPEG、GIF、WebP 或 BMP 图片，每轮最多 3 张、单张最多 20 MiB。
- “加载微信历史消息”：默认关闭；开启后可加载 `1–500` 条，只用于窗口展示，不进入 Agent 上下文。

文件传输助手适合用手机向 PC 发送测试消息。桥接自身发出的文件助手消息会被识别并阻止再次进入 Agent，避免回复循环。

会话触发 Agent 后，工作台会在右侧同一张回复卡中显示 Codex 或 Claude 的真实流式文本；生成完成前不会向微信发送内容。最终文本完成后才进入 SQLite 发件箱。你持续使用键盘或鼠标时，卡片显示“等待你停止操作后自动发送”；空闲达到配置时间后，系统先通过 UIA `SetValue`、`Select` 和 `Invoke` 尝试后台发送。微信数据库验证成功后卡片标记“已发送”，并提供“再次发送”；失败或过期时提供“重新发送”。该主路径不调用 `SetCursorPos`、鼠标事件、坐标点击或 `SendKeys`。

右上角齿轮设置中有一项“允许前台备用发送”，按当前微信账号保存且默认关闭。开启后，后台能力不可用时，系统可在再次确认目标、草稿和空闲状态后短暂激活微信，对已确认的输入框触发一次回车，并在微信数据库验证后恢复原前台窗口和置顶状态。目标歧义、用户草稿或发送结果未知时不会自动重发。排队任务可在消息卡片中取消，失败或过期任务可手动重新发送；超过最长排队时间的旧回复不会补发。

图片发送需要同时开启当前会话的“允许 Agent 发送图片”和账号级“允许前台备用发送”。Agent 只会收到受控图片指令；桥接会解析并移除指令、校验真实路径仍位于 Session 工作目录内，再通过 Windows 文件剪贴板粘贴到已核验的目标会话。工作台显示图片预览与独立发送状态，微信图片回显会关联原卡片，不会生成重复回复卡片。URL、工作目录外路径、超限文件和不支持的格式都会被拒绝。

本项目不会代为启动或重启微信、Codex、Claude 之外的业务服务。停止桥接进程使用 `Ctrl+C`。

## 微信使用方式

私聊白名单会话中的文本直接进入对应 `UnifiedSession`。群聊必须满足白名单，并通过以下方式触发：

- `@机器人`；或
- 消息按 `group_prefixes_rule` 匹配配置的群聊触发词（默认 `/ai` 仅匹配开头；可设为 `contains` 或 `suffix`）。

控制命令：

```text
/agent codex
/agent claude
/ask codex <任务>
/ask claude <任务>
/session info
/session new <agent>
/session bind <agent> <session-id>
/session history <agent>
/cancel
/help
```

每个渠道会话只有一个逻辑 session。切换 Agent 时逻辑 session 不变；系统保留各 Provider 的原生句柄，并通过统一摘要和最近消息为新 Provider 构造上下文。

## 安全默认值

- Codex 默认 `workspace-write` + `deny_all`，不允许升级为整机权限。
- Claude 默认通过 SDK 权限回调把读写限制在 session 工作目录，并禁用 Bash 与网络工具；可在配置中显式开启。
- 微信白名单默认只包含文件传输助手。
- 消息不能修改允许根目录、白名单或凭据。
- 每个私聊或群聊各自使用独立的进程内 FIFO 回复队列；同一会话按顺序处理，不同会话可并行。
- 工作台的“队列”按钮可查看并删除等待中的单条消息，或清空当前会话的等待队列；正在处理的任务不会被删除或清空。
- 回复队列不持久化：重启后排队中或运行中的任务标记为 `interrupted`，未发送的出站回复标记为 `expired`，都不会自动重复执行；已接收消息仍保留在历史记录中。

## 测试

普通测试只使用 Fake Channel 和 Fake Agent，不连接真实微信、Codex 或 Claude：

```powershell
python -m pytest -q
```

内部设计文档保留在本地，不随公开源码或安装包发布。
