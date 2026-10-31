# -*- coding: utf-8 -*-
"""handoff.py —— 会话交接：把「记忆」落成文件，并生成新会话的启动消息。

配合 session_ctx.py 使用：上下文达到 80% 压缩预算时，
  1) python phone\\session_ctx.py --check          # 返回 10/20 说明该交接
  2) python phone\\handoff.py write --note "...一句话说清现在在做什么"
  3) 新会话里粘贴 python phone\\handoff.py seed 打印的启动消息（或直接发 MEMORY.md 路径）
  4) 新会话按提示 listen --claim，自动解除旧会话的监听器

产物：
  runs/memory/MEMORY.md   交接记忆（结构化，新会话必读）
  runs/memory/SEED.md     新会话启动消息（可整段粘贴）
  runs/memory/handoff-<时间戳>.md  历史快照

用法：
  python phone\\handoff.py write --note "在给用户做 v5.1 视频" [--json]
  python phone\\handoff.py seed
  python phone\\handoff.py show
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
MEM_DIR = RUNS / "memory"
TASKS = RUNS / "wechat_tasks" / "tasks.json"
OWNER = RUNS / "_wechat" / "owner.json"
LAST_EVT = RUNS / "_wechat" / "last.json"
EVT_DIR = RUNS / "wechat_events"
SKILL_NAME = "wechat-takeover"
USER_CONTACT = os.environ.get("WECHAT_CONTACT", "").strip() or "用户"
SKILL_MD = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))) / "skills" / SKILL_NAME / "SKILL.md"
REPO = "https://github.com/w1sdoM777/dsh-phone-wechat-takeover"


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _recent_events(n=3):
    if not EVT_DIR.is_dir():
        return []
    files = sorted(EVT_DIR.glob("evt-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    out = []
    for p in files:
        d = _load_json(p, {})
        msgs = d.get("messages") or []
        text = " / ".join(m.get("text", "")[:80] for m in msgs if m.get("side") == "in") or d.get("notifications", [{}])[0].get("text", "")
        out.append({"at": d.get("at", ""), "text": text, "file": p.name})
    return out


def _ctx_summary():
    try:
        out = subprocess.run(
            [sys.executable, str(ROOT / "phone" / "session_ctx.py"), "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if out.returncode == 0 and out.stdout.strip().startswith("{"):
            return json.loads(out.stdout)
    except Exception:
        pass
    return {}


def _listener_alive():
    try:
        out = subprocess.run(
            [sys.executable, str(ROOT / "phone" / "wechat_trigger.py"), "status"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        return out.stdout.strip()
    except Exception as e:
        return f"status 调用失败: {e}"


def build_memory(note: str = "", keep_done: int = 6) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ctx = _ctx_summary()
    tasks = _load_json(TASKS, []) or []
    pending = [t for t in tasks if t.get("status") == "pending"]
    done = [t for t in tasks if t.get("status") == "done"][-keep_done:]
    owner = _load_json(OWNER, {})
    last = _load_json(LAST_EVT, {})
    events = _recent_events(3)

    L = []
    L.append("# DSH 会话交接记忆（自动生成）")
    L.append("")
    L.append(f"> 生成时间：{now}")
    if ctx:
        L.append(f"> 源会话：`{ctx.get('sessionId')}`")
        L.append(f"> 上下文：估算 {ctx.get('usedTokens', 0):,} tokens ≈ 压缩预算 {ctx.get('pctBudget', 0) * 100:.1f}%"
                 f"（窗口 {ctx.get('contextWindow', 0):,}，预算 {ctx.get('budget', 0):,}）")
    L.append("> 新会话读到这里就等于接上记忆，不用再问用户之前发生了什么。")
    L.append("")
    L.append("## 0. 新会话怎么用这份记忆（必读）")
    L.append("")
    L.append(f"1. 读本文件（就是这里）+ `{SKILL_MD}`；")
    L.append("2. `python phone\\wechat_trigger.py status` 看布防/设备状态；")
    L.append("3. 微信停在前台就先 `adb shell input keyevent 3`；")
    L.append("4. `python phone\\wechat_trigger.py listen --max-hours 0 --claim`（后台任务）接管布防 —— "
             "`--claim` 会自动停掉旧会话的监听器，不会双监听；")
    L.append("5. 把「三、待办」处理完，每条微信消息只回一条结果（`wechat_trigger.py finish --id N --result \"...\"`）；")
    L.append("6. 每次任务结束跑 `python phone\\session_ctx.py --check`，到 80% 就再按本流程交接一次。")
    L.append("")
    L.append("## 1. 我在干什么")
    L.append("")
    L.append(f"- 服务对象：**{USER_CONTACT}**（微信），通过手机微信下发指令；结果也回微信。")
    L.append(f"- 技能：`{SKILL_NAME}`（手机微信 → 唤醒 agent → 手机操作/本机任务 → 微信回结果），空闲不扫 token。")
    L.append("- 工作目录：`D:\\手机agent`；开源仓库：" + REPO)
    if note:
        L.append("")
        L.append("## 2. 上一会话交班时正在做的事")
        L.append("")
        L.append(f"- {note}")
    L.append("")
    L.append("## 3. 待办任务（tasks.json 里 status=pending）")
    L.append("")
    if pending:
        for t in pending:
            L.append(f"- **#{t.get('id')}** [{t.get('created', '')}] {(t.get('text') or '').replace(chr(10), ' ')}")
    else:
        L.append("- （无待办）")
    L.append("")
    L.append(f"## 4. 最近完成的任务（最近 {len(done)} 条，给新会话补上下文用）")
    L.append("")
    if done:
        for t in done:
            res = (t.get("result") or "").replace("\n", " ")[:160]
            L.append(f"- #{t.get('id')} ✅ [{(t.get('text') or '').replace(chr(10), ' ')[:60]}] → {res}")
    else:
        L.append("- （无）")
    L.append("")
    L.append("## 5. 布防 / 设备状态快照")
    L.append("")
    L.append(f"- owner.json：`{json.dumps(owner, ensure_ascii=False)}`")
    if last:
        L.append(f"- 最近事件：{last.get('at', '')} {last.get('event', '')}")
    for e in events:
        L.append(f"- 事件 {e['at']}: {e['text'][:100]}（{e['file']}）")
    L.append("")
    L.append("### status 原文")
    L.append("")
    L.append("```text")
    L.append(_listener_alive()[:3000])
    L.append("```")
    L.append("")
    L.append("## 6. 关键路径与常用命令")
    L.append("")
    L.append("| 用途 | 路径 / 命令 |")
    L.append("|---|---|")
    L.append("| 手机侧脚本 | `D:\\手机agent\\phone\\` (adb.py / wechat.py / wechat_task.py / wechat_trigger.py / cn_input.py / ocr.py) |")
    L.append("| 运行数据 | `D:\\手机agent\\runs\\`（wechat_events/、wechat_tasks/tasks.json、_wechat/owner.json、memory/） |")
    L.append("| 布防 | `python phone\\wechat_trigger.py listen --max-hours 0 --claim` |")
    L.append("| 收任务 | `python phone\\wechat_trigger.py inbox` |")
    L.append("| 回结果 | `python phone\\wechat_trigger.py finish --id <id> --result \"...\"` |")
    L.append("| 状态 | `python phone\\wechat_trigger.py status` |")
    L.append("| 上下文用量 | `python phone\\session_ctx.py [--check]` |")
    L.append("| 交接记忆 | `python phone\\handoff.py write --note \"...\"` |")
    L.append("| 自媒体项目 | `D:\\自媒体\\`（成片 out\\被偷走的24小时.mp4，工程 work\\edit\\） |")
    L.append("| 交付物 | `D:\\手机agent\\deliverables\\`（含本技能的 GitHub 克隆 dsh-phone-wechat-takeover） |")
    L.append("")
    L.append("## 7. 用户习惯（别踩坑）")
    L.append("")
    L.append("- 不要回执轰炸：每条微信消息只回一条结果；没消息不主动说话。")
    L.append("- 含糊的需求先问清楚再动手；不要出错人（视频/图片里的人物对应关系要按用户确认的来）。")
    L.append("- 大文件（视频/PDF）用微信文件发送，`adb push` 到 `/sdcard/Download/` 再走「+ → 文件 → 手机文件」。")
    L.append("- 手机坐标是 1080×2400；微信输入框 `(430, 2255)`；文件选择器是横向 2400×1080，坐标要 ×1.2245。")
    L.append("- 改动技能代码后同步到仓库：`D:\\手机agent\\deliverables\\dsh-phone-wechat-takeover` 然后 git commit/push。")
    L.append("")
    return "\n".join(L) + "\n"


def build_seed(note: str = "") -> str:
    note_line = f"\n（上一会话在做的：{note}）" if note else ""
    return (
        "【上下文交接】上一个会话上下文快到 80% 预算，已把记忆写到 "
        "`D:\\手机agent\\runs\\memory\\MEMORY.md`。\n"
        "请按顺序执行：\n"
        f"1) 先读 `{(ROOT / 'runs' / 'memory' / 'MEMORY.md')}`（就是上一会话的记忆）和 "
        f"`{SKILL_MD}`；\n"
        "2) `cd D:\\手机agent; python phone\\wechat_trigger.py status` 看布防状态；\n"
        "3) 微信在前台就先 `adb shell input keyevent 3`；\n"
        "4) `python phone\\wechat_trigger.py listen --max-hours 0 --claim` 后台接管布防"
        "（会自动停掉旧会话的监听器）；\n"
        "5) 处理 MEMORY.md 里的待办任务，每条微信消息只回一条结果。"
        f"{note_line}\n"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="会话交接：写记忆 + 生成新会话启动消息")
    ap.add_argument("action", choices=["write", "seed", "show", "clear"], help="write=写记忆, seed=打印启动消息, show=看记忆, clear=清掉旧记忆")
    ap.add_argument("--note", default="", help="一句话说明上一会话在做什么")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    MEM_DIR.mkdir(parents=True, exist_ok=True)
    mem = MEM_DIR / "MEMORY.md"
    seed = MEM_DIR / "SEED.md"

    if args.action == "clear":
        for p in (mem, seed):
            if p.exists():
                p.unlink()
        print("已清空交接记忆")
        return 0
    if args.action == "show":
        print(mem.read_text(encoding="utf-8") if mem.exists() else "（还没有交接记忆，先 write）")
        return 0
    if args.action == "seed":
        print(seed.read_text(encoding="utf-8") if seed.exists() else build_seed(args.note))
        return 0

    text = build_memory(args.note)
    mem.write_text(text, encoding="utf-8")
    seed_text = build_seed(args.note)
    seed.write_text(seed_text, encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (MEM_DIR / f"handoff-{stamp}.md").write_text(text, encoding="utf-8")
    info = {"memory": str(mem), "seed": str(seed), "bytes": len(text.encode('utf-8'))}
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=1))
    else:
        print(f"✅ 交接记忆已写入：{mem}（{info['bytes']} 字节）")
        print(f"✅ 新会话启动消息：{seed}")
        print("")
        print("下一步：打开 DSH 新会话，把 SEED.md 的内容整段发过去即可（新会话会自己读记忆并 --claim 接管）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
