# -*- coding: utf-8 -*-
r"""微信即时接管触发器：**事件驱动**，不做定时扫描。

原理（不轮询微信内容）：
1. 手机端只要收到新微信消息，系统就会在 EventLog 里写一条
   `notification_enqueue: [uid,pid,com.tencent.mm,...]`（本机实测可见）。
2. `listen` 是一条常驻的 `adb logcat -b events` **事件流**（阻塞读，不是定时截图/OCR）；
   一旦看到 com.tencent.mm 的通知事件，立刻把事件写入 runs/wechat_events/ 并退出。
3. 把这个进程作为 DSH 后台任务跑：它一退出，DSH 就会唤醒 agent（房间任务完成通知），
   agent 醒来读事件 -> 读聊天页新增消息 -> 当任务执行 -> 微信回结果 -> 重新布防。
4. **前台看板（2026-10-31 加）**：微信停在前台（尤其停在 目标联系人 聊天页）时，
   微信自己不弹系统通知，纯通知事件会漏消息。所以事件流里顺带读
   `wm_set_resumed_activity` / `screen_toggled` 来跟踪「微信是否在前台、屏幕是否亮」：
   微信不在前台 = 一个 adb 调用都不做；微信在前台 = 每 --fg-interval 秒在手机本地
   截一张图算 md5（约 0.5s，0 token），**只有画面变了**才把图拉回电脑做本地 OCR，
   并且只有真出现「目标聊天页新的白色气泡」或「会话列表 目标联系人 有未读红标」才写事件唤醒 agent。
   空闲时不扫描、不截图、不烧 token。

子命令：
  python phone/wechat_trigger.py listen [--include-shell] [--burst 2.0] [--max-hours 0]
                                       [--fg-interval 3] [--no-foreground-watch] [--claim]
  python phone/wechat_trigger.py inbox [--json]        # 读聊天页新增消息并建任务（唤醒后第一步之一）
  python phone/wechat_trigger.py status
  python phone/wechat_trigger.py reply --text "…"      # 微信回结果
  python phone/wechat_trigger.py finish --id N --result "…"   # 兼容旧任务队列（会微信回结果）
  python phone/wechat_trigger.py busy on|off           # 处理中标记（暂停前台看板，防自触发）
  python phone/wechat_trigger.py fg-probe              # 调试：看一次前台看板判断（不落事件）
  python phone/wechat_trigger.py fg-selftest --baseline a.png --new b.png   # 调试：两张图验证判定
  python phone/wechat_trigger.py test-notif            # 发一条系统测试通知，自测事件链路
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adb  # noqa: E402
import awake  # noqa: E402
import wechat  # noqa: E402
import wechat_task as wt  # noqa: E402
from ocr import ocr_image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EV_DIR = ROOT / "runs" / "wechat_events"
EV_DIR.mkdir(parents=True, exist_ok=True)
EV_LOG = EV_DIR / "listen.log"
EV_JSONL = EV_DIR / "events.jsonl"
STATE = ROOT / "runs" / "_wechat" / "trigger_state.json"
DISARM_FLAG = ROOT / "runs" / "_wechat" / "DISARMED"
OWNER_FILE = ROOT / "runs" / "_wechat" / "owner.json"
# 可选：只接管指定发送人的通知（避免群消息把 agent 反复唤醒、白花 token）。
# 文件内容为关键字（逗号/换行分隔，不区分大小写）；文件不存在或为空 = 不过滤=旧行为。
FILTER_FILE = ROOT / "runs" / "_wechat" / "sender_filter.txt"
# 上次已处理的事件行：logcat -T 1 启动时会「回放」缓冲区最后一行，
# 若那行正好是上次已处理的事件，重新布防时会把它当成新事件 → 多唤醒一次（极端时可能循环）。
# 记录原文，启动后遇到完全相同的行直接跳过（真新消息的线程时间戳不同，不会被误跳）。
LAST_HIT_FILE = ROOT / "runs" / "_wechat" / "last_hit.txt"
# ---- 前台看板（微信在前台时不弹通知的兜底识别，详见文件头）
FG_IMAGE = ROOT / "runs" / "_wechat" / "fg_watch.png"
FG_BADGE_STATE = ROOT / "runs" / "_wechat" / "foreground_badge.json"
REMOTE_SHOT = "/sdcard/_dsh_fg.png"                     # 手机上的临时截图（本地差分用）
PAUSE_FLAG = ROOT / "runs" / "_wechat" / "pause"         # 手动暂停（旧约定）
BUSY_FLAG = ROOT / "runs" / "_wechat" / "busy.lock"      # agent 正在处理：前台看板暂停
SEND_LOCK = ROOT / "runs" / "_wechat" / "sending.lock"   # wechat.send 发送中（与 wechat.py 同名）
CONTACT = wt.CONTACT
WECHAT_PKG = "com.tencent.mm"
SHELL_PKG = "com.android.shell"

_ENQ = re.compile(r"notification_enqueue:\s*\[(\d+),(\d+),([^,\]]+),([^,\]]*),([^,\]]*)," )
_TITLE = re.compile(r"android\.title=String \((.*)\)")
_TEXT = re.compile(r"android\.text=String \((.*)\)")
_BIGTEXT = re.compile(r"android\.bigText=String \((.*)\)")


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
    try:
        with EV_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 通知内容

def dump_notifications(pkgs):
    """一次性读系统通知详情（只在事件触发后跑一次，不轮询）。

    返回 [{pkg, title, text}]；读不到就返回 []。
    """
    try:
        out = adb.shell("dumpsys notification --noredact", timeout=25)
    except Exception as e:
        _log("dumpsys notification 失败: " + repr(e))
        return []
    items = []
    for block in out.split("NotificationRecord(")[1:]:
        head = block.split("\n", 1)[0]
        m = re.search(r"pkg=([\w\.]+)", head)
        if not m or m.group(1) not in pkgs:
            continue
        item = {"pkg": m.group(1)}
        for key, rx, src in (("title", _TITLE, block), ("text", _TEXT, block), ("bigText", _BIGTEXT, block)):
            mm = rx.search(src)
            if mm:
                item[key] = mm.group(1).strip()
        if item.get("title") or item.get("text"):
            items.append(item)
    return items


def _sender_filter(cli_value=""):
    """发送人过滤关键字：命令行 --only-title 优先，否则读 runs/_wechat/sender_filter.txt。

    返回关键字列表；空列表 = 不过滤（任何微信通知都唤醒，旧行为）。
    """
    raw = (cli_value or "").strip().lstrip("\ufeff")
    if not raw:
        try:
            if FILTER_FILE.exists():
                # utf-8-sig：兼容 Windows PowerShell 写出的带 BOM 文件
                raw = FILTER_FILE.read_text(encoding="utf-8-sig").strip()
        except Exception:
            raw = ""
    return [x.strip().strip("\ufeff").strip() for x in re.split(r"[,，\n]+", raw) if x.strip()]


def _title_matches(notif, keywords):
    """通知里是否有一条标题命中任一关键字（不区分大小写）。"""
    for n in notif:
        title = (n.get("title") or "")
        low = title.lower()
        if any(k.lower() in low for k in keywords):
            return True
    return False


def _load_last_hit():
    """上次已处理的事件原始行（用于跳过 logcat -T 1 的回放行）。"""
    try:
        return LAST_HIT_FILE.read_text(encoding="utf-8-sig").strip()
    except Exception:
        return ""


def _save_last_hit(raw_line):
    try:
        LAST_HIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_HIT_FILE.write_text((raw_line or "").strip(), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 监听事件流

def _reader(proc, q):
    try:
        for line in proc.stdout:
            q.put(line)
    except Exception as e:  # pragma: no cover
        q.put(f"__READER_ERR__ {e!r}")
    finally:
        q.put("__EOF__")


def _real_adb_serial():
    try:
        return adb.device()
    except Exception:
        return None


def _restart_adb():
    """温和重启一次 adb server（对齐 USB 握手；不碰手机端数据）。"""
    for args_ in (["kill-server"], ["start-server"]):
        try:
            subprocess.run([adb.ADB, *args_], capture_output=True, timeout=60)
        except Exception:
            pass


def _ensure_device(max_wait=900):
    """确保手机在线：掉线 / adb 守护进程挂掉时自动 kill-server+start-server 并等待。

    返回 serial 或 None（等满 max_wait 仍不在线）。监听器**运行中**断流时用这个（15 分钟内自愈）。
    """
    serial = _real_adb_serial()
    if serial:
        return serial
    _log("listen: 手机不在线（adb 掉线/守护进程异常），开始自愈（最多等 %d 秒）" % max_wait)
    t0 = time.time()
    while time.time() - t0 < max_wait:
        _restart_adb()
        for _ in range(5):
            time.sleep(2)
            serial = _real_adb_serial()
            if serial:
                _log("listen: adb 已恢复 " + serial)
                return serial
    _log("listen: adb 自愈失败（等满 %d 秒仍无设备）" % max_wait)
    return None


def _wait_for_device(max_wait=900, poll=5.0):
    """启动时**静默等**手机上线：不唤醒 agent、0 token、不打搅别的 adb 用户。

    与 _ensure_device 的区别：只在开始和每 5 分钟温和重启一次 adb server，
    期间每 5 秒查一次设备（手机重启后停在锁屏/需要人解锁时，就安静等它回来）。
    返回 serial 或 None（等满 max_wait 仍不在线）。
    """
    serial = _real_adb_serial()
    if serial:
        return serial
    _log("listen: 手机不在线，静默等待上线（最多 %.0f 分钟，期间不耗 token）"
         % (max_wait / 60.0))
    _restart_adb()
    t0 = time.time()
    last_restart = time.time()
    while time.time() - t0 < max_wait:
        time.sleep(poll)
        serial = _real_adb_serial()
        if serial:
            _log("listen: 手机已上线 " + serial)
            return serial
        if time.time() - last_restart > 300:
            _restart_adb()
            last_restart = time.time()
    _log("listen: 静默等待超时（%.0f 分钟仍无设备）" % (max_wait / 60.0))
    return None


def _spawn_logcat(serial):
    """起一条 adb logcat -b events 事件流，返回 (proc, queue)。"""
    cmd = [adb.ADB, "-s", serial, "logcat", "-b", "events", "-v", "threadtime", "-T", "1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc, q), daemon=True).start()
    return proc, q


def _ps_listener_pids():
    """列出所有 wechat_trigger.py listen 进程（跨会话）。"""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
          "$_.CommandLine -like '*wechat_trigger.py listen*' } | ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=90)
        return [line.strip() for line in out.stdout.splitlines() if line.strip().isdigit()]
    except Exception:
        return []


# ---------------------------------------------------------------- 全局唯一 owner
# 规则：同一时刻只有一个会话「拥有」监听器。
# - 用户在会话里说「布防」→ listen --claim：接管所有权，停掉其它会话的监听器；
# - 被唤醒后的自动重布防 → listen（不带 --claim）：若 owner 是别的会话，只进入待命（不监听、不耗 token）。

def _my_session():
    return (os.environ.get("DSH_SESSION_ID") or "").strip() or "unknown"


def _load_owner():
    try:
        data = json.loads(OWNER_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_owner(session):
    try:
        OWNER_FILE.parent.mkdir(parents=True, exist_ok=True)
        OWNER_FILE.write_text(json.dumps(
            {"session": session, "pid": os.getpid(), "at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        _log("owner 写入失败: " + repr(e))


def _kill_listeners(except_pids=()):
    """停掉其它监听器进程，并清掉残留的 adb logcat 子进程（调用方随后会起自己的）。"""
    mine = {os.getpid(), os.getppid()}
    mine.update(int(p) for p in except_pids if p)
    killed = []
    for pid in _ps_listener_pids():
        try:
            ipid = int(pid)
        except ValueError:
            continue
        if ipid in mine:
            continue
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"Stop-Process -Id {ipid} -Force -ErrorAction SilentlyContinue"],
                           capture_output=True, timeout=60)
            killed.append(ipid)
        except Exception:
            pass
    # 残留的 logcat 事件流（被杀监听器的孤儿进程）
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'adb.exe' -and "
          "$_.CommandLine -like '*logcat*events*' } | ForEach-Object { "
          "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=90)
        killed += [int(line.strip()) for line in out.stdout.splitlines() if line.strip().isdigit()]
    except Exception:
        pass
    return killed


def _park(reason, session, args):
    """待命：不监听、不产生事件、也不结束（避免反复唤醒 agent 重新布防）。

    条件解除（撤防标记消失 且 owner 变成自己/无 owner）后返回 True 继续监听；
    达到 --max-hours 上限则返回 False。
    """
    _log("listen: " + reason + "，进入待命，不监听")
    print(json.dumps({"event": "listener-parked", "reason": reason, "session": session,
                      "hint": "待命中：不监听、不耗 token；条件解除后会自动开始监听"},
                     ensure_ascii=False), flush=True)
    waited = 0.0
    while True:
        if not DISARM_FLAG.exists():
            owner = _load_owner()
            owner_session = (owner or {}).get("session") or ""
            if not owner_session or owner_session == session:
                _log("listen: 待命条件解除，开始监听")
                return True
        time.sleep(3)
        waited += 3
        if args.max_hours and waited > args.max_hours * 3600:
            print(json.dumps({"event": "listener-timeout", "state": "parked"}, ensure_ascii=False))
            return False


# ---------------------------------------------------------------- 前台看板
# 微信在前台（尤其停在 目标联系人 聊天页）时微信自己不弹通知，纯事件流会漏消息。
# 这一层是「只在微信前台才工作」的本地看板：
#   1) 前台/亮屏状态从 logcat 事件流免费拿（wm_set_resumed_activity / screen_toggled），
#      微信不在前台时一个 adb 调用都不做；
#   2) 微信在前台时每 --fg-interval 秒在手机本地截一张图 + md5（约 0.5s），
#      只有画面变了才把图拉回电脑；
#   3) 拉回来先算低分辨率指纹，聊天区没变（只有状态栏时钟变）就跳过 OCR；
#   4) 本地 OCR（Windows OCR，0 token）判断是聊天页还是会话列表：
#      - 目标聊天页：出现新的「白色气泡（对方）」才算新消息，并二次截屏复核；
#      - 会话列表：目标联系人 那一行出现未读红标（签名变化）才算新消息；
#   5) 只有确认有新消息才写事件 JSON 并退出 → DSH 唤醒 agent（一次 token 回合）。
# 空闲期不截图、不 OCR、不唤醒，成本≈0。

_FG_ACT = re.compile(r"wm_set_resumed_activity:\s*\[\d+,([^\],]+)")
_FG_FOCUS = re.compile(r"input_focus:\s*\[Focus (?:entering|request) [0-9a-f]+ ([A-Za-z0-9_\.]+)/")
_FG_SCREEN = re.compile(r"screen_toggled:\s*([01])\b")
_PAGE_NOISE = ("再发一件", "多搞副业", "曝光越多")
_FILE_SIZE = re.compile(r"\d+\s*(?:[.·．]\s*\d+)?\s*(?:KB|MB|GB)\b", re.I)
_FILE_NAME = re.compile(r"\S*[.．](?:md|pdf|docx?|xlsx?|pptx?|zip|rar|7z|mp4|mov|avi|png|jpe?g|gif|txt)\b", re.I)


def _fg_is_file_card(text):
    """文件卡片气泡（不是真正的文字消息）：'106.7 MB'、'自检报告.md 5.8KB' 之类。"""
    t = (text or "").strip()
    if not t or len(t) > 80:
        return False
    has_size = bool(_FILE_SIZE.search(t))
    has_name = bool(_FILE_NAME.search(t))
    if not (has_size or has_name):
        return False
    rest = _FILE_SIZE.sub(" ", t)
    rest = _FILE_NAME.sub(" ", rest)
    rest = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", rest)
    return len(rest) <= 2


def _pkg_of(comp):
    """`com.tencent.mm/.ui.LauncherUI` -> `com.tencent.mm`"""
    comp = (comp or "").strip()
    return comp.split("/", 1)[0].strip() if "/" in comp else comp


def _focus_pkg():
    """当前真正拿到输入的包名（一次性 dumpsys，只在校验/启动时用）。"""
    try:
        focus = awake._focus()
    except Exception:
        return ""
    return _pkg_of(focus)


def _screen_awake():
    try:
        return "Awake" in adb.shell("dumpsys power | grep -m1 mWakefulness=")
    except Exception:
        return False


def _keyboard_shown():
    """键盘弹出时聊天页布局会变形（白色按键容易被当气泡），看板直接跳过这一轮。"""
    try:
        out = adb.shell("dumpsys input_method | grep -m1 mInputShown")
    except Exception:
        return False
    return "mInputShown=true" in out


def _fg_fingerprint(path):
    """聊天区低分辨率灰度指纹（截图是确定性的：静止画面 diff=0）。"""
    from PIL import Image
    im = Image.open(path).convert("L")
    w, h = im.size
    crop = im.crop((0, int(h * 0.10), w, int(h * 0.915))).resize((36, 90))
    return crop.tobytes()


def _fg_changed(old, new):
    """画面是否真的变了（≥3 个网格明显变化）。新消息再短也会占 6~9 个格。"""
    if not old or len(old) != len(new):
        return True
    changed = 0
    for a, b in zip(old, new):
        if abs(a - b) > 10:
            changed += 1
    return changed >= 3


def _fg_shot_local(serial):
    """把手机上的截图拉回本地（只在画面变化时调用，约 0.1s）。"""
    data = adb._run("-s", serial, "exec-out", "cat", REMOTE_SHOT, timeout=25, binary=True)
    if not data.startswith(b"\x89PNG"):
        raise adb.AdbError("前台看板：拉取截图失败")
    FG_IMAGE.parent.mkdir(parents=True, exist_ok=True)
    FG_IMAGE.write_bytes(data)
    return str(FG_IMAGE)


def _fg_y_bottom(path=None):
    """气泡扫描下边界（1080x2400 时 = 2184，与 inbox 的 2185 对齐）。"""
    from PIL import Image
    im = Image.open(path or str(FG_IMAGE))
    return int(im.size[1] * 0.91)


def _fg_mark(msgs):
    return [{"text": m["text"], "norm": wt._norm(m["text"]),
             "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            for m in msgs if wt._norm(m["text"])]


def _fg_update_baseline(baseline, msgs):
    merged = (baseline or []) + _fg_mark(msgs)
    seen, out = set(), []
    for item in reversed(merged):
        key = item["norm"]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.reverse()
    return out[-80:]


def _fg_sane(msgs):
    """页面误识别防护（沿用 poll/inbox 的经验值）。"""
    if len(msgs) > 40:
        return "suspicious-count:%d" % len(msgs)
    for m in msgs:
        if len(m["text"]) > 800:
            return "suspicious-long"
    home = ("通讯录", "微信团队", "公众号", "腾讯新闻")
    for m in msgs:
        if m["side"] == "in" and len(m["text"]) > 120 \
                and sum(1 for b in home if b in m["text"]) >= 2:
            return "suspicious-home"
    return ""


def _fg_new_incoming(msgs, baseline):
    """相对基线新增的「对方发来」消息（模糊去重，过滤无意义/页面文案/文件卡片）。"""
    out = []
    for m in msgs:
        if m["side"] != "in":
            continue
        if not wt._norm(m["text"]) or wt._trivial(m["text"]):
            continue
        if _fg_is_file_card(m["text"]):
            continue
        if wt._is_processed(m["text"], baseline or []):
            continue
        if sum(1 for x in _PAGE_NOISE if x in m["text"]) >= 2:
            continue
        out.append(m)
    return out


def _fg_find_anchor(msgs, prev_bottom):
    """在本次可见消息里找「上次最后一条消息」的位置；返回下标，找不到返回 -1。

    这是防误报的关键：聊天页往上滚会把**旧消息**翻出来，那些旧消息不在基线里，
    如果按「没见过就算新」会误唤醒。新消息一定出现在上次最后一条的**下方**，
    所以只有找到锚点、且新气泡在锚点之后，才算新消息。
    """
    if not prev_bottom:
        return -1
    target = prev_bottom.get("norm") or ""
    if not target:
        return -1
    best, best_sim = -1, 0.0
    for i, m in enumerate(msgs):
        n = wt._norm(m["text"])
        if not n:
            continue
        sim = 1.0 if n == target else wt._similar(n, target)
        if sim >= 0.72 and sim > best_sim:
            best, best_sim = i, sim
    return best


def _fg_unread_chip(rows):
    """聊天页底部「新消息」气泡提示（上翻看历史时来了新消息，微信会显示）。

    只在右下角区域认「新消息」/「以下为新消息」，避免正文里出现这几个字误判。
    """
    for r in rows:
        t = (r["text"] or "").strip()
        if not t or len(t) > 12:
            continue
        if "新消息" not in t and "未读" not in t:
            continue
        cx = r.get("x", 0) + r.get("w", 0) / 2
        if cx > 0.45 * 1080 and r.get("y", 0) > 1500:
            return t
    return ""


def _fg_badge_state():
    try:
        data = json.loads(FG_BADGE_STATE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fg_save_badge_state(data):
    try:
        _write_json(FG_BADGE_STATE, data)
    except Exception:
        pass


def _fg_recent_event_seconds():
    """最近一次事件文件距今秒数（刚唤醒/正在处理时避免自己导航造成的重复唤醒）。"""
    try:
        files = sorted(EV_DIR.glob("evt-*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return 9e9
        return time.time() - files[-1].stat().st_mtime
    except Exception:
        return 9e9


def _fg_badge_sig(shot, rows):
    """会话列表里目标联系人那一行的未读红标签名；没有红标返回 (None, None)。

    本机微信的未读红标画在**头像右上角**（屏幕左侧），不是行尾；
    签名 = 红标尺寸 + 红标内白字缩略图：未读 1->2 时数字变、签名变；
    单纯滚动列表不改变签名。
    """
    hit = None
    for r in rows:
        if r["y"] < 260 or not r["text"]:
            continue
        if wechat._name_match(r["text"], CONTACT):
            hit = r
            break
    if not hit:
        return None, None
    try:
        from PIL import Image
        im = Image.open(shot).convert("RGB")
        w, h = im.size
    except Exception:
        return None, None
    x0, x1 = 0, int(w * 0.25)          # 头像区（左）
    y0 = max(0, int(hit["y"]) - int(hit["h"] * 1.1))
    y1 = min(h, int(hit["y"]) + int(hit["h"] * 1.3))
    px = im.load()
    pts = []
    for yy in range(y0, y1, 2):
        for xx in range(x0, x1, 2):
            r_, g_, b_ = px[xx, yy]
            if r_ > 190 and g_ < 90 and b_ < 90:
                pts.append((xx, yy))
    if len(pts) < 20:
        return None, None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    if (bx1 - bx0) < 8 or (by1 - by0) < 8:
        return None, None
    badge = im.crop((bx0, by0, bx1 + 1, by1 + 1)).convert("L").resize((8, 8))
    sig = "%dx%d:%s" % (bx1 - bx0, by1 - by0, badge.tobytes().hex())
    return sig, hit["text"][:40]


def _fg_scan_chat(shot, rows, st):
    """目标聊天页：返回 (新消息列表|None, 说明)。基线在第一次扫描时建立。"""
    if not wechat._in_chat(rows, CONTACT):
        st["base"] = None
        return None, "not-target-chat"
    try:
        msgs = wt.extract_messages(shot, y_bottom=_fg_y_bottom(shot), rows=rows)
    except Exception as e:
        return None, "extract-err:" + repr(e)[:100]
    bad = _fg_sane(msgs)
    if bad:
        st["base"] = None
        return None, bad
    if st.get("base") is None:
        st["base"] = _fg_mark(msgs)
        return None, "baseline(%d)" % len(msgs)
    # 上翻看历史时微信会给「新消息」提示：这是明确的未读信号（此时锚点可能已滚走）
    chip = _fg_unread_chip(rows)
    if chip and _fg_recent_event_seconds() > 90:
        return [{"side": "in", "text": "[微信提示] " + chip}], "chip:" + chip
    prev_bottom = (st.get("base") or [{}])[-1]
    anchor = _fg_find_anchor(msgs, prev_bottom)
    if anchor < 0:
        # 画面滚动/切走：不判定新消息，只更新基线（防「旧消息翻出来」误唤醒）
        st["base"] = _fg_update_baseline(st["base"], msgs)
        return None, "scroll-or-move"
    after = msgs[anchor + 1:]
    new_in = _fg_new_incoming(after, st["base"])
    if not new_in:
        st["base"] = _fg_update_baseline(st["base"], msgs)
        return None, "no-new"
    return new_in, "new(%d)" % len(new_in)


def _fg_scan_list(shot, rows, st):
    """会话列表页：目标联系人行出现未读红标（或红标数字变化）才算新消息。"""
    texts = " ".join(r["text"] for r in rows if r["text"])
    if not ("通讯录" in texts and "发现" in texts):
        return None, "not-list"
    sig, row_text = _fg_badge_sig(shot, rows)
    state = _fg_badge_state()
    prev = state.get("sig")
    if sig is None:
        if prev:
            _fg_save_badge_state({"sig": None, "event_at": state.get("event_at")})
        return None, "no-badge"
    if sig == prev:
        return None, "badge-same"
    # 刚有事件（agent 可能正在处理这批消息）→ 先当已记录，避免重复唤醒
    if _fg_recent_event_seconds() < 90:
        return None, "badge-recent-event"
    _fg_save_badge_state({"sig": sig, "event_at": time.time()})
    return {"detected_via": "conversation-list-badge", "messages": [],
            "evidence": {"contact_row": row_text, "badge_sig": sig}}, "badge-new"


def _fg_confirm(texts):
    """二次确认：1 秒后新消息仍能在聊天页读到（防 OCR 抖动误唤醒）。"""
    time.sleep(0.9)
    try:
        adb.shell("screencap -p " + REMOTE_SHOT, timeout=20)
        shot = _fg_shot_local(adb.device())
        rows = ocr_image(shot)
        if not wechat._in_chat(rows, CONTACT):
            return False
        msgs = wt.extract_messages(shot, y_bottom=_fg_y_bottom(shot), rows=rows)
    except Exception:
        return False
    marks = _fg_mark(msgs)
    return all(wt._is_processed(t, marks) for t in texts)


def _fg_build_event(payload, serial, only_kw=None):
    msgs = payload.get("messages") or []
    via = payload.get("detected_via")
    if via == "target-chat":
        note = ("微信在前台聊天页时系统不弹通知；本条由前台看板（手机本地截图差分 + 电脑本地 OCR）"
                "识别，空闲期不扫描、0 token。")
    elif via == "unread-chip":
        note = ("微信聊天页底部出现「新消息」提示（通常是上翻历史时来了新消息，消息不一定在可见区域）；"
                "请先回到底部再 inbox 读正文。")
    else:
        note = ("微信在前台会话列表看到 目标联系人 行出现未读红标；系统不弹通知。"
                "请先确保在聊天页，再用 inbox 读正文。")
    event = {
        "event": "wechat-foreground-message",
        "detected_via": via,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device": serial,
        "pkgs": [WECHAT_PKG],
        "count": max(1, len(msgs)),
        "sender_filter": only_kw or None,
        "notifications": [{"pkg": WECHAT_PKG, "title": CONTACT, "text": m.get("text", "")[:500]}
                          for m in msgs],
        "messages": msgs,
        "note": note,
    }
    if payload.get("evidence"):
        event["evidence"] = payload["evidence"]
    return event


def _fg_paused():
    """前台看板是否应暂停：手动暂停 / 发送中 / agent 正在操作手机（busy，超时自愈）。"""
    if PAUSE_FLAG.exists() or SEND_LOCK.exists():
        return True
    if BUSY_FLAG.exists():
        try:
            if time.time() - BUSY_FLAG.stat().st_mtime > 180:
                BUSY_FLAG.unlink()
                _log("listen: busy.lock 超过 3 分钟，自动清除（视为对方已释放）")
                return False
        except Exception:
            pass
        return True
    return False


def _fg_tick(serial, st, args):
    """跑一轮前台看板；返回事件 dict 或 None。"""
    if _fg_paused():
        st["note"] = "paused"
        return None
    if _keyboard_shown():
        st["note"] = "keyboard"
        return None
    # 手机本地截图 + md5：只有画面变化才把图拉回电脑（省 USB / 省本地 OCR）
    try:
        out = adb.shell("screencap -p %s && md5sum %s" % (REMOTE_SHOT, REMOTE_SHOT), timeout=30)
    except Exception as e:
        _log("listen: 前台看板截图失败 " + repr(e)[:120])
        st["digest"] = ""
        return None
    parts = (out or "").split()
    digest = parts[0] if parts else ""
    if not digest:
        return None
    if digest == st.get("digest"):
        return None
    st["digest"] = digest
    st["last_change"] = time.time()
    try:
        shot = _fg_shot_local(serial)
    except Exception as e:
        _log("listen: 前台看板拉图失败 " + repr(e)[:120])
        st["digest"] = ""
        return None
    fp = _fg_fingerprint(shot)
    if st.get("fp") is not None and not _fg_changed(st["fp"], fp):
        st["fp"] = fp      # 只有状态栏时钟变了：不 OCR
        return None
    st["fp"] = fp
    try:
        rows = ocr_image(shot)
    except Exception as e:
        _log("listen: 前台看板 OCR 失败 " + repr(e)[:120])
        st["digest"] = ""
        return None
    if wechat._in_chat(rows, CONTACT):
        new_in, note = _fg_scan_chat(shot, rows, st)
    else:
        new_in, note = _fg_scan_list(shot, rows, st)
    if note != st.get("note"):
        st["note"] = note
        _log("listen: 前台看板 " + note)
    if not new_in:
        return None
    if isinstance(new_in, dict):
        return _fg_build_event(new_in, serial, getattr(args, "only_title_effective", None))
    if note.startswith("chip:"):
        # 微信自己的「新消息」提示：直接触发（让 agent 进聊天页读正文）
        return _fg_build_event({"detected_via": "unread-chip",
                                "messages": [{"side": "in", "text": new_in[0]["text"]}]},
                               serial, getattr(args, "only_title_effective", None))
    texts = [m["text"] for m in new_in]
    if _fg_confirm(texts):
        st["retry"] = 0
        return _fg_build_event({"detected_via": "target-chat",
                                "messages": [{"side": "in", "text": m["text"][:500]}
                                             for m in new_in]},
                               serial, getattr(args, "only_title_effective", None))
    st["retry"] = st.get("retry", 0) + 1
    st["digest"] = ""      # 强制下一轮重扫
    if st["retry"] >= 3:
        _log("listen: 前台看板连续 3 次复核失败，先把这批记为已处理（防 OCR 抖动循环）")
        st["base"] = _fg_update_baseline(st["base"], new_in)
        st["retry"] = 0
    return None


def _emit_event(event):
    path = EV_DIR / ("evt-%s-%03d.json" % (time.strftime("%Y%m%d-%H%M%S"),
                                           int(time.time() * 1000) % 1000))
    _write_json(path, event)
    try:
        with EV_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _log("listen: 事件已落盘 " + str(path))
    return path


def cmd_listen(args):
    pkgs = {WECHAT_PKG} | ({SHELL_PKG} if args.include_shell else set())
    session = _my_session()
    # 用户口头「布防」（--claim）优先于之前的全局撤防：自动解除撤防标记
    if args.claim and DISARM_FLAG.exists():
        try:
            DISARM_FLAG.unlink()
            _log("listen: claim 布防，自动解除全局撤防标记")
        except Exception as e:
            _log("解除撤防标记失败: " + repr(e))
    if args.claim and BUSY_FLAG.exists():
        try:
            BUSY_FLAG.unlink()
            _log("listen: claim 布防，清除残留的 busy.lock")
        except Exception:
            pass
    while True:
        if DISARM_FLAG.exists():
            if not _park("全局撤防中（DISARMED 标记存在）", session, args):
                return 0
        owner = _load_owner()
        owner_session = (owner or {}).get("session") or ""
        if not owner_session or owner_session == session:
            break
        if args.claim:
            # 用户要求布防：先登记所有权（让旧会话的重布防进入待命），再停掉旧监听器
            _save_owner(session)
            killed = _kill_listeners(except_pids=(os.getpid(), os.getppid()))
            _log(f"listen: claim 接管，停掉旧监听器 {killed}（原 owner={owner_session}）")
            break
        if not _park(f"监听已由其他会话持有（owner={owner_session}）", session, args):
            return 0
    _save_owner(session)
    only_kw = _sender_filter(getattr(args, "only_title", "") or "")
    if only_kw:
        _log("listen: 发送人过滤启用 " + str(only_kw) + "（其它会话通知不唤醒，只写日志）")
    dev_wait = float(getattr(args, "device_wait", 0) or 900)
    serial = _wait_for_device(dev_wait)   # 启动时静默等设备（手机重启/锁屏时不反复唤醒）
    if not serial:
        print(json.dumps({"event": "listener-error", "error": "no-android-device",
                          "hint": "手机没连上/没授权 USB 调试（已尝试重启 adb server）"},
                         ensure_ascii=False))
        return 3
    # ---- 前台看板状态：微信不在前台时不做任何截图/OCR（状态从 logcat 事件流免费更新）
    fg_enabled = not getattr(args, "no_foreground_watch", False)
    args.only_title_effective = only_kw
    fg_pkg, screen_on = "", True
    fg_state = {"digest": "", "fp": None, "base": None, "note": "", "retry": 0,
                "last_change": 0.0}
    next_scan, next_verify = 0.0, 0.0
    if fg_enabled:
        fg_pkg, screen_on = _focus_pkg(), _screen_awake()
        next_verify = time.time() + max(20.0, args.fg_verify)
        _log("listen: 前台看板启用（当前前台=%s 亮屏=%s 间隔=%.1fs）"
             % (fg_pkg or "?", screen_on, args.fg_interval))
    _log("listen: start " + serial + " logcat -b events -v threadtime -T 1"
         + f" pkgs={sorted(pkgs)}")
    try:
        proc, q = _spawn_logcat(serial)
    except Exception as e:
        print(json.dumps({"event": "listener-error", "error": "spawn-failed", "detail": repr(e)},
                         ensure_ascii=False))
        return 3
    started = time.time()
    hits = []          # [(ts_str, raw_line, pkg)]
    deadline = None    # burst 截止时间
    pending_notif = None  # 命中时提前 dump 的通知（过滤判断用）
    last_hit = _load_last_hit()  # 上次已处理的事件行（跳过 logcat 回放）
    while True:
        broke = None
        try:
            item = q.get(timeout=(0.3 if fg_enabled else 5.0))
        except queue.Empty:
            item = None
            if proc.poll() is not None:
                broke = f"logcat-exited(code={proc.returncode})"
        if broke is None and item is not None:
            if item == "__EOF__":
                broke = "logcat-eof"
            elif item.startswith("__READER_ERR__"):
                broke = "reader"
        if broke:
            # adb 掉线 / USB 断开 / logcat 被杀：先自愈重连，别让 agent 白醒一场
            _log("listen: logcat 断流（" + broke + "），尝试自愈重连")
            try:
                proc.kill()
            except Exception:
                pass
            serial = _ensure_device(900)
            if not serial:
                print(json.dumps({"event": "listener-error", "error": "adb-reconnect-failed",
                                  "detail": broke}, ensure_ascii=False))
                return 3
            try:
                proc, q = _spawn_logcat(serial)
            except Exception as e:
                print(json.dumps({"event": "listener-error", "error": "spawn-failed",
                                  "detail": repr(e)}, ensure_ascii=False))
                return 3
            _log("listen: 已重连 " + serial + "，继续监听")
            started = time.time()
            hits.clear()
            deadline = None
            pending_notif = None
            if fg_enabled:      # 重连后重新校准前台/亮屏状态
                fg_pkg, screen_on = _focus_pkg(), _screen_awake()
                fg_state.update({"digest": "", "fp": None, "base": None})
                next_verify = time.time() + max(20.0, args.fg_verify)
            continue
        if item is not None:
            m = _ENQ.search(item)
            if m:
                pkg = m.group(3).strip()
                if pkg in pkgs:
                    raw_line = item.rstrip("\n")
                    if last_hit and raw_line.strip() == last_hit:
                        _log("listen: 跳过 logcat 回放的旧事件（上次已处理，不重复唤醒）")
                    else:
                        stamp = item.split()[0] if item.split() else ""
                        hits.append((stamp, raw_line, pkg))
                        _log(f"listen: 命中通知事件 pkg={pkg} id={m.group(4)} at={stamp}")
                        deadline = time.time() + args.burst
            if fg_enabled:
                # 顺带从事件流解析「前台 App / 亮屏」：微信不在前台时不做任何 adb 扫描
                if "wm_set_resumed_activity" in item:
                    m2 = _FG_ACT.search(item)
                    if m2:
                        new_pkg = _pkg_of(m2.group(1))
                        if new_pkg != fg_pkg:
                            fg_pkg = new_pkg
                            fg_state.update({"digest": "", "fp": None, "base": None})
                            _log("listen: 前台切到 " + (fg_pkg or "?"))
                elif "screen_toggled" in item:
                    m2 = _FG_SCREEN.search(item)
                    if m2:
                        new_on = (m2.group(1) == "1")
                        if new_on != screen_on:
                            screen_on = new_on
                            fg_state.update({"digest": "", "base": None})
                            _log("listen: 屏幕" + ("点亮" if screen_on else "熄灭（暂停前台看板）"))
                elif "input_focus" in item and "Focus entering" in item:
                    m2 = _FG_FOCUS.search(item)
                    if m2 and m2.group(1) == WECHAT_PKG and fg_pkg != WECHAT_PKG:
                        fg_pkg = WECHAT_PKG
                        fg_state.update({"digest": "", "fp": None, "base": None})
                        _log("listen: 前台切到 " + WECHAT_PKG)
        # 事件突发窗口结束后收网
        if deadline is not None and time.time() > deadline:
            if only_kw:
                try:
                    notif_now = dump_notifications(pkgs)
                except Exception:
                    notif_now = []
                # 只读到「别的会话」的通知（如群消息）→ 不落盘、不唤醒、继续监听。
                # dump 失败/为空时按旧行为照常唤醒（宁可多花一次，也不漏掉真任务）。
                if notif_now and not _title_matches(notif_now, only_kw):
                    _log("listen: 忽略非目标会话通知 "
                         + str([n.get("title") for n in notif_now])[:160] + "，继续监听")
                    hits.clear()
                    deadline = None
                    continue
                pending_notif = notif_now
            break
        # 长时间无事件时的运行时长上限（唤醒 agent 重新布防；不是扫描）
        if args.max_hours and (time.time() - started) > args.max_hours * 3600:
            _log("listen: 达到 max-hours，正常退出让 agent 重新布防")
            print(json.dumps({"event": "listener-timeout", "run_hours": args.max_hours},
                             ensure_ascii=False))
            return 0
        if not fg_enabled:
            continue
        # ---- 前台看板：仅当微信在前台且亮屏时才工作（其余时候不截图、不 OCR）
        now = time.time()
        if now >= next_verify:
            next_verify = now + max(20.0, args.fg_verify)
            try:
                real_pkg, real_on = _focus_pkg(), _screen_awake()
            except Exception:
                real_pkg, real_on = fg_pkg, screen_on
            if real_pkg != fg_pkg or real_on != screen_on:
                _log("listen: 前台状态校正 %s/%s -> %s/%s"
                     % (fg_pkg or "?", screen_on, real_pkg or "?", real_on))
                fg_pkg, screen_on = real_pkg, real_on
                fg_state.update({"digest": "", "fp": None, "base": None})
        if fg_pkg != WECHAT_PKG or not screen_on or deadline is not None:
            if now >= next_scan:
                next_scan = now + 1.0
            continue
        if now < next_scan:
            continue
        interval = args.fg_interval
        if now - fg_state.get("last_change", 0.0) > 120:
            interval = max(interval, args.fg_idle_interval)   # 长时间静止：降频省电
        next_scan = now + interval
        try:
            fg_event = _fg_tick(serial, fg_state, args)
        except Exception as e:
            _log("listen: 前台看板异常 " + repr(e) + "\n" + traceback.format_exc()[:400])
            fg_event = None
            fg_state["digest"] = ""
        if fg_event:
            try:
                proc.kill()
            except Exception:
                pass
            path = _emit_event(fg_event)
            fg_event["file"] = str(path)
            print(json.dumps(fg_event, ensure_ascii=False))
            return 0
        # 连续两轮 OCR 都看不到微信界面（可能 wm 事件漏了/被别的 App 抢前台）→ 立刻校准
        if fg_state.get("note") in ("not-list", "not-target-chat"):
            fg_state["odd"] = fg_state.get("odd", 0) + 1
            if fg_state["odd"] >= 2:
                next_verify = 0.0
                fg_state["odd"] = 0
        else:
            fg_state["odd"] = 0
    try:
        proc.kill()
    except Exception:
        pass
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    notif = pending_notif if pending_notif is not None else dump_notifications(pkgs)
    event = {
        "event": "wechat-notification",
        "at": now_str,
        "device": serial,
        "pkgs": sorted({h[2] for h in hits}),
        "count": len(hits),
        "sender_filter": only_kw or None,
        "logcat_log": [h[1][:400] for h in hits[-6:]],
        "notifications": notif,
    }
    path = _emit_event(event)
    _save_last_hit(hits[-1][1] if hits else "")
    event["file"] = str(path)
    print(json.dumps(event, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 读新增消息

def _set_busy(mark=""):
    """标记「agent 正在处理手机」：前台看板会暂停扫描，避免自己导航/发送被当成新消息。"""
    try:
        BUSY_FLAG.parent.mkdir(parents=True, exist_ok=True)
        BUSY_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + " " + (mark or ""),
                             encoding="utf-8")
    except Exception:
        pass


def _clear_busy():
    try:
        if BUSY_FLAG.exists():
            BUSY_FLAG.unlink()
    except Exception:
        pass


def cmd_inbox(args):
    # 本轮会操作手机（进聊天页/截图/滚动）→ 先让前台看板暂停，读完即恢复（只锁命令执行期间）
    _set_busy("inbox")
    state = wt._load(STATE, None)
    try:
        if not wt.ensure_chat():
            print(json.dumps({"ok": False, "error": "ensure_chat-failed",
                              "hint": "微信没在前台/没进 目标联系人 聊天页，需人工看一眼手机"},
                             ensure_ascii=False, indent=1))
            return 1
        wt._scroll_to_latest()
        shot = adb.shot(str(wt.TMP / "trigger_inbox.png"))
        rows_now = wt.ocr_image(str(shot))
        if not wt.wechat._in_chat(rows_now, wt.CONTACT):
            print(json.dumps({"ok": False, "error": "not-in-chat", "skipped": True},
                             ensure_ascii=False, indent=1))
            return 1
        # 2185：最新一条消息常贴在输入框上方，需要扫到更下面才能取到
        msgs = wt.extract_messages(str(shot), y_bottom=2185)
        if len(msgs) > 40:
            print(json.dumps({"ok": False, "error": "suspicious-count", "count": len(msgs)},
                             ensure_ascii=False, indent=1))
            return 1
        context = [{"side": m["side"], "text": m["text"][:300]} for m in msgs][-args.limit:]
        if state is None:
            history = wt._collect_history(3)
            processed = [{"text": m["text"], "norm": wt._norm(m["text"]),
                          "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                         for m in history if m["side"] == "in" and wt._norm(m["text"])]
            seq0 = [(m["side"], wt._norm(m["text"])) for m in history][-12:]
            _write_json(STATE, {"processed": processed, "seq": seq0,
                                "inited": time.strftime("%Y-%m-%d %H:%M:%S")})
            print(json.dumps({"ok": True, "init": True, "new_tasks": [],
                              "baseline_count": len(processed),
                              "context": context}, ensure_ascii=False, indent=1))
            return 0
        processed = state.get("processed", [])
        cur_seq = [(m["side"], wt._norm(m["text"])) for m in msgs][-12:]
        prev_seq = state.get("seq", [])
        overlap = 0
        for kk in range(min(len(prev_seq), len(cur_seq)), 0, -1):
            ok = True
            for i in range(kk):
                ps = prev_seq[len(prev_seq) - kk + i]
                cs = cur_seq[i]
                if ps[0] != cs[0] or wt._similar(ps[1], cs[1]) < 0.7:
                    ok = False
                    break
            if ok:
                overlap = kk
                break
        candidates = msgs[overlap:] if overlap else msgs
        raw_new = [m for m in candidates
                   if m["side"] == "in" and wt._norm(m["text"])
                   and not wt._is_processed(m["text"], processed)]
        page_like = [m for m in raw_new
                     if sum(1 for x in ("再发一件", "多搞副业", "曝光越多") if x in m["text"]) >= 2]
        if page_like:
            raw_new = [m for m in raw_new if m not in page_like]
        new_tasks = []
        for m in raw_new:
            if wt._trivial(m["text"]):
                processed.append({"text": m["text"], "norm": wt._norm(m["text"]),
                                  "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
                continue
            t = wt.add_task(m["text"], source="wechat-event")
            new_tasks.append({"id": t["id"], "text": t["text"]})
            processed.append({"text": m["text"], "norm": wt._norm(m["text"]),
                              "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        _write_json(STATE, {"processed": processed[-300:], "seq": cur_seq,
                            "inited": state.get("inited"),
                            "last_inbox": time.strftime("%Y-%m-%d %H:%M:%S")})
        out = {"ok": True, "new_tasks": new_tasks, "context": context,
               "state_inited": state.get("inited")}
        if not new_tasks:
            out["note"] = ("没有新的文字消息。若对方发的是图片/语音/文件，inbox 取不到正文；"
                           "请开聊天页截图，必要时裁剪图片气泡后用视觉识别。")
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    except Exception as e:
        _log("inbox ERR: " + repr(e) + "\n" + traceback.format_exc()[:800])
        print(json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False, indent=1))
        return 1
    finally:
        _clear_busy()      # 命令结束就恢复前台看板（避免长时间占用导致漏消息）


# ---------------------------------------------------------------- 其他命令

def cmd_status(args):
    tasks = wt.load_tasks() if hasattr(wt, "load_tasks") else []
    evs = sorted(EV_DIR.glob("evt-*.json"))[-5:]
    out = {
        "adb": _real_adb_serial(),
        "disarmed": DISARM_FLAG.exists(),
        "owner": _load_owner(),
        "listeners": _ps_listener_pids(),
        "focus": _focus_pkg() or None,
        "screen_on": _screen_awake(),
        "busy": BUSY_FLAG.exists(),
        "badge_state": _fg_badge_state().get("sig"),
        "state": str(STATE),
        "state_exists": STATE.exists(),
        "events_total": len(list(EV_DIR.glob("evt-*.json"))),
        "last_events": [p.name for p in evs],
        "tasks_total": len(tasks),
        "tasks_pending": len(wt.pending_tasks()),
        "last_tasks": tasks[-3:],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_reply(args):
    text = " ".join(args.text).strip()
    if not text:
        print("reply 需要 --text 文本")
        return 2
    res = wt.wechat.send(text, to=wt.CONTACT)
    _clear_busy()
    print(json.dumps({"ok": bool(res.get("ok")), "stage": res.get("stage")},
                     ensure_ascii=False))
    return 0 if res.get("ok") else 1


def cmd_finish(args):
    if not args.id or not args.result:
        print("finish 需要 --id 和 --result")
        return 2
    res = wt.finish(args.id, args.result)
    _clear_busy()
    print(json.dumps({"ok": bool(res.get("ok")), "task": res.get("task")}, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 1


def cmd_busy(args):
    """处理中标记：on=暂停前台看板；off=恢复；不带参数=查看状态。"""
    if args.state == "on":
        _set_busy("manual")
    elif args.state == "off":
        _clear_busy()
    age = None
    if BUSY_FLAG.exists():
        try:
            age = round(time.time() - BUSY_FLAG.stat().st_mtime, 1)
        except Exception:
            age = None
    print(json.dumps({"ok": True, "busy": BUSY_FLAG.exists(), "age_seconds": age,
                      "file": str(BUSY_FLAG)}, ensure_ascii=False))
    return 0


def cmd_fg_probe(args):
    """调试：跑一次前台看板的判断（不落事件、不改基线）。"""
    out = {"focus": _focus_pkg() or None, "screen_on": _screen_awake(),
           "keyboard": _keyboard_shown(), "busy": BUSY_FLAG.exists(),
           "paused": PAUSE_FLAG.exists(), "send_lock": SEND_LOCK.exists()}
    shot = args.image
    if not shot:
        if out["focus"] != WECHAT_PKG:
            out["note"] = "微信不在前台 → 前台看板不会截图/扫描"
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0
        try:
            adb.shell("screencap -p " + REMOTE_SHOT, timeout=30)
            shot = _fg_shot_local(adb.device())
        except Exception as e:
            out["error"] = repr(e)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 1
    out["image"] = shot
    try:
        rows = ocr_image(shot)
    except Exception as e:
        out["error"] = repr(e)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 1
    out["in_chat"] = wechat._in_chat(rows, CONTACT)
    out["title_rows"] = [r["text"] for r in rows if r["y"] < 260][:4]
    st = {"base": None, "digest": "", "fp": None, "note": ""}
    if out["in_chat"]:
        try:
            msgs = wt.extract_messages(shot, y_bottom=_fg_y_bottom(shot), rows=rows)
        except Exception as e:
            out["error"] = repr(e)
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 1
        out["messages"] = [{"side": m["side"], "y": m["y"], "text": m["text"][:120]}
                           for m in msgs][-10:]
        out["baseline_marks"] = len(_fg_mark(msgs))
    else:
        sig, row = _fg_badge_sig(shot, rows)
        out["list_page"] = ("通讯录" in " ".join(r["text"] for r in rows if r["text"]))
        out["badge_sig"] = sig
        out["badge_row"] = row
        out["badge_state"] = _fg_badge_state()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_fg_selftest(args):
    """调试/回归：用两张图验证判定（默认测「聊天页新消息」，--mode list 测「会话列表红标」）。"""
    st = {"base": None, "digest": "", "fp": None, "note": ""}
    mode = getattr(args, "mode", "chat") or "chat"
    if mode == "list":
        global FG_BADGE_STATE
        saved = FG_BADGE_STATE
        tmp = ROOT / "runs" / "_wechat" / "_selftest_badge.json"
        try:
            if tmp.exists():
                tmp.unlink()
            FG_BADGE_STATE = tmp
            rows0 = ocr_image(args.baseline)
            _ev0, note0 = _fg_scan_list(args.baseline, rows0, st)
            rows1 = ocr_image(args.new)
            ev1, note1 = _fg_scan_list(args.new, rows1, st)
            out = {"ok": True, "mode": "list", "baseline_note": note0,
                   "new_note": note1, "event": ev1}
        finally:
            FG_BADGE_STATE = saved
            try:
                tmp.unlink()
            except Exception:
                pass
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    rows0 = ocr_image(args.baseline)
    _fg_scan_chat(args.baseline, rows0, st)
    rows1 = ocr_image(args.new)
    new_in, note = _fg_scan_chat(args.new, rows1, st)
    print(json.dumps({"ok": True, "mode": "chat", "note": note,
                      "new": [{"side": m["side"], "text": m["text"]} for m in (new_in or [])]},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_disarm(args):
    """全局撤防：先写 DISARMED 标记（让任何会话的重布防都只进入待命），再杀掉所有监听进程。"""
    try:
        DISARM_FLAG.parent.mkdir(parents=True, exist_ok=True)
        DISARM_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except Exception as e:
        print(json.dumps({"ok": False, "error": "写撤防标记失败: " + repr(e)}, ensure_ascii=False))
        return 1
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'python.exe' -and $_.CommandLine -like '*wechat_trigger.py listen*') -or "
        "($_.Name -eq 'adb.exe' -and $_.CommandLine -like '*logcat*events*') "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )
    killed = []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=90)
        killed = [line.strip() for line in out.stdout.splitlines() if line.strip().isdigit()]
    except Exception as e:
        print(json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
        return 1
    time.sleep(1.5)
    left = _ps_listener_pids()
    print(json.dumps({"ok": not left, "disarmed_flag": str(DISARM_FLAG),
                      "killed_pids": killed, "remaining_listeners": left},
                     ensure_ascii=False, indent=1))
    return 0 if not left else 1


def cmd_arm(args):
    """解除全局撤防（删除 DISARMED 标记）。待命中的监听器会自动开始监听。"""
    try:
        if DISARM_FLAG.exists():
            DISARM_FLAG.unlink()
    except Exception as e:
        print(json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
        return 1
    listeners = _ps_listener_pids()
    print(json.dumps({"ok": True, "disarmed_flag": DISARM_FLAG.exists(),
                      "listeners": listeners,
                      "hint": "标记已清除；若 listeners 为空，需要在会话里重新布防（后台任务）"},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_test_notif(args):
    stamp = time.strftime("%H:%M:%S")
    try:
        out = adb.shell(f"cmd notification post -S bigtext -t 'WECHAT-TRIGGER-TEST' triggertest '自测事件 {stamp}'")
    except Exception as e:
        print(json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "posted": stamp, "out": (out or "")[:120]}, ensure_ascii=False))
    return 0


def main(argv):
    _utf8()
    ap = argparse.ArgumentParser(description="微信事件驱动接管触发器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("listen", help="常驻监听 adb 事件流；命中微信新消息通知即退出（给 DSH 后台任务用）")
    p.add_argument("--include-shell", action="store_true", help="把 com.android.shell 通知也算事件（自测用）")
    p.add_argument("--burst", type=float, default=2.0, help="事件突发聚合窗口秒数（默认 2）")
    p.add_argument("--max-hours", type=float, default=0, help="最长运行小时数；0=不限（到点退出以唤醒 agent 重新布防）")
    p.add_argument("--claim", action="store_true",
                   help="接管所有权：停掉其他会话的监听器（用户口头要求布防时用；唤醒后的自动重布防不要加）")
    p.add_argument("--only-title", default="",
                   help="只唤醒标题含这些关键字的通知（逗号分隔，如 目标联系人）；"
                        "默认读 runs/_wechat/sender_filter.txt，空=不过滤（任何微信通知都唤醒）")
    p.add_argument("--no-foreground-watch", action="store_true",
                   help="关闭前台看板（默认开启：微信停在前台时也能识别新消息，空闲不扫描）")
    p.add_argument("--fg-interval", type=float, default=3.0,
                   help="微信在前台时的截图间隔秒数（默认 3；画面没变就不会 OCR/不花 token）")
    p.add_argument("--fg-idle-interval", type=float, default=6.0,
                   help="画面连续 2 分钟没变化后的降频间隔秒数（默认 6，省电）")
    p.add_argument("--fg-verify", type=float, default=60.0,
                   help="每隔多少秒用 dumpsys 校正一次「前台 App/亮屏」状态（默认 60）")
    p.add_argument("--device-wait", type=float, default=900,
                   help="启动时等手机上线的最长秒数（默认 900）。手机暂时掉线/重启时给 21600（6 小时）："
                        "进程静默等待、不耗 token，手机一回来自动开始监听；期间不反复重启 adb server。")

    p = sub.add_parser("inbox", help="读聊天页新增消息并建任务")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")

    sub.add_parser("status", help="查看触发器状态")

    p = sub.add_parser("busy", help="处理中标记：on=暂停前台看板，off=恢复（不带参数=查看）")
    p.add_argument("state", nargs="?", choices=["on", "off"], default=None)

    p = sub.add_parser("fg-probe", help="调试：跑一次前台看板判断，不落事件")
    p.add_argument("--image", default="", help="指定现成截图（默认现场截一张，需要微信在前台）")

    p = sub.add_parser("fg-selftest", help="调试/回归：用两张截图验证判定")
    p.add_argument("--baseline", required=True)
    p.add_argument("--new", required=True)
    p.add_argument("--mode", choices=["chat", "list"], default="chat",
                   help="chat=目标聊天页新消息（默认）；list=会话列表 目标联系人 未读红标")

    sub.add_parser("disarm", help="全局撤防：写 DISARMED 标记 + 杀掉所有监听进程（可从任意会话执行）")
    sub.add_parser("arm", help="解除全局撤防（删除 DISARMED 标记；待命中的监听器会自动开始监听）")

    p = sub.add_parser("reply", help="微信回结果")
    p.add_argument("--text", nargs="+", default=[])

    p = sub.add_parser("finish", help="标记旧任务完成并微信回结果")
    p.add_argument("--id", type=int, default=0)
    p.add_argument("--result", default="")

    sub.add_parser("test-notif", help="发一条系统测试通知，自测事件链路")

    args = ap.parse_args(argv[1:])
    return {
        "listen": cmd_listen,
        "inbox": cmd_inbox,
        "status": cmd_status,
        "disarm": cmd_disarm,
        "arm": cmd_arm,
        "reply": cmd_reply,
        "finish": cmd_finish,
        "busy": cmd_busy,
        "fg-probe": cmd_fg_probe,
        "fg-selftest": cmd_fg_selftest,
        "test-notif": cmd_test_notif,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
