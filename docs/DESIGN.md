# 设计细节

本文记录实现里的关键事实、踩过的坑和验证方法，方便二次开发或移植到别的 agent 框架。

## 一、事件源：为什么 `notification_enqueue` 可靠

安卓系统在**每次**有 App 投递通知时，会往 EventLog（`logcat -b events`）写一条：

```
notification_enqueue: [uid,pid,pkg,id,tag,flags,Notification(channel=... category=msg ...),...]
```

实测微信新消息（脱敏后）：

```
I notification_enqueue: [10290,12560,com.tencent.mm,726213234,NULL,0,
    Notification(channel=message_channel_new_id shortcut=null ... tick defaults=0x0
    flags=0x11 color=0x00000000 category=msg vis=PRIVATE),0]
```

要点：

- `pkg` 是第 3 个字段（`com.tencent.mm`），过滤简单可靠。
- 这条事件由 `NotificationManagerService` 在**入队时**写，与通知是否响铃/横幅/免打扰无关；
  只要 App 真的 post 了通知就会写。
- 微信在「该聊天正开着且在前台」时**不会 post 通知**，所以那种情况没有事件——这是本方案唯一的
  主要盲区（README 已写）。
- 事件行本身不含正文；正文靠事件后立刻跑一次 `dumpsys notification --noredact` 解析
  `android.title` / `android.text` / `android.bigText` 拿到（只在命中事件时跑一次，不是轮询）。

## 二、监听器（`phone/wechat_trigger.py listen`）

- 命令：`adb -s <serial> logcat -b events -v threadtime -T 1`
  - `-T 1`：先打印最近的 1 行再持续跟随（避免读到历史事件）。
  - `-b events`：只读事件缓冲区，数据量极小。
- 主循环阻塞读 stdout（独立读线程 + 队列），5 秒超时只用于检查 **adb 进程是否还活着**，
  不检查微信内容、不截屏、不 OCR。adb 断线时以非 0 退出，触发 agent 自愈。
- 命中首个事件后进入 **burst 窗口**（默认 2 秒）：把同一批多消息/更新聚合进一个事件。
- 落盘 `runs/wechat_events/evt-<时间>.json`，同时把 JSON 打到 stdout（作为后台任务输出，唤醒后
  `job_output` 直接可读）。
- `--include-shell`：把 `com.android.shell` 通知也当事件，用于自测（`test-notif` 配套）。
- `--max-hours N`：到点正常退出（唤醒 agent 重新布防）；默认 0 = 不限。

## 三、唤醒机制：DSH 后台任务完成通知

本项目不写插件，直接借用 DSH 的行为（`@deepseek-ai/dsh-tool-jobs`）：

> 任务完成时，拥有它的 agent 会收到会话内消息
> `background job <id> (<kind>: <label>) finished ...`；
> **空闲的 agent 则被一个 follow-up 轮次唤醒**。

所以「常驻监听器」用 `pwsh` 工具的 `run_in_background: true` 启动即可：

- 监听器运行期间：0 token。
- 监听器退出：DSH 唤醒会话 → agent 读 `job_output` → 处理 → **再起一个新监听器**。

注意：

- 后台任务属于会话；**切换/关闭会话或 DSH 退出后任务消失**，需要重新布防。
- 监听器退出必须是非 hang 的（正常 0 或错误非 0 都会结算并通知）。
- 如果实现成了「agent 一直等」或「定时轮询」，就失去了零成本、零延迟的意义——不要那样做。

## 四、读消息（`inbox`）

1. `ensure_chat()`：点亮+上滑解锁 → 打开微信 → 找到 `WECHAT_CONTACT` 会话（OCR 模糊匹配，
   OCR 常把 `1` 认成 `l`/`i`）→ 进聊天页。
2. 截屏，按颜色切气泡：
   - 白 `(>250,>250,>250)` = 对方发来（in）
   - 绿（`g>180, r<190, g-r>40, g-b>60`）= 自己发出（out）
   - 聊天背景约 `(237,237,237)`，不会误判
   然后做连通域，按面积过滤，把 OCR 文本按气泡框归属。
3. **坑**：最新一条消息常贴在输入框上方（y≈2050–2160），旧的扫描边界 y=2060 会漏掉它。
   `extract_messages(shot, y_bottom=2185)` 修掉了这个问题。
4. 新消息判定：用「可见消息序列」做滑窗对齐（老消息滚出、新消息追加），再对文本做
   归一化 + 相似度去重（≥0.75 视为同一条）；纯数字/纯符号视为误触，不建任务。
5. 首次运行只建立基线（回翻 3 屏历史记入 processed），不把历史消息当任务。

## 五、发消息（`wechat.send`）

微信屏蔽了无障碍树（`uiautomator dump` 只有空节点），所以发送全走「截屏 + Windows 本地 OCR」：

1. 打开目标聊天 → 点输入框 → 用 ADBKeyboard 广播中文（`input text` 不支持中文）。
2. 找绿色「发送」按钮：颜色掩码 + 连通域 + 尺寸过滤（80–240 × 50–140 px），
   并排除绿色**消息气泡**（气泡面积大得多）。
3. 发送后先收键盘，再在气泡区找目标文字 + 校验气泡底色必须是绿色，
   避免把输入框里的草稿误判成「已发送」。

## 六、失败模式速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 监听器跑着但永远不触发 | 微信前台开着聊天 / 聊天免打扰 / 通知被关 | 切后台或熄屏；检查通知权限 |
| `listener-error: no-android-device` | USB 断 / 未授权 / 手机重启 | `python phone\adb.py devices`，恢复后重新布防 |
| `listener-error: logcat-exited` | adb server 被杀 | 同上，重启 adb |
| `inbox` 返回 `ensure_chat-failed` | 微信不在前台/不在目标会话/安全锁屏 | 人工看一眼手机；确认无 PIN 锁屏 |
| 事件正文是「你收到一条新消息」 | 微信关闭了「显示消息详情」 | 正常，`inbox` 用 OCR 读正文 |
| 重复建任务 | 微信重发/更新通知 | 已用相似度去重；必要时改 `tasks.json` 状态 |
| 发消息 `verify` 失败 | 发送按钮没找到/草稿没发出去 | 看 `runs/_wechat/before_send.png`、`verify.png` |

## 七、验证方法

1. **只验证事件链路（不用真发微信）**：

   ```powershell
   # 终端 A：把 shell 通知也当事件
   python phone\wechat_trigger.py listen --include-shell --burst 1.5
   # 终端 B：
   python phone\wechat_trigger.py test-notif
   ```

   A 应打印 `wechat-notification` 事件 JSON（含 `pkgs: ["com.android.shell"]` 与通知标题/正文）。

2. **验证唤醒链路（不用真发微信）**：把监听器作为 DSH 后台任务启动（`--include-shell`），
   再在别的进程里 `test-notif`；监听器退出后会话应被自动唤醒。

3. **端到端**：布防（不带 `--include-shell`）→ 让 `WECHAT_CONTACT` 发一条微信 → 观察
   agent 是否自动醒来、执行、微信回结果。

## 八、成本测量

- 监听器：本地进程 + adb，无 API 调用，**0 token**。
- 唤醒回合：DSH 每次唤醒都是一次完整模型回合，成本 ≈ 会话上下文 × 请求数 × 单价。
  本项目实测：37 万 token 会话、缓存命中 88%、deepseek-flash ≈ **¥0.07/条**。
- 想省钱：保持会话别太长，周期性开新会话并重新布防；或给监听器设 `--max-hours` 减少心跳唤醒。

## 九、可移植性

- 事件源部分是 Android 通用的，任何能「收到本地进程退出事件」的 agent 框架都能复用
  （把 `listen` 当作事件生产者，退出码/输出当作事件载荷）。
- 只有「唤醒」和「读/发消息」绑定本机环境：唤醒绑定 DSH 后台任务；发送绑定 Windows OCR + ADBKeyboard。
  移植到 Linux/macOS 需要替换 OCR（例如 RapidOCR / tesseract）。
