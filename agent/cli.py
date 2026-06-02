"""交互式 CLI（PRD §7.3：用'视角切换'在单机命令行模拟多人评审）。

用法：
    python -m agent.cli                 # 家庭场景，真人交互
    python -m agent.cli --scenario friends

协同评审环节进入一个小 REPL，用 `as 老婆` 切到某人视角去改方案、`add ...` 加约束。
改动是真的在改 Plan 数据、author 是真的记录——只有'分享到另一台设备'用视角切换代替。
绝不搭真实多端 / 网络同步（PRD §2 / §7.3）。
"""
from __future__ import annotations

import argparse
from typing import Optional

from . import display as D
from .models import Decision, Edit, EditType, Option, Plan
from .orchestrator import Responder, orchestrate
from .planner import _opt_from_restaurant
from .scenarios import Scenario, get_scenario
from .tools import RESTAURANTS


def _ask(prompt: str, default: str = "") -> str:
    try:
        s = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return s or default


class InteractiveResponder(Responder):
    def answer_open_question(self, d: Decision) -> str:
        return _ask(f"   👤 你的回答（直接回车=用默认）> ", default="按你的默认建议来")

    def choose_conflict(self, conflict) -> Optional[Edit]:
        print(f"   请 A 裁决：[1] 采纳建议（{conflict.suggestion_owner()}的"
              f" {conflict.agent_suggestion.label if conflict.agent_suggestion else '?'}）", end="")
        opts = {"1": conflict.suggested_edit}
        n = 2
        for e in conflict.competing_edits:
            if e is conflict.suggested_edit:
                continue
            label = e.after.label if e.after else "?"
            print(f"  [{n}] 选 {e.author.id} 的 {label}", end="")
            opts[str(n)] = e
            n += 1
        print()
        choice = _ask("   > 选择编号（回车=采纳建议）> ", default="1")
        return opts.get(choice, conflict.suggested_edit)

    def confirm_suggestion(self, d: Decision) -> bool:
        ans = _ask("   👤 采纳这条建议吗？[Y/n] > ", default="y")
        return ans.lower() != "n"

    def confirm_execute(self, plan: Plan) -> bool:
        ans = _ask("\n   👤 A 最终确认，开始执行吗？[Y/n] > ", default="y")
        return ans.lower() != "n"


# ---------------------------------------------------------------------------
# 视角切换评审 REPL —— §7.3 的字面实现
# ---------------------------------------------------------------------------
def interactive_edits(plan: Plan, scenario: Scenario) -> list[Edit]:
    parts = {p.id: p for p in scenario.participants}
    rest_dec = plan.find_by_type("choose_restaurant")
    edits: list[Edit] = []
    current = scenario.participants[0]

    print(f"\n{D.BOLD}—— 视角切换评审（单机模拟多人）——{D.RESET}")
    print(f"{D.DIM}命令：as <人名> 切视角 | rest <编号|rest_id> 换餐厅 | add <约束> 加需求"
          f" | list 看候选 | done 结束 | 直接 done = 用预设改动演示{D.RESET}")
    print(f"局里的人：{', '.join(f'{p.id}(w={p.weight})' for p in scenario.participants)}")

    def show_rest_options():
        print(f"{D.DIM}可选餐厅：{D.RESET}")
        for i, o in enumerate(rest_dec.options, 1):
            print(f"   [{i}] {o.get('id')} {o.label}（人均{int(o.get('per_capita',0))}, "
                  f"{o.get('distance_km')}km, "
                  f"{'有儿童餐' if o.get('child_friendly') else '无儿童餐'}, "
                  f"{'有低卡' if o.get('has_low_cal') else '无低卡'}）")

    show_rest_options()
    while True:
        cmd = _ask(f"\n[{D.CYAN}{current.id}{D.RESET} 视角] > ", default="done")
        if cmd in ("done", ""):
            break
        parts_cmd = cmd.split(maxsplit=1)
        op = parts_cmd[0].lower()
        arg = parts_cmd[1] if len(parts_cmd) > 1 else ""

        if op == "as":
            if arg in parts:
                current = parts[arg]
                print(f"   已切到 {arg} 的视角")
            else:
                print(f"   没有这个人。可选：{', '.join(parts)}")
        elif op in ("rest", "restaurant"):
            o = _resolve_restaurant(arg, rest_dec.options)
            if o is None:
                print("   没找到这家店，输入 list 看候选")
                continue
            note = _ask("   附言（可空）> ", default="")
            edits.append(Edit(author=current, target_decision="choose_restaurant",
                              type=EditType.REPLACE, before=rest_dec.chosen, after=o, note=note))
            print(f"   {D.GREEN}已记录：{current.id} 把餐厅改为 {o.label}{D.RESET}")
        elif op == "add":
            if not arg:
                print("   用法：add 我对海鲜过敏")
                continue
            edits.append(Edit(author=current, target_decision="constraint",
                              type=EditType.ADD, constraint=arg))
            print(f"   {D.GREEN}已记录：{current.id} 新增约束「{arg}」{D.RESET}")
        elif op == "list":
            show_rest_options()
            if edits:
                print(f"{D.DIM}已提交改动：{D.RESET}")
                for e in edits:
                    tgt = e.constraint or (e.after.label if e.after else "?")
                    print(f"   · {e.author.id} → {tgt}")
        else:
            print("   命令：as / rest / add / list / done")

    return edits


def _resolve_restaurant(arg: str, options: list[Option]) -> Optional[Option]:
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(options):
            return options[idx]
    for o in options:
        if o.get("id") == arg or arg in o.label:
            return o
    # 也允许选不在当前候选里的店（从全量 DB）
    for r in RESTAURANTS:
        if r["id"] == arg or arg in r["name"]:
            return _opt_from_restaurant(r)
    return None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="活动规划执行 Agent · 交互式")
    ap.add_argument("--scenario", default="family", choices=["family", "friends"])
    ap.add_argument("--slow", action="store_true")
    args = ap.parse_args(argv)
    scenario = get_scenario(args.scenario)
    orchestrate(scenario, InteractiveResponder(), fast=not args.slow,
                edits_provider=interactive_edits)


if __name__ == "__main__":
    main()
