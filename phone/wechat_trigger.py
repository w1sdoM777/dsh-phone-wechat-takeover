# -*- coding: utf-8 -*-
r"""微信即时接管触发器：**事件驱动**，不做定时扫描。

原理（不轮询微信内容）：
1. 手机端只要收到新微信消息，系统就会在 EventLog 里写一条
   `notification_enqueue: [uid,pid,com.tencent.mm,...]`（本机实测可见）。
2. `listen` 是一条常驻的 `adb logcat -b events` **事件流**（阻塞读，不是定时截图/OCR）；
   一旦看到 com.tencent.mm 的通知事件，立刻把事件写入 runs/wechat_events/ 并退出。
3. 把这个进程作为 DSH 后台任务跑：它一退出，DSH 就会唤醒 agent（房间任务完成通知），
   agent 醒来读事件 -> 读聊天页新增消息 -> 当任务执行 -> 微信回结果 -> 重新布防。

子命令：
  python phone/wechat_trigger.py listen [--include-shell] [--burst 2.0] [--max-hours 0]
  python phone/wechat_trigger.py inbox [--json]        # 读聊天页新增消息并建任务（唤醒后第一步之一）
  python phone/wechat_trigger.py status
  python phone/wechat_trigger.py reply --text "…"      # 微信回结果
  python phone/wechat_trigger.py finish --id N --result "…"   # 兼容旧任务队列（会微信回结果）
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
import wechat_task as wt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EV_DIR = ROOT / "runs" / "wechat_events"
EV_DIR.mkdir(parents=True, exist_ok=True)
EV_LOG = EV_DIR / "listen.log"
EV_JSONL = EV_DIR / "events.jsonl"
STATE = ROOT / "runs" / "_wechat" / "trigger_state.json"
DISARM_FLAG = ROOT / "runs" / "_wechat" / "DISARMED"
OWNER_FILE = ROOT / "runs" / "_wechat" / "owner.json"
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
    serial = _real_adb_serial()
    if not serial:
        print(json.dumps({"event": "listener-error", "error": "no-android-device",
                          "hint": "手机没连上/没授权 USB 调试"}, ensure_ascii=False))
        return 3
    cmd = [adb.ADB, "-s", serial, "logcat", "-b", "events", "-v", "threadtime", "-T", "1"]
    _log("listen: start " + " ".join(cmd[2:]) + f" pkgs={sorted(pkgs)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
    except Exception as e:
        print(json.dumps({"event": "listener-error", "error": "spawn-failed", "detail": repr(e)},
                         ensure_ascii=False))
        return 3
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc, q), daemon=True).start()
    started = time.time()
    hits = []          # [(ts_str, raw_line, pkg)]
    deadline = None    # burst 截止时间
    while True:
        try:
            item = q.get(timeout=5.0)
        except queue.Empty:
            item = None
            if proc.poll() is not None:
                _log("listen: adb logcat 进程已退出")
                print(json.dumps({"event": "listener-error", "error": "logcat-exited",
                                  "code": proc.returncode}, ensure_ascii=False))
                return 3
        if item is not None:
            if item == "__EOF__":
                _log("listen: logcat 输出结束")
                print(json.dumps({"event": "listener-error", "error": "logcat-eof"}, ensure_ascii=False))
                return 3
            if item.startswith("__READER_ERR__"):
                print(json.dumps({"event": "listener-error", "error": "reader", "detail": item},
                                 ensure_ascii=False))
                return 3
            m = _ENQ.search(item)
            if m:
                pkg = m.group(3).strip()
                if pkg in pkgs:
                    stamp = item.split()[0] if item.split() else ""
                    hits.append((stamp, item.rstrip("\n"), pkg))
                    _log(f"listen: 命中通知事件 pkg={pkg} id={m.group(4)} at={stamp}")
                    deadline = time.time() + args.burst
        # 事件突发窗口结束后收网
        if deadline is not None and time.time() > deadline:
            break
        # 长时间无事件时的运行时长上限（唤醒 agent 重新布防；不是扫描）
        if args.max_hours and (time.time() - started) > args.max_hours * 3600:
            _log("listen: 达到 max-hours，正常退出让 agent 重新布防")
            print(json.dumps({"event": "listener-timeout", "run_hours": args.max_hours},
                             ensure_ascii=False))
            return 0
    try:
        proc.kill()
    except Exception:
        pass
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    notif = dump_notifications(pkgs)
    event = {
        "event": "wechat-notification",
        "at": now,
        "device": serial,
        "pkgs": sorted({h[2] for h in hits}),
        "count": len(hits),
        "logcat_log": [h[1][:400] for h in hits[-6:]],
        "notifications": notif,
    }
    path = EV_DIR / f"evt-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.json"
    _write_json(path, event)
    try:
        with EV_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass
    event["file"] = str(path)
    _log("listen: 事件已落盘 " + str(path))
    print(json.dumps(event, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- 读新增消息

def cmd_inbox(args):
    state = wt._load(STATE, None)
    try:
        if not wt.ensure_chat():
            print(json.dumps({"ok": False, "error": "ensure_chat-failed",
                              "hint": "微信没在前台/没进目标联系人聊天页（WECHAT_CONTACT），需人工看一眼手机"},
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


# ---------------------------------------------------------------- 其他命令

def cmd_status(args):
    tasks = wt.load_tasks() if hasattr(wt, "load_tasks") else []
    evs = sorted(EV_DIR.glob("evt-*.json"))[-5:]
    out = {
        "adb": _real_adb_serial(),
        "disarmed": DISARM_FLAG.exists(),
        "owner": _load_owner(),
        "listeners": _ps_listener_pids(),
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
    print(json.dumps({"ok": bool(res.get("ok")), "stage": res.get("stage")},
                     ensure_ascii=False))
    return 0 if res.get("ok") else 1


def cmd_finish(args):
    if not args.id or not args.result:
        print("finish 需要 --id 和 --result")
        return 2
    res = wt.finish(args.id, args.result)
    print(json.dumps({"ok": bool(res.get("ok")), "task": res.get("task")}, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 1


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

    p = sub.add_parser("inbox", help="读聊天页新增消息并建任务")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")

    sub.add_parser("status", help="查看触发器状态")

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
        "test-notif": cmd_test_notif,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
