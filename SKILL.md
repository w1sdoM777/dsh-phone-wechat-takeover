---
name: wechat-takeover
description: 把手机微信变成 agent 的即时任务入口——指定联系人一发消息，agent 立刻被唤醒接管执行并微信回结果。事件驱动（adb 事件流 + DSH 后台任务唤醒），不做定时扫描。用户说「布防」「微信一发消息就接管」「微信发消息自动处理」「不要按时扫描」「撤防」「看看触发器状态」时使用。
---

# 微信即时接管（事件驱动，不轮询）

## 这是什么

用户需求：**指定联系人（`WECHAT_CONTACT`）在手机微信上一发消息，agent 立刻接管，把消息当任务处理，完成后微信回结果；不要定时扫描。**

实现链路（本仓库已验证）：

```
WECHAT_CONTACT 发微信 → 手机微信 POST 通知
   ↓（Android EventLog，阻塞事件流，非轮询）
adb logcat -b events  →  notification_enqueue: [...,com.tencent.mm,...]
   ↓  phone/wechat_trigger.py listen（常驻后台任务，命中即退出）
DSH 后台任务结束 → DSH 唤醒本会话（follow-up 轮次）
   ↓
agent：job_output 读事件 → wechat_trigger.py inbox 读新增消息/建任务
   ↓
当成用户任务执行 → 微信回结果 → **立刻重新布防** → 收工
```

关键点：
- **不是定时扫描**：监听器是 `adb logcat -b events` 的阻塞读，微信不发通知就一个字节都不产生。
- **唤醒靠 DSH 后台任务完成通知**：空闲 agent 会被 follow-up 轮次唤醒（同会话、同上下文）。
- 事件里直接带 **发送人 + 消息正文**；正文被脱敏时用 `inbox` 的 OCR 兜底。

## 前置

- 仓库根目录记为 `<REPO>`（例如 `D:\dsh-phone-wechat-takeover`）。
- 环境变量 `WECHAT_CONTACT` = 给你发消息那个人的微信备注名（对话页顶部名字）。
- 手机 USB 连接、adb 可用：`python phone\adb.py devices` 能看到设备。
- 微信已登录、微信在**后台**（前台开着聊天时微信自己不弹通知）。

## 一、布防（每次会话 / DSH 重启后做一次）

1. 自检：

```powershell
cd <REPO>; python phone\wechat_trigger.py status
```

2. 确认微信在后台：`adb shell "dumpsys window | grep -m1 mCurrentFocus"`，
   若前台是 `com.tencent.mm`，按 Home：`adb shell input keyevent 3`（熄屏也可以收到事件）。

3. **用 DSH 后台任务启动监听器**（关键：必须后台任务，它结束才会唤醒 agent）：

   工具 `pwsh`，`run_in_background: true`，命令：

   ```powershell
   cd <REPO>; $env:PYTHONIOENCODING="utf-8"; python phone\wechat_trigger.py listen --max-hours 0
   ```

   - `--max-hours 0` = 不限时长（推荐）；`--max-hours 12` 可做每日心跳自愈。
   - **不要**加 `--include-shell`（那是自测用，会把系统通知也当事件）。
   - 起完用 `job_list` 确认状态 running，记住 job id。

4. 告诉用户：已布防；DSH 关掉/换会话后需要重新布防。

## 二、被唤醒后要做什么（顺序很重要）

DSH 会注入 `background job <id> (...) finished ... Read its output with job_output`。
**每一步都别漏，尤其是第 1 步和第 7 步。**

1. **先重新布防**（在开始处理之前把新监听器起起来，避免处理期间漏消息）——重复上面第 3 步。
2. `job_output(job_id)` 读事件 JSON；拿不到就读 `runs\wechat_events\` 下最新的 `evt-*.json`。
3. 判定是不是真任务：
   - `pkgs` 只有 `com.tencent.mm`；`notifications[0].title` 应含 `WECHAT_CONTACT`（表情可能显示成 `[表情]`）。
   - 其它联系人/公众号/系统通知 → 不回复、不建任务，直接收工（监听器已重新布防）。
4. 跑 `python phone\wechat_trigger.py inbox` → 返回 `new_tasks`（已入 `runs/wechat_tasks/tasks.json`）和 `context`
   （最近聊天上下文）。首次运行会 `init: true`（只建基线，不派任务），正常。
   - 若返回 `note: 没有新的文字消息…`：对方发的是**图片/语音/文件**，`inbox` 取不到正文 →
     开聊天页截图（`python phone\adb.py shot runs\chat.png`），图片气泡裁剪后直接看图或送视觉模型识别。
   - 事件里 `notifications` 为空（dumpsys 没解析到）时同理：以 `inbox` + 截图为准。
5. **当成用户任务执行**：消息内容就是指令，用平常工具干活。
   - 测试/问候/闲聊 → 简短回应，不要加戏。
   - 含糊 → 微信回一条澄清问题，不要瞎猜。
6. **微信回结果**：
   - 有任务 id：`python phone\wechat_trigger.py finish --id <id> --result "结果摘要"`（同时标记 done）。
   - 纯回复：`python phone\wechat_trigger.py reply --text "结果摘要"`。
   - 没消息的时候不要主动回；每条消息只回一条结果。
7. **确认监听器在跑**：`job_list` 里应有一个 running 的 listener，然后收工。

## 三、撤防 / 状态 / 排障

```powershell
cd <REPO>
python phone\wechat_trigger.py status          # adb、事件数、任务数、最近事件
python phone\wechat_trigger.py inbox           # 手动读一次新消息并建任务
python phone\wechat_trigger.py reply --text "…"
python phone\wechat_trigger.py test-notif      # 系统测试通知（配合 listen --include-shell 自测）
```

- **撤防**：`job_list` 找到 listener 的 job id → `job_kill`。
- **监听器自己退出（listener-error）**：job 结束会唤醒 agent（自愈入口）。常见原因：USB 断/手机重启/
  adb 掉线/安全锁屏。先 `python phone\adb.py devices` 确认，恢复后重新布防。
- **收不到事件的三类原因**（按概率）：
  1. 微信正前台开着这个聊天 → 微信不弹通知。切后台/熄屏即可。
  2. 该聊天「消息免打扰」或微信通知被关 → 不产生 notification_enqueue。
  3. adb 掉线 / DSH 关了 / 监听器被撤防。
- **正文脱敏**：`notifications[].text` 变成「你收到一条新消息」之类时，用 `inbox` 开屏 OCR 读聊天页。
- **重复事件**：微信会重发/更新通知；`inbox` 有模糊去重（相似度 ≥0.75），一般不用手工清理。
  误建任务把 `tasks.json` 里 status 改成 `cancelled` 并写原因。

## 四、成本（实测参考）

- 常驻监听器：**0 token / ¥0**（纯本地 adb 事件流，不调模型）；没有消息 = 不花钱。
- 每条消息唤醒一次 agent 回合：大上下文会话（≈37 万 token、缓存命中 88%）实测 **≈¥0.07/条**
  （deepseek-flash）。会话越长单次唤醒越贵；想省就周期性开新会话并重新布防。
- 数据源：DSH `~/.dsh/.dshw-usage.json` 的 `events`（按回合记录 cost/tokens/model）。

## 五、验证记录（2026-10-29）

- 真机（Redmi K60 Pro / Android 13 / MIUI）：事件捕获 → 解析出「发送人 + 正文」→ `inbox` 建任务 → 微信回结果。
- 全链路：消息 18:28:23 → 监听器 18:28:30 落盘退出 → agent 立即被唤醒 → 18:28:56 微信收到结果（≈33 秒）。
- 旧的 60 秒轮询 watcher + 每 30 分钟定时任务方案已废弃：**不要恢复定时扫描**。

## 六、相关文档

- `README.md`（环境、命令、与现有方案对比、已知限制）
- `docs/DESIGN.md`（事件格式、气泡识别、去重、失败模式、验证方法）
- `phone/wechat.py`（发消息底座：截屏 + Windows OCR + ADBKeyboard）
- `phone/awake.py`（熄屏自动点亮 + 上滑解锁）
