# -*- coding: utf-8 -*-
"""adb 封装：只用标准库，Windows 直接跑。

  python phone/adb.py devices
  python phone/adb.py shot shot.png
  python phone/adb.py tap 540 1200
  python phone/adb.py text hello
  python phone/adb.py ui
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADB = os.environ.get("ADB_PATH", str(ROOT / "tools" / "platform-tools" / "adb.exe"))


class AdbError(RuntimeError):
    pass


def _run(*args, timeout=30, binary=False):
    if not Path(ADB).exists():
        raise AdbError(f"找不到 adb：{ADB}（先跑 tools/install-adb.ps1）")
    p = subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
    if binary:
        if p.returncode != 0:
            raise AdbError(p.stderr.decode("utf-8", "replace").strip())
        return p.stdout
    out = p.stdout.decode("utf-8", "replace").strip()
    err = p.stderr.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        raise AdbError(f"adb {' '.join(args)} 失败：{err or out}")
    return out


def version():
    return _run("version")


def devices():
    rows = []
    for line in _run("devices").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append({"serial": parts[0], "state": parts[1]})
    return rows


def device():
    ok = [d for d in devices() if d["state"] == "device"]
    if not ok:
        raise AdbError("没有可用设备：插 USB、开 USB 调试、在手机上点「允许」")
    return ok[0]["serial"]


def shell(cmd, timeout=30):
    return _run("-s", device(), "shell", cmd, timeout=timeout)


def shot(path="shot.png"):
    data = _run("-s", device(), "exec-out", "screencap", "-p", timeout=25, binary=True)
    if not data.startswith(b"\x89PNG"):
        raise AdbError("截图失败（锁屏/未授权/被安全策略拦截）")
    Path(path).write_bytes(data)
    return str(Path(path).resolve())


def ui_xml():
    # 注意：uiautomator dump 在界面有动画/多窗口时可能失败，绝不能读旧文件当结果
    shell("rm -f /sdcard/ui_dump.xml")
    out = shell("uiautomator dump /sdcard/ui_dump.xml")
    if "dumped to" not in out.lower():
        raise AdbError(f"uiautomator dump 失败（界面在动画或安全窗口？）：{out.strip()[:200]}")
    return _run("-s", device(), "exec-out", "cat", "/sdcard/ui_dump.xml", timeout=25)


def tap(x, y):
    shell(f"input tap {int(x)} {int(y)}")


def swipe(x1, y1, x2, y2, ms=300):
    shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")


def key(code):
    shell(f"input keyevent {int(code)}")


def text(s):
    if any(ord(c) > 127 for c in s):
        raise AdbError("input text 不支持中文：装 ADBKeyboard 后用 broadcast 输入（见 phone/README.md）")
    shell("input text " + s.replace(" ", "%s"))


def open_app(pkg):
    shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")


# 常用 App 包名（按需增删；也可直接传包名给 open_app）
APPS = {
    "微信": "com.tencent.mm",
    "支付宝": "com.eg.android.AlipayGphone",
    "Chrome": "com.android.chrome",
    "设置": "com.android.settings",
    "抖音": "com.ss.android.ugc.aweme",
}


def open_name(name):
    pkg = APPS.get(name, name)
    open_app(pkg)
    return pkg



def wm_size():
    m = re.search(r"(\d+)x(\d+)", shell("wm size"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def ime_list():
    return [x for x in shell("ime list -s").splitlines() if x.strip()]


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv):
    _utf8()
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd, args = argv[1], argv[2:]
    try:
        if cmd == "devices":
            for d in devices():
                print(d["serial"], d["state"])
        elif cmd == "shot":
            print(shot(args[0] if args else "shot.png"))
        elif cmd == "tap":
            tap(int(args[0]), int(args[1]))
            print("ok")
        elif cmd == "swipe":
            nums = [int(x) for x in args[:4]]
            swipe(*nums, ms=int(args[4]) if len(args) > 4 else 300)
            print("ok")
        elif cmd == "text":
            text(" ".join(args))
            print("ok")
        elif cmd == "ui":
            print(ui_xml())
        elif cmd == "size":
            print(wm_size())
        elif cmd == "ime":
            print("\n".join(ime_list()))
        elif cmd == "open":
            print("已打开:", open_name(" ".join(args)))
        elif cmd == "apps":
            for name, pkg in APPS.items():
                print(f"{name:<6} {pkg}")
        else:
            print(__doc__)
            return 2
    except (AdbError, IndexError, ValueError) as e:
        print(f"[错误] {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
