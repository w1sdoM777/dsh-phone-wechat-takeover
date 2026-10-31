# -*- coding: utf-8 -*-
r"""微信汇报：给指定联系人发消息（OCR 驱动，因为微信不暴露 uiautomator 树）。

  python phone/wechat.py send "汇报文本"
  python phone/wechat.py send --to 目标联系人 "汇报文本"
  python phone/wechat.py open
  python phone/wechat.py status

说明：
- 微信主界面/聊天页的 uiautomator dump 只有空节点（App 屏蔽了无障碍树），
  所以全部用 截屏 + Windows OCR（本地免费）定位控件。
- 认人规则：主页最近会话/搜索结果里名字模糊匹配 目标联系人（OCR 常把 1 读成 l）。
- 发送：点输入框 → ADBKeyboard 广播中文 → OCR 找「发送」→ 点 → OCR 验证气泡。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adb  # noqa: E402
import awake  # noqa: E402
from ocr import ocr_image  # noqa: E402

PKG = "com.tencent.mm"
ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "runs" / "_wechat"
TMP.mkdir(parents=True, exist_ok=True)
RESULT = TMP / "last.json"
# 发送期间给「前台看板」用的互斥标记：识别器看到它就暂停截图，避免把 agent 自己
# 打字/发送的过程当成新消息（wechat_trigger.py 里同名路径）。
SEND_LOCK = TMP / "sending.lock"
DEFAULT_TO = os.environ.get("WECHAT_CONTACT", "").strip()


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _save(res):
    RESULT.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def _shot(name="wx.png"):
    path = TMP / name
    adb.shot(str(path))
    return path


def _ocr():
    return ocr_image(str(_shot()))


def _kb():
    try:
        return bool(adb.shell("dumpsys input_method | grep -m1 'mInputShown=true'").strip())
    except adb.AdbError:
        return False


def _focus_ok():
    try:
        return PKG in adb.shell("dumpsys window | grep -m1 mCurrentFocus")
    except adb.AdbError:
        return False


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def _name_match(text, keyword=DEFAULT_TO):
    t = _norm(text).lower()
    k = keyword.lower()
    if k in t:
        return True
    # OCR 常把 1 读成 l/i
    pat = "".join("[il1]" if c in "1il" else re.escape(c) for c in k)
    return re.search(pat, t) is not None


def open_app():
    awake.ensure_awake()
    adb.open_app(PKG)
    time.sleep(2.8)


def _find_contact(rows, keyword=DEFAULT_TO, ymin=250):
    cands = []
    for r in rows:
        if r["y"] < ymin or not r["text"]:
            continue
        if _name_match(r["text"], keyword):
            cands.append(r)
    return cands[0] if cands else None


def _in_chat(rows, keyword=DEFAULT_TO):
    """顶部标题是否就是目标联系人。"""
    for r in rows:
        if r["y"] < 260 and _name_match(r["text"], keyword):
            return True
    return False


def open_chat(keyword=DEFAULT_TO):
    open_app()
    rows = _ocr()
    if _in_chat(rows, keyword):
        return True
    hit = _find_contact(rows, keyword)
    if not hit:
        return False
    adb.tap(hit["x"] + max(hit["w"] // 2, 150), hit["y"] + hit["h"] // 2)
    time.sleep(2.4)
    rows = _ocr()
    return _in_chat(rows, keyword)


def _tap_input():
    # 键盘已经弹出来（比如有草稿）就别再点，避免点到键盘按键
    if _kb():
        return True
    # 微信底部输入栏：常见 y≈2200-2320，点中左侧文本区
    adb.tap(430, 2255)
    time.sleep(1.3)
    if not _kb():
        adb.tap(600, 2245)
        time.sleep(1.2)
    return _kb()


def find_green_send(path, y_from=300, y_to=1900):
    """截屏找微信绿色「发送」按钮，返回 (x, y) 或 None（本地零 token）。"""
    from PIL import Image
    from collections import deque
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    scale = 2
    mw, mh = w // scale, h // scale
    mask = bytearray(mw * mh)
    for yy in range(max(0, y_from // scale), min(mh, y_to // scale)):
        for xx in range(mw // 2, mw):
            r, g, b = px[xx * scale, yy * scale]
            if g > 140 and (g - r) > 60 and (g - b) > 40:
                mask[yy * mw + xx] = 1
    seen = bytearray(mw * mh)
    best = None
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
                    if 0 <= ny < mh and 0 <= nx < mw and mask[ny * mw + nx] and not seen[ny * mw + nx]:
                        seen[ny * mw + nx] = 1
                        q.append(ny * mw + nx)
        if not (400 <= len(pts) <= 4000):
            continue
        xs = [p % mw for p in pts]; ys = [p // mw for p in pts]
        bw = (max(xs) - min(xs)) * scale; bh = (max(ys) - min(ys)) * scale
        # 发送按钮是小圆角矩形；绿色消息气泡尺寸会大很多，必须排除
        if not (80 <= bw <= 240 and 50 <= bh <= 140):
            continue
        cx = (min(xs) + max(xs)) // 2 * scale
        cy = (min(ys) + max(ys)) // 2 * scale
        if best is None or len(pts) > best[0]:
            best = (len(pts), cx, cy)
    return (best[1], best[2]) if best else None


def _find_send(rows):
    for r in rows:
        if "发送" in _norm(r["text"]):
            return r
    return None


def send(text, to=DEFAULT_TO):
    """对外入口：发送期间打上 sending.lock，供前台看板暂停识别（避免自触发）。"""
    try:
        SEND_LOCK.parent.mkdir(parents=True, exist_ok=True)
        SEND_LOCK.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except Exception:
        pass
    try:
        return _send(text, to=to)
    finally:
        try:
            SEND_LOCK.unlink()
        except Exception:
            pass


def _send(text, to=DEFAULT_TO):
    if not open_chat(to):
        return _save({"ok": False, "stage": "open_chat", "error": "没找到联系人/没进聊天页"})
    if not _tap_input():
        return _save({"ok": False, "stage": "input", "error": "输入框没弹出来/键盘没起"})
    from cn_input import input_cn
    try:
        input_cn(text, field=(430, 2255))
    except Exception as e:
        return _save({"ok": False, "stage": "type", "error": repr(e)})
    time.sleep(1.2)
    shot = _shot("before_send.png")
    gb = find_green_send(str(shot))
    if gb:
        adb.tap(*gb)
    else:
        rows = _ocr()
        hit = _find_send(rows)
        if not hit:
            time.sleep(1.2)
            rows = _ocr()
            hit = _find_send(rows)
        if not hit:
            return _save({"ok": False, "stage": "send_btn", "error": "没找到「发送」按钮（草稿可能还在输入框）"})
        adb.tap(hit["x"] + hit["w"] // 2, hit["y"] + hit["h"] // 2)
    time.sleep(2.0)
    # 先收键盘再验证：避免把输入框里的草稿误判成已发送
    if _kb():
        adb.shell("input keyevent 4")
        time.sleep(1.0)
    vshot = _shot("verify.png")
    rows2 = ocr_image(str(vshot))

    def _core(s):
        return "".join(ch for ch in _norm(s) if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")

    def _cjk(s):
        return "".join(ch for ch in s if "\u4e00" <= ch <= "\u9fff")

    target = _core(text)[:12]
    t_cjk = _cjk(target)
    mcjk = _cjk(text)
    # 长文本/OCR 噪声兜底用：正文的全部 6 字片段（行匹配只要有任意片段命中即可）
    cjk6 = {mcjk[i:i + 6] for i in range(max(0, len(mcjk) - 5))}
    ok = False
    px = None
    try:
        from PIL import Image
        px = Image.open(str(vshot)).convert("RGB").load()
    except Exception:
        px = None
    for r in rows2:
        body = _core(r["text"])
        if not (300 < r["y"] < 2100 and body):
            continue
        # #3 在 OCR 里可能读成 #5，所以优先用中文主干匹配，数字不参与
        hit = (len(t_cjk) >= 6 and t_cjk[:8] in _cjk(body)) or (len(target) >= 6 and target[:8] in body)
        if not hit and cjk6:
            # 长汇报常见：开头【任务完成·#N】等装饰符被 OCR 读成怪字，逐行「开头匹配」会漏；
            # 改为「该行任意 6 字片段在正文里出现过」就算命中，再用气泡颜色确认是自己发的。
            bcjk = _cjk(body)
            win = range(max(0, len(bcjk) - 5))
            hit = any(bcjk[i:i + 6] in mcjk for i in win)
        if not hit:
            continue
        if px is None:
            ok = True
            break
        col = None
        for dx in (25, 45, 65):
            sx = max(2, r["x"] - dx)
            rr, gg, bb = px[sx, r["y"] + r["h"] // 2]
            if gg > 140 and gg - rr > 60 and gg - bb > 30:
                col = "green"
                break
            if rr > 250 and gg > 250 and bb > 250:
                col = "white"
                break
        if col == "green":
            ok = True
            break
    if not ok and px is None:
        # 取不到像素时的最后兜底：正文尾部 8 个汉字出现在消息区即认为已发
        body_all = _cjk("".join(r["text"] for r in rows2 if 300 < r["y"] < 2200))
        tail = mcjk[-8:]
        if len(tail) >= 6 and tail in body_all:
            ok = True
    return _save({
        "ok": bool(ok),
        "stage": "done" if ok else "verify",
        "to": to,
        "text": text,
        "chat_title": [r["text"] for r in rows2 if r["y"] < 260][:3],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def read_chat(limit=40):
    """读当前聊天页的可见消息（OCR），返回行列表（新消息在下面）。"""
    if not open_chat():
        return {"ok": False, "error": "没进目标聊天页"}
    rows = _ocr()
    lines = []
    for r in rows:
        t = (r["text"] or "").strip()
        if not t:
            continue
        # 过滤状态栏/输入栏/键盘
        if r["y"] < 260 or r["y"] > 2050:
            continue
        lines.append({"y": r["y"], "text": t})
    return {"ok": True, "lines": lines[-limit:]}


def status():
    open_app()
    rows = _ocr()
    return {
        "focus": _focus_ok(),
        "in_chat": _in_chat(rows),
        "top_rows": [{"y": r["y"], "text": r["text"][:40]} for r in rows[:8]],
    }


def main(argv):
    _utf8()
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "read":
        res = read_chat()
        _save(res)
        print("READ:", "OK" if res.get("ok") else "FAIL", "->", str(RESULT))
        return 0 if res.get("ok") else 1
    if cmd == "open":
        print("IN_CHAT:", open_chat())
        return 0
    if cmd == "send":
        args = argv[2:]
        to = DEFAULT_TO
        if args and args[0] == "--to":
            to = args[1]; args = args[2:]
        text = " ".join(args)
        if not text:
            print("need text")
            return 2
        res = send(text, to=to)
        print("SENT:", "OK" if res.get("ok") else "FAIL", "->", str(RESULT))
        return 0 if res.get("ok") else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
