"""置信度 × 代价评估（PRD §4.2）——整个项目的核心引擎。

第一版用一套清晰的规则即可（PRD 明确：不必依赖模型，规则讲得清、演得出最重要）。
这台引擎有'三种喂料'：信息缺失 / 偏好分歧 / 情境未知（PRD §7.2）。
"""
from __future__ import annotations

from typing import Optional

from .models import Cost, Disposition, Option


# ---------------------------------------------------------------------------
# 主轴：confidence × cost -> disposition
# ---------------------------------------------------------------------------
def derive_disposition(confidence: float, cost: Cost) -> Disposition:
    """PRD §0 的那条规则，本项目的'大脑'：

        置信度高 且 代价低         -> 直接做（auto）
        置信度低 或 代价高         -> 停下来问（ask）
        其余（中等置信 / 中等代价） -> 给建议（suggest）
    """
    if cost == Cost.HIGH or confidence < 0.4:
        return Disposition.ASK
    if confidence >= 0.7 and cost == Cost.LOW:
        return Disposition.AUTO
    return Disposition.SUGGEST


# ---------------------------------------------------------------------------
# 给餐厅候选项打分：根据它满足了多少'已知约束'算置信度（可解释）
# ---------------------------------------------------------------------------
def score_restaurant(opt: Option, constraints: dict) -> tuple[float, str, str]:
    """返回 (confidence, basis, reasoning)。

    constraints 里可能有：need_child_friendly / need_low_cal / max_distance_km /
    allergens(禁忌过敏原列表) / budget_per_capita(若已知)。
    预算未知会显著压低置信度（因为'问错代价高'）。
    """
    hits: list[str] = []
    misses: list[str] = []
    conf = 0.5  # 基准

    if constraints.get("need_child_friendly"):
        if opt.get("child_friendly"):
            conf += 0.18
            hits.append("有儿童餐（满足 5 岁孩子）")
        else:
            conf -= 0.30
            misses.append("无儿童餐")

    if constraints.get("need_low_cal"):
        if opt.get("has_low_cal"):
            conf += 0.18
            hits.append("有低卡沙拉（贴合老婆减肥）")
        else:
            conf -= 0.18
            misses.append("无低卡选项")

    max_d = constraints.get("max_distance_km")
    dist = opt.get("distance_km")
    if max_d is not None and dist is not None:
        if dist <= max_d:
            conf += 0.10
            hits.append(f"{dist}km，不算远")
        else:
            conf -= 0.20
            misses.append(f"{dist}km，超出'别太远'")

    # 过敏原是硬约束（PRD §7：海鲜过敏 -> 剔除海鲜店）
    banned = set(constraints.get("allergens") or [])
    has = set(opt.get("allergens") or [])
    if banned & has:
        conf -= 0.40
        misses.append("含" + "/".join(banned & has) + "（触犯过敏约束）")

    # 菜系/口味偏好：你点名想吃什么就优先什么（让不同输入选到不同店）
    cui_pref = constraints.get("cuisine_pref")
    if cui_pref:
        if cui_pref in (opt.get("cuisine") or ""):
            conf += 0.28
            hits.append(f"正是你想吃的{cui_pref}")
        else:
            conf -= 0.32   # 你点名要某口味，不对口的强力压低，让选店可靠跟着口味走
            misses.append(f"不是{cui_pref}（你点名要{cui_pref}）")

    # 口味/辣度（PRD §5 明确要求 search_restaurants 带'口味'过滤）
    if constraints.get("need_spicy"):
        if opt.get("has_spicy_option"):
            conf += 0.10
            hits.append("有辣口菜（满足无辣不欢）")
        else:
            conf -= 0.10
            misses.append("无辣口菜")
    if constraints.get("avoid_spicy"):
        if opt.get("spicy_only"):
            conf -= 0.12
            misses.append("以辣为主（怕辣/清淡者吃不消）")
        else:
            conf += 0.06
            hits.append("有清淡选择")

    # 预算：已知则核对；未知则压低置信度并标注（这正是'缺失'喂料）
    budget = constraints.get("budget_per_capita")
    pc = opt.get("per_capita")
    if budget is not None and pc is not None:
        if pc <= budget * 1.1:
            conf += 0.08
            hits.append(f"人均{int(pc)}，在预算内")
        else:
            conf -= 0.15
            misses.append(f"人均{int(pc)}，超预算")
    elif pc is not None:
        # 预算未知 —— 这是会触发'问'的关键缺失
        conf -= 0.05
        misses.append(f"人均{int(pc)}，但预算未知")

    conf = max(0.05, min(0.97, conf))
    basis = "命中：" + "；".join(hits) if hits else "无明显匹配项"
    reason = "；".join(misses) if misses else "各项约束均满足"
    return conf, basis, ("被排除点：" + reason if misses else reason)


# ---------------------------------------------------------------------------
# §7 评审：把'是谁改的'纳入置信度（PRD §4.2 末 + §7.2）
# ---------------------------------------------------------------------------
def confidence_from_author(weight: float, content_fit: float) -> float:
    """采纳某条改动的置信度 = f(作者权重, 内容契合度)。

    老婆改的餐厅，置信度天然比不熟的朋友高 —— weight 就是干这个的。
    """
    conf = 0.55 * content_fit + 0.45 * weight
    return max(0.05, min(0.97, conf))


def restaurant_content_fit(opt: Option, constraints: dict) -> float:
    """改动内容本身有多契合约束（0~1），喂给 confidence_from_author。"""
    conf, _, _ = score_restaurant(opt, constraints)
    return conf
