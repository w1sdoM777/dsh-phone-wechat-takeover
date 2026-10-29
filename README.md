# dsh-phone-wechat-takeover

**把一台安卓手机的微信变成 agent 的即时任务入口：微信一发消息，agent 立刻被唤醒接管并回结果。**

事件驱动，**零轮询**、空闲 **零 token**、**不装任何手机 App**、**不接微信机器人协议**——只用 `adb` 读安卓系统事件流（EventLog），命中微信新消息的那一刻才唤醒 agent。

> 目前已在一台备用 Redmi K60 Pro（Android 13 / MIUI）上真机跑通：
> 微信消息 → 事件捕获 → agent 唤醒 → 执行 → 微信回结果，端到端 ≈ **33 秒**，监听器全程 0 token。
>
> **使用手册（布防 / 换新会话 / 成本 / 后台进程 / 排障）见 [docs/USAGE.md](docs/USAGE.md)。**

---

## 它解决什么问题

以前的「微信任务通道」只能定时扫描：每 N 分钟截屏 + OCR 看一眼微信有没有新消息。代价是：

- 延迟几分钟到几十分钟；
- 不管有没有消息都在花钱（每轮都喂一遍对话上下文）；
- 本质是「监控屏幕」，手机被占用、误识别多。

本项目把触发源换成**系统事件**：

```
你的微信  ──发消息──>  备用手机上的微信
                          │ 系统写 EventLog：
                          │ notification_enqueue: [...,com.tencent.mm,...]
                          ▼
          adb logcat -b events（阻塞式事件流：没消息就永远没有输出）
                          │
             phone/wechat_trigger.py listen（常驻，0 token）
                          │ 命中 → 事件落盘（发送人 + 正文）→ 退出
                          ▼
          DSH 后台任务结束 → 用 follow-up 轮次唤醒 agent
                          │
          agent：读事件 → 读聊天页 → 当任务执行 → 微信回结果 → 重新布防
```

关键点：

- **监听器不调模型**：它只是本地 Python 进程读 adb 事件流；微信不发消息，它一个字节都不产生。
- **唤醒用 DSH 自带能力**：`@deepseek-ai/dsh-tool-jobs` 的完成通知会把空闲 agent 用 follow-up 轮次唤醒，
  所以「常驻监听器 = 后台任务」本身就是唤醒开关，不需要写插件、不需要定时任务。
- **事件里就有发送人和正文**（来自系统通知），通常不用先开手机屏幕；正文被微信设置脱敏时再走 OCR 兜底。

## 与现有方案对比

| 方案 | 消息来源 | 需要什么 | 代价 / 缺点 |
|---|---|---|---|
| 微信机器人桥（iLink / ClawBot / 各类 `dsh-*-wechat` 插件） | 微信机器人协议 | 扫码绑定机器人账号、消息过腾讯服务器 | 需要额外绑定/服务；消息不经端到端加密；协议非官方、可能漂移 |
| 通知转发 App（NotificationListenerService 类项目） | 手机通知 | 安装 APK、授予通知读取权限 | 要装第三方 App；部分 ROM 会杀后台 |
| **本项目** | **adb 读系统 EventLog** | 备用手机 + USB 线 + DSH | 需要一直连着线；微信前台开着聊天时微信自己不弹通知 |

## 环境要求

- Windows 10/11（用了 Windows 自带 OCR；Linux/macOS 需换 OCR 后端）
- [DSH / DeepSeek Harness](https://www.deepseek.com/harness/)（需要后台任务 + 空闲唤醒能力）
- 一台安卓手机：常亮或充电、开 USB 调试、**建议用微信小号**（隔离风险）
- adb（`tools\install-adb.ps1` 自动下载）
- 可选：[ADBKeyboard](https://github.com/senzhk/ADBKeyboard)（中文输入；不装则只能回英文/数字）
- 无网络 OCR 依赖：Windows 自带 `Windows.Media.Ocr`（zh-Hans），本地、零 token

## 快速开始

```powershell
# 1) 装 adb（下载 platform-tools 到 tools\）
powershell -ExecutionPolicy Bypass -File tools\install-adb.ps1

# 2) 安装 ADBKeyboard（可选，中文回复用）
powershell -ExecutionPolicy Bypass -File tools\fetch-adbkeyboard.ps1

# 3) 手机开 USB 调试并授权，确认 adb 能看到设备
python phone\adb.py devices

# 4) 配置「给你发消息的人」的微信备注名（对话页顶部那个名字）
setx WECHAT_CONTACT "我的微信备注名"

# 5) 自检
python phone\wechat_trigger.py status

# 6) 布防（在 DSH 里要用「后台任务」方式跑，见下节）
python phone\wechat_trigger.py listen --max-hours 0
```

布防后，让 `WECHAT_CONTACT` 那个人给手机发一条微信。正常会在十几秒内看到 agent 被唤醒。

## 命令

| 命令 | 作用 |
|---|---|
| `python phone\wechat_trigger.py listen` | 常驻事件监听；命中微信通知即落盘退出（给 DSH 后台任务用） |
| `python phone\wechat_trigger.py inbox` | 读聊天页新增消息、模糊去重、写入任务队列 |
| `python phone\wechat_trigger.py status` | 触发器状态 / 事件数 / 任务数 |
| `python phone\wechat_trigger.py reply --text "…"` | 微信回一条 |
| `python phone\wechat_trigger.py finish --id N --result "…"` | 标记任务完成并微信回结果 |
| `python phone\wechat_trigger.py test-notif` | 发系统测试通知（配合 `listen --include-shell` 自测事件链路） |
| `python phone\wechat.py read` / `open` | 读聊天页 / 打开目标聊天 |
| `python phone\adb.py shot shot.png` | 手机截图 |

数据落点（均被 `.gitignore` 忽略）：

```
runs/wechat_events/evt-*.json   每次事件（发送人/正文/原始 logcat 行）
runs/wechat_events/listen.log   监听器日志
runs/_wechat/trigger_state.json 已处理消息基线（相似度 ≥0.75 视为同一条）
runs/wechat_tasks/tasks.json    任务队列
```

## 在 DSH 里怎么接（关键）

1. 把 [`SKILL.md`](SKILL.md) 放进 `~/.dsh/skills/wechat-takeover/SKILL.md`（DSH 会热加载）。
2. 对 agent 说「布防」，它应当用 **`pwsh` 工具 + `run_in_background: true`** 启动：

   ```powershell
   cd <repo>; $env:PYTHONIOENCODING="utf-8"; python phone\wechat_trigger.py listen --max-hours 0
   ```

3. 监听器退出时，DSH 注入 `background job ... finished`，**空闲 agent 会被 follow-up 轮次唤醒**。
4. 醒来的 agent 按 SKILL.md 的固定流程走：**先重新布防 → 读事件 → 读新增消息 → 执行 → 微信回结果**。
5. DSH 重启 / 切会话后后台任务会消失，重新说一声「布防」即可。
6. **换会话 / DSH 重启后的完整步骤**（先撤防旧的、再在新会话布防；任何时候只保留一个监听器）
   见 [docs/USAGE.md 第 3 节](docs/USAGE.md)。

## 实测记录（2026-10-29）

- **18:22** 真机抓到第一条微信事件：`notification_enqueue: [10290,12560,com.tencent.mm,…]`，
  事件 JSON 解析出发送人 + 正文，`inbox` 建任务成功。
- **18:28 全链路**：微信消息 18:28:23 → 监听器 18:28:30 落盘并退出 → agent 立刻被唤醒
  → 重新布防 → 读任务 → 执行 → 18:28:56 微信收到结果。**端到端 ≈ 33 秒，无任何定时任务参与。**
- **成本**（DSH usage 记录）：监听器 0 token / ¥0；每条消息唤醒 ≈ **¥0.07**
  （37 万 token 大上下文、缓存命中 88%、deepseek-flash）。没有消息 = 不花钱。

## 已知限制（诚实披露）

- **微信在前台且正开着这个聊天**时，微信自己不弹通知 → 收不到事件。把微信切后台或熄屏即可。
- 该聊天被设「消息免打扰」、或微信通知被关闭 → 不产生通知事件。
- 微信关闭「显示消息详情」→ 事件正文被脱敏，此时靠 `inbox` 开屏 OCR 读正文。
- 对方发的是**图片/语音/文件**时没有文字正文，`inbox` 会返回 `note` 提示；用聊天页截图 + 视觉识别处理。
- DSH 关闭 / 切换会话 → 后台任务消失，需要重新布防。
- 手机设了安全锁屏（PIN/图案）时无法自动解锁（读消息/发消息会失败）；默认需「无密码滑动解锁」。
- 只验证了 Windows + 安卓；iOS 不能这样读事件。
- **微信用小号**：第三方自动化有账号风险，风险自担。

## 安全边界

- 「全权限」模式下 agent 能执行任意工具——**只绑定你自己的手机和你自己的账号**。
- 不上传任何聊天内容；事件与截图只落本地 `runs/`。
- 不要用来做骚扰、营销群发、刷单等违反微信规则的事。

## 目录结构

```
phone/
  wechat_trigger.py   事件监听 / inbox / status / reply / finish
  wechat.py           微信发消息（截屏 + 本地 OCR + ADBKeyboard 中文输入）
  wechat_task.py      聊天页气泡识别、去重、任务队列
  adb.py              adb 封装（截图 / UI 树 / 点击 / 文本）
  awake.py            熄屏自动点亮 + 上滑解锁
  cn_input.py         ADBKeyboard 中文输入
  ocr.py              Windows 本地 OCR
tools/
  ocr.ps1             Windows.Media.Ocr 调用脚本
  install-adb.ps1     下载 platform-tools
  fetch-adbkeyboard.ps1 下载 ADBKeyboard.apk
docs/USAGE.md         使用说明：布防、换新会话、成本、后台进程、排障
docs/DESIGN.md        设计细节、事件格式、失败模式
SKILL.md              DSH skill（布防/唤醒后流程/排障）
```

## License

MIT
