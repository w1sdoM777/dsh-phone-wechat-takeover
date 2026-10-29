# -*- coding: utf-8 -*-
"""唤醒并解锁手机（熄屏自动处理）。

  python phone/awake.py            # 唤醒 + 上滑解锁 + 校验
  python phone/awake.py --status   # 只看状态，不动手

原理：本机锁屏是「无密码滑动解锁」，所以 agent 可全自动：
  1. input keyevent 224 (WAKEUP) 点亮屏幕
  2. input swipe 540 1800 540 700 上滑
  3. dumpsys window 校验前台不是 NotificationShade/Keyguard
如果用户以后设了 PIN/图案密码，第三步会停在锁屏，脚本会如实报错（不会瞎试密码）。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adb  # noqa: E402

LOCKED_HINTS = ("NotificationShade", "Keyguard", "AOD", "LockScreen")


def _focus():
    try:
        out = adb.shell("dumpsys window | grep -E 'mCurrentFocus'")
    except adb.AdbError:
        return ""
    m = re.search(r"mCurrentFocus=Window\{[^ ]* u0 ([^}]+)\}", out)
    return m.group(1).strip() if m else out.strip()


def is_locked(focus=None):
    f = focus if focus is not None else _focus()
    return any(h in f for h in LOCKED_HINTS)


def wake():
    """点亮屏幕（如果已亮则无副作用）。"""
    adb.shell("input keyevent 224")  # KEYCODE_WAKEUP
    time.sleep(0.8)


def unlock(max_attempts=3):
    """唤醒 + 上滑解锁；返回 (ok, focus)。"""
    wake()
    for _ in range(max_attempts):
        focus = _focus()
        if not is_locked(focus):
            return True, focus
        adb.shell("input swipe 540 1800 540 700 260")
        time.sleep(1.2)
    focus = _focus()
    return (not is_locked(focus)), focus


def ensure_awake():
    """给其他脚本用：保证亮屏+解锁，失败抛异常。"""
    ok, focus = unlock()
    if not ok:
        raise adb.AdbError(
            f"手机处于安全锁屏（{focus}），无法自动解锁："
            "请人工解锁一次，或把锁屏改成「无密码滑动解锁」/ 告诉我 PIN（我可尝试注入）"
        )
    return focus


def status():
    return {
        "focus": _focus(),
        "locked": is_locked(),
        "wakefulness": adb.shell("dumpsys power | grep -E 'mWakefulness='").strip(),
    }


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(argv) > 1 and argv[1] == "--status":
        s = status()
        print("前台:", s["focus"])
        print("锁屏:", "是" if s["locked"] else "否")
        print("电源:", s["wakefulness"])
        return 0
    ok, focus = unlock()
    print("解锁:", "成功" if ok else "失败（可能是安全锁屏）")
    print("前台:", focus)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
