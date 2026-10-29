# -*- coding: utf-8 -*-
r"""微信任务通道：把目标联系人发来的微信消息当成任务，排队交给 agent 执行。

  python phone/wechat_task.py status                 # 看状态
  python phone/wechat_task.py poll                   # 轮询一次（新消息 -> 任务队列 + 微信回执）
  python phone/wechat_task.py watch --interval 60    # 常驻轮询（后台跑）
  python phone/wechat_task.py pending --json         # 列出待处理任务（给定时任务/agent用）
  python phone/wechat_task.py finish --id 3 --result "结果摘要"   # 标记完成 + 微信回结果
  python phone/wechat_task.py inject "测试任务"       # 手动塞一条任务（自测用）

原理：
- 微信不暴露 uiautomator 树 → 截屏 + 本地 OCR（零 token）；
- 聊天气泡按颜色分类：白色=对方发来，绿色(149,236,105)=自己发出；
- 只处理「白色气泡里的新文字」（和自己已处理的做模糊去重）；
- 第一次运行只记基线（不把历史消息当任务）；
- 新任务写入 runs/wechat_tasks/tasks.json，并发微信回执「收到」；
- agent 端定时任务读 pending -> 执行 -> finish（finish 会微信回复结果）。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adb  # noqa: E402
import awake  # noqa: E402
from ocr import ocr_image  # noqa: E402
import wechat  # noqa: E402

PKG = "com.tencent.mm"
ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "runs" / "_wechat"
TASKS_DIR = ROOT / "runs" / "wechat_tasks"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
STATE = TMP / "task_state.json"
TASKS = TASKS_DIR / "tasks.json"
WATCH_LOG = TASKS_DIR / "watch.log"
CONTACT = os.environ.get("WECHAT_CONTACT", "").strip()


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg
    try:
        with WATCH_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 气泡提取

def _components(mask, mw, mh, min_area):
    from collections import deque
    seen = bytearray(mw * mh)
    out = []
    for i in range(mw * mh):
        if not mask[i] or seen[i]:
            continue
        q = deque([i]); seen[i] = 1; pts = []
        while q:
            cur = q.popleft(); pts.append(cur)
            cy, cx = divmod(cur, mw)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < mh and 0 <= nx < mw:
                        ni = ny * mw + nx
                        if mask[ni] and not seen[ni]:
                            seen[ni] = 1
                            q.append(ni)
        if len(pts) >= min_area:
            xs = [p % mw for p in pts]; ys = [p // mw for p in pts]
            out.append((min(xs), min(ys), max(xs), max(ys), len(pts)))
    return out


def extract_messages(shot_path, y_bottom=2060):
    """从聊天页截图提取消息：[{side:'in'|'out', y, x1,y1,x2,y2, text}]

    y_bottom：气泡底部扫描边界。最新一条消息常贴在输入框上方（y≈2050-2160），
    需要更靠下才能截到，调用方可传 2185；默认 2060 保持旧行为。
    """
    from PIL import Image
    im = Image.open(shot_path).convert("RGB")
    w, h = im.size
    px = im.load()
    scale = 3
    mw, mh = w // scale, h // scale
    white = bytearray(mw * mh)
    green = bytearray(mw * mh)
    y_top, y_bottom = 250 // scale, y_bottom // scale
    for yy in range(y_top, min(mh, y_bottom)):
        for xx in range(mw):
            r, g, b = px[xx * scale, yy * scale]
            if r > 250 and g > 250 and b > 250:
                white[yy * mw + xx] = 1
            elif g > 180 and r < 190 and (g - r) > 40 and (g - b) > 60:
                green[yy * mw + xx] = 1
    # 白气泡（对方）：min_area 换算到 downscale 后 ~ 600 px
    bubbles = [{"side": "in", "box": b} for b in _components(white, mw, mh, 350)]
    bubbles += [{"side": "out", "box": b} for b in _components(green, mw, mh, 350)]
    rows = ocr_image(str(shot_path))
    msgs = []
    for bb in bubbles:
        x1, y1, x2, y2, _ = bb["box"]
        x1 *= scale; y1 *= scale; x2 *= scale; y2 *= scale
        lines = []
        for r in rows:
            cx = r["x"] + r["w"] / 2
            cy = r["y"] + r["h"] / 2
            if x1 - 20 <= cx <= x2 + 20 and y1 - 10 <= cy <= y2 + 10:
                lines.append(r)
        lines.sort(key=lambda r: (r["y"], r["x"]))
        text = "\n".join(x["text"].strip() for x in lines if x["text"].strip())
        if text.strip():
            msgs.append({"side": bb["side"], "y": y1, "x1": x1, "y1": y1,
                         "x2": x2, "y2": y2, "text": text.strip()})
    msgs.sort(key=lambda m: m["y"])
    return msgs


# ---------------------------------------------------------------- 文本去重

def _norm(s):
    return "".join(ch for ch in (s or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff").lower()


def _similar(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _is_processed(text, processed):
    n = _norm(text)
    for p in processed[-120:]:
        if _similar(n, p.get("norm", "")) >= 0.75:
            return True
    return False


# ---------------------------------------------------------------- 导航

def _focus_ok():
    try:
        return PKG in adb.shell("dumpsys window | grep -m1 mCurrentFocus")
    except adb.AdbError:
        return False


def ensure_chat():
    """确保微信在前台、且停在目标联系人的聊天页；返回是否成功。"""
    if not CONTACT:
        _log("ensure_chat: 未设置 WECHAT_CONTACT")
        return False
    awake.ensure_awake()
    if _focus_ok() and wechat._in_chat(wechat._ocr(), CONTACT):
        return True
    for attempt in range(5):
        if not _focus_ok():
            adb.open_app(PKG)
            time.sleep(2.8)
        rows = wechat._ocr()
        if wechat._in_chat(rows, CONTACT):
            return True
        hit = wechat._find_contact(rows, CONTACT)
        if hit:
            adb.tap(hit["x"] + max(hit["w"] // 2, 150), hit["y"] + hit["h"] // 2)
            time.sleep(2.4)
            continue
        texts = " ".join(r["text"] for r in rows if r["text"])
        # 已经在一级聊天列表、但 OCR 还没认出联系人（列表未渲染完/被横幅挤下去）：别按返回键，稍等重试
        if "通讯录" in texts and "发现" in texts:
            time.sleep(1.2)
            continue
        # 不在聊天列表：返回一级页
        adb.shell("input keyevent 4")
        time.sleep(1.0)
    return wechat._in_chat(wechat._ocr(), CONTACT)


def _scroll_to_latest():
    for _ in range(2):
        adb.swipe(540, 1500, 540, 800, 280)
        time.sleep(0.5)


# ---------------------------------------------------------------- 任务存取

def load_tasks():
    return _load(TASKS, [])


def save_tasks(tasks):
    _save_json(TASKS, tasks)


def add_task(text, source="wechat"):
    tasks = load_tasks()
    tid = (max([t["id"] for t in tasks]) + 1) if tasks else 1
    item = {"id": tid, "text": text, "source": source,
            "status": "pending", "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    tasks.append(item)
    save_tasks(tasks)
    return item


def pending_tasks():
    return [t for t in load_tasks() if t.get("status") == "pending"]


# ---------------------------------------------------------------- 轮询

def _trivial(text):
    """明显的误触/无意义消息（纯数字、纯符号），不建任务、不回执。"""
    n = _norm(text)
    if not n:
        return True
    if len(n) <= 2 and all(ch.isdigit() or ch in ".,!?;:-_/\\'\"()[]{}<>+=*&^%$#@~`|" for ch in n):
        return True
    return False


def _collect_history(max_screens=3):
    """重建基线用：从最新往旧翻几屏，收集所有消息（去重，旧->新排序）。"""
    _scroll_to_latest()
    all_msgs = []
    for _ in range(max_screens):
        shot = adb.shot(str(TMP / "seed.png"))
        rows = ocr_image(str(shot))
        if not wechat._in_chat(rows, CONTACT):
            break
        msgs = extract_messages(str(shot))
        all_msgs = msgs + all_msgs
        adb.swipe(540, 900, 540, 1700, 300)   # 往下滑 = 看更早的消息
        time.sleep(1.0)
    _scroll_to_latest()
    time.sleep(0.6)
    out = []
    seen = []
    for m in all_msgs:
        n = _norm(m["text"])
        if not n:
            continue
        if any(m["side"] == s and _similar(n, t) > 0.85 for s, t in seen[-40:]):
            continue
        seen.append((m["side"], n))
        out.append(m)
    return out


def poll(ack=False, quiet=False):
    """检查一次新消息；返回 {ok, new, acked, msgs}。"""
    state = _load(STATE, None)
    shot = None
    try:
        if not ensure_chat():
            _log("poll: 没能进入目标聊天页")
            return {"ok": False, "error": "ensure_chat failed"}
        _scroll_to_latest()
        shot = adb.shot(str(TMP / "poll.png"))
        # 关键防护：手机可能正被用户手动操作，截图未必停在目标聊天页
        rows_now = ocr_image(str(shot))
        if not wechat._in_chat(rows_now, CONTACT):
            _log("poll: 截图不在目标聊天页（可能被手动切走），跳过")
            return {"ok": False, "error": "not_in_chat", "skipped": True}
        msgs = extract_messages(str(shot))
        if len(msgs) > 40:
            _log(f"poll: 可疑消息数 {len(msgs)}，跳过")
            return {"ok": False, "error": "suspicious_count", "skipped": True}
        home_markers = ("通讯录", "微信团队", "公众号", "腾讯新闻")
        for m in msgs:
            txt = m["text"]
            if len(txt) > 800:
                _log("poll: 可疑超长消息（可能不是聊天页），跳过")
                return {"ok": False, "error": "suspicious_long_message", "skipped": True}
            if m["side"] == "in" and len(txt) > 120 and sum(1 for b in home_markers if b in txt) >= 2:
                _log("poll: 可疑消息含微信主页特征，跳过")
                return {"ok": False, "error": "suspicious_homepage", "skipped": True}
        incoming = [m for m in msgs if m["side"] == "in" and _norm(m["text"])]
        if state is None:
            # 首次/重置：翻几屏历史，只记基线不派任务（避免把旧消息当新任务）
            history = _collect_history(3)
            processed = [{"text": m["text"], "norm": _norm(m["text"]),
                          "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                         for m in history if m["side"] == "in" and _norm(m["text"])]
            seq0 = [(m["side"], _norm(m["text"])) for m in history][-12:]
            _save_json(STATE, {"processed": processed, "seq": seq0,
                              "inited": time.strftime("%Y-%m-%d %H:%M:%S")})
            _log(f"poll: 初始化基线（翻历史），记入 {len(processed)} 条旧消息")
            return {"ok": True, "new": [], "acked": False, "init": True}
        processed = state.get("processed", [])
        # 用「可见消息序列」做滑窗对齐：老消息滚出屏幕、新消息追加在底部
        cur_seq = [(m["side"], _norm(m["text"])) for m in msgs][-12:]
        prev_seq = state.get("seq", [])
        overlap = 0
        for kk in range(min(len(prev_seq), len(cur_seq)), 0, -1):
            ok = True
            for i in range(kk):
                ps = prev_seq[len(prev_seq) - kk + i]
                cs = cur_seq[i]
                if ps[0] != cs[0] or _similar(ps[1], cs[1]) < 0.7:
                    ok = False
                    break
            if ok:
                overlap = kk
                break
        candidates = [msgs[i] for i in range(overlap, len(msgs))] if overlap else msgs
        PAGE_MARKERS = ("再发一件", "多搞副业", "曝光越多", "副业越多")
        raw_new = [m for m in candidates if m["side"] == "in" and _norm(m["text"]) and not _is_processed(m["text"], processed)]
        # 页面文案误识别（某些 App 的发布/成功页文字）不是用户消息
        page_like = [m for m in raw_new if sum(1 for x in PAGE_MARKERS if x in m["text"]) >= 2]
        if page_like:
            _log("poll: 跳过疑似页面文案 " + repr(page_like[0]["text"][:40]))
            raw_new = [m for m in raw_new if m not in page_like]
        new = [m for m in raw_new if not _trivial(m["text"])]
        # 误触/无意义消息：静默记入已处理，不建任务不回执
        for m in raw_new:
            if _trivial(m["text"]):
                processed.append({"text": m["text"], "norm": _norm(m["text"]),
                                  "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
                _log("poll: 忽略无意义消息 " + repr(m["text"][:20]))
        state["seq"] = cur_seq
        out = {"ok": True, "new": [], "acked": False,
               "msgs": [{"side": m["side"], "text": m["text"][:120]} for m in msgs]}
        if new:
            for m in new:
                t = add_task(m["text"])
                out["new"].append({"id": t["id"], "text": t["text"]})
                processed.append({"text": m["text"], "norm": _norm(m["text"]),
                                  "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
            _save_json(STATE, {"processed": processed, "seq": state.get("seq"),
                              "last_poll": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "inited": state.get("inited")})
            _log("poll: 新任务 " + json.dumps(out["new"], ensure_ascii=False))
            if ack:
                lines = ["收到：" + (t["text"].splitlines()[0][:40]) for t in out["new"]]
                ack_text = "【微信任务已收到】" + "；".join(lines) + "。处理完发你结果。"
                try:
                    r = wechat.send(ack_text, to=CONTACT)
                    out["acked"] = bool(r.get("ok"))
                except Exception as e:
                    _log("poll: 回执失败 " + repr(e))
        else:
            _save_json(STATE, {"processed": processed, "seq": state.get("seq"),
                              "last_poll": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "inited": state.get("inited")})
        return out
    except Exception as e:
        _log("poll ERR: " + repr(e) + "\n" + traceback.format_exc()[:800])
        return {"ok": False, "error": repr(e)}


# ---------------------------------------------------------------- 完成 + 微信回结果

def finish(task_id, result):
    tasks = load_tasks()
    hit = next((t for t in tasks if t["id"] == task_id), None)
    if not hit:
        return {"ok": False, "error": f"任务 {task_id} 不存在"}
    hit["status"] = "done"
    hit["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    hit["result"] = result
    save_tasks(tasks)
    msg = "【任务完成·#" + str(task_id) + "】" + result
    try:
        r = wechat.send(msg, to=CONTACT)
        ok = bool(r.get("ok"))
    except Exception as e:
        ok = False
        _log("finish: 微信回复失败 " + repr(e))
    return {"ok": ok, "task": hit}


# ---------------------------------------------------------------- CLI

def main(argv):
    _utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "poll", "watch", "pending", "finish", "inject"])
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--id", type=int, default=0)
    ap.add_argument("--result", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("text", nargs="*")
    args = ap.parse_args(argv[1:])
    cmd = args.cmd
    if cmd == "status":
        tasks = load_tasks()
        out = {"state_exists": STATE.exists(), "tasks": len(tasks),
               "pending": len(pending_tasks()),
               "last_tasks": tasks[-5:], "watch_log": str(WATCH_LOG)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if cmd == "poll":
        res = poll()
        print(json.dumps({k: v for k, v in res.items() if k != "msgs"}, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if cmd == "watch":
        _log(f"watch: 启动，间隔 {args.interval}s")
        last_force = 0.0
        while True:
            try:
                fg = ""
                try:
                    fg = adb.shell("dumpsys window | grep -m1 mCurrentFocus")
                except Exception:
                    pass
                wechat_front = "com.tencent.mm" in fg
                force = (time.time() - last_force) > 1500
                if (TMP / "pause").exists():
                    time.sleep(max(15, args.interval))
                    continue
                if wechat_front or force:
                    res = poll()
                    last_force = time.time()
                    if res.get("new"):
                        _log("watch: 新任务 " + json.dumps(res["new"], ensure_ascii=False))
                else:
                    _log("watch: 前台是其他 App，跳过本轮（最多 10 分钟强制轮询一次）")
            except Exception as e:
                _log("watch ERR: " + repr(e))
            time.sleep(max(15, args.interval))
    if cmd == "pending":
        res = {"pending": pending_tasks(), "count": len(pending_tasks())}
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if cmd == "finish":
        if not args.id or not args.result:
            print("finish 需要 --id 和 --result"); return 2
        res = finish(args.id, args.result)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if cmd == "inject":
        text = " ".join(args.text)
        if not text:
            print("inject 需要文本"); return 2
        t = add_task(text, source="manual-test")
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
