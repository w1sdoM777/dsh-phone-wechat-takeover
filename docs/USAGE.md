# 使用说明（USAGE）

面向使用者的操作手册：怎么布防、怎么验证、**怎么迁移到新会话**、花多少钱、后台在跑什么。

> 安装/环境准备见 [README](../README.md)；实现细节见 [DESIGN](DESIGN.md)。

---

## 1. 布防（每个会话一次）

**为什么每个会话都要布防**：监听器是一个「后台任务」，它属于发起它的那个会话；会话关了/切了，任务就没了。
唤醒 agent 靠的正是「后台任务结束」这个事件，所以它必须活着。

### 给 agent 的一句话

在新会话里直接说：

```text
布防微信接管（按 wechat-takeover skill）
```

agent 会自己读 skill 并完成：自检 → 确认微信在后台 → 用后台任务启动监听器 → `job_list` 确认 running。

### 如果 agent 没自动接住，把这段发给它

```text
按 wechat-takeover skill 布防微信接管。
workspace = <本仓库绝对路径>（如果当前目录不是这个，先 cd 过去）
步骤：
1) python phone\wechat_trigger.py status 自检（adb 在线）
2) 看微信是否在前台，是的话 adb shell input keyevent 3 切后台
3) 用 pwsh 工具、run_in_background=true 启动：
   cd <本仓库绝对路径>; $env:PYTHONIOENCODING="utf-8"; python phone\wechat_trigger.py listen --max-hours 0
4) job_list 确认 running，告诉我 job id 和「已布防」
```

### 布防成功的判定（三选一都对）

- `job_list` 里有一个 running 的后台任务（命令含 `wechat_trigger.py listen`）
- `runs/wechat_events/listen.log` 最后一行是 `listen: start ...`
- `python phone\wechat_trigger.py status` 显示 `adb` 是设备序列号

---

## 2. 正常工作时是什么样

```
你发微信 → 监听器 1~2 秒内命中并退出 → DSH 唤醒 agent
        → agent 先重新布防 → 读事件 → 读新增消息 → 执行 → 微信回结果 → 确认布防
```

你不需要做任何事；手机可以熄屏，微信要待在**后台**（前台开着那个聊天时微信不弹通知）。

---

## 3. 迁移到新会话（换会话 / DSH 重启后）

后台任务不能跨会话继承，**必须在新会话重新布防**。推荐顺序：

1. **先在旧会话撤防**：说「撤防微信监听」，agent 会 `job_kill` 掉监听器。
   （不撤的话，新旧两个监听器会同时跑，同一条消息触发两次、双份 token。）
2. **打开新会话**（同一工作区），把上面「给 agent 的一句话」发过去。
3. **验证**：让目标联系人发一条测试微信，应在十几秒内被处理并收到微信回复。

注意事项：

- 新会话要选**完全权限**（否则后台任务/命令会被权限拦住）。
- 新会话的 workspace 必须是本仓库目录；不是的话让 agent 先 `cd`。
- **任何时候只保留一个监听器**（一个 DSH 会话里的一个后台任务）。
- 新会话上下文小，**每次唤醒更便宜**——这也是换会话的一个实际好处。

---

## 4. 撤防 / 临时关闭

```text
撤防微信监听
```

agent 会 `job_kill` 监听任务；Python 与 `adb logcat` 子进程随之退出，后台回到 0 消耗。
之后手机微信消息仍会正常收到，只是不再自动唤醒 agent。

> scrcpy 投屏窗口与本触发器无关，关掉不影响事件监听（熄屏照样能收到通知事件）。

---

## 5. 成本（实测，2026-10-29）

| 项目 | 花费 |
|---|---|
| 常驻监听器（本地 Python + adb 事件流） | **0 token / ¥0**，不调用模型 |
| 没有消息时 | **¥0**（无定时扫描、无心跳） |
| 每条微信消息触发的 agent 回合 | 大上下文会话实测 **¥0.07**（简单任务）～ **¥0.25**（看图+改代码这类复杂任务） |
| 新会话（小上下文） | 通常 **¥0.01–0.03/条**，便宜一个数量级 |
| skill 描述常驻系统提示 | 约 **+80 token/次请求**，可忽略 |
| adb / logcat / scrcpy | 0 token（只占少量 CPU/内存） |

**怎么查自己的实际花费**：

- DSH 界面里的余额/用量显示；
- 或看 `~/.dsh/.dshw-usage.json` 的 `events`（每条含 `ts` / `cost` / `tokens` / `model`，按回合记录）。

**省钱建议**：

1. 不用时撤防 → 彻底 0；
2. 定期开新会话再布防 → 单次唤醒便宜一个数量级；
3. 复杂任务一条消息说完（每条消息 = 一个回合）；
4. 纯测试/闲聊在桌面 GUI 里说，不走微信。

---

## 6. 后台在跑什么（排查用）

正常布防后，与本项目相关的只有这些：

| 对象 | 作用 | 备注 |
|---|---|---|
| DSH 后台任务（`pwsh-…`） | 唤醒开关 | `job_list` 可见，running |
| Python 监听器 ×2（启动器 + 实际进程） | 读 adb 事件流 | 内存约 20 MB，几乎不耗 CPU |
| `adb logcat -b events -v threadtime -T 1` | 真正的事件流 | 命中事件时由监听器结束它 |
| `adb server` | adb 基础服务 | 常驻 |
| 手机端 scrcpy server + 桌面 scrcpy 窗口 | 你的投屏（可选） | 与触发器无关 |

**没有任何** DSH 定时任务 / cron / 轮询脚本在跑（`schedule_list` 应为空）。

---

## 7. 图片 / 语音 / 文件消息

微信通知只带文字正文；图片/语音/文件消息没有正文。此时：

- 事件 JSON 的 `notifications` 可能为空或只有占位文字；
- `inbox` 返回 `note: 没有新的文字消息…`。

处理办法（agent 会自动做）：开聊天页截图 → 裁剪图片气泡 → 直接看图 / 送视觉模型识别；
语音/文件按需再处理。

---

## 8. 排障速查

| 现象 | 先查什么 |
|---|---|
| 发了微信没反应 | ① 监听器还在跑吗（`job_list`）② 微信是否在前台开着聊天 ③ 该聊天是否「消息免打扰」 |
| `inbox` 报 `ensure_chat-failed` | 微信不在前台/安全锁屏；解锁后重试 |
| 监听器退出且报 `no-android-device` | USB 掉线/未授权；`python phone\adb.py devices` |
| 收到重复处理 | 是否开了两个监听器（旧会话没撤防） |
| 微信回不出去 | 手机锁屏（需无密码滑动解锁）、ADBKeyboard 是否安装 |
| 事件正文是「你收到一条新消息」 | 微信关闭了「显示消息详情」；走 OCR 兜底 |

---

## 9. 上线前验证清单（5 项）

1. `python phone\adb.py devices` → 有设备
2. `python phone\wechat_trigger.py status` → adb 在线、状态文件可写
3. 布防 → `job_list` 有 running 的 listener
4. 目标联系人发一条测试微信 → 十几秒内收到微信回复
5. 再发第二条 → 确认第一条处理完后监听器已重新布防（没死）
