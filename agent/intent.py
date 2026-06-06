"""意图解析 + 缺失识别（PRD §4.1）。

产出不是一份'填好的需求'，而是一张【已知 / 未知】清单——这是认知向的起点：
先承认'用户要什么'本身是不确定的，需要主动估计，绝不默默填默认值。

第一版用关键词规则即可（PRD §12：不依赖模型）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# 场所类型词表：把你说的词映射到活动库的 category（tools.ACTIVITIES）
ACTIVITY_PREF = {
    "amusement": ["游乐场", "游乐园", "乐园", "主题乐园", "欢乐谷", "迪士尼", "儿童乐园"],
    "museum": ["博物馆", "展览", "看展", "美术馆", "科技馆", "展", "艺术馆"],
    "aquarium": ["海洋馆", "水族馆", "海底世界"],
    "park": ["公园", "户外", "骑行", "踏青", "散步", "草坪", "野餐"],
    "cinema": ["电影", "影院", "看片"],
    "internet": ["上网", "网吧", "网咖", "开黑", "打游戏", "电竞", "lol", "吃鸡"],
    "ktv": ["ktv", "唱歌", "k歌", "唱k", "麦霸"],
    "walk": ["citywalk", "city walk", "逛街", "逛", "胡同", "小吃街", "商场", "压马路"],
}
ACTIVITY_PREF_LABEL = {
    "amusement": "游乐场/乐园", "museum": "博物馆/展览", "aquarium": "海洋馆",
    "park": "公园/户外", "cinema": "电影", "internet": "网吧/网咖",
    "ktv": "KTV/唱歌", "walk": "citywalk/逛街",
}


def all_activity_cats(text: str) -> list:
    """文本里提到的所有活动类别（去重保序）——支持'一次加多个活动'（唱K + 游乐园）。"""
    low = text.lower()
    out = []
    for cat, kws in ACTIVITY_PREF.items():
        if any(k in low for k in kws) and cat not in out:
            out.append(cat)
    return out


def _scan_activity(text: str) -> tuple:
    """从目标里识别'想去的活动类型'。返回 (已知类别 或 None, 用户原话 或 None)。

    先匹配已知类别；没命中但说了'去/玩X'就把原话拎出来（让高德按关键词搜，
    而不是默默默认成儿童乐园）。
    """
    for cat, kws in ACTIVITY_PREF.items():
        if any(k in text.lower() for k in kws):
            return cat, None
    # 非贪婪 + 边界前瞻：在'然后/再/和/玩/吃…'等连接词处停住，只取活动名本身
    m = re.search(r"(?:出去|想去|要去|去)\s*([一-龥A-Za-z]{2,6}?)"
                  r"(?=然后|之后|再|还|，|,|。|、|和|跟|与|后|了|玩|吃|看|$|\s)", text)
    if m:
        raw = m.group(1)
        STOP = ("吃饭", "吃个", "聚餐", "玩玩", "哪", "这", "那", "里", "外", "走走", "转转")
        if raw and not any(w in raw for w in STOP):
            return None, raw
    return None, None

# 菜系/口味词表：把你说的词映射到餐厅 cuisine 串里的可匹配关键词（tools.RESTAURANTS）
CUISINE_PREF = {
    "火锅": ["火锅", "麻辣烫", "串串"],
    "粤菜": ["粤菜", "广式", "早茶", "港式", "茶点"],
    "日料": ["日料", "寿司", "刺身", "日本菜", "居酒屋"],
    "西餐": ["西餐", "牛排", "意大利", "披萨", "意餐"],
    "烧烤": ["烧烤", "烤肉", "烤串", "撸串", "串儿"],
    "海鲜": ["海鲜", "生蚝", "螃蟹", "大虾"],
    "轻食": ["轻食", "沙拉", "健康餐", "减脂餐", "健身餐"],
    "家常": ["家常", "中餐", "炒菜", "西北菜", "面馆"],
}

# 否定词：出现在菜系词前/后表示'不想要'，绝不能当成'想吃'（'不吃海鲜''海鲜过敏'≠想吃海鲜）
_CUISINE_NEG = ("不", "别", "忌", "讨厌", "没", "少", "戒", "怕")


def positive_cuisine(text: str):
    """从文本里识别'正向想吃的菜系'，跳过否定语境。返回 cui 或 None。

    例：'想吃海鲜'→海鲜；'有人不吃海鲜'→None；'海鲜过敏'→None。
    """
    for cui, kws in CUISINE_PREF.items():
        for kw in kws:
            start = 0
            while True:
                i = text.find(kw, start)
                if i == -1:
                    break
                pre = text[max(0, i - 2):i]
                suf = text[i + len(kw):i + len(kw) + 2]
                if not any(n in pre for n in _CUISINE_NEG) and "过敏" not in suf:
                    return cui      # 命中一个无否定语境的正向提及
                start = i + 1
    return None


@dataclass
class Intent:
    raw: str
    known: list[str] = field(default_factory=list)        # 已知约束（人话）
    unknown: list[str] = field(default_factory=list)      # 显式列出的缺失
    constraints: dict = field(default_factory=dict)       # 结构化，喂给 planner / rules
    party: list[str] = field(default_factory=list)        # 谁去
    flags: dict = field(default_factory=dict)             # emotional_diet 等情境信号


def parse_goal(text: str, use_llm: bool = False) -> Intent:
    """解析目标。use_llm=True 时优先用 LLM（听懂任意说法），失败回退关键词规则。"""
    if use_llm:
        try:
            from . import llm_tools
            r = llm_tools.llm_parse(text)
            it = Intent(raw=text, known=r["known"], unknown=r["unknown"],
                        constraints=r["constraints"], flags=r["flags"])
            it.constraints.setdefault("allergens", [])
            it.party = ["参与者"] * r["party_size"]   # 仅记人数，便于 party_size 推断
            it.flags["party_size"] = r["party_size"]
            # LLM 的 activity_pref 只认 6 类白名单，认不出'上网/网吧/KTV'等会置 None；
            # 本地再扫一遍补上类别或原话，避免被默默当成儿童乐园
            if not it.constraints.get("activity_pref"):
                cat, raw = _scan_activity(text)
                if cat:
                    it.constraints["activity_pref"] = cat
                    it.flags["wants_activity"] = True
                elif raw:
                    it.constraints.setdefault("activity_raw", raw)
                    it.flags["wants_activity"] = True
            return it
        except Exception:
            pass   # 回退规则解析（PRD §8：AI 不灵也优雅）

    t = text
    intent = Intent(raw=text)
    c = intent.constraints

    # --- 同行人 ---
    if any(k in t for k in ("老婆", "妻子", "媳妇", "爱人")):
        intent.party.append("老婆")
    if any(k in t for k in ("孩子", "娃", "宝宝", "儿子", "女儿")) or "岁" in t:
        intent.party.append("孩子")
        intent.known.append("有 5 岁孩子 → 避开辣、避开酒吧街、避免太累的项目")
        c["need_child_friendly"] = True
        c["avoid_spicy"] = True
        c["avoid_bar_street"] = True
        c["avoid_tiring"] = True
    if any(k in t for k in ("朋友", "哥们", "同事", "闺蜜")):
        intent.party.append("朋友")

    # --- 减肥 / 情感敏感信号 ---
    if any(k in t for k in ("减肥", "瘦身", "控制饮食", "健身")):
        intent.known.append("老婆最近在减肥 → 倾向清淡低卡，送礼避免高热量")
        c["need_low_cal"] = True
        intent.flags["emotional_diet"] = True

    # --- 过敏 / 忌口（'不吃海鲜''海鲜过敏''忌花生'）→ 硬约束，避开相关店 ---
    for a in ("海鲜", "花生", "坚果", "乳制品", "牛奶", "鸡蛋", "麸质", "大豆", "芒果", "虾", "蟹"):
        if any(p in t for p in (f"不吃{a}", f"{a}过敏", f"对{a}过敏", f"忌{a}", f"不能吃{a}", f"{a}忌口")):
            c.setdefault("allergens", [])
            if a not in c["allergens"]:
                c["allergens"].append(a)
            intent.known.append(f"有人不吃/忌{a} → 硬约束，避开{a}相关的店")

    # --- 地理约束 ---
    if any(k in t for k in ("别离家太远", "别太远", "附近", "近一点", "离家近")):
        intent.known.append("别离家太远（地理约束）→ 控制在 ~10km 内")
        c["max_distance_km"] = 10.0

    # --- 时长 ---
    if any(k in t for k in ("几小时", "半天", "下午", "几个小时")):
        intent.known.append("时长约 4–6 小时（下午档）")
        c["duration_window_h"] = (4, 6)

    # --- 你想去的'场所类型'（精准匹配，而不是只挑最近的）---
    cat, raw = _scan_activity(t)
    if cat:
        c["activity_pref"] = cat
        intent.flags["wants_activity"] = True
        intent.known.append(f"想去的类型：{ACTIVITY_PREF_LABEL[cat]}（按此优先找场所）")
    elif raw:
        c["activity_raw"] = raw
        intent.flags["wants_activity"] = True
        intent.known.append(f"想去的场所：{raw}（按你的原话搜附近）")

    # --- 是否要安排'活动'（出去玩）还是单纯吃饭 ---
    if any(k in t for k in ("玩", "出去", "出门", "逛", "活动", "景点", "游")):
        intent.flags["wants_activity"] = True

    # --- 你想吃的'菜系/口味'（据此选店；'不吃X/X过敏'不算想吃）---
    cui = positive_cuisine(t)
    if cui:
        c["cuisine_pref"] = cui
        intent.known.append(f"想吃的口味：{cui}（按此优先选店）")
    # 没命中已知菜系，但明确说了"想吃X"——也别装没听见，记下来好诚实回应
    if "cuisine_pref" not in c:
        m = re.search(r"(?:想吃|要吃|爱吃|来点|整点|想整点|馋)([一-龥]{2,4})", t)
        if m and not any(ch in m.group(1) for ch in "的地个点家饭顿啥东西好喝"):
            c["cuisine_raw"] = m.group(1)

    # --- 显式列出'未知'（绝不默默填默认值）---
    if "预算" not in t and "人均" not in t:
        intent.unknown.append("预算 / 餐厅消费档位？（完全未知，问错代价高）")
    if "忌口" not in t and "过敏" not in t:
        intent.unknown.append("孩子忌口 / 吃不吃辣？（影响选店）")
    if intent.flags.get("emotional_diet"):
        intent.unknown.append("老婆减肥忌口到什么程度？（是否完全戒糖）")
    if not any(k in t for k in ("几点", "到家", "回家")):
        intent.unknown.append("几点必须到家 / 散场？（影响整条时间线，错了全盘崩）")

    # --- 礼物意图（攒局通常想给个惊喜）---
    c.setdefault("allergens", [])
    return intent
