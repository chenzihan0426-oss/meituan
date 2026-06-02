"""端到端编排（PRD §6 流程总览）。

一句话目标 → 解析意图(已知/未知) → 算 置信度×代价 决定 做/建议/问 → 内部冲突协调
→ 第一版 Plan → 回答 ask 类问题 → 协同评审回合(多人改+合并+冲突建议+A 裁决)
→ 新版 Plan → A 最终确认 → 执行 + 异常兜底 → send_plan。

同一套流程被 demo.py（自动应答）与 cli.py（真人应答）复用，差别只在 Responder。
"""
from __future__ import annotations

import re
from typing import Optional

from . import display as D
from .execution import Executor
from .intent import parse_goal
from .models import (Decision, Disposition, Edit, Option, Plan, Status)
from .planner import build_first_plan
from .review import finalize_review, resolve_conflict, run_review_round
from .scenarios import Scenario
from .tools import ToolBox


# ---------------------------------------------------------------------------
# Responder：把'需要 A 拍板'的地方抽象出来（自动 / 交互两种实现）
# ---------------------------------------------------------------------------
class Responder:
    def answer_open_question(self, d: Decision) -> str:          # ask 类决定
        raise NotImplementedError

    def choose_conflict(self, conflict) -> Optional[Edit]:        # 冲突裁决
        raise NotImplementedError

    def confirm_execute(self, plan: Plan) -> bool:
        raise NotImplementedError

    def confirm_suggestion(self, d: Decision) -> bool:           # suggest 类决定是否采纳
        return True


class AutoResponder(Responder):
    """按 Scenario 预设自动应答，让 Demo 无人值守跑通。"""
    def __init__(self, scenario: Scenario):
        self.s = scenario

    def answer_open_question(self, d: Decision) -> str:
        ans = self.s.ask_answers.get(d.type, "按你的默认建议来")
        print(f"   {D.CYAN}👤 A 回答：{ans}{D.RESET}")
        return ans

    def choose_conflict(self, conflict) -> Optional[Edit]:
        choice = self.s.conflict_choices.get(conflict.target_decision, "suggestion")
        if choice == "suggestion":
            print(f"   {D.CYAN}👤 A 裁决：[1] 采纳建议{D.RESET}")
            return conflict.suggested_edit
        for e in conflict.competing_edits:
            if e.author.id == choice:
                print(f"   {D.CYAN}👤 A 裁决：选 {choice} 的方案{D.RESET}")
                return e
        return conflict.suggested_edit

    def confirm_execute(self, plan: Plan) -> bool:
        print(f"\n   {D.CYAN}👤 A 最终确认：开始执行 ✅{D.RESET}")
        return True

    def confirm_suggestion(self, d: Decision) -> bool:
        print(f"   {D.CYAN}👤 A：采纳这条建议 ✅{D.RESET}")
        return True


# ---------------------------------------------------------------------------
def orchestrate(scenario: Scenario, responder: Responder,
                fast: bool = True, tb: Optional[ToolBox] = None,
                edits_provider=None, card_path: Optional[str] = None,
                use_llm: bool = False, user_location: Optional[str] = None) -> dict:
    # use_llm=True 时，查餐厅/活动走真实高德 POI（带磁盘缓存→仍可复现/离线复演），
    # 任何失败由 ToolBox 内部回退本地库（PRD §8）。意图解析仍走本地确定性逻辑，
    # 保证'已知/未知'清单与脚本化评审稳定。
    # user_location='lng,lat'(GCJ-02) 时按用户真实位置搜附近，否则回退配置里的'家'。
    tb = tb or ToolBox(seed=7, fast=fast, forced=dict(scenario.forced), use_llm=use_llm)

    D.header(f"活动规划 Agent · 场景：{scenario.name}")
    print(f"{D.BOLD}🗣  目标：{D.RESET}{scenario.goal}")

    # === [4.1] 解析意图 + 列出已知/未知 ===
    intent = parse_goal(scenario.goal)
    if user_location:   # 把用户真实位置塞进约束，build_first_plan 会带进 tb.context 供高德搜附近
        intent.constraints["user_location"] = user_location
    constraints = dict(intent.constraints)
    D.section("意图解析：先承认'用户要什么'本身不确定")
    print(f"{D.GREEN}✓ 已知约束：{D.RESET}")
    for k in intent.known:
        print(f"   · {k}")
    print(f"{D.YELLOW}? 未知（显式列出，绝不默默填默认值）：{D.RESET}")
    for u in intent.unknown:
        print(f"   · {u}")

    # === [4.2 + 4.3] 第一版 Plan：每个决定算置信度×代价，含内部冲突协调 ===
    plan = build_first_plan(intent, tb, party_size=scenario.party_size)
    _show_plan(plan, "第一版方案 v1（每个决定都带置信度与理由）")

    # === A 回答 ask 类问题（拦在执行前的 open_questions）===
    if plan.open_questions:
        D.section("Agent 停下来问 A（低置信 / 高代价的决定，不敢替你定）")
        for d in list(plan.open_questions):
            D.show_decision(d, indent="")
            ans = responder.answer_open_question(d)
            _apply_open_answer(d, ans, constraints)
        plan.recompute_open_questions()

    # === A 对 suggest 类决定拍板（情感辅线在这里落地）===
    _confirm_suggestions(plan, responder)

    # === [7] 协同评审回合（升维亮点）===
    rr_plan, merged_constraints = _collaborative_review(
        plan, scenario, constraints, responder, edits_provider)

    # === A 最终确认 → 执行 ===
    if not responder.confirm_execute(rr_plan):
        print("已取消执行。")
        return {"cancelled": True}

    # 执行用'评审后合并的约束'（含评审新增的过敏原），与最终方案保持一致
    execu = Executor(rr_plan, tb, merged_constraints,
                     party_size=scenario.party_size, send_to=scenario.send_to)
    try:
        result = execu.run()
    except Exception as ex:  # 安全网：任何未预料的工具异常都优雅收场，不在 Demo 现场崩栈
        print(f"\n{D.RED}⚠️ 执行中遇到未预料的问题：{ex}{D.RESET}")
        print(f"{D.GREY}已停在安全点；已落地订单见下方，未完成项可重试。{D.RESET}")
        result = {"gmv": tb.gmv, "orders": list(tb.ledger),
                  "notes": execu.results + [f"未预料异常：{ex}"]}

    _final_summary(rr_plan, tb)

    if card_path:   # 生成"递给老婆/发小张"的移动端可分享方案卡
        try:
            from . import card
            html = card.render_plan_card(rr_plan, send_to=scenario.send_to, gmv=tb.gmv)
            with open(card_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"\n{D.CYAN}📲 可分享方案卡已生成：{card_path}"
                  f"（双击用浏览器打开，就是递给老婆/发小张看的那一屏）{D.RESET}")
        except Exception as ex:  # noqa
            print(f"{D.GREY}（方案卡生成跳过：{ex}）{D.RESET}")

    return {"plan": rr_plan, "result": result, "gmv": tb.gmv}


# ---------------------------------------------------------------------------
def _show_plan(plan: Plan, title: str) -> None:
    D.section(title)
    for d in plan.decisions:
        D.show_decision(d)
        print()
    D.show_timeline(plan.timeline)
    # 动态生成构成说明，只列真正出现的交易项（诚实呈现）
    parts = []
    if plan.find_by_type("choose_activity") and plan.find_by_type("choose_activity").chosen:
        parts.append("门票")
    if plan.find_by_type("choose_restaurant") and plan.find_by_type("choose_restaurant").chosen:
        parts.append("餐厅")
    if plan.find_by_type("send_gift") and plan.find_by_type("send_gift").chosen:
        parts.append("礼物")
    breakdown = "+".join(parts) if parts else "本次"
    print(f"\n{D.CYAN}💰 本方案预计撬动 GMV：¥{plan.gmv_estimate:.0f}{D.RESET}"
          f"  {D.DIM}({breakdown}，一句话撬动多笔交易){D.RESET}")


def _apply_open_answer(d: Decision, answer: str, constraints: dict) -> None:
    d.chosen = Option(label=answer, kind="value")
    d.status = Status.CONFIRMED
    d.update_confidence(0.9, f"已由 A 回答：{answer}")
    d.reasoning = f"A 已拍板：{answer}"   # 不再保留'请确认…'的追问文案（已被回答）
    if d.type == "set_budget":
        m = re.search(r"(\d+)", answer)
        if m:
            constraints["budget_per_capita"] = int(m.group(1))
    if d.type == "set_return_time":
        constraints["return_time"] = answer


def _confirm_suggestions(plan: Plan, responder: Responder) -> None:
    suggests = [d for d in plan.decisions
                if d.disposition == Disposition.SUGGEST and d.status == Status.PENDING]
    if not suggests:
        return
    D.section("Agent 给建议，等 A 拍板（中置信 / 中代价；含情感辅线）")
    for d in suggests:
        D.show_decision(d)
        if responder.confirm_suggestion(d):
            d.status = Status.CONFIRMED
        else:
            d.chosen = None
            d.status = Status.PENDING


def _collaborative_review(plan: Plan, scenario: Scenario, constraints: dict,
                          responder: Responder, edits_provider=None):
    parts_by_name = {p.id: p for p in scenario.participants}
    if edits_provider is not None:
        edits = edits_provider(plan, scenario)
    else:
        edits = scenario.edits_builder(parts_by_name, plan)
    if not edits:   # 真人评审时若没人改动，回退到预设改动，保证有东西可演
        edits = scenario.edits_builder(parts_by_name, plan)

    D.header("协同评审回合（个人助理 → 群体决策协调器）")
    print(f"{D.DIM}A 把 v{plan.version} 方案分享给局里的人；下面用'视角切换'模拟多人各自编辑。{D.RESET}")
    print(f"\n{D.BOLD}📥 收到改动（每条都带署名 author）：{D.RESET}")
    for e in edits:
        D.show_incoming_edit(e)

    rr = run_review_round(plan, scenario.participants, edits, constraints,
                          party_size=scenario.party_size)

    if rr.auto_merged:
        print()
        for e in rr.auto_merged:
            D.show_auto_merged(e)

    if rr.conflicts:
        print()
        for i, c in enumerate(rr.conflicts, 1):
            D.show_conflict(c, i)
            chosen = responder.choose_conflict(c)
            resolve_conflict(c, chosen)
            D.show_resolution(c)

    final = finalize_review(rr, party_size=scenario.party_size)
    _show_plan(final, f"再规划：新版方案 v{final.version}（能看到是谁改了什么）")
    return final, getattr(rr, "_constraints", constraints)


def _final_summary(plan: Plan, tb: ToolBox) -> None:
    D.header("收尾")
    print(f"{D.BOLD}最终方案 v{plan.version}：{D.RESET}")
    for d in plan.decisions:
        if d.chosen:
            mark = "✅"
            print(f"  {mark} {d.description}：{d.chosen.label}")
    print(f"\n{D.GREEN}{D.BOLD}🎉 全员满意 → 所有单子下完 → 计划已发出。{D.RESET}")
