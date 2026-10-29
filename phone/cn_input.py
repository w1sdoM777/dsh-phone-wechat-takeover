# -*- coding: utf-8 -*-
"""中文输入：用 ADBKeyboard 广播注入（adb shell input text 不支持中文）。

  python phone/cn_input.py "测试中文输入 ABC123"     # 输入一次，结束后自动恢复原输入法
  python phone/cn_input.py --status                   # 设备 / ADBKeyboard / 输入法状态
  python phone/cn_input.py --no-restore "连续输入"    # 不恢复原输入法（之后自己 --restore 回）
  python phone/cn_input.py --restore                  # 恢复默认输入法（默认 baidu，可用参数指定）
  python phone/cn_input.py --field 540,900 "文本"     # 手动指定输入框坐标（自动定位失败时）

代码里用：

  from phone.cn_input import input_cn      # 需在 workspace 根目录，或 sys.path 加 phone/
  input_cn("中文")                          # -> {text, ime_before, ime_after, restored}

机制（ADBKeyboard == com.android.adbkeyboard）：
  1. adb shell ime enable com.android.adbkeyboard/.AdbIME    # 只做一次
  2. adb shell ime set com.android.adbkeyboard/.AdbIME       # 临时切过去
  3. 切 IME 时 MIUI 会把软键盘收起 → 重新点一下输入框，等 AdbIME 的输入连接建立
  4. adb shell am broadcast -a ADB_INPUT_TEXT --es msg '中文'
  5. adb shell ime set <原输入法>                            # 恢复并校验

前提：ADBKeyboard.apk 已安装（仓库内 phone/ADBKeyboard.apk）。
实测环境：Redmi K60 Pro / Android 13 / MIUI，原默认输入法 com.baidu.input_mi/.ImeService。
"""
from __future__ import annotations

import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adb import AdbError, device, ime_list, shell, tap, ui_xml  # noqa: E402

ADBIME = "com.android.adbkeyboard/.AdbIME"
ADBKEYBOARD_PKG = "com.android.adbkeyboard"
APK = Path(__file__).resolve().parent / "ADBKeyboard.apk"

# 实测（2026-10-28）：Redmi K60 Pro / Android 13 上原默认输入法
KNOWN_DEFAULT_IME = "com.baidu.input_mi/.ImeService"


class CnInputError(AdbError):
    """中文输入链路错误（缺 ADBKeyboard、IME 启停失败、找不到输入框、恢复失败等）。"""


# ---------------------------------------------------------------- IME 状态


def default_ime():
    """当前默认输入法 component（如 com.baidu.input_mi/.ImeService）。"""
    return shell("settings get secure default_input_method").strip()


def enabled_imes():
    """已启用的输入法列表（ime list -s）。"""
    return ime_list()


def installed():
    """ADBKeyboard 是否已安装。"""
    return ADBKEYBOARD_PKG in shell(f"pm list packages {ADBKEYBOARD_PKG}")


def ensure_enabled():
    """确保 ADBKeyboard 已安装且 IME 已启用；没启用就自动 ime enable。"""
    if not installed():
        raise CnInputError(
            "ADBKeyboard 未安装：\n"
            f"  1) adb install \"{APK}\"\n"
            f"  2) adb shell ime enable {ADBIME}"
        )
    if ADBIME in enabled_imes():
        return
    try:
        shell(f"ime enable {ADBIME}")
    except AdbError as e:
        raise CnInputError(
            f"ADBKeyboard 已安装但 ime enable 失败：{e}\n"
            f"  手动执行：adb shell ime enable {ADBIME}"
        ) from e
    time.sleep(0.3)
    if ADBIME not in enabled_imes():
        raise CnInputError(
            f"ADBKeyboard 已安装但 IME 仍未启用（{ADBIME} 不在 ime list -s 中）\n"
            f"  手动执行：adb shell ime enable {ADBIME}"
        )


def set_ime(ime):
    """切换默认输入法并校验（失败抛 CnInputError）。"""
    if not ime or "/" not in ime:
        raise CnInputError(f"输入法 component 不合法：{ime!r}（应形如 com.baidu.input_mi/.ImeService）")
    try:
        shell(f"ime set {ime}")
    except AdbError as e:
        raise CnInputError(f"ime set {ime} 失败：{e}") from e
    time.sleep(0.2)
    cur = default_ime()
    if cur != ime:
        raise CnInputError(f"ime set {ime} 后默认输入法仍是 {cur}")
    return cur


def _keyboard_shown():
    """软键盘是否正在显示（mInputShown=true 才代表 AdbIME 有输入连接）。"""
    try:
        out = shell("dumpsys input_method | grep 'mInputShown=true'")
    except AdbError:
        return False
    return bool(out.strip())


def _wait_keyboard(timeout=3.0, interval=0.15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _keyboard_shown():
            return True
        time.sleep(interval)
    return _keyboard_shown()


# ---------------------------------------------------------------- 输入框定位（uiautomator）

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _bounds_info(bounds):
    """'[l,t][r,b]' -> (center_x, center_y, area) 或 None。"""
    m = _BOUNDS_RE.fullmatch((bounds or "").strip())
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2, max(0, (x2 - x1)) * max(0, (y2 - y1)))


def _ui_nodes():
    """当前界面所有 node（失败返回空列表，不抛错）。"""
    try:
        root = ET.fromstring(ui_xml())
    except (AdbError, ET.ParseError):
        return []
    return list(root.iter("node"))


def find_input_point():
    """找当前输入框的点击坐标：优先 focused 的输入框，否则面积最大的 EditText。

    返回 (x, y) 或 None。切输入法会收起软键盘，需要再点一下这个位置来重建输入连接。
    """
    nodes = _ui_nodes()
    if not nodes:
        return None
    candidates = []   # (priority, area, center)
    for n in nodes:
        cls = n.get("class") or ""
        info = _bounds_info(n.get("bounds"))
        if not info:
            continue
        cx, cy, area = info
        if area <= 0:
            continue
        focused = n.get("focused") == "true"
        is_edit = "EditText" in cls or "TextInput" in cls
        if focused and is_edit:
            return (cx, cy)
        if focused and n.get("clickable") == "true":
            candidates.append((0, area, (cx, cy)))
        elif is_edit:
            candidates.append((1, area, (cx, cy)))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], -c[1]))
    return candidates[0][2]


# ---------------------------------------------------------------- 广播注入


def _quote_for_device(s):
    """把参数包成设备端 sh 的单引号字符串（adb shell 会在手机上再解析一次）。"""
    return "'" + s.replace("'", "'\\''") + "'"


def broadcast_text(text):
    """把文本广播给 ADBKeyboard（当前默认输入法需是 AdbIME 且已有输入连接）。"""
    cmd = f"am broadcast -a ADB_INPUT_TEXT --es msg {_quote_for_device(text)}"
    try:
        out = shell(cmd)
    except AdbError as e:
        raise CnInputError(f"广播输入失败：{e}") from e
    if "Broadcast completed" not in out and "Broadcasting" not in out:
        raise CnInputError(f"广播输出异常（可能没装 ADBKeyboard）：{out!r}")
    return out


def _restore_ime(original, attempts=2):
    """把默认输入法恢复成 original，并轮询校验；失败抛 CnInputError。"""
    if not original:
        return
    last = None
    for _ in range(attempts):
        try:
            shell(f"ime set {original}")
        except AdbError as e:  # 个别机型 ime set 返回非 0 但实际已切换，继续校验
            last = e
        time.sleep(0.3)
        if default_ime() == original:
            return
    raise CnInputError(
        f"恢复原输入法失败：期望 {original}，实际 {default_ime()}；手动执行 adb shell ime set {original}"
        + (f"（最后一次错误：{last}）" if last else "")
    )


# ---------------------------------------------------------------- 主入口


def input_cn(text, restore=True, field=None):
    """把 text 输入到手机当前焦点输入框（中文/英文/空格均可）。

    restore=True（默认）：临时把默认输入法切到 AdbIME，输入完切回原输入法并校验。
    restore=False：不切回（连续输入场景，结束前自己调用 restore_ime(原输入法)）。
    field=(x, y)：手动指定输入框坐标；None 时用 uiautomator 自动定位。

    返回 {text, ime_before, ime_after, restored, point}。
    """
    if not isinstance(text, str) or not text:
        raise ValueError("text 不能是空字符串")
    bad = [c for c in text if ord(c) < 32 and c not in "\n\t"]
    if bad:
        raise ValueError(f"text 含控制字符 {bad!r}")
    if field is not None:
        field = (int(field[0]), int(field[1]))

    ensure_enabled()
    before = default_ime()
    switched = before != ADBIME
    restored = True
    point = field
    try:
        if switched:
            if point is None:
                point = find_input_point()
            set_ime(ADBIME)
            # MIUI 切 IME 时会把软键盘收起，AdbIME 的 getCurrentInputConnection() 变 null；
            # 重新点一下输入框，让软键盘弹出来、onStartInput 建立连接，广播才落得进去。
            if not _keyboard_shown():
                if point is None:
                    raise CnInputError(
                        "已切到 AdbIME 但软键盘未弹出，且没找到输入框，无法建立输入连接：\n"
                        "  - 先手动点一下输入框再重试，或\n"
                        "  - input_cn(text, field=(x, y)) / CLI --field x,y 指定输入框坐标"
                    )
                tap(*point)
                if not _wait_keyboard(3.0):
                    raise CnInputError(
                        f"点按 {point} 后软键盘仍未弹出（AdbIME 输入连接没建立）；"
                        "确认坐标落在输入框上，或先手动点一下输入框"
                    )
        else:
            # 已经是 AdbIME：键盘没显示（例如上一轮 --no-restore 后）就再点一下输入框
            if not _keyboard_shown():
                point = point or find_input_point()
                if point is None:
                    raise CnInputError("当前是 AdbIME 但软键盘未显示，也没找到输入框；先点一下输入框再重试")
                tap(*point)
                _wait_keyboard(3.0)

        broadcast_text(text)
        time.sleep(0.2)
    finally:
        if switched and restore:
            try:
                _restore_ime(before)
            except CnInputError as e:
                restored = False
                print(f"[错误] {e}", file=sys.stderr)
    after = default_ime()
    return {
        "text": text,
        "ime_before": before,
        "ime_after": after,
        "restored": (not switched) or (restored and after == before),
        "point": point,
    }


def restore_ime(ime=None):
    """手动恢复默认输入法；不传则用环境变量 CN_INPUT_RESTORE_IME 或内置 baidu。"""
    ime = ime or os.environ.get("CN_INPUT_RESTORE_IME") or KNOWN_DEFAULT_IME
    _restore_ime(ime)
    return default_ime()


def status():
    """返回环境状态 dict，方便自检。"""
    info = {
        "device": None,
        "adbkeyboard_installed": False,
        "adbkeyboard_version": None,
        "adbime_enabled": False,
        "default_ime": None,
        "enabled_imes": [],
    }
    try:
        info["device"] = device()
    except AdbError as e:
        info["error"] = str(e)
        return info
    try:
        info["adbkeyboard_installed"] = installed()
        if info["adbkeyboard_installed"]:
            out = shell(f"dumpsys package {ADBKEYBOARD_PKG}")
            for line in out.splitlines():
                if "versionName=" in line:
                    info["adbkeyboard_version"] = line.split("versionName=", 1)[1].strip()
                    break
        info["enabled_imes"] = enabled_imes()
        info["adbime_enabled"] = ADBIME in info["enabled_imes"]
        info["default_ime"] = default_ime()
    except AdbError as e:
        info["error"] = str(e)
    return info


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _parse_field(value):
    try:
        x, y = value.replace("，", ",").split(",")
        return (int(x), int(y))
    except Exception:
        raise SystemExit(f"--field 需要形如 540,900 的坐标：{value!r}")


def main(argv):
    _utf8()
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    try:
        if args[0] == "--status":
            info = status()
            print("设备:", info.get("device"))
            print("ADBKeyboard 已安装:", "是" if info["adbkeyboard_installed"] else "否",
                  f"(v{info['adbkeyboard_version']})" if info["adbkeyboard_version"] else "")
            print("AdbIME 已启用:", "是" if info["adbime_enabled"] else "否")
            print("当前默认输入法:", info["default_ime"])
            print("已启用输入法:")
            for ime in info["enabled_imes"]:
                print("  ", ime)
            if info.get("error"):
                print("[错误]", info["error"])
                return 1
            return 0
        if args[0] == "--restore":
            ime = args[1] if len(args) > 1 else None
            print("已恢复默认输入法:", restore_ime(ime))
            return 0
        field = None
        restore = True
        rest = args
        while rest and rest[0].startswith("--"):
            if rest[0] == "--no-restore":
                restore = False
                rest = rest[1:]
            elif rest[0] == "--field":
                if len(rest) < 2:
                    raise SystemExit("--field 后面要跟 x,y")
                field = _parse_field(rest[1])
                rest = rest[2:]
            else:
                raise SystemExit(f"未知参数：{rest[0]}")
        text = " ".join(rest)
        if not text:
            print(__doc__)
            return 0
        info = input_cn(text, restore=restore, field=field)
        print(f"已输入：{info['text']}")
        print(f"输入框坐标：{info['point']}")
        print(f"输入法：{info['ime_before']} -> {info['ime_after']}"
              + ("（已恢复）" if info["restored"] else "（未恢复！）"))
        return 0 if info["restored"] or not restore else 1
    except (AdbError, CnInputError, ValueError) as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
