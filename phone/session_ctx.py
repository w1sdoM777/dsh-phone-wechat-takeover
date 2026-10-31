# -*- coding: utf-8 -*-
"""session_ctx.py —— 估算当前 DSH 会话的上下文用量，给「80% 自动交接」用。

原理（本机实测标定，2026-10-31）：
  * DSH 把会话事件写在 $DSH_HOME/sessions/<escaped-cwd>/<session-id>/session.v4.jsonl.zstd
    （zstd 压缩的 JSONL，Python 3.14+ 用 stdlib `compression.zstd` 就能解）。
  * 会话日志里的 `request/context` 记录带 `contextWindow`（本机 deepseek-flash = 1,048,576）。
  * DSH 的自动压缩（compaction）在上下文到 ~53.3 万 token 时触发；
    所以「压缩预算」= 最近一次 `compaction/summary` 的 `usage.inputTokens`，
    没有压缩记录时用 0.508 × contextWindow 兜底。
  * 用量估算：只统计真正进入模型上下文的文本（系统提示、工具清单、用户/助手消息、
    工具调用与结果），工具结果按 DSH 的 pruner 规则（>8192 字符截到 4096+1024）截断；
    字符→token 系数用压缩前后的真实 usage 标定为 ~2.9 字符/token。
    估算误差约 ±10~15%，对「80% 触发交接」这种策略性判断足够。

用法：
  python phone\\session_ctx.py                # 人读摘要
  python phone\\session_ctx.py --json         # 机器可读
  python phone\\session_ctx.py --check        # 退出码：0 正常 / 10 达到交接阈值 / 20 已压缩过
  python phone\\session_ctx.py --check --threshold 0.8
  python phone\\session_ctx.py --budget 600000   # 手动指定压缩预算
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

CHARS_PER_TOKEN = 2.9          # 标定值：字符 / token
DEFAULT_WINDOW = 1048576       # deepseek-flash 本机实测
DEFAULT_COMPACT_RATIO = 0.508  # 未压缩过时的预算比例（533042/1048576）
PRUNE_THRESHOLD, PRUNE_HEAD, PRUNE_TAIL = 8192, 4096, 1024


# ---------------------------------------------------------------- 定位会话
def dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))


def current_session_id() -> str | None:
    sid = os.environ.get("DSH_SESSION_ID")
    if sid:
        return sid.strip()
    # 退路：从 wechat owner.json 读
    try:
        owner = json.loads((Path.cwd() / "runs" / "_wechat" / "owner.json").read_text(encoding="utf-8"))
        return owner.get("session")
    except Exception:
        return None


def find_session_file(session_id: str) -> Path | None:
    root = dsh_home() / "sessions"
    if not root.is_dir():
        return None
    # 目录名可能是 session-<uuid>，也可能直接是 <uuid>
    for pat in (f"**/{session_id}/session.v4.jsonl.zstd", f"**/session-{session_id}/session.v4.jsonl.zstd"):
        hits = glob.glob(str(root / pat), recursive=True)
        if hits:
            return Path(sorted(hits, key=os.path.getmtime)[-1])
    # 模糊匹配
    for p in glob.glob(str(root / "**" / "session.v4.jsonl.zstd"), recursive=True):
        if session_id in p:
            return Path(p)
    return None


def read_records(path: Path) -> list[dict]:
    from compression import zstd  # Python 3.14 stdlib

    raw = zstd.decompress(path.read_bytes()).decode("utf-8", errors="replace")
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 计数
def _len(obj) -> int:
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, (dict, list)):
        return len(json.dumps(obj, ensure_ascii=False))
    return 0


def _blocks_chars(blocks, acc: list[int]) -> None:
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("text", "reasoning"):
            acc.append(len(b.get("text", "")))
        elif t == "tool-call":
            acc.append(len(b.get("name", "")) + len(b.get("arguments", "")))
        elif t == "tool-result":
            n = _len(b.get("content"))
            acc.append(min(n, PRUNE_HEAD + PRUNE_TAIL + 40) if n > PRUNE_THRESHOLD else n)
        elif t == "image":
            acc.append(1000)


def measure(recs: list[dict], manual_budget: int | None = None) -> dict:
    window = None
    tools_chars = 0
    system_chars = 0
    last_compact_end = -1
    compact_budget = None
    n_compactions = 0

    for i, d in enumerate(recs):
        t = d.get("type")
        data = d.get("data") or {}
        if t == "request/context":
            window = data.get("contextWindow") or window
        elif t == "request/header" and not tools_chars:
            h = data.get("header") or {}
            tools_chars = _len(h.get("tools", []))
            system_chars = len(h.get("system", "")) if isinstance(h.get("system"), str) else 0
        elif t == "system/message":
            msg = data.get("message") or {}
            system_chars = max(system_chars, _len(msg))
        elif t == "compaction/summary":
            n_compactions += 1
            usage = data.get("usage") or {}
            if usage.get("inputTokens"):
                compact_budget = int(usage["inputTokens"])
        elif t == "compaction/end":
            last_compact_end = i

    window = window or DEFAULT_WINDOW
    budget = manual_budget or compact_budget or int(window * DEFAULT_COMPACT_RATIO)

    chars = system_chars + tools_chars
    per = {"system": system_chars, "tools": tools_chars}
    summary_chars = 0
    if last_compact_end >= 0:
        # 压缩后：压缩摘要 + 压缩点之后的所有上下文
        for d in recs[:last_compact_end + 1]:
            if d.get("type") == "compaction/summary":
                summary_chars = sum(len(b.get("text", "")) for b in (d.get("data", {}).get("summary") or [])
                                    if isinstance(b, dict))
        for d in recs[last_compact_end + 1:]:
            chars += _context_chars(d, per)
        chars += summary_chars
        per["summary"] = summary_chars
    else:
        for d in recs:
            chars += _context_chars(d, per)

    tokens = int(chars / CHARS_PER_TOKEN)
    pct_window = tokens / window
    pct_budget = tokens / budget
    return {
        "sessionId": None,
        "contextWindow": window,
        "budget": budget,
        "budgetSource": "manual" if manual_budget else ("compaction" if compact_budget else "default-ratio"),
        "compactions": n_compactions,
        "chars": chars,
        "charsPerToken": CHARS_PER_TOKEN,
        "usedTokens": tokens,
        "pctWindow": round(pct_window, 4),
        "pctBudget": round(pct_budget, 4),
    }


def _context_chars(d: dict, per: dict) -> int:
    t = d.get("type")
    data = d.get("data") or {}
    acc: list[int] = []
    if t in ("user/message", "developer/message"):
        msg = data.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            acc.append(len(c))
        else:
            _blocks_chars(c, acc)
    elif t == "assistant/message":
        _blocks_chars((data.get("message") or {}).get("content"), acc)
    elif t == "tool/call":
        acc.append(len(data.get("arguments", "")))
    elif t == "tool/result":
        n = _len(data.get("content"))
        acc.append(min(n, PRUNE_HEAD + PRUNE_TAIL + 40) if n > PRUNE_THRESHOLD else n)
    n = sum(acc)
    if n:
        per[t] = per.get(t, 0) + n
    return n


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="估算当前 DSH 会话上下文用量")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--check", action="store_true", help="策略检查：达到阈值返回 10，已压缩过返回 20")
    ap.add_argument("--threshold", type=float, default=0.8, help="交接阈值（默认 0.8 = 80%% 压缩预算）")
    ap.add_argument("--budget", type=int, default=None, help="手动指定压缩预算（token）")
    ap.add_argument("--session", default=None, help="指定会话 id（默认取 $DSH_SESSION_ID）")
    args = ap.parse_args(argv)

    sid = args.session or current_session_id()
    if not sid:
        print("找不到当前会话 id（未设置 DSH_SESSION_ID）", file=sys.stderr)
        return 2
    path = find_session_file(sid)
    if not path:
        print(f"找不到会话日志：{sid}", file=sys.stderr)
        return 2

    recs = read_records(path)
    info = measure(recs, args.budget)
    info["sessionId"] = sid
    info["sessionFile"] = str(path)
    info["threshold"] = args.threshold

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=1))
    else:
        print(f"会话      : {sid}")
        print(f"日志      : {path}  ({path.stat().st_size / 1e6:.1f} MB)")
        print(f"窗口      : {info['contextWindow']:,} tokens")
        print(f"压缩预算  : {info['budget']:,} tokens（来源 {info['budgetSource']}，本会话已压缩 {info['compactions']} 次）")
        print(f"估算用量  : {info['usedTokens']:,} tokens ≈ 窗口 {info['pctWindow'] * 100:.1f}% / 预算 {info['pctBudget'] * 100:.1f}%")
        print(f"交接阈值  : 预算的 {args.threshold * 100:.0f}%  ≈ {int(info['budget'] * args.threshold):,} tokens")

    if args.check:
        if info["pctBudget"] >= 1.0:
            print("VERDICT: COMPACTED（已达压缩预算，建议立即交接）")
            return 20
        if info["pctBudget"] >= args.threshold:
            print("VERDICT: HANDOFF（达到交接阈值，建议写记忆并换新会话）")
            return 10
        if not args.json:
            print("VERDICT: OK（未到阈值，继续在当前会话工作）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
