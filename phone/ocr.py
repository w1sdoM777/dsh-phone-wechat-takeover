# -*- coding: utf-8 -*-
r"""Windows 自带 OCR（免费的本地中文识别），把手机截图变成文字。

  python phone/ocr.py runs/xxx.png          # 打印识别结果（按 y 排序）
  python phone/ocr.py runs/xxx.png --json   # 输出 JSON

代码里：

  from ocr import ocr_image
  lines = ocr_image(r"runs/xxx.png")   # [{text,x,y,w,h}] 已按阅读顺序排序

原理：调 Windows PowerShell 5.1 的 Windows.Media.Ocr（系统已带 zh-Hans 识别引擎），
用 OcrLine 的文字 + BoundingRect 还原行位置。零 token 成本。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS1 = ROOT / "tools" / "ocr.ps1"


class OcrError(RuntimeError):
    pass


def _strip_cjk_spaces(text: str) -> str:
    """把 Windows OCR 在中文字之间插入的空格去掉。"""
    if not text:
        return ""
    # 中文字符/中文标点之间的空格去掉
    cjk = r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]"
    prev = None
    while prev != text:
        prev = text
        text = re.sub(rf"(?<={cjk}) (?={cjk})", "", text)
        text = re.sub(rf"(?<={cjk}) (?=[0-9A-Za-z])", "", text)
        text = re.sub(rf"(?<=[0-9A-Za-z]) (?={cjk})", "", text)
    return text


def ocr_image(path: str, timeout: int = 90):
    path = str(Path(path).resolve())
    out = Path(tempfile.gettempdir()) / "_ocr_result.json"
    if out.exists():
        out.unlink()
    cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(PS1), "-ImagePath", path, "-OutPath", str(out),
    ]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if not out.exists():
        err = (p.stderr or b"").decode("utf-8", "replace")[:500]
        raise OcrError(f"OCR 失败：{err or p.stdout.decode('utf-8','replace')[:300]}")
    rows = json.loads(out.read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = [rows]
    for r in rows:
        r["text"] = _strip_cjk_spaces(r.get("text", ""))
    rows.sort(key=lambda r: (r.get("y", 0), r.get("x", 0)))
    return rows


def main(argv):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    rows = ocr_image(path)
    if "--json" in argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            print(f"({r['x']:>4},{r['y']:>4}) {r['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
