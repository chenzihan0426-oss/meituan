"""生成第一版 Plan（PRD §4.3 内部冲突协调 + §6 流程）。

对每个待定决定：算 置信度 × 代价 -> 推导 做/建议/问，并写清 reasoning。
内部冲突协调（娃要亲子+不累 vs 老婆要清淡低卡）在 choose_restaurant 里显式权衡。
"""
from __future__ import annotations

import re
from typing import Optional

from . import rules
from .intent import Intent
from .models import (Cost, Decision, Disposition, Option, Plan, Slot, Status)
from .tips import build_tips
from .tools import ToolBox


def _opt_from_restaurant(r: dict) -> Option:
    return Option(label=r["name"], kind="restaurant", price=r["per_capita"], data=dict(r))


def _opt_from_activity(a: dict) -> Option:
    return Option(label=a["name"], kind="activity", price=a["price_per_person"], data=dict(a))


def _opt_from_gift(g: dict) -> Option:
    return Option(label=g["name"], kind="gift", price=g["price"], data=dict(g))


def _mk(dtype: str, desc: str, *, options=None, chosen=None,
        confidence: float, basis: str, cost: Cost, reasoning: str,
        counterfactual: str = "") -> Decision:
    """造一个 Decision，disposition 由 confidence × cost 推导（主轴）。"""
    return Decision(
        type=dtype, description=desc, options=options or [], chosen=chosen,
        confidence=confidence, confidence_basis=basis, cost=cost, reasoning=reasoning,
        disposition=rules.derive_disposition(confidence, cost),
        counterfactual=counterfactual,
    )


# ---------------------------------------------------------------------------
def build_first_plan(intent: Intent, tb: ToolBox, party_size: int = 3) -> Plan:
    c = intent.constraints
    tb.context = {"goal": intent.raw, "constraints": c}   # 供 AI 查询用（若 tb.use_llm）
    has_child = bool(c.get("need_child_friendly"))
    wants_activity = bool(intent.flags.get("wants_activity"))
    decisions: list[Decision] = []

    # ---- 1. 孩子安全常识：高置信 + 低代价 -> 直接定（PRD 验收 #1）----
    if has_child:
        # 置信度 = 命中几条'普遍育儿常识'累加出来的（不是拍 0.92）
        safe_conf, safe_basis = rules.confidence_from_factors(0.50, [
            ("普遍育儿常识：避辣", 0.14),
            ("避酒吧街", 0.14),
            ("避免太累的项目", 0.14),
        ])
        decisions.append(_mk(
            "child_safety", "孩子相关常识约束",
            chosen=Option(label="避辣 / 避酒吧街 / 避免太累的项目", kind="value"),
            confidence=safe_conf, basis=safe_basis,
            cost=Cost.LOW, reasoning="这些是公认的安全/舒适底线，错了也容易纠正，无需打扰用户",
        ))

    # ---- 2. 选活动：仅当用户想'出去玩'时才排（单纯聚餐不强加活动）----
    if wants_activity:
        acts = tb.search_activities()
        act_opts = [_opt_from_activity(a) for a in acts]
        chosen_act, act_conf, act_basis, act_reason = _pick_activity(act_opts, c, has_child)
        decisions.append(_mk(
            "choose_activity", "选活动",
            options=act_opts, chosen=chosen_act,
            confidence=act_conf, basis=act_basis, cost=Cost.LOW, reasoning=act_reason,
        ))
        # 动线优化：没点名特定口味时，就在'要去的活动'附近吃，省得唱完 K 还要专程跨城找饭辙。
        # （只在真实数据下活动带坐标时生效；点名了菜系则保留'专程去吃'的自由。）
        if chosen_act is not None and chosen_act.get("location") and not c.get("cuisine_pref"):
            c["near_dining_loc"] = chosen_act.get("location")
            c["near_dining_label"] = chosen_act.label

    # ---- 3. 选餐厅：内部冲突协调（娃 vs 老婆）----
    rests = tb.search_restaurants(child_friendly=c.get("need_child_friendly"),
                                  low_cal=c.get("need_low_cal"),
                                  exclude_allergens=c.get("allergens"))
    rest_opts = [_opt_from_restaurant(r) for r in rests]
    decisions.append(_build_restaurant_decision(rest_opts, c, has_child))

    # ---- 4. 餐厅消费档位：预算未知 + 问错代价高 -> 停下来问（验收 #2）----
    budget_conf, budget_basis = rules.confidence_from_factors(0.50, [
        ("预算完全未知", -0.15),
        ("问错代价高（可能直接超预算、孩子也未必吃得惯）", -0.05),
    ])
    decisions.append(_mk(
        "set_budget", "餐厅消费档位",
        confidence=budget_conf, basis=budget_basis,
        cost=Cost.HIGH,
        reasoning="这家人均不高（~¥85），但若您本想升级到人均 300 的精致餐厅，我不敢替您定——"
                  "请确认：控制在人均 100 左右，还是放开到 300？",
        counterfactual="如果我自作主张定了人均 300 那家：可能直接超你预算、孩子也未必吃得惯——"
                       "钱花出去难收回，所以这条我没敢替你定。",
    ))

    # ---- 5. 几点到家：影响整条时间线，错了全盘崩 -> 停下来问（验收 #2）----
    ret_conf, ret_basis = rules.confidence_from_factors(0.50, [
        ("到家时刻完全未知", -0.15),
        ("它决定整条时间线，错了全盘崩", -0.10),
    ])
    decisions.append(_mk(
        "set_return_time", "几点必须到家",
        confidence=ret_conf, basis=ret_basis,
        cost=Cost.HIGH,
        reasoning="请确认今天几点前要到家？（默认按 19:30 排，但不敢替您拍板）",
        counterfactual="如果我按默认 19:30 硬排：万一你其实要早回接老人/孩子要睡，整条行程全得推翻——"
                       "影响面太大，所以先问你一句。",
    ))

    # ---- 6. 留不留午睡口子：中置信 + 低代价 -> 给建议（验收 #3，仅亲子出游）----
    if has_child and wants_activity:
        nap_conf, nap_basis = rules.confidence_from_factors(0.50, [
            ("留口子代价很低、可随时取消", 0.15),
            ("今天孩子是否犯困未知", -0.10),
        ])
        decisions.append(_mk(
            "nap_window", "下午留不留午睡口子",
            chosen=Option(label="14:00 出门前先让孩子睡 40 分钟", kind="value"),
            confidence=nap_conf, basis=nap_basis,
            cost=Cost.LOW,
            reasoning="5 岁孩子下午容易犯困，建议留 40 分钟午睡口子再出门；要不要这么排，您定",
        ))

    # ---- 7. 送礼：情感辅线（减肥 -> 花+票 而非蛋糕）-> 给建议（验收 #5）----
    if intent.flags.get("emotional_diet"):
        decisions.append(_build_gift_decision(tb))

    plan = Plan(version=1, decisions=decisions)
    plan.timeline = _build_timeline(plan, c)
    plan.tips = build_tips(plan, c)
    plan.recompute_open_questions()
    plan.gmv_estimate = estimate_gmv(plan, party_size)
    return plan


# ---------------------------------------------------------------------------
def _pick_activity(opts: list[Option], c: dict, has_child: bool) -> tuple[Option, float, str, str]:
    from .intent import ACTIVITY_PREF_LABEL
    excluded = []
    candidates = []
    for o in opts:
        if c.get("avoid_bar_street") and o.get("near_bar_street"):
            excluded.append(f"{o.label}（挨着酒吧街，5 岁娃不宜）")
            continue
        if c.get("avoid_tiring") and o.get("tiring") == "high":
            excluded.append(f"{o.label}（太累，娃吃不消）")
            continue
        candidates.append(o)

    # 你明确说了想去的'场所类型' -> 按类型精准匹配（而不是只挑最近的）
    pref = c.get("activity_pref")
    if pref:
        label = ACTIVITY_PREF_LABEL.get(pref, pref)
        matched = [o for o in candidates if o.get("category") == pref]
        if matched:
            matched.sort(key=lambda o: o.get("distance_km") or 99)
            chosen = matched[0]
            others = "，".join(o.label for o in matched[1:]) if len(matched) > 1 else ""
            reason = f"按你想去的「{label}」优先：选 {chosen.label}（{chosen.get('distance_km')}km）"
            reason += f"；同类还可选：{others}" if others else ""
            return chosen, 0.86, f"命中你点名的「{label}」类，且已按约束筛过", reason
        # 这类场所本地数据里没有 -> 老实说，不偷偷换个别的（降到'建议'让你定）
        pool = candidates or opts
        pool.sort(key=lambda o: o.get("distance_km") or 99)
        chosen = pool[0]
        return (chosen, 0.45,
                f"附近暂没有「{label}」类场所（Mock 数据所限）",
                f"你想去「{label}」，但库里没有这类；先给最接近的 {chosen.label}，你也可以换个说法或我再找")

    if has_child:
        # 儿童友好 + 距离近优先
        candidates.sort(key=lambda o: (not o.get("child_friendly"), o.get("distance_km") or 99))
        chosen = candidates[0]
        basis = "命中：亲子友好且不太累；已按'避酒吧街/避免太累'筛过"
        reason = "选 {}（亲子友好、强度低、{}km）".format(chosen.label, chosen.get("distance_km"))
    else:
        # 成人局：就近优先。max_d 可能被意图解析显式设成 None（没提'别太远'），兜底 99
        max_d = c.get("max_distance_km") or 99
        candidates.sort(key=lambda o: ((o.get("distance_km") or 99) > max_d, o.get("distance_km") or 99))
        chosen = candidates[0]
        basis = "命中：离家近、强度适中（成人局，不强加亲子约束）"
        reason = "选 {}（{}km，就近不折腾）".format(chosen.label, chosen.get("distance_km"))
    if excluded:
        reason += "；排除：" + "，".join(excluded)
    return chosen, 0.84 if has_child else 0.7, basis, reason


def _build_restaurant_decision(opts: list[Option], c: dict, has_child: bool) -> Decision:
    """§4.3 内部冲突协调：在'没有唯一正确答案'处做带理由的权衡。
    有娃：娃要儿童餐 vs 老婆要低卡，找同时满足两边的店；成人局：兼顾各人口味/约束。"""
    scored = []
    for o in opts:
        conf, basis, reason = rules.score_restaurant(o, c)
        scored.append((conf, o, basis, reason))
    scored.sort(key=lambda x: x[0], reverse=True)
    best_conf, best, best_basis, _ = scored[0]

    # 写清'为什么选它、为什么排除别的'（两全其美的取舍）
    excluded_bits = []
    for conf, o, _, reason in scored[1:]:
        bits = []
        if not o.get("child_friendly"):
            bits.append("无儿童餐")
        if not o.get("has_low_cal"):
            bits.append("无低卡选项")
        if (o.get("per_capita") or 0) >= 300:
            bits.append("人均偏高")
        if o.get("allergens"):
            bits.append("含" + "/".join(o.get("allergens")))
        if bits:
            excluded_bits.append(f"{o.label}（{'、'.join(bits)}）")
    cui_pref = c.get("cuisine_pref")
    no_cuisine = cui_pref and not any(cui_pref in (o.get("cuisine") or "") for o in opts)
    if has_child:
        desc = "选餐厅（娃 vs 老婆，内部冲突协调）"
        head = (f"内部冲突：娃要'亲子+儿童餐'、老婆要'清淡低卡'，是一对真实矛盾。"
                f"{best.label} 同时有儿童餐和低卡沙拉，两全；")
        basis_tail = "；同时满足'孩子要儿童餐'与'老婆要低卡'两个已知约束"
    elif cui_pref and not no_cuisine:
        desc = f"选餐厅（你点名要{cui_pref}）"
        head = f"按你想吃的「{cui_pref}」优先，{best.label} 在该口味里综合最优；"
        basis_tail = f"；命中你点名的{cui_pref}口味"
    else:
        desc = "选餐厅（兼顾各人口味与约束）"
        head = f"在'离家别太远 + 各人口味不一'之间权衡，{best.label} 综合最优；"
        basis_tail = "；按'就近 + 满足在场约束'综合评分得出"
    if no_cuisine:
        head = (f"你点名想吃「{cui_pref}」，但本地店库里暂没有这一类——先给最接近约束的 "
                f"{best.label}，你可以换个口味说法；") + head
    elif c.get("cuisine_raw") and not cui_pref:
        raw = c["cuisine_raw"]
        desc = f"选餐厅（你想吃{raw}）"
        head = (f"你提到想吃「{raw}」，但我的 Mock 店库里没有对应的这一类——先给最接近约束的 "
                f"{best.label}，你可以换个说法（如火锅/日料/烧烤/轻食）我再找；") + head
    if c.get("near_dining_label") and not cui_pref:
        head = (f"就在你要去的「{c['near_dining_label']}」附近吃，玩完顺路就能解决，不用专程跨城找饭辙；"
                + head)
    reasoning = head + ("排除：" + "，".join(excluded_bits) if excluded_bits else "")
    # 换店代价低 -> 高置信即可自动定
    return _mk("choose_restaurant", desc,
               options=opts, chosen=best,
               confidence=round(best_conf, 2),
               basis=best_basis + basis_tail,
               cost=Cost.LOW, reasoning=reasoning)


def _build_gift_decision(tb: ToolBox) -> Decision:
    from .tools import GIFTS
    cake = _opt_from_gift(next(g for g in GIFTS if g["id"] == "gift_cake"))
    flowers = _opt_from_gift(next(g for g in GIFTS if g["id"] == "gift_flowers"))
    ticket = _opt_from_gift(next(g for g in GIFTS if g["id"] == "gift_ticket"))
    # 把'花+票'打包成一个候选项
    bundle = Option(label="一束花 + 一张她一直想看的展的票", kind="gift",
                    price=flowers.price + ticket.price,
                    data={"members": ["gift_flowers", "gift_ticket"], "high_calorie": False})
    return _mk(
        "send_gift", "给老婆的惊喜礼物",
        options=[cake, bundle], chosen=bundle,
        confidence=0.58,
        basis="情绪敏感推断：她在减肥的当口，送高热量蛋糕可能适得其反",
        cost=Cost.MID,
        reasoning="建议把蛋糕换成『花 + 展览票』——更贴心也不破坏她的饮食计划；"
                  "但这是情感上的事，分寸由您拍板（我只建议，不擅改）",
    )


def _parse_return_minutes(s: str) -> Optional[int]:
    """从'19:30 前到家'/'下午5点半'/'晚上12点'解析出'到家时刻'的分钟数（当天 0 点起）。

    12 小时制消歧：下午/晚上 1–11 点 +12；晚上12点=午夜(记 24:00=1440)；中午12点=正午。
    """
    if not s:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})\s*点(半)?", s)
        if not m:
            return None
        h, mi = int(m.group(1)), (30 if m.group(2) else 0)
    noon = bool(re.search(r"中午|正午", s))
    night = bool(re.search(r"晚上|傍晚|夜|晚", s))
    afternoon = "下午" in s
    if h == 12:
        if night and not noon:
            h = 24            # 晚上12点=午夜，时间线上记 24:00（次日0点）
        # 中午12点 / 默认 → 保持 12（正午）
    elif 1 <= h <= 11 and (afternoon or night):
        h += 12               # 下午3点→15、晚上8点→20
    if not (0 <= h <= 24):
        return None
    return h * 60 + mi


def _hm(total_min: int) -> str:
    total_min = max(0, total_min)
    return f"{(total_min // 60) % 24:02d}:{total_min % 60:02d}"


# 交通：市区出行约 15km/h（含找车/红灯/找车位），落在早晚高峰再乘堵车系数
_PEAK = [(7 * 60, 9 * 60, "早高峰"), (17 * 60, 19 * 60, "晚高峰")]


def _peak_label(minute: int):
    for a, b, name in _PEAK:
        if a <= minute < b:
            return name
    return None


def _travel_minutes(dist_km) -> int:
    d = dist_km if isinstance(dist_km, (int, float)) and dist_km > 0 else 3.0
    return max(15, int(round(d * 4)))   # ~15km/h 的保守市区车程


def _dist_of(dec) -> float:
    if dec and dec.chosen:
        v = dec.chosen.get("distance_km")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 3.0


def _build_timeline(plan: Plan, c: dict) -> list[Slot]:
    act = plan.find_by_type("choose_activity")
    rest = plan.find_by_type("choose_restaurant")
    nap = plan.find_by_type("nap_window")
    gift = plan.find_by_type("send_gift")

    ret = c.get("return_time")
    end = _parse_return_minutes(ret) if ret else None
    end = end if end is not None else 19 * 60 + 30   # 默认 19:30 到家

    # 倒排：从'到家时刻'往前推每个环节，让整条线真正卡在你定的时间结束。
    # 通勤段按真实距离估车程，落在早晚高峰再加堵车 buffer。
    blocks = []   # {dur, title, note, travel}
    if nap and nap.chosen:
        blocks.append({"dur": 40, "title": "孩子午睡口子",
                       "note": "5 岁娃下午易困，先补觉（建议项，待您定）"})
    if act and act.chosen:
        ad = _dist_of(act)
        blocks.append({"dur": _travel_minutes(ad), "title": "出发去玩",
                       "note": f"离家约 {ad}km", "travel": True})
        adur = int(round(float(act.chosen.get("duration_h", 2.0) or 2.0) * 60))
        blocks.append({"dur": adur, "title": act.chosen.label,
                       "note": act.reasoning.split("；")[0]})
        blocks.append({"dur": _travel_minutes(_dist_of(rest)), "title": "前往餐厅",
                       "note": "顺路过去", "travel": True})
    if rest and rest.chosen:
        blocks.append({"dur": 90, "title": f"用餐 @ {rest.chosen.label}",
                       "note": rest.reasoning.split("；")[0]})
    if gift and gift.chosen:
        blocks.append({"dur": 30, "title": "礼物惊喜（花+票，待确认）",
                       "note": "情感高光，建议项"})

    FLOOR = 6 * 60   # 没人下午出游早于 06:00：倒排到它之前，说明窗口根本装不下
    for b in blocks:                 # 记住未加堵车 buffer 的原始时长/备注，便于反复重排
        b["_base_dur"] = b["dur"]
        b["_base_note"] = b["note"]

    def _layout():
        total = sum(b["dur"] for b in blocks)
        cur = end - total
        if cur < FLOOR:              # 装不下：从清晨正排，老实承认会超时（不再静默堆到 00:00）
            cur = FLOOR
        for b in blocks:
            b["_s"], b["_e"] = cur, cur + b["dur"]
            cur = b["_e"]

    def _apply_peak_buffers():
        """按当前布局判高峰、给通勤段加堵车 buffer；返回是否有改动（用于迭代到稳定）。"""
        changed = False
        for b in blocks:
            if not b.get("travel"):
                continue
            base = b["_base_dur"]
            pk = _peak_label(b["_s"])
            new_dur = int(round(base * 1.5)) if pk else base
            new_note = (f"{pk}路上堵，已多留 {new_dur - base} 分钟，建议打车并尽量错峰"
                        if pk else b["_base_note"])
            if new_dur != b["dur"] or new_note != b["note"]:
                changed = True
            b["dur"], b["note"] = new_dur, new_note
        return changed

    _layout()
    # 反复'重排→按最终位置复判高峰'，直到稳定：修掉'按旧位置判高峰、提示与时刻对不上'的瑕疵
    for _ in range(4):
        changed = _apply_peak_buffers()
        _layout()                    # 总按最新时长重排，保证位置与时长始终一致
        if not changed:
            break

    total = sum(b["dur"] for b in blocks)
    overflow = bool(blocks) and (end - total) < FLOOR
    slots = [Slot(_hm(b["_s"]), _hm(b["_e"]), b["title"], b["note"]) for b in blocks]
    if overflow:
        actual = blocks[-1]["_e"]
        slots.append(Slot(_hm(actual), _hm(actual),
                          f"⚠️ 预计 {_hm(actual)} 才到家（晚于你定的 {ret or '默认 19:30'}）",
                          f"整条行程约 {total // 60} 小时 {total % 60} 分，从清晨正排都塞不进这个窗口——"
                          f"建议精简一个环节或放宽到家时间，我没替你硬塞"))
    elif ret:
        slots.append(Slot(_hm(end), _hm(end), f"到家（按您定的：{ret}）",
                          "set_return_time 已由 A 确认，整条行程已按它倒排"))
    else:
        slots.append(Slot(_hm(end), _hm(end), "散场 / 回家（时间待确认）",
                          "set_return_time 未定，暂按 19:30 排"))
    return slots


# ---------------------------------------------------------------------------
def estimate_gmv(plan: Plan, party_size: int = 3) -> float:
    """本方案预计撬动的交易额（PRD §10：商业价值在产品里'做'出来）。"""
    total = 0.0
    act = plan.find_by_type("choose_activity")
    if act and act.chosen:
        total += (act.chosen.price or 0) * party_size
    rest = plan.find_by_type("choose_restaurant")
    if rest and rest.chosen:
        total += (rest.chosen.price or 0) * party_size
    gift = plan.find_by_type("send_gift")
    if gift and gift.chosen:
        total += gift.chosen.price or 0
    return round(total, 0)
