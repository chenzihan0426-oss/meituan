"""协同评审与再规划回合（PRD §7）——把'个人助理'升维成'群体决策协调器'。

异步、串行的评审回合，不是实时同步。多人用'视角切换'在单机模拟（见 cli.py 的 `as`）。
本模块只管纯逻辑：合并无争议改动、在撞车处给带理由的建议、按 A 的裁决生成新版 Plan。

它和主轴是同一个大脑：别人改方案 = 外部补全 A 一个人说不全的偏好；
'采纳谁的改动' = 又一次 置信度×代价 决策，且把 author（谁说的）纳入了置信度。
"""
from __future__ import annotations

import copy
from typing import Optional

from . import rules
from .models import (Conflict, Cost, Decision, Disposition, Edit, EditType,
                     Option, Participant, Plan, ReviewRound, Status)
from .planner import estimate_gmv, _build_timeline
from .tips import build_tips

# 引擎能建模的过敏原词表（PRD §5：过敏原过滤是硬要求）。命中才据此筛店。
KNOWN_ALLERGENS = ["海鲜", "花生", "坚果", "乳制品", "牛奶", "鸡蛋", "麸质", "大豆", "芒果", "虾", "蟹"]


def run_review_round(plan: Plan, participants: list[Participant],
                     edits: list[Edit], constraints: dict,
                     party_size: int = 3) -> ReviewRound:
    """处理一轮评审：自动合并无争议改动，拎出撞车点并给带理由的建议。

    返回的 ReviewRound.merged_plan 已经吸收了无争议改动；冲突点的 chosen 暂不动，
    等 A 裁决（resolve_conflict）后再 finalize。
    """
    rr = ReviewRound(plan_version=plan.version, participants=list(participants), edits=list(edits))
    merged = copy.deepcopy(plan)
    merged.version = plan.version + 1
    merged_constraints = copy.deepcopy(constraints)

    # --- 1. 先处理'新增约束'类改动（如 海鲜过敏）：无争议，自动吸收并产生副作用 ---
    constraint_edits = [e for e in edits if e.constraint]
    for e in constraint_edits:
        _merge_constraint(merged, merged_constraints, e, rr)

    # --- 2. 按目标决定分组其余改动 ---
    by_target: dict[str, list[Edit]] = {}
    for e in edits:
        if e.constraint:
            continue
        by_target.setdefault(e.target_decision, []).append(e)

    for target, group in by_target.items():
        if len(group) == 1:
            # PRD §4.2/§7.2：'采纳谁的改动'本身也是一次 置信度×代价 决策，且把 author 纳入置信度。
            # 只有'采纳置信度'够高才静默吸收；否则不替 A 做主，拎出来交还给 A。
            e = group[0]
            dec = merged.find_by_type(target)
            fit = (rules.restaurant_content_fit(e.after, merged_constraints)
                   if target == "choose_restaurant" and e.after is not None else 0.6)
            adopt_conf = rules.confidence_from_author(e.author.weight, fit)
            cost = dec.cost if dec else Cost.LOW
            if rules.derive_disposition(adopt_conf, cost) == Disposition.AUTO:
                _apply_decision_edit(merged, merged_constraints, e)
                rr.auto_merged.append(e)
            else:
                rr.conflicts.append(_single_edit_conflict(target, e, adopt_conf, merged_constraints))
        else:
            # 撞车 -> 作为 Conflict 拎出来，给带理由的建议
            rr.conflicts.append(_build_conflict(target, group, merged_constraints))

    merged.recompute_open_questions()
    merged.timeline = _build_timeline(merged, merged_constraints)
    merged.tips = build_tips(merged, merged_constraints)
    merged.gmv_estimate = estimate_gmv(merged, party_size)
    rr.merged_plan = merged
    rr._constraints = merged_constraints  # 给 finalize 用
    return rr


# ---------------------------------------------------------------------------
def _merge_constraint(merged: Plan, constraints: dict, e: Edit, rr: ReviewRound) -> None:
    """吸收一条新增约束，诚实地只在'引擎真能据此行动'时声称'已纳入'。

    - 已知过敏原（硬约束）：纳入 + 剔除相关备选 + 触犯则降已选项置信度。
    - 口味/辣度（PRD §5 要求的'口味'维度）：设 need_spicy / avoid_spicy，影响后续打分。
    - 其它无法建模的约束：诚实标 actionable=False（屏幕显示'已记录'而非'已纳入'）。
    """
    rr.auto_merged.append(e)
    text = e.constraint or ""

    # 1) 过敏原（硬约束）
    allergen = next((a for a in KNOWN_ALLERGENS if a in text), None)
    if allergen:
        constraints.setdefault("allergens", [])
        if allergen not in constraints["allergens"]:
            constraints["allergens"].append(allergen)
        rest = merged.find_by_type("choose_restaurant")
        removed = 0
        if rest:
            before = len(rest.options)
            rest.options = [o for o in rest.options
                            if allergen not in (o.get("allergens") or [])]
            removed = before - len(rest.options)
            if rest.chosen and allergen in (rest.chosen.get("allergens") or []):
                rest.update_confidence(0.2, f"被『{e.author.id}: {text}』推翻——当前店含{allergen}")
        e.merged_note = (f"已纳入过敏约束，剔除 {removed} 个含{allergen}的备选"
                         if removed else f"已纳入过敏约束（{allergen}）")
        return

    # 提到'过敏'但不在已知词表 -> 诚实记录，不假装筛了店
    if "过敏" in text:
        e.actionable = False
        e.merged_note = "已记录该过敏诉求，但当前数据未建模此过敏原，无法自动筛店"
        return

    # 2) 口味 / 辣度（先判'避辣'，让'不想吃辣''不吃辣'这类否定不被'辣'误判成要辣）
    if any(k in text for k in ("怕辣", "清淡", "不想吃辣", "不吃辣", "少辣", "不辣", "微辣")):
        constraints["avoid_spicy"] = True
        e.merged_note = "已纳入口味偏好：偏清淡 / 避免纯辣店"
        return
    if any(k in text for k in ("无辣不欢", "重辣", "爱辣", "要辣", "辣口", "能吃辣", "嗜辣",
                               "想吃辣", "爱吃辣", "多放辣", "重口味")):
        constraints["need_spicy"] = True
        e.merged_note = "已纳入口味偏好：优先有辣口菜的店"
        return

    # 3) 其它无法建模的约束 -> 诚实记录
    e.actionable = False
    e.merged_note = "已记录，但当前数据无法据此自动筛店"


def _apply_decision_edit(merged: Plan, constraints: dict, e: Edit) -> None:
    d = merged.find_by_type(e.target_decision)
    if d is None:
        return
    if e.type == EditType.REMOVE:
        d.chosen = None
        d.reasoning = f"{e.author.id} 删除了此项：{e.note}"
        return
    if e.after is not None:
        old = d.chosen.label if d.chosen else "（无）"
        # 按业务 id（如 rest_C）去重：同一家店不要因 Option.id 不同而重复进候选
        match = next((o for o in d.options
                      if o.get("id") and o.get("id") == e.after.get("id")), None)
        if match is not None:
            d.chosen = match
        else:
            d.chosen = e.after
            d.options.append(e.after)
        # 置信度因'谁的反馈'而更新（PRD §3.1：被某人确认/推翻后重算）
        fit = rules.restaurant_content_fit(e.after, constraints) if e.target_decision == "choose_restaurant" else 0.6
        new_conf = rules.confidence_from_author(e.author.weight, fit)
        d.update_confidence(round(new_conf, 2),
                            f"被『{e.author.id}』修正（{old} → {e.after.label}）"
                            + (f"：{e.note}" if e.note else ""))
        d.reasoning = f"采纳 {e.author.id} 的改动：{e.after.label}" + (f"（{e.note}）" if e.note else "")


def _single_edit_conflict(target: str, e: Edit, adopt_conf: float, constraints: dict) -> Conflict:
    """单条改动但'采纳置信度'不够高：不静默吸收，交还给 A 确认（PRD §0：有分歧/没把握就给建议）。"""
    what = e.after.label if e.after else "（移除）"
    reason = (f"{e.author.id}（weight={e.author.weight}）想改为 {what}"
              + (f"（{e.note}）" if e.note else "")
              + f"；但采纳置信度仅 {adopt_conf:.2f}（偏低），我不敢替您静默采纳，请确认。")
    return Conflict(target_decision=target, competing_edits=[e],
                    agent_suggestion=e.after, suggested_edit=e, suggestion_reason=reason)


def _build_conflict(target: str, group: list[Edit], constraints: dict) -> Conflict:
    """对一处撞车给出带理由的建议：结合约束契合度 + author.weight 算采纳置信度。"""
    scored = []
    for e in group:
        if target == "choose_restaurant" and e.after is not None:
            fit = rules.restaurant_content_fit(e.after, constraints)
        else:
            fit = 0.6
        adopt_conf = rules.confidence_from_author(e.author.weight, fit)
        scored.append((adopt_conf, fit, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    best_conf, best_fit, best_edit = scored[0]

    reason = _conflict_reason(target, scored, constraints)
    return Conflict(
        target_decision=target,
        competing_edits=group,
        agent_suggestion=best_edit.after,
        suggested_edit=best_edit,
        suggestion_reason=reason,
    )


def _conflict_reason(target: str, scored: list, constraints: dict) -> str:
    best_conf, best_fit, best_edit = scored[0]
    lines = []
    if target == "choose_restaurant" and best_edit.after is not None:
        o = best_edit.after
        pros = []
        if o.get("distance_km") is not None and o.get("distance_km") <= (constraints.get("max_distance_km") or 99):
            pros.append(f"离家近（{o.get('distance_km')}km，贴合'别太远'）")
        if o.get("has_low_cal"):
            pros.append("有低卡选项（贴合减肥）")
        if o.get("child_friendly"):
            pros.append("有儿童餐")
        lines.append(f"{best_edit.author.id}的 {o.label}：" + "、".join(pros or ["综合最优"]))
        # 落败者为什么差
        for conf, fit, e in scored[1:]:
            if e.after is None:
                continue
            cons = []
            if not e.after.get("child_friendly"):
                cons.append("无儿童餐")
            if e.after.get("per_capita", 0) >= 150:
                cons.append("人均偏高")
            banned = set(constraints.get("allergens") or [])
            if banned & set(e.after.get("allergens") or []):
                cons.append("含" + "/".join(banned & set(e.after.get("allergens"))) + "（触犯刚加入的过敏约束）")
            if cons:
                lines.append(f"{e.author.id}的 {e.after.label}：" + "、".join(cons))
    lines.append(f"（依据含：{best_edit.author.id} weight={best_edit.author.weight} "
                 f"高于其他人；采纳置信度 {best_conf:.2f}）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def resolve_conflict(conflict: Conflict, chosen_edit: Optional[Edit]) -> None:
    """A 裁决：chosen_edit 为采纳的那条（None 表示 A 另定，外部再处理）。"""
    conflict.resolution = chosen_edit


def finalize_review(rr: ReviewRound, party_size: int = 3) -> Plan:
    """把 A 对各冲突的裁决应用到 merged_plan，生成最终新版 Plan。"""
    merged = rr.merged_plan
    constraints = getattr(rr, "_constraints", {})
    for c in rr.conflicts:
        if c.resolution is not None:
            _apply_decision_edit(merged, constraints, c.resolution)
            dec = merged.find_by_type(c.target_decision)
            if dec is None:
                continue
            # 安全闸：即便 A 亲自裁决，若选定项仍触犯'硬过敏约束'，不静默确认——
            # 留在 PENDING（disposition 已随低置信度重算为 ASK），在 v2 里再亮一次红灯请 A 复核。
            banned = set(constraints.get("allergens") or [])
            hit = banned & set(dec.chosen.get("allergens") or []) if dec.chosen else set()
            if hit:
                dec.reasoning = (f"⚠️ 您选的 {dec.chosen.label} 含{'/'.join(hit)}，"
                                 f"触犯本局刚加入的过敏约束——这事代价太高，请您再确认一次是否坚持。")
                # 保持 PENDING，交还给 A 复核
            else:
                dec.status = Status.CONFIRMED   # A 已亲自裁决且无硬约束冲突 -> 视为已确认
    merged.recompute_open_questions()
    merged.timeline = _build_timeline(merged, constraints)
    merged.tips = build_tips(merged, constraints)
    merged.gmv_estimate = estimate_gmv(merged, party_size)
    return merged
