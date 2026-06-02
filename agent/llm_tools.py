"""用 LLM 做'意图解析'和'候选生成'（替代死库），但严格把输出 coerce 成大脑要的字段形状。

设计要点（守认知主轴）：LLM 只负责'听懂'和'找店/找活动'；
置信度×代价→做/建议/问 仍由 rules.py 的确定性大脑算。任何异常都向上抛，由调用方回退本地库。
"""
from __future__ import annotations

import itertools

from . import llm

_ai_id = itertools.count(1)


def _b(v) -> bool:
    return bool(v) if not isinstance(v, str) else v.strip().lower() in ("true", "1", "yes", "是")


def _num(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 意图解析
# ---------------------------------------------------------------------------
_INTENT_SYS = """你是本地活动规划助手的"意图解析"模块。把用户的一句中文目标解析成 JSON，只输出 JSON，不要任何多余文字或解释。

输出字段：
{
 "known": [已知约束的人话短句],
 "unknown": [用户没说、但规划前需要澄清的关键缺失，如预算/忌口/到家时间],
 "party_size": 整数(默认3),
 "constraints": {
   "need_child_friendly": 布尔,   // 提到孩子/娃/几岁 → true
   "need_low_cal": 布尔,          // 提到减肥/瘦身/控制饮食 → true
   "max_distance_km": 数字或null, // 提到"别太远/附近/离家近" → 给 10 左右
   "allergens": [字符串],         // 如 ["海鲜"]
   "avoid_spicy": 布尔,           // 有孩子或明确说怕辣/清淡 → true
   "need_spicy": 布尔,            // 明确说无辣不欢/爱辣 → true
   "cuisine_pref": 字符串或null,  // 想吃的菜系，如 火锅/日料/烧烤/粤菜/西餐/轻食/家常/海鲜
   "activity_pref": 字符串或null, // 只能取其一: amusement(游乐场) / museum(博物馆展览) / aquarium(海洋馆) / park(公园户外) / cinema(电影) / internet(网吧网咖上网) / ktv(唱歌K歌) / walk(citywalk逛街)
   "wants_activity": 布尔,        // 想出去玩/逛/景点/带孩子出去 → true；单纯聚餐 → false
   "budget_per_capita": 数字或null,
   "emotional_diet": 布尔         // 同行者在减肥、适合体贴换礼物 → true
 }
}
规则：宁可把不确定的列进 unknown，也不要替用户臆测填默认值。"""


def llm_parse(goal: str) -> dict:
    """返回 {known, unknown, party_size, constraints}，字段已 coerce。失败抛 LLMError。"""
    raw = llm.chat_json(_INTENT_SYS, f"用户目标：{goal}", temperature=0.2)
    if not isinstance(raw, dict):
        raise llm.LLMError("意图解析返回非对象")
    c = raw.get("constraints") or {}
    constraints = {
        "need_child_friendly": _b(c.get("need_child_friendly")),
        "need_low_cal": _b(c.get("need_low_cal")),
        "max_distance_km": _num(c.get("max_distance_km")),
        "allergens": [str(x) for x in (c.get("allergens") or []) if x],
        "avoid_spicy": _b(c.get("avoid_spicy")),
        "need_spicy": _b(c.get("need_spicy")),
        "cuisine_pref": (c.get("cuisine_pref") or None),
        "activity_pref": (c.get("activity_pref") or None),
        "wants_activity": _b(c.get("wants_activity")),
        "budget_per_capita": _num(c.get("budget_per_capita")),
    }
    if constraints["activity_pref"] not in (None, "amusement", "museum", "aquarium",
                                             "park", "cinema", "internet", "ktv", "walk"):
        constraints["activity_pref"] = None
    # 有孩子 -> 补齐安全子约束（与规则解析一致，供 planner 用）
    if constraints["need_child_friendly"]:
        constraints["avoid_bar_street"] = True
        constraints["avoid_tiring"] = True
        constraints["avoid_spicy"] = True
    flags = {}
    if _b(c.get("emotional_diet")) or constraints["need_low_cal"]:
        flags["emotional_diet"] = True
    if constraints["wants_activity"]:
        flags["wants_activity"] = True
    try:
        party = int(raw.get("party_size") or 3)
    except Exception:
        party = 3
    return {
        "known": [str(x) for x in (raw.get("known") or []) if x],
        "unknown": [str(x) for x in (raw.get("unknown") or []) if x],
        "party_size": max(1, min(12, party)),
        "constraints": constraints,
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# 候选生成：餐厅
# ---------------------------------------------------------------------------
_REST_SYS = """你是美团"选餐厅"工具。根据用户目标和约束，生成若干家**高度逼真的北京餐厅**候选，只输出 JSON 数组，不要多余文字。
每家字段必须齐全：
{ "name":"店名", "area":"片区", "cuisine":"含菜系关键词，如 川味火锅/日料·寿司/西北家常菜",
  "per_capita":人均数字, "child_friendly":布尔, "has_low_cal":布尔, "allergens":[如 "海鲜"],
  "distance_km":离家约数, "needs_queue":布尔, "queue_minutes":数字,
  "has_spicy_option":布尔, "spicy_only":布尔 }
要求：贴合用户口味/预算/距离/忌口；候选之间要有差异（价位、远近、是否儿童友好）；
为了让决策可对比，请至少包含 1-2 家明显契合约束的、以及 1 家明显不契合的（比如太贵或无儿童餐）。给 5 家。"""


def llm_restaurants(goal: str, constraints: dict, n: int = 5) -> list[dict]:
    user = f"用户目标：{goal}\n已知约束：{constraints}\n请给 {n} 家候选。"
    raw = llm.chat_json(_REST_SYS, user)
    rows = raw if isinstance(raw, list) else raw.get("restaurants") or raw.get("list") or []
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        out.append({
            "id": f"air_{next(_ai_id)}",
            "name": str(r["name"]), "area": str(r.get("area", "")),
            "cuisine": str(r.get("cuisine", "")),
            "per_capita": _num(r.get("per_capita"), 100) or 100,
            "child_friendly": _b(r.get("child_friendly")),
            "has_low_cal": _b(r.get("has_low_cal")),
            "allergens": [str(x) for x in (r.get("allergens") or []) if x],
            "distance_km": _num(r.get("distance_km"), 6.0) or 6.0,
            "needs_queue": _b(r.get("needs_queue")),
            "queue_minutes": int(_num(r.get("queue_minutes"), 0) or 0),
            "has_spicy_option": _b(r.get("has_spicy_option")),
            "spicy_only": _b(r.get("spicy_only")),
        })
    if not out:
        raise llm.LLMError("餐厅候选为空")
    return out


# ---------------------------------------------------------------------------
# 候选生成：活动
# ---------------------------------------------------------------------------
_ACT_SYS = """你是"找活动/景点"工具。根据用户目标和约束，生成若干个**逼真的北京活动/场所**候选，只输出 JSON 数组。
每个字段必须齐全：
{ "name":"名称", "area":"片区",
  "category":"只能取其一: amusement/museum/aquarium/park/cinema/walk",
  "distance_km":数字, "child_friendly":布尔, "tiring":"low|mid|high",
  "duration_h":数字, "price_per_person":数字, "near_bar_street":布尔 }
要求：紧扣用户想去的类型（若指定了 activity_pref，多数候选应属于该 category）；候选要有远近/强度差异。给 5 个。"""


def llm_activities(goal: str, constraints: dict, n: int = 5) -> list[dict]:
    user = f"用户目标：{goal}\n已知约束：{constraints}\n请给 {n} 个候选。"
    raw = llm.chat_json(_ACT_SYS, user)
    rows = raw if isinstance(raw, list) else raw.get("activities") or raw.get("list") or []
    out = []
    for a in rows:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        cat = a.get("category")
        if cat not in ("amusement", "museum", "aquarium", "park", "cinema", "walk"):
            cat = "walk"
        tiring = a.get("tiring") if a.get("tiring") in ("low", "mid", "high") else "mid"
        out.append({
            "id": f"aia_{next(_ai_id)}",
            "name": str(a["name"]), "area": str(a.get("area", "")), "category": cat,
            "distance_km": _num(a.get("distance_km"), 6.0) or 6.0,
            "child_friendly": _b(a.get("child_friendly")),
            "tiring": tiring,
            "duration_h": _num(a.get("duration_h"), 2.0) or 2.0,
            "price_per_person": int(_num(a.get("price_per_person"), 0) or 0),
            "near_bar_street": _b(a.get("near_bar_street")),
        })
    if not out:
        raise llm.LLMError("活动候选为空")
    return out
