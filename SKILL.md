---
name: wechat-takeover
description: 让手机微信变成 agent 的即时任务入口——目标联系人 一发消息，agent 立刻被唤醒接管执行并微信回结果。事件驱动（adb 事件流 + DSH 后台任务唤醒）+ 前台看板（微信停在前台/聊天页时也能识别），空闲不扫描、不耗 token。用户说「布防」「微信一发消息就接管」「目标联系人 发消息自动处理」「微信开着也能接管」「当前页有消息也识别」「不要按时扫描」「撤防/看看触发器状态」时使用。
---

# 微信即时接管（事件驱动 + 前台看板）

## 这是什么

用户的需求：**目标联系人 在手机微信上一发消息，agent 立刻接管，把消息当任务处理，完成后微信回结果；不要定时扫描去消耗 token。**

两条检测链路（都只在**真有消息**时才唤醒 agent）：

```
① 微信在后台/熄屏（主路径，2026-10-29 真机验证）
目标联系人 发微信 → 手机微信 POST 通知
   ↓ Android EventLog 阻塞事件流（非轮询）
adb logcat -b events → notification_enqueue: [...,com.tencent.mm,...]
   ↓  phone/wechat_trigger.py listen（常驻后台任务，命中即退出）
DSH 后台任务结束 → DSH 唤醒本会话（follow-up 轮次）

② 微信停在**前台**（2026-10-31 加：微信前台时自己不弹通知，①会漏）
listen 同时从同一条事件流免费读 wm_set_resumed_activity / screen_toggled
   ↓ 微信不在前台 → 一个 adb 调用都不做（不截图、不 OCR、0 token）
   ↓ 微信在前台 → 每 3s 在手机本地截一张图算 md5（≈0.5s，0 token）
   ↓ 只有画面变了才把图拉回电脑做本地 OCR（Windows OCR，0 token）
   ↓ 只有「目标聊天页出现新的对方气泡 / 会话列表 目标联系人 有未读红标 / 微信提示新消息」才落事件

落事件后（两条链路一样）：
agent 醒来：job_output 读事件 → wechat_trigger.py inbox 读新增消息/建任务
   ↓ 当成用户任务执行 → 微信回结果 → **立刻重新布防** → 收工
```

关键点：
- **不是定时扫描烧 token**：没有消息时，agent 一个回合都不会被叫醒；监听器本身 0 token。
  微信不在前台时连截图都不做（实测：后台 9 秒内截图文件 mtime 不变）。
- **唤醒靠 DSH 自己的后台任务完成通知**：空闲 agent 会被 follow-up 轮次唤醒（同会话、同上下文）。
- 事件里带 **发送人 + 消息正文**（`notifications[].title/text`）；正文被脱敏/来自前台看板时，
  用 `inbox` 的 OCR 兜底。

## 文件与命令

| 文件 | 作用 |
|---|---|
| `D:\手机agent\phone\wechat_trigger.py` | 全部入口（listen / inbox / status / reply / finish / busy / fg-probe / fg-selftest / test-notif） |
| `D:\手机agent\runs\wechat_events\evt-*.json` | 每次事件落盘（含通知标题/正文/前台看板识别结果） |
| `D:\手机agent\runs\wechat_events\listen.log` | 监听器日志（前台看板的状态变化也写这里） |
| `D:\手机agent\runs\_wechat\trigger_state.json` | 已处理消息基线（模糊去重，防重复建任务） |
| `D:\手机agent\runs\_wechat\foreground_badge.json` | 会话列表未读红标的签名基线（前台看板用） |
| `D:\手机agent\runs\_wechat\busy.lock` | 「agent 正在操作手机」标记：前台看板暂停（`inbox` 自动加/删，3 分钟超时自愈） |
| `D:\手机agent\runs\_wechat\sending.lock` | `wechat.send` 发送中标记：前台看板暂停，避免把 agent 自己发的消息当新消息 |
| `D:\手机agent\runs\wechat_tasks\tasks.json` | 任务队列（`inbox` 自动建任务） |
| `D:\手机agent\phone\session_ctx.py` | 上下文用量估算：`--check` 返回 10（该交接）/20（已压缩），数据直接读 DSH 会话日志 |
| `D:\手机agent\phone\handoff.py` | 会话交接：写 `runs/memory/MEMORY.md` + 新会话启动消息 `runs/memory/SEED.md` |
| `D:\手机agent\runs\memory\MEMORY.md` | 跨会话记忆（新会话开局必读，交接时自动生成） |

所有命令在 `D:\手机agent` 下执行，依赖 `tools\platform-tools\adb.exe`（脚本自动找）。

## 一、布防（每次会话/DSH 重启后做一次）

1. 先自检（手机连线、adb 在线、触发器状态）：

```powershell
cd D:\手机agent; python phone\wechat_trigger.py status
```

2. 微信切到后台或熄屏最省电（不是必须）：前台看板现在也能接管，但后台/熄屏走系统通知更稳。
   `adb shell "dumpsys window | grep -m1 mCurrentFocus"` 显示 `com.tencent.mm` 时可
   `adb shell input keyevent 3` 回桌面（相当于按 Home）。**注意**：微信停在前台时，
   看板会每 3s 截一张图（本地、0 token），屏幕常亮时会一直跑——想省电就切后台/熄屏。

3. **用 DSH 后台任务启动监听器**（关键：必须以后台任务方式跑，它结束才会唤醒 agent）：

   工具 `pwsh`，`run_in_background: true`，命令：

   ```powershell
   cd D:\手机agent; $env:PYTHONIOENCODING="utf-8"; python phone\wechat_trigger.py listen --max-hours 0 --claim
   ```

   - `--claim` = **接管所有权**（用户口头说「布防」时才加）：自动停掉其它会话的监听器，
     并清掉残留的 `busy.lock`，实现「新会话布防 → 老对话的布防自动撤销」。
   - 之前做过全局撤防（`runs/_wechat/DISARMED` 存在）时，`--claim` 会**自动解除撤防**，不需要先 `arm`。
   - `--max-hours 0` = 不限时长（推荐）。`--max-hours 12` 可做每日心跳自愈。
   - **不要**加 `--include-shell`（那是自测用的，会把系统通知也当事件）。
   - 前台看板默认开启，参数：`--fg-interval 3`（微信前台时截图间隔秒）、
     `--fg-idle-interval 6`（画面连续 2 分钟没变后降频）、`--fg-verify 60`（dumpsys 校正前台状态）、
     `--no-foreground-watch`（关闭前台看板，只走通知）。
   - **手机暂时不在线时**（拔了线 / 重启后停在锁屏 / adb 掉线）：加 `--device-wait 21600`
     （6 小时）。监听器会**静默等手机上线**：不唤醒 agent、0 token、不反复重启 adb server，
     手机一回来（人解锁/重插线后）自动开始监听。不带这个参数默认只等 900 秒，等不到就退出唤醒一下。
   - 起完用 `job_list` 确认状态是 running，并记住 job id。
   - 日志自查：`listen.log` 里应出现
     `前台看板启用（当前前台=… 亮屏=… 间隔=3.0s）`；微信在前台时随后会有
     `前台看板 baseline(N)` / `no-badge`，代表已进入看板工作状态。

4. 告诉用户：已布防（已接管）；之前布防过的老会话会被自动撤销，目标联系人 发微信即会被本会话接管——
   **无论微信在前台聊天页还是后台，都能识别**。

## 二、前台看板能识别什么、识别不了什么（2026-10-31 加）

**能识别**（微信停在前台时）：
- 目标聊天页出现新的**对方文字气泡**（白色气泡）。判定规则是「在上次最后一条消息**下方**新出现」，
  所以往上翻旧消息不会误报；识别到后会**再截一张图二次确认**才落事件。
- 目标聊天页底部微信自己的**「新消息」提示**（上翻历史时来消息，正文不一定在可见区）。
- 会话列表里 **目标联系人 那一行的未读红标**（本机微信画在头像右上角）；红标出现/数字变化（1→2）都会触发。
  红标签名持久化在 `foreground_badge.json`，重复布防不会重复唤醒。

**识别不了**（按现状）：
- **纯图片/语音/视频消息**：聊天页前台时没有文字气泡可读，不会唤醒（后台通知路径不受影响）。
- 微信停在**别人的聊天页/朋友圈/设置页**等非列表页时看不到未读信号，可能漏；
  建议把微信切回后台或熄屏（走系统通知）。
- 手机**熄屏**时不看板（`screen_toggled 0` 暂停），走系统通知。

**防误报/防自触发**：
- `inbox` 执行期间自动加 `busy.lock`（结束时自动删），`wechat.send` 发送期间加 `sending.lock`，
  看板遇到这两个标记就暂停，不会把 agent 自己的导航/打字/发送当成新消息。
- 文件卡片气泡（`106.7 MB`、`自检报告.md 5.8KB` 这类）不算文字消息，不会误唤醒。
- 画面低分辨率指纹（36×90 灰度）只有真变化才 OCR：只有状态栏时钟变了会直接跳过。
- 微信不在前台时**完全不做截图/OCR**（状态从 logcat 事件流免费更新，每 60s 用 dumpsys 校正一次；
  若连续两轮 OCR 都看不到微信界面，会立刻再校正一次，防止 wm 事件漏报后一直扫错页面）。

## 三、被唤醒后要做什么（**顺序很重要**）

DSH 会注入一条 `background job <id> (...) finished ... Read its output with job_output`。
**每一步都别漏，尤其是第 1 步和第 6 步。**

1. **先重新布防**（在开始处理之前把新监听器起起来，避免处理期间漏消息）——重复上面「布防」第 3 步，
   **但不要加 `--claim`**：自动重布防只续自己的所有权；若 owner 是别的会话就只进入待命，不会抢回来。
   **手机不在线**（`adb devices` 空/offline）时加 `--device-wait 21600`：静默等手机回来，别再频繁唤醒。
2. `job_output(job_id)` 读事件 JSON。若拿不到，就读 `runs\wechat_events\` 下最新的 `evt-*.json`。
3. 判定是不是真任务（看 `event` / `detected_via` / `pkgs`）：
   - `wechat-notification`：按 `notifications[0].title` 判定发送人（应含 `目标联系人`；表情可能显示成 `[表情]`）。
     其它联系人/公众号/系统通知 → 不回复、不建任务，直接收工（监听器已重新布防）。
   - `wechat-foreground-message`：`detected_via` 三种：
     - `target-chat`：`messages[]` 里已经带正文，直接当任务（`inbox` 仍要跑一遍以更新去重基线）。
     - `conversation-list-badge`：只知道有未读、没正文 → **必须 `inbox` 读**。
     - `unread-chip`：上翻历史时来的消息 → 先让聊天页回到底部（`wechat.py open` 会自动到最新），再 `inbox`。
4. `python phone\wechat_trigger.py inbox` → 返回 `new_tasks`（新消息已入 `tasks.json`）和 `context`（最近聊天上下文）。
   - 通知正文已经够用时可跳过 OCR；**但涉及「上面那个图/上一条」这种指代，必须看 `context`**。
   - 首次运行会返回 `init: true`（只建基线，不派任务），属正常。
   - 若返回 `note: 没有新的文字消息…`：对方发的是**图片/语音/文件**，`inbox` 取不到正文 →
     开聊天页截图（`python phone\adb.py shot runs\_chat_latest.png`），图片气泡裁剪后直接看图（多模态）
     或用视觉模型识别；语音/文件按需再处理。
   - **前台看板误报时** `new_tasks` 为空：这是本地 OCR 抖动，**不要微信回复**，确认监听器在跑（第 7 步）就收工。
     （`inbox` 结束会自动清 `busy.lock`，看板恢复，不会漏后续消息。）
   - 事件里 `notifications` 为空（dumpsys 没解析到）时同理：以 `inbox` + 截图为准。
5. **当成用户任务执行**：消息内容就是指令。用平常的工具去干活（读文件、跑命令、写交付物）。
   - 消息若只是问候/测试/闲聊 → 简短回应即可，别加戏。
   - 消息含糊 → 微信回一条澄清问题，而不是瞎猜。
   - 长任务也可以先微信回一句「收到，在做」，但每条消息最多回一条结果，不要轰炸。
6. **微信回结果**：
   - 有任务 id：`python phone\wechat_trigger.py finish --id <id> --result "结果摘要"`（同时标记 done）。
   - 没有任务 id（纯回复）：`python phone\wechat_trigger.py reply --text "结果摘要"`。
   - 用户明确说过：**没消息的时候不要回**；每条消息只回一条结果，不要回执轰炸。
7. **确认监听器在跑**：`job_list` 里应有一个 running 的 listener。
   然后收工（最终回答里说一句「已重新布防」即可）。
8. **上下文自检（自动，别等用户提醒）**：

```powershell
cd D:\手机agent; python phone\session_ctx.py --check
```

   - 退出码 0 / `VERDICT: OK` → 收工；
   - 10 / `HANDOFF`（≥80%）或 20 / `COMPACTED` → 立刻走「第七节 上下文 80% 自动交接」：
     `python phone\handoff.py write --note "<一句话说清现在在做什么>"`，
     再提示用户（或自己）开新会话，把 `handoff.py seed` 的内容整段发过去。

## 四、撤防 / 状态 / 排障

```powershell
cd D:\手机agent
python phone\wechat_trigger.py status              # adb、前台/亮屏、busy、事件数、任务数
python phone\wechat_trigger.py inbox               # 手动读一次新消息并建任务
python phone\wechat_trigger.py reply --text "…"    # 微信回一条
python phone\wechat_trigger.py busy on|off         # 手动暂停/恢复前台看板（不带参数=查看）
python phone\wechat_trigger.py fg-probe            # 调试：现场跑一次前台看板判断（不落事件）
python phone\wechat_trigger.py fg-probe --image 截图.png          # 用现成截图验证布局/识别
python phone\wechat_trigger.py fg-selftest --baseline A.png --new B.png            # 回归：聊天页新消息
python phone\wechat_trigger.py fg-selftest --mode list --baseline A.png --new B.png # 回归：会话列表红标
python phone\wechat_trigger.py test-notif          # 发系统测试通知（只对 --include-shell 自测有效）
```

- **撤防（全局，推荐）**：`python phone\wechat_trigger.py disarm`
  - 写标记 `runs/_wechat/DISARMED` + 杀掉所有监听进程；
  - 之后**任何会话**再布防都只会「待命」：进程活着但**不监听、不耗 token、不会反复唤醒 agent**；
  - `status` 里 `disarmed: true`、`listeners` 为空或待命进程。
- **重新启用**：`python phone\wechat_trigger.py arm`（删除标记；已存在的待命监听器会自动开始监听。
  若 `listeners` 为空，还需要在目标会话里布防）。
- **只撤本会话**：`job_list` 找 listener 的 job id → `job_kill`（不防别的会话重布防，一般不用）。
  注意：`job_kill` / `Stop-Process` 可能杀不掉 python 子进程和它的 `adb logcat` 子进程，
  手动清理后要确认只剩一条事件流：
  ```powershell
  Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*wechat_trigger.py listen*' } | Select-Object ProcessId
  Get-CimInstance Win32_Process -Filter "Name='adb.exe'" | Where-Object { $_.CommandLine -like '*logcat*events*' } | Select-Object ProcessId,ParentProcessId
  # 正常只应有 1 条 logcat，且 ParentProcessId 是当前 listener；其它 Stop-Process 掉
  ```
- **切会话/关 DSH/重装后**：后台任务随会话释放而消失，重新布防即可。
- **监听器自己退出（listener-error）**：job 会结束并唤醒 agent —— 这就是自愈入口。
  常见原因：USB 断/手机重启/`adb devices` 掉线/屏幕被安全锁屏。
  先 `python phone\adb.py devices` 确认，恢复后重新布防。
  **2026-10-30 起内置 adb 自愈**：logcat 断流时监听器自动 `adb kill-server` + `start-server`，
  15 分钟内设备回来就重开 logcat 继续监听（**同一个后台任务不退出、不唤醒 agent、0 token**）；
  只有 15 分钟仍无设备才 listener-error 退出唤醒。排障先看 `listen.log` 里有没有
  「logcat 断流…自愈重连 / 已重连」行；实测 00:41:33 kill adb server → 00:41:38 自动重连。
- **收不到事件的原因**（按概率排）：
  1. 微信**停在前台**且不在「目标聊天页/会话列表」→ 前台看板看不到信号；切后台/熄屏即可。
  2. 该聊天被设了「消息免打扰」，或微信全局通知被关 → 后台也不产生 notification_enqueue。
  3. adb 掉线 / DSH 关了 / 监听器被撤防。
  4. 手机安全锁屏（有密码）→ 需要人工解锁一次。
- **`adb devices` 显示 `(no serial number) offline`**（2026-10-31 05:48 遇到过）：
  手机 USB 枚举正常（设备管理器里 `ADB Interface` 在、MTP 显示机型），但 adbd 不握手。
  最常见是**手机重启后停在需要输密码的锁屏**（重启后首次解锁前 adb 不授权），或 USB 调试被重置。
  现场处理：让用户**解锁手机一次**（或重插 USB、在手机上重新点「允许 USB 调试」）。
  监听器侧：用 `--device-wait 21600` 静默等它回来（0 token、不打断其它 adb 使用者）；
  或先 `adb kill-server; adb start-server; adb devices` 手动确认是不是 `unauthorized`。
  注意：`adb reconnect offline` 可能让设备从 `offline` 变成空列表（正常，重插线后会回来）。
- **前台看板不工作怎么查**：
  1. `python phone\wechat_trigger.py status` 看 `focus`、`screen_on`、`busy`；
  2. `listen.log` 看有没有 `前台看板启用`、`前台切到 com.tencent.mm`、`前台看板 baseline(N)`；
  3. `fg-probe` 现场跑一次，确认 `in_chat/list_page` 判定是否符合实际页面；
  4. 若 `busy: true` 卡住：`busy off`（正常 `inbox` 结束会自动清）。
- **通知正文脱敏**（微信设置里关闭「显示消息详情」）：`notifications[].text` 会变成
  「你收到一条新消息」之类；此时靠 `inbox` 的开屏 + 本地 OCR 读聊天页，不额外花 token。
- **重复事件**：微信会重发/更新通知；`inbox` 有模糊去重（相似度 ≥0.75 视为已处理），
  不用手工清理；确实误建的任务把 `tasks.json` 里 status 改为 `cancelled` 并写明原因。
- **发送人过滤（2026-10-29 19:52 加，省 token）**：`runs/_wechat/sender_filter.txt` 存在且非空时，
  监听器只把「通知标题含关键字」的微信通知算事件（本机已设为 `目标联系人`，**群消息/别人的消息不再唤醒**，
  只在 `listen.log` 记一行「忽略非目标会话通知」）；文件不存在或为空 = 旧行为（任何微信通知都唤醒，
  agent 醒来再自行判定）。改完文件后下一次重新布防生效；要恢复旧行为删掉该文件即可。
  注意：`dump_notifications` 读不到通知时按旧行为照常唤醒，不会漏真任务。

## 五、成本（2026-10-29 实测 + 2026-10-31 前台看板）

- **常驻监听器：0 token / ¥0**（纯本地 adb 事件流 + Python，不调模型）；没有消息 = 不花钱。
- **前台看板：0 token**。微信不在前台：0 个 adb 调用；微信在前台：每 3s 一次手机本地截图
  （≈0.5s，PNG md5 比对），只有画面变化才拉图 + 本地 OCR（≈2s）。全程不调模型。
- **每条真消息触发一次唤醒回合**：大上下文会话（≈37 万 token、缓存命中 88%）实测 **¥0.072/条**
  （2.36M 计费 token，deepseek-flash）。前台看板误报复核失败不会落事件，不会白花。
- **静默等设备/待命/撤防：0 token**（进程活着但不调模型，也不会反复唤醒 agent）。
- skill 描述常驻系统提示约 +80 token/次请求，可忽略。
- 省钱办法：会话越长单次唤醒越贵 → 现在有**自动交接**（第七节）：session_ctx.py --check 到 80% 就
  handoff.py write 写记忆 + 新会话 listen --claim 接管；监听器成本始终为 0。
  数据源：`%USERPROFILE%\.dsh\.dshw-usage.json` 的 `events`。

## 六、验证记录

- 2026-10-29 18:17 `test-notif` 验证「事件流 → 解析 → 落盘」链路（`pkgs: com.android.shell`）。
- 2026-10-29 18:22 **真机验证**：目标联系人 发「测试接管」，监听器 1 秒内命中
  `notification_enqueue: [...com.tencent.mm...]`，事件 JSON 解析出发送人、正文；
  `inbox` 读到并建任务 #11。
- 2026-10-31 **前台看板**（本轮加的）：
  - 起因：10-31 00:36 / 01:30 目标联系人 在聊天页发了两条（「音画不同步啊…」「在干活吗？」），
    当时微信停在前台聊天页 → 系统不弹通知 → 通知链路漏掉，正是这个 bug 的证据。
  - 界面判定：`fg-probe` 在真实聊天页/会话列表截图上都正确识别 `in_chat` / `list_page`。
  - 判定回归：`fg-selftest` 合成「新气泡」→ `new(1)`；同一张图两次 → `no-new`；
    列表未读红标图 → `badge-new`；红标 1→2 → 签名变化触发。
  - 防误报：实际上翻聊天页时曾误报一次（旧消息翻出来被当新消息，已抓到），
    已改成「锚点 = 上次最后一条，只认它下方的新气泡」+ 文件卡片过滤；复测滚动不再触发。
  - 前台状态：实际按 Home / 回到微信，`listen.log` 立即出现 `前台切到 com.miui.home/com.tencent.mm`；
    微信后台 9 秒内手机截图文件 mtime 不变（确认 0 截图），回到微信后恢复 3s 一次。
- 2026-10-31 **隔夜跑（03:28–05:48）**：前台看板正常跟随真实切 App（微信前后来回 3 次、拼多多、B站、桌面），
  每次进微信都自动重建基线，未产生误唤醒；04:26/05:48 两次 adb 断流，前一次自愈成功，
  后一次 15 分钟未恢复 → listener-error 退出唤醒 agent（符合设计）。
  本次新增 `--device-wait` 静默等待模式，避免手机长时间不在线时反复唤醒。
- 旧的 `phone/wechat_task.py watch`（60 秒轮询 + DSH 每 30 分钟定时任务）**已停用**，
  不要再恢复：本 skill 是它的替代品。

## 七、上下文 80% 自动交接（2026-10-31 加）

目的：会话越长，每次被微信唤醒重跑上下文的成本越高（37 万 token 时 ≈¥0.07/条）；而且 DSH 在
上下文到 **~53.3 万 token 时会自动压缩（compaction）**，细节会被摘要替换掉。所以在
**上下文用量达到压缩预算的 80%（≈42.6 万 token）时主动换新会话**，但把记忆完整落盘带过去。

### 原理（本机实测标定，2026-10-31）

- DSH 会话日志：`$env:DSH_HOME\sessions\<转义后的cwd>\<session-id>\session.v4.jsonl.zstd`
  （zstd 压缩的 JSONL；会话 id 在环境变量 `DSH_SESSION_ID`；Python 3.14+ 用 stdlib `compression.zstd` 直接读）。
- 日志里 `request/context` 记录带 `contextWindow`：本机 deepseek-flash = **1,048,576** tokens。
- DSH 自动压缩触发点：`compaction/summary.data.usage.inputTokens` 实测 **533,042** → 把它当「压缩预算」。
- 估算：只统计真正进入上下文的文本（系统提示 + 工具清单 + 消息 + 工具结果，工具结果按 pruner
  规则截断），字符→token 系数 **2.9**，用压缩前真实 usage 反标定，误差 ≈0.1%。
- **用量% = 估算 token ÷ 压缩预算**；默认 **80% 触发**交接（`--threshold` 可改）。

### 每次任务结束后自检（自动做，不用等用户提醒）

```powershell
cd D:\手机agent; python phone\session_ctx.py --check
# 退出码：0=OK / 10=HANDOFF（≥80%，该交接）/ 20=COMPACTED（≥100%，已经压过，立即交接）
```

### 交接流程（agent 自动执行）

1. **写记忆**（把「在做什么」一句话塞进 `--note`）：

```powershell
python phone\handoff.py write --note "在给用户做 X / 卡在 Y / 下一步 Z"
```

   生成：
   - `runs/memory/MEMORY.md` — 服务对象、目标、待办（tasks.json 的 pending）、最近完成、
     布防/设备快照、关键路径、用户习惯；新会话读它就能接上，不用再问用户之前发生了什么；
   - `runs/memory/SEED.md` — 给新会话的启动消息（已包含「读记忆 → status → listen --claim → 处理待办」）；
   - `runs/memory/handoff-<时间戳>.md` — 历史快照。

2. **开新会话**：把 `python phone\handoff.py seed` 打印的内容整段发进新会话（DSH 的会话由 GUI
   创建，本机 HTTP API 需鉴权，脚本不代开）。新会话会自己读 MEMORY.md、执行
   `listen --claim` —— 会**自动停掉旧会话的监听器**，不会双监听。

3. 旧会话交接完就不要再抢布防（被唤醒也只会 `listener-parked` 待命），可以留着也可以关掉。

4. 用户侧完全无感：微信照发、任务照做，只是单次唤醒成本回落到最低。

### 什么时候不用交接

- 用量 < 80%，或刚压缩完（<20%）→ 继续在当前会话；
- 任务处在不能断的中间态（大文件上传、视频渲染、批量任务）→ 先把当前任务收尾再交接。

## 八、换新会话 / DSH 重启后（全局唯一 owner）

规则：同一时刻只有一个会话拥有监听器（记录在 `runs/_wechat/owner.json`）。

- **新会话说「布防」= 接管**：用 `listen --claim`，自动停掉旧会话的监听器 → **老对话的布防自动撤销**，
  不用先手动撤防。
- **旧会话被唤醒后会尝试重布防**（skill 流程），但它用的是**不带 `--claim`** 的命令 → 检测到 owner 是
  新会话，只进入「待命」（不监听、不耗 token、不会抢回来），不会造成双监听。
- 想切回旧会话：直接在旧会话说「布防」（同样 `--claim`），再接管一次即可。
- 新会话要开**完全权限**；`listen` 立即输出 `listener-parked` 就说明已被别的会话持有（正常，无需处理）。

```text
按 wechat-takeover skill 布防微信接管。
workspace = D:\手机agent（不是的话先 cd）
步骤：1) python phone\wechat_trigger.py status 自检（含 adb 是否在线）
      2) （可选/省电）微信在前台就先 adb shell input keyevent 3 切后台；不切也能识别
      3) pwsh 后台任务启动：cd D:\手机agent; $env:PYTHONIOENCODING="utf-8"; python phone\wechat_trigger.py listen --max-hours 0 --claim
         （手机不在线时追加 --device-wait 21600，静默等它回来，不要反复唤醒）
      4) job_list 确认 running 后告诉我 job id 和「已布防（已接管）」
```

## 手机 adb 掉线时的自助处理（2026-10-31 加）

```powershell
cd D:\手机agent
tools\platform-tools\adb.exe devices -l          # 看是空 / offline / unauthorized
tools\platform-tools\adb.exe kill-server; tools\platform-tools\adb.exe start-server
Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_2717*' }   # 小米 VID；确认 USB 枚举还在
```

- **空列表**：线掉了/手机没开 USB 调试 → 提示用户重插线、打开开发者选项里的 USB 调试。
- **`(no serial number) offline`**：USB 枚举在但 adbd 不握手，**多半是手机重启后停在要输密码的锁屏**，
  或 USB 调试授权被重置 → 让用户解锁手机一次 / 重插线 / 点「允许 USB 调试」。
- 恢复后：重新布防（`--device-wait 21600` 可让监听器自己等，不用人工盯）。

## 九、历史与相关文档

- **开源仓库**：<https://github.com/w1sdoM777/dsh-phone-wechat-takeover>（脱敏版：联系人用环境变量
  `WECHAT_CONTACT`，可直接给别人复现；本机 `deliverables/dsh-phone-wechat-takeover/` 是同一份）
  - 使用手册在仓库 `docs/USAGE.md`（布防/换会话/成本/后台进程/排障），本机同步方式：改本 skill 或直接看仓库
  - 注意：仓库版目前还是**只有通知链路**的旧代码；前台看板（2026-10-31）只在本机 `D:\手机agent\phone\` 里，
    要发布需要把 `wechat_trigger.py`/`wechat.py`/`wechat_task.py` 的改动脱敏后同步过去。
- `D:\手机agent\docs\12-微信任务通道.md`（旧的轮询方案，已停用）
- `D:\手机agent\docs\13-微信即时接管.md`（本方案的落地记录与环境说明）
- `D:\手机agent\phone\wechat.py`（发消息底座：OCR 驱动，手机需亮屏可滑动解锁）
- `D:\手机agent\phone\awake.py`（熄屏自动点亮 + 上滑解锁）
