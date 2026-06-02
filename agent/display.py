"""让'思考'可见（PRD §9）——认知向的命门。

大脑想了什么，屏幕上必须看得见。这里只负责把 Decision / 合并 / 冲突 / 执行状态
按 PRD §9 的格式打印出来。纯 ANSI，不依赖任何第三方库。
"""
from __future__ import annotations

import os
import sys

from .models import Decision, Disposition, Cost, Conflict, Edit, Slot, Plan, Status

# ---------------------------------------------------------------------------
# ANSI 颜色（可用 NO_COLOR=1 关闭）
# ---------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str) -> str:
    return code if _USE_COLOR else ""


RESET = _c("\033[0m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
BLUE = _c("\033[34m")
MAGENTA = _c("\033[35m")
CYAN = _c("\033[36m")
GREY = _c("\033[90m")


def colorize(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


# ---------------------------------------------------------------------------
# 置信度 / 处置 的呈现
# ---------------------------------------------------------------------------
def conf_label(conf: float) -> str:
    if conf >= 0.7:
        return "高"
    if conf >= 0.4:
        return "中"
    return "低"


def conf_color(conf: float) -> str:
    if conf >= 0.7:
        return GREEN
    if conf >= 0.4:
        return YELLOW
    return RED


_DISPO_BADGE = {
    Disposition.AUTO: ("✅ 直接定", GREEN),
    Disposition.SUGGEST: ("💡 给建议", YELLOW),
    Disposition.ASK: ("❓ 停下来问", RED),
}

_COST_CN = {Cost.LOW: "低", Cost.MID: "中", Cost.HIGH: "高"}


def dispo_badge(d: Disposition) -> str:
    text, color = _DISPO_BADGE[d]
    return colorize(text, color)


# ---------------------------------------------------------------------------
# 通用排版
# ---------------------------------------------------------------------------
def header(title: str) -> None:
    bar = "═" * (len(title) + 2)
    print(f"\n{CYAN}{BOLD}╔{bar}╗{RESET}")
    print(f"{CYAN}{BOLD}║ {title} ║{RESET}")
    print(f"{CYAN}{BOLD}╚{bar}╝{RESET}")


def section(title: str) -> None:
    print(f"\n{MAGENTA}{BOLD}—— {title} ——{RESET}")


def info(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


def line(text: str = "") -> None:
    print(text)


# ---------------------------------------------------------------------------
# 决定的可见化（PRD §9 核心格式）
# ---------------------------------------------------------------------------
def show_decision(d: Decision, indent: str = "") -> None:
    cc = conf_color(d.confidence)
    print(f"{indent}{BOLD}🤖 决定：{d.description}{RESET}")

    if d.options:
        cand = " / ".join(_fmt_option_inline(o) for o in d.options)
        print(f"{indent}   候选：{cand}")
    if d.chosen is not None:
        print(f"{indent}   选定：{BOLD}{d.chosen.label}{RESET}")

    print(f"{indent}   置信度：{cc}{d.confidence:.2f}（{conf_label(d.confidence)}）{RESET}"
          f"｜依据：{d.confidence_basis}")
    print(f"{indent}   代价：{_COST_CN[d.cost]}｜理由：{d.reasoning}")

    resolved = d.status in (Status.CONFIRMED, Status.DONE, Status.EXECUTING)
    if resolved:
        # 已被 A 回答/确认的决定：显示'已确认'，不再重复追问（避免自相矛盾）
        print(f"{indent}   处置：{colorize('✅ 已确认（A 已拍板）', GREEN)}"
              f"  {GREY}(原处置：{_DISPO_BADGE[d.disposition][0]}){RESET}")
    else:
        print(f"{indent}   处置：{dispo_badge(d.disposition)}")
        if d.disposition == Disposition.ASK:
            print(f"{indent}   {RED}❓ 请确认：{d.reasoning}{RESET}")
    if d.confidence_history:
        for h in d.confidence_history:
            print(f"{indent}   {GREY}↻ 置信度更新：{h}{RESET}")


def _fmt_option_inline(o) -> str:
    bits = []
    if "per_capita" in o.data:
        bits.append(f"人均{int(o.data['per_capita'])}")
    if o.data.get("child_friendly"):
        bits.append("有儿童餐" if o.kind == "restaurant" else "亲子友好")
    if o.data.get("has_low_cal"):
        bits.append("有沙拉")
    if o.kind == "activity" and o.data.get("near_bar_street"):
        bits.append("近酒吧街")
    if o.kind == "activity" and o.data.get("tiring"):
        bits.append({"low": "强度低", "mid": "强度中", "high": "强度高"}.get(o.data["tiring"], ""))
    if o.data.get("distance_km") is not None:
        bits.append(f"{o.data['distance_km']}km")
    if o.data.get("allergens"):
        bits.append("含" + "/".join(o.data["allergens"]))
    bits = [b for b in bits if b]
    suffix = f"({','.join(bits)})" if bits else ""
    return f"{o.label}{suffix}"


def show_timeline(timeline: list[Slot]) -> None:
    section("行程时间线")
    for s in timeline:
        print(f"  {BLUE}{s.start}–{s.end}{RESET}  {s.title}")
        if s.reason:
            print(f"            {GREY}↳ {s.reason}{RESET}")


# ---------------------------------------------------------------------------
# 协同评审的可见化
# ---------------------------------------------------------------------------
def show_incoming_edit(e: Edit) -> None:
    who = colorize(e.author.id, CYAN)
    if e.constraint:
        print(f"   · {who}：新增约束「{e.constraint}」"
              + (f"（{e.note}）" if e.note else ""))
    elif e.after is not None and e.before is not None:
        print(f"   · {who}：把{_target_cn(e.target_decision)} {e.before.label} → "
              f"{BOLD}{e.after.label}{RESET}"
              + (f"（附言：{e.note}）" if e.note else ""))
    elif e.after is not None:
        print(f"   · {who}：{_target_cn(e.target_decision)} → {BOLD}{e.after.label}{RESET}"
              + (f"（附言：{e.note}）" if e.note else ""))
    else:
        print(f"   · {who}：移除 {_target_cn(e.target_decision)}"
              + (f"（{e.note}）" if e.note else ""))


def show_auto_merged(e: Edit) -> None:
    if e.constraint:
        # 诚实呈现：真被引擎采纳才说"已纳入"，否则只说"已记录"（PRD §9：屏幕必须真实）
        if e.actionable:
            effect = e.merged_note or "已纳入约束"
            print(f"{GREEN}✅ 自动合并（无争议）：{e.author.id} 的「{e.constraint}」{effect}{RESET}")
        else:
            effect = e.merged_note or "已记录（当前数据无法据此自动筛店）"
            print(f"{CYAN}📝 已记录（无需打扰）：{e.author.id} 的「{e.constraint}」—— {effect}{RESET}")
    else:
        tgt = _target_cn(e.target_decision)
        to = e.after.label if e.after else "（移除）"
        print(f"{GREEN}✅ 自动合并（无争议）：{e.author.id} 把{tgt}改为 {to}{RESET}")


def show_conflict(c: Conflict, idx: int) -> None:
    tgt = _target_cn(c.target_decision)
    competitors = " / ".join(
        f"{e.author.id}→{e.after.label if e.after else '?'}" for e in c.competing_edits)
    print(f"{YELLOW}⚠️ 冲突待裁决[{idx}]：{tgt}（{competitors}）{RESET}")
    if c.agent_suggestion is not None:
        print(f"   {BOLD}🤖 建议：采纳 {c.suggestion_owner()}的 {c.agent_suggestion.label}{RESET}")
        for ln in c.suggestion_reason.split("\n"):
            print(f"      理由：{ln}")


def show_resolution(c: Conflict) -> None:
    if c.resolution is None:
        return
    who = c.resolution.author.id
    what = c.resolution.after.label if c.resolution.after else "(自定)"
    print(f"   {GREEN}✓ A 裁决：采纳 {who} 的 {what}{RESET}")


def _target_cn(target: str) -> str:
    return {
        "choose_restaurant": "餐厅",
        "choose_activity": "活动",
        "send_gift": "礼物",
        "set_budget": "预算档位",
        "set_return_time": "到家时间",
        "nap_window": "午睡口子",
    }.get(target, target)
