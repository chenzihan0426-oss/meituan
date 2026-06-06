"""图形化验收台（本地网页）——仅用 Python 标准库，零依赖、零网络同步。

两种模式（页面顶部切换）：
  ✍️ 自己输入   —— 真交互：你输一句话目标，Agent 实时解析已知/未知、给每个决定算
                  置信度×代价→做/建议/问；该问的停下来等你答、该建议的等你拍板；
                  你还能拉人进来改方案触发协同评审；确认后真的执行并跳 GMV。
  🎬 预设演示   —— 把家庭/朋友两个内置场景端到端跑给你看，左侧 9 条验收清单自动打勾。

它不是 PRD 要交付的'产品前端'（PRD §2 不做花哨前端），而是给你自己点一点验收的镜子，
跑的是同一套真实引擎。绝不搭真实多端/网络同步（仍遵守 §2/§7.3）。

用法：
    python3 -m agent.web              # 自动选空闲端口并打开浏览器
    python3 -m agent.web --port 8000
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

os.environ.setdefault("NO_COLOR", "1")

from . import amap, card, llm                                          # noqa: E402
from .acceptance_spec import DIMENSIONS, evaluate                      # noqa: E402
from .execution import Executor                                        # noqa: E402
from .intent import parse_goal                                         # noqa: E402
from .models import (Cost, Disposition, Edit, EditType, Option,        # noqa: E402
                     Participant, Status)
from .orchestrator import (AutoResponder, _apply_open_answer,          # noqa: E402
                           orchestrate)
from .planner import (_build_restaurant_decision, _build_timeline,     # noqa: E402
                      _opt_from_restaurant, build_first_plan, estimate_gmv)
from .review import (finalize_review, resolve_conflict,                # noqa: E402
                     run_review_round)
from . import prefs                                                    # noqa: E402
from .scenarios import get_scenario                                    # noqa: E402
from .tips import build_tips                                           # noqa: E402
from .tools import RESTAURANTS, ToolBox                                # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 交互会话默认开启的'异常演示'强制项（与 family 同款，让兜底看得见；可在页面关闭）
INTERACTIVE_FORCED = {
    "book_full:*": "once", "queue_eta:*": "45",
    "timeout:order_flowers": "once", "delivery_failed": "always",
}

SESSIONS: dict[str, dict] = {}
SESSION_ORDER: list[str] = []
_PORT = 0   # 实际监听端口（serve 时写入），供生成分享链接用


def _lan_ip() -> str:
    """本机局域网 IP（让朋友手机能扫/点进来）。优先真实私网段（192.168/10/172.16-31），
    避开 VPN/虚拟网卡（198.18、100.64 等）；都取不到回退 127.0.0.1（同机另开标签也能用）。"""
    import socket
    cands = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            cands.append(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # 不真正发包，只为确定出口网卡 IP
        cands.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    def _score(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            try:
                if 16 <= int(ip.split(".")[1]) <= 31:
                    return 1
            except Exception:
                pass
        if ip.startswith(("127.", "169.254.", "198.18.", "198.19.", "100.")):
            return 9     # 本地回环 / 自动专用 / VPN/CGNAT，手机多半连不上
        return 5

    cands = [c for c in cands if c and ":" not in c]
    if not cands:
        return "127.0.0.1"
    cands.sort(key=_score)
    return cands[0]


def _new_session(data: dict) -> str:
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = data
    SESSION_ORDER.append(sid)
    while len(SESSION_ORDER) > 50:        # 简单上限，防内存堆积
        old = SESSION_ORDER.pop(0)
        SESSIONS.pop(old, None)
    return sid


# ---------------------------------------------------------------------------
# 序列化（把引擎对象转成前端要的 JSON）
# ---------------------------------------------------------------------------
def _ser_opt(o: Option | None):
    if o is None:
        return None
    d = {"id": o.get("id") or o.id, "label": o.label, "kind": o.kind, "price": o.price}
    for k in ("per_capita", "child_friendly", "has_low_cal", "distance_km", "allergens",
              "needs_queue", "queue_minutes", "has_spicy_option", "near_bar_street", "tiring"):
        if k in o.data:
            d[k] = o.data[k]
    return d


def _ser_dec(d):
    return {
        "id": d.id, "type": d.type, "description": d.description,
        "confidence": round(d.confidence, 2), "confidence_basis": d.confidence_basis,
        "cost": d.cost.value, "reasoning": d.reasoning, "disposition": d.disposition.value,
        "status": d.status.value, "chosen": _ser_opt(d.chosen),
        "options": [_ser_opt(o) for o in d.options], "history": list(d.confidence_history),
        "counterfactual": getattr(d, "counterfactual", ""),
    }


def _ser_plan(p):
    return {
        "version": p.version, "gmv_estimate": p.gmv_estimate,
        "decisions": [_ser_dec(d) for d in p.decisions],
        "timeline": [{"start": s.start, "end": s.end, "title": s.title, "reason": s.reason}
                     for s in p.timeline],
        "open_questions": [d.id for d in p.open_questions],
        "tips": list(p.tips or []),
    }


def _ser_conflict(c, idx):
    return {
        "idx": idx, "target": c.target_decision,
        "competing": [{"edit_id": e.id, "author": e.author.id, "weight": e.author.weight,
                       "label": (e.after.label if e.after else None), "note": e.note}
                      for e in c.competing_edits],
        "suggested_edit_id": c.suggested_edit.id if c.suggested_edit else None,
        "suggestion": _ser_opt(c.agent_suggestion), "suggestion_owner": c.suggestion_owner(),
        "reason": c.suggestion_reason,
    }


# ---------------------------------------------------------------------------
# 交互端点逻辑
# ---------------------------------------------------------------------------
def _apply_trust(plan, constraints: dict, party: int) -> dict:
    """信任账户：按已建立的信任，① 把'反复确认过的事'直接替你定（❓→✅，错了随时撤）；
    ② 其余未拍板决定按信任程度调 做/建议/问。让'该替你决定多少'成为一段会成长的关系。"""
    from . import rules
    t = prefs.load_trust()
    score, confirms = t["score"], t["confirms"]
    level = prefs.trust_level(score)
    saved = prefs.load()
    unlocked, budget_unlocked = [], False
    _UNLOCK = {"set_budget": ("budget_per_capita", lambda v: f"人均 {int(v)} 以内"),
               "set_return_time": ("return_time", lambda v: str(v))}
    for d in plan.decisions:                       # ① 信任解锁：替你定你反复确认过的事
        if d.type in _UNLOCK and confirms.get(d.type, 0) >= 2:
            key, fmt = _UNLOCK[d.type]
            val = saved.get(key)
            if val in (None, "", []):
                continue
            d.chosen = Option(label=fmt(val), kind="value")
            d.status = Status.CONFIRMED
            d.confidence = 0.9
            d.confidence_basis = f"信任账户：你以前 {confirms[d.type]} 次都这么定"
            d.disposition = Disposition.AUTO
            d.reasoning = (f"🔓 这件事你以前确认过 {confirms[d.type]} 次（都按「{fmt(val)}」）——"
                           f"信任够了，这次我替你定了，错了随时撤。")
            if key == "budget_per_capita":
                constraints["budget_per_capita"] = int(val)
                budget_unlocked = True
            else:
                constraints["return_time"] = val
            unlocked.append({"type": d.type, "label": fmt(val)})
    if budget_unlocked:
        _reselect_restaurant(plan, constraints)
    for d in plan.decisions:                       # ② 其余按信任程度调处置
        if d.status in (Status.CONFIRMED, Status.DONE):
            continue
        d.disposition = rules.derive_disposition(d.confidence, d.cost, level)
    plan.timeline = _build_timeline(plan, constraints)
    plan.tips = build_tips(plan, constraints)
    plan.gmv_estimate = estimate_gmv(plan, party)
    plan.recompute_open_questions()
    return {"score": score, "level": level, "label": prefs.trust_label(score),
            "unlocked": unlocked}


def api_start(body: dict) -> dict:
    goal = (body.get("goal") or "").strip()
    if not goal:
        return {"ok": False, "error": "请先输入一句话目标"}
    party = max(1, min(12, int(body.get("party_size") or 3)))
    exceptions = bool(body.get("exceptions", True))
    opted_in = bool(body.get("use_llm"))
    # 解耦：意图解析要 LLM；查真实 POI 高德或 LLM 任一可用即可（只有高德 key 也能搜附近）
    parse_with_llm = opted_in and llm.is_enabled()
    want_real = opted_in and (llm.is_enabled() or amap.is_enabled())
    intent = parse_goal(goal, use_llm=parse_with_llm)
    loc = (body.get("loc") or "").strip()
    if loc:   # 用户实时定位 → 高德按真实位置搜附近
        intent.constraints["user_location"] = loc
    if parse_with_llm and intent.flags.get("party_size"):
        party = max(1, min(12, int(intent.flags["party_size"])))
    tb = ToolBox(seed=7, fast=True, use_llm=want_real,
                 forced=dict(INTERACTIVE_FORCED) if exceptions else {})
    plan = build_first_plan(intent, tb, party_size=party)
    # 拷贝放在 build_first_plan 之后：planner 会往 intent.constraints 写入'就近吃'(near_dining_*)
    # 等派生信息，复用这份才完整，否则 _reselect_restaurant 会丢掉动线理由。
    constraints = dict(intent.constraints)
    trust = _apply_trust(plan, constraints, party)   # 按信任账户调整方案（脑洞主线）
    sid = _new_session({"intent": intent, "constraints": constraints, "tb": tb,
                        "plan": plan, "party": party, "executed": False, "use_llm": want_real})
    mem = prefs.load()
    return {"ok": True, "sid": sid, "goal": goal,
            "known": intent.known, "unknown": intent.unknown,
            "party": party, "plan": _ser_plan(plan),
            "source": tb.last_source, "llm_used": want_real,
            "memory": {"prefs": mem, "summary": prefs.summary(mem)}, "trust": trust}


def api_geo(body: dict) -> dict:
    """定位：浏览器 GPS({lng,lat}) 或手动地址({address}) → 高德坐标 + 地名。"""
    if not amap.is_enabled():
        return {"ok": False, "error": "未配置高德 key，无法定位（仍可用本地库演示）"}
    try:
        addr = (body.get("address") or "").strip()
        if addr:
            loc = amap.geocode(addr)
            if not loc:
                return {"ok": False, "error": f"找不到「{addr}」，换个说法试试"}
            return {"ok": True, "loc": loc, "area": addr, "source": "manual"}
        lng, lat = body.get("lng"), body.get("lat")
        if lng is None or lat is None:
            return {"ok": False, "error": "缺少坐标"}
        r = amap.resolve_gps(lng, lat)
        return {"ok": True, "source": "gps", **r}
    except Exception as ex:  # noqa
        return {"ok": False, "error": f"定位失败：{ex}"}


def _reselect_restaurant(plan, constraints: dict) -> None:
    """用更新后的约束（如刚确认的预算）重选餐厅，把结果搬回原决定（保留 id）。

    重算会让超预算的店掉出、合预算的店升上来，reasoning 里也会写明
    '人均X，超预算'的排除理由——让推荐真正跟着你的调整走。
    """
    rest = plan.find_by_type("choose_restaurant")
    if not rest or not rest.options:
        return
    has_child = bool(constraints.get("need_child_friendly"))
    fresh = _build_restaurant_decision(list(rest.options), constraints, has_child)
    rest.chosen = fresh.chosen
    rest.confidence = fresh.confidence
    rest.confidence_basis = fresh.confidence_basis + "（已按你确认的预算重算）"
    rest.reasoning = fresh.reasoning
    rest.description = fresh.description
    rest.disposition = fresh.disposition


def api_answer(body: dict) -> dict:
    s = SESSIONS.get(body.get("sid"))
    if not s:
        return {"ok": False, "error": "会话已过期，请重新分析"}
    plan, constraints = s["plan"], s["constraints"]
    answers = body.get("answers") or {}
    suggestions = body.get("suggestions") or {}
    budget_changed = False
    confirmed_asks, accepted_sugg, confirmed_types = 0, 0, []
    for d in plan.decisions:
        if d.id in answers and d.disposition == Disposition.ASK:
            txt = str(answers[d.id]).strip()
            if txt:
                _apply_open_answer(d, txt, constraints)
                confirmed_asks += 1
                confirmed_types.append(d.type)
                if d.type == "set_budget":
                    budget_changed = True
        if d.id in suggestions and d.disposition == Disposition.SUGGEST:
            if suggestions[d.id]:
                d.status = Status.CONFIRMED
                accepted_sugg += 1
            else:
                d.chosen, d.status = None, Status.PENDING
    # 你回答预算后，按新预算重选餐厅——否则推荐还停在'预算未知'时的选择（与你的调整脱节）
    if budget_changed:
        _reselect_restaurant(plan, constraints)
    plan.recompute_open_questions()
    plan.timeline = _build_timeline(plan, constraints)
    plan.tips = build_tips(plan, constraints)
    plan.gmv_estimate = estimate_gmv(plan, s["party"])
    prefs.save(constraints)   # 记住你这次确认的预算/到家时间/忌口，下次先按这个（仍会问你）
    # 信任账户：你每确认一次它的判断，信任就长一点（下次它敢替你多定一些）
    before = prefs.load_trust()["score"]
    t = prefs.bump_trust(confirmed_asks, accepted_sugg, confirmed_types)
    return {"ok": True, "plan": _ser_plan(plan),
            "candidates": [_ser_opt(o) for o in (plan.find_by_type("choose_restaurant").options
                                                 if plan.find_by_type("choose_restaurant") else [])],
            "trust": {"score": t["score"], "delta": t["score"] - before,
                      "level": prefs.trust_level(t["score"]), "label": prefs.trust_label(t["score"])}}


def _resolve_option(after_id: str, plan) -> Option | None:
    rest = plan.find_by_type("choose_restaurant")
    if rest:
        for o in rest.options:
            if (o.get("id") or o.id) == after_id:
                return o
    for r in RESTAURANTS:
        if r["id"] == after_id:
            return _opt_from_restaurant(r)
    return None


def api_review(body: dict) -> dict:
    s = SESSIONS.get(body.get("sid"))
    if not s:
        return {"ok": False, "error": "会话已过期"}
    plan, constraints, party = s["plan"], s["constraints"], s["party"]
    raw_edits = body.get("edits") or []
    # 先把'改活动/菜系/距离/预算'这类要求拎出来，直接用高德重搜重排——评审合并通道做不了这些，
    # 以前会当'无法筛店'丢掉（比如'带小孩去游乐园'就没规划进去）。
    replan_merged = []
    rest_edits = []
    for e in raw_edits:
        txt = (e.get("constraint") or "").strip()
        if e.get("type") == "constraint" and _text_has_replan_pref(txt):
            for a in _replan_with_text(s, txt):
                replan_merged.append({"author": (e.get("author") or "我"), "constraint": a,
                                      "label": None, "merged_note": "已据此用高德重搜重排", "actionable": True})
        else:
            rest_edits.append(e)

    plan = s["plan"]                       # 重排可能换了餐厅候选，重新取
    rest = plan.find_by_type("choose_restaurant")
    cur = rest.chosen if rest else None
    edits, participants, seen = [], [], {}
    for e in rest_edits:
        name = (e.get("author") or "某人").strip() or "某人"
        weight = max(0.0, min(1.0, float(e.get("weight", 0.6))))
        if name not in seen:
            seen[name] = Participant(name, weight)
            participants.append(seen[name])
        author = seen[name]
        if e.get("type") == "constraint":
            txt = (e.get("constraint") or "").strip()
            if txt:
                edits.append(Edit(author=author, target_decision="constraint",
                                  type=EditType.ADD, constraint=txt))
        else:  # 换餐厅
            opt = _resolve_option(e.get("after_id", ""), plan)
            if opt is not None:
                edits.append(Edit(author=author, target_decision="choose_restaurant",
                                  type=EditType.REPLACE, before=cur, after=opt,
                                  note=(e.get("note") or "").strip()))
    if not edits and not replan_merged:
        return {"ok": False, "error": "没有有效改动；可直接执行，或添加至少一条改动"}
    if not edits:
        # 只有'重搜类'要求：无需评审回合，s["plan"] 已重排好，直接出新方案
        return {"ok": True, "auto_merged": replan_merged, "conflicts": [],
                "merged_plan": _ser_plan(plan)}
    rr = run_review_round(plan, participants, edits, constraints, party_size=party)
    s["rr"] = rr
    return {"ok": True,
            "auto_merged": replan_merged + [{"author": e.author.id, "constraint": e.constraint,
                             "label": (e.after.label if e.after else None),
                             "merged_note": e.merged_note, "actionable": e.actionable}
                            for e in rr.auto_merged],
            "conflicts": [_ser_conflict(c, i) for i, c in enumerate(rr.conflicts)],
            "merged_plan": _ser_plan(rr.merged_plan)}


# ---------------------------------------------------------------------------
# 协同：朋友通过分享链接（/join）看到方案、提交自己的改动；攒局人再收取合并
# ---------------------------------------------------------------------------
def api_shareurl(sid: str) -> dict:
    if sid not in SESSIONS:
        return {"ok": False, "error": "会话不存在或已过期"}
    url = f"http://{_lan_ip()}:{_PORT}/join?sid={sid}"
    return {"ok": True, "url": url}


def api_plan_view(sid: str) -> dict:
    """朋友页要的数据：目标 + 当前方案 + 可选餐厅候选。"""
    s = SESSIONS.get(sid)
    if not s:
        return {"ok": False, "error": "链接已过期，请让攒局人重新分享"}
    rest = s["plan"].find_by_type("choose_restaurant")
    cands = [_ser_opt(o) for o in (rest.options if rest else [])]
    return {"ok": True, "goal": s["intent"].raw, "party": s.get("party", 3),
            "plan": _ser_plan(s["plan"]), "candidates": cands,
            "received": len(s.get("remote_edits", []))}


def api_submit_edit(body: dict) -> dict:
    """朋友提交一条改动（换餐厅 / 加约束），进 session 的待收取队列。"""
    s = SESSIONS.get(body.get("sid"))
    if not s:
        return {"ok": False, "error": "链接已过期，请让攒局人重新分享"}
    author = (body.get("author") or "").strip()
    if not author:
        return {"ok": False, "error": "先填你的名字，让大家知道是谁改的"}
    typ = body.get("type")
    edit = {"author": author, "weight": max(0.0, min(1.0, float(body.get("weight", 0.5)))),
            "type": typ, "note": (body.get("note") or "").strip()}
    if typ == "constraint":
        c = (body.get("constraint") or "").strip()
        if not c:
            return {"ok": False, "error": "填一下你的约束，如：我对海鲜过敏"}
        edit["constraint"] = c
        label = f"加约束「{c}」"
    else:
        aid = body.get("after_id") or ""
        opt = _resolve_option(aid, s["plan"])
        if opt is None:
            return {"ok": False, "error": "请选一个餐厅"}
        edit["after_id"] = aid
        edit["_label"] = opt.label
        label = f"换餐厅 → {opt.label}"
    s.setdefault("remote_edits", []).append(edit)
    return {"ok": True, "label": label, "received": len(s["remote_edits"])}


def api_edits(sid: str) -> dict:
    """攒局人收取朋友提交的改动。"""
    s = SESSIONS.get(sid)
    if not s:
        return {"ok": False, "error": "会话已过期"}
    return {"ok": True, "edits": list(s.get("remote_edits", []))}


def _map_spots(s: dict) -> list:
    """按游玩顺序的坐标点 [(loc, 顺序号), ...]：家(1) → 玩(2) → 吃(3)，只取有真实坐标的。"""
    cons = s.get("constraints", {})
    ordered = [amap.search_center(cons)]
    plan = s["plan"]
    for act in [d for d in plan.decisions if d.type.startswith("choose_activity") and d.chosen]:
        if act.chosen.get("location"):
            ordered.append(act.chosen.get("location"))
    rest = plan.find_by_type("choose_restaurant")
    if rest and rest.chosen and rest.chosen.get("location"):
        ordered.append(rest.chosen.get("location"))
    return [(loc, str(i + 1)) for i, loc in enumerate(ordered)]   # 路线上标 1→2→3 顺序


def api_map(sid: str):
    """返回'家→玩→吃'真实路线静态地图 PNG（bytes）；不可用时返回 None。"""
    s = SESSIONS.get(sid)
    if not s or not amap.is_enabled():
        return None
    try:
        return amap.staticmap_png(_map_spots(s))
    except Exception:
        return None


def api_forget(body: dict) -> dict:
    """忘记我的偏好（清空跨会话记忆）。"""
    prefs.clear()
    return {"ok": True}


def api_autonomy(body: dict) -> dict:
    """自主性旋钮：按 保守/平衡/大胆 重算每个决定的 做/建议/问（不重跑搜索）。"""
    s = SESSIONS.get(body.get("sid"))
    if not s:
        return {"ok": False, "error": "会话已过期"}
    level = body.get("level", "balanced")
    if level not in ("conservative", "balanced", "bold"):
        level = "balanced"
    from . import rules
    plan = s["plan"]
    for d in plan.decisions:
        if d.status in (Status.CONFIRMED, Status.DONE):
            continue   # 你已拍板的不动
        d.disposition = rules.derive_disposition(d.confidence, d.cost, level)
    plan.recompute_open_questions()
    s["autonomy"] = level
    return {"ok": True, "level": level, "plan": _ser_plan(plan)}


def api_resolve(body: dict) -> dict:
    s = SESSIONS.get(body.get("sid"))
    if not s or "rr" not in s:
        return {"ok": False, "error": "请先运行评审"}
    rr, party = s["rr"], s["party"]
    res = body.get("resolutions") or {}
    for i, c in enumerate(rr.conflicts):
        choice = res.get(str(i), "suggestion")
        if choice == "suggestion":
            resolve_conflict(c, c.suggested_edit)
        elif choice == "none":
            resolve_conflict(c, None)
        else:
            pick = next((e for e in c.competing_edits if e.id == choice), c.suggested_edit)
            resolve_conflict(c, pick)
    final = finalize_review(rr, party_size=party)
    s["plan"] = final
    s["constraints"] = getattr(rr, "_constraints", s["constraints"])
    return {"ok": True, "plan": _ser_plan(final)}


def _text_has_replan_pref(text: str) -> bool:
    """这句要求是否包含'需要重搜重排'的偏好（改活动/菜系/距离/预算）——
    用于让协同评审通道也把这类要求交给 _replan_with_text，而不是当'无法筛店'丢掉。
    注意：过敏/辣度不算（仍交给评审回合处理，保住'守过敏/带权重撞车'那套）。"""
    from .intent import _scan_activity, CUISINE_PREF
    if not text:
        return False
    cat, raw = _scan_activity(text)
    if cat or raw:
        return True
    if any(k in text for kws in CUISINE_PREF.values() for k in kws):
        return True
    if any(k in text for k in ("近一点", "别太远", "近点", "离家近", "远点也行", "可以远", "远一些")):
        return True
    if re.search(r"人均\s*\d+", text) or any(k in text for k in ("便宜", "实惠", "省点", "高档", "上档次", "精致", "好一点")):
        return True
    return False


def _replan_with_text(s: dict, text: str) -> list:
    """把一句自由要求识别成改活动/菜系/距离/预算/过敏/辣度，用会话的高德 ToolBox
    真正重搜重排。返回已执行的人话清单（空 = 没听出可执行偏好）。被 refine 与评审通道共用。"""
    from . import rules
    from .intent import _scan_activity, all_activity_cats, ACTIVITY_PREF_LABEL, positive_cuisine
    from .planner import _pick_activity, _opt_from_activity, _mk
    from .review import KNOWN_ALLERGENS

    plan, tb, c, party = s["plan"], s["tb"], s["constraints"], s["party"]
    applied, re_activity, re_restaurant = [], False, False

    new_acts = all_activity_cats(text)                   # 1) 改活动（可一次多个；累加不覆盖）
    if not new_acts:
        _, raw = _scan_activity(text)
        if raw:
            new_acts = ["raw:" + raw]
    if new_acts:
        lst = c.get("activity_cats")
        if lst is None:                                  # 首次：以原有活动为底，再往上加
            lst = []
            if c.get("activity_pref"):
                lst.append(c["activity_pref"])
            elif c.get("activity_raw"):
                lst.append("raw:" + c["activity_raw"])
        added = []
        for a in new_acts:
            if a not in lst:
                lst.append(a)
                added.append(a[4:] if a.startswith("raw:") else ACTIVITY_PREF_LABEL.get(a, a))
        c["activity_cats"] = lst
        if added:
            applied.append("活动加上：" + "、".join(added))
        re_activity = True
    cui = positive_cuisine(text)                          # 2) 改菜系（否定语境如'不吃海鲜'不算）
    if cui:
        c["cuisine_pref"] = cui; c.pop("near_dining_loc", None); c.pop("near_dining_label", None)
        applied.append(f"口味改为「{cui}」（点名了菜系，可专程去吃）"); re_restaurant = True
    if any(k in text for k in ("近一点", "别太远", "近点", "离家近")):   # 3) 改距离
        c["max_distance_km"] = min(c.get("max_distance_km") or 10, 5.0); applied.append("更就近找"); re_restaurant = True
    elif any(k in text for k in ("远点也行", "可以远", "远一些")):
        c["max_distance_km"] = max(c.get("max_distance_km") or 10, 20.0); applied.append("放宽距离"); re_restaurant = True
    m = re.search(r"人均\s*(\d+)", text)                   # 4) 改预算
    if m:
        c["budget_per_capita"] = int(m.group(1)); applied.append(f"预算调到人均 {m.group(1)}"); re_restaurant = True
    elif any(k in text for k in ("便宜", "实惠", "省点")):
        c["budget_per_capita"] = min(c.get("budget_per_capita") or 100, 80); applied.append("往便宜里挑"); re_restaurant = True
    elif any(k in text for k in ("高档", "上档次", "精致", "好一点")):
        c["budget_per_capita"] = max(c.get("budget_per_capita") or 100, 250); applied.append("往高档里挑"); re_restaurant = True
    al = next((a for a in KNOWN_ALLERGENS if a in text), None)   # 5) 过敏（要有'过敏/忌/不吃'语境，'想吃海鲜'不算）
    if al and any(w in text for w in ("过敏", "忌口", "忌", "不吃", "不能吃", "避开")):
        c.setdefault("allergens", [])
        if al not in c["allergens"]:
            c["allergens"].append(al)
        applied.append(f"避开{al}"); re_restaurant = True
    if any(k in text for k in ("怕辣", "清淡", "不想吃辣", "不吃辣", "少辣", "不辣")):   # 6) 辣度
        c["avoid_spicy"] = True; c.pop("need_spicy", None); applied.append("偏清淡"); re_restaurant = True
    elif any(k in text for k in ("想吃辣", "爱吃辣", "重辣", "无辣不欢", "重口味")):
        c["need_spicy"] = True; c.pop("avoid_spicy", None); applied.append("要够辣"); re_restaurant = True

    if not applied:
        return []

    tb.context = {"goal": s["intent"].raw, "constraints": c}   # 让搜索用最新约束
    has_child = bool(c.get("need_child_friendly"))

    if re_activity:                                       # 按 activity_cats 重建全部活动决定（多活动并存）
        plan.decisions = [d for d in plan.decisions if not d.type.startswith("choose_activity")]
        insert_idx = next((i for i, d in enumerate(plan.decisions) if d.type == "choose_restaurant"), len(plan.decisions))
        last_loc = last_label = None
        for n, item in enumerate(c.get("activity_cats", [])):
            if item.startswith("raw:"):
                c.pop("activity_pref", None); c["activity_raw"] = item[4:]
            else:
                c["activity_pref"] = item; c.pop("activity_raw", None)
            try:
                opts = [_opt_from_activity(a) for a in tb.search_activities()]
                chosen_act, conf, basis, reason = _pick_activity(opts, c, has_child)
            except Exception:
                continue
            typ = "choose_activity" if n == 0 else f"choose_activity_{n + 1}"
            desc = "选活动" if n == 0 else f"选活动 #{n + 1}"
            plan.decisions.insert(insert_idx, _mk(typ, desc, options=opts, chosen=chosen_act,
                                                  confidence=conf, basis=basis, cost=Cost.LOW, reasoning=reason))
            insert_idx += 1
            if chosen_act is not None and chosen_act.get("location"):
                last_loc, last_label = chosen_act.get("location"), chosen_act.label
        c.pop("activity_pref", None); c.pop("activity_raw", None)   # 临时键清掉，以 activity_cats 为准
        if last_loc and not c.get("cuisine_pref"):       # 吃饭锚到最后一个活动
            c["near_dining_loc"], c["near_dining_label"] = last_loc, last_label
        re_restaurant = True

    if re_restaurant:                                     # 重搜餐厅（高德）
        try:
            rests = tb.search_restaurants(child_friendly=c.get("need_child_friendly"),
                                          low_cal=c.get("need_low_cal"),
                                          exclude_allergens=c.get("allergens"))
            fresh = _build_restaurant_decision([_opt_from_restaurant(r) for r in rests], c, has_child)
            dec = plan.find_by_type("choose_restaurant")
            if dec is not None:
                dec.options, dec.chosen = fresh.options, fresh.chosen
                dec.confidence, dec.confidence_basis = fresh.confidence, fresh.confidence_basis
                dec.reasoning, dec.description, dec.disposition = fresh.reasoning, fresh.description, fresh.disposition
        except Exception:
            pass

    plan.version += 1
    plan.recompute_open_questions()
    plan.timeline = _build_timeline(plan, c)
    plan.tips = build_tips(plan, c)
    plan.gmv_estimate = estimate_gmv(plan, party)
    return applied


def api_refine(body: dict) -> dict:
    """'我来加个要求'：识别成改活动/菜系/距离/预算/过敏/辣度，用高德重搜重排。"""
    s = SESSIONS.get(body.get("sid"))
    if not s:
        return {"ok": False, "error": "会话已过期"}
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "先写一句你的新要求"}
    applied = _replan_with_text(s, text)
    if not applied:
        return {"ok": False, "error": "没听出可执行的偏好——试试'想去唱K / 想吃火锅 / 便宜点 / 我对花生过敏'"}
    return {"ok": True, "applied": applied, "plan": _ser_plan(s["plan"]), "source": s["tb"].last_source}


def api_execute(body: dict) -> dict:
    s = SESSIONS.get(body.get("sid"))
    if not s:
        return {"ok": False, "error": "会话已过期"}
    if s.get("executed"):
        return {"ok": False, "error": "本会话已执行过；请重新分析以再次执行"}
    plan, tb, constraints = s["plan"], s["tb"], s["constraints"]
    send_to = "群里" if s["party"] > 3 else "对方"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            Executor(plan, tb, constraints, party_size=s["party"], send_to=send_to).run()
    except Exception as ex:  # noqa  顶层安全网
        print(f"\n⚠️ 执行中遇到未预料的问题：{ex}", file=buf)
    s["executed"] = True
    text = buf.getvalue()
    gmv_trace = [int(x) for x in re.findall(r"GMV ¥(\d+)", text)]
    # 生成"递给老婆/发小张"的移动端可分享方案卡
    try:
        s["card_html"] = card.render_plan_card(
            plan, send_to=("小张" if s["party"] <= 3 else "群里"), gmv=tb.gmv)
    except Exception:  # noqa
        s["card_html"] = None
    return {"ok": True, "text": text, "gmv": tb.gmv, "gmv_trace": gmv_trace,
            "orders": len(tb.ledger), "has_card": bool(s.get("card_html")),
            "business": _business_summary(plan, tb, s["party"])}


def _business_summary(plan, tb, party: int) -> dict:
    """从真实下单算"连带账单"：一句话撬动几个 SKU、客单多少、相对单点外卖翻几倍。

    用的是**美团天天在测、可优化**的标准指标(连带率/客单价/留存)，不是拍脑袋的假数。
    """
    items = []
    for act in [d for d in plan.decisions if d.type.startswith("choose_activity") and d.chosen]:
        items.append({"label": f"活动/门票（{act.chosen.label}）", "amt": round((act.chosen.price or 0) * party)})
    rest = plan.find_by_type("choose_restaurant")
    if rest and rest.chosen:
        items.append({"label": "餐厅", "amt": round((rest.chosen.price or 0) * party)})
    gift = plan.find_by_type("send_gift")
    if gift and gift.chosen:
        items.append({"label": "礼物", "amt": round(gift.chosen.price or 0)})
    # 商业面板讲'一句话撬动了几个 SKU 的连带'，用撬动总额(与分项一致)；
    # tb.gmv 是执行后(含演示回滚)的真实值，归到'异常兜底'那条故事，不在这里混淆。
    gmv = sum(it["amt"] for it in items)
    base = 50   # 单点一份外卖的示意基线（标清楚是示意）
    return {"items": items, "sku_count": len(items), "gmv": gmv,
            "per_capita": round(gmv / max(1, party)), "baseline": base,
            "multiple": round(gmv / base, 1) if gmv else 0,
            "party": party, "trust": prefs.load_trust()["score"]}


# ---------------------------------------------------------------------------
# 预设演示（保留）
# ---------------------------------------------------------------------------
class WebResponder(AutoResponder):
    def __init__(self, scenario, conflict_mode: str = "suggestion"):
        super().__init__(scenario)
        self.conflict_mode = conflict_mode

    def choose_conflict(self, conflict):
        if self.conflict_mode == "alt":
            for e in conflict.competing_edits:
                if e is not conflict.suggested_edit:
                    print(f"   👤 A 裁决：选另一方（{e.author.id}）")
                    return e
        return super().choose_conflict(conflict)


_CACHE: dict[tuple, str] = {}


def _transcript(scenario_name: str, conflict_mode: str, loc: str = None) -> str:
    key = (scenario_name, conflict_mode, loc)
    if key not in _CACHE:
        scn = get_scenario(scenario_name)
        # 自动演示也接真高德：有 key 就用真实 POI（带缓存→仍可复现），无 key 自动走本地库
        real_data = amap.is_enabled() or llm.is_enabled()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            orchestrate(scn, WebResponder(scn, conflict_mode), fast=True,
                        use_llm=real_data, user_location=loc)
        _CACHE[key] = buf.getvalue()
    return _CACHE[key]


def run_scenario(scenario_name: str, conflict_mode: str, loc: str = None) -> dict:
    text = _transcript(scenario_name, conflict_mode, loc)
    gmv_trace = [int(x) for x in re.findall(r"GMV ¥(\d+)", text)]
    final = re.findall(r"实际撬动 GMV：¥(\d+)", text)
    gmv = int(final[-1]) if final else (gmv_trace[-1] if gmv_trace else 0)
    # 验收清单始终用'家'默认位置跑，保证 9 条核验离线确定（与用户当前位置解耦）
    family_text = _transcript("family", conflict_mode)
    friends_text = _transcript("friends", conflict_mode)
    checklist = evaluate(family_text, "family") + evaluate(friends_text, "friends")
    return {"ok": True, "scenario": scenario_name, "conflict": conflict_mode,
            "text": text, "gmv": gmv, "gmv_trace": gmv_trace, "checklist": checklist,
            "dimensions": [{"name": n, "how": h} for n, h in DIMENSIONS]}


def run_selftest() -> dict:
    try:
        p = subprocess.run([sys.executable, "-m", "agent.selftest"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        out = p.stdout + p.stderr
        m = re.search(r"自检结果：(\d+)/(\d+)", out)
        passed, total = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        tail = "\n".join(out.strip().splitlines()[-6:])
        return {"ok": p.returncode == 0, "passed": passed, "total": total, "tail": tail}
    except Exception as ex:  # noqa
        return {"ok": False, "passed": 0, "total": 0, "tail": str(ex)}


# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 禁缓存：避免浏览器拿旧页面/旧结果，改了代码刷新就一定是新的
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif u.path == "/join":
            self._send(200, JOIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif u.path == "/api/shareurl":
            self._json(api_shareurl(parse_qs(u.query).get("sid", [""])[0]))
        elif u.path == "/api/plan":
            self._json(api_plan_view(parse_qs(u.query).get("sid", [""])[0]))
        elif u.path == "/api/edits":
            self._json(api_edits(parse_qs(u.query).get("sid", [""])[0]))
        elif u.path == "/api/map":
            png = api_map(parse_qs(u.query).get("sid", [""])[0])
            if png:
                self._send(200, png, "image/png")
            else:
                self._send(404, b"no map", "text/plain; charset=utf-8")
        elif u.path == "/api/run":
            q = parse_qs(u.query)
            scn = q.get("scenario", ["family"])[0]
            cm = q.get("conflict", ["suggestion"])[0]
            scn = scn if scn in ("family", "friends") else "family"
            loc = (q.get("loc", [""])[0] or "").strip() or None
            try:
                self._json(run_scenario(scn, cm, loc))
            except Exception as ex:  # noqa
                self._json({"ok": False, "error": str(ex)})
        elif u.path == "/api/selftest":
            self._json(run_selftest())
        elif u.path == "/api/llm_status":
            from . import amap
            self._json({**llm.status(), "amap": amap.status()})
        elif u.path == "/api/card":
            q = parse_qs(u.query)
            s = SESSIONS.get(q.get("sid", [""])[0])
            html = (s or {}).get("card_html")
            if html:
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, "方案卡未生成（请先执行）".encode("utf-8"),
                           "text/plain; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        handlers = {"/api/start": api_start, "/api/answer": api_answer,
                    "/api/review": api_review, "/api/resolve": api_resolve,
                    "/api/refine": api_refine, "/api/execute": api_execute,
                    "/api/geo": api_geo, "/api/submit_edit": api_submit_edit,
                    "/api/autonomy": api_autonomy, "/api/forget": api_forget}
        fn = handlers.get(u.path)
        if not fn:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            self._json(fn(self._read_body()))
        except Exception as ex:  # noqa
            self._json({"ok": False, "error": f"{type(ex).__name__}: {ex}"})


def serve(port: int = 0, open_browser: bool = True):
    global _PORT
    # 绑 0.0.0.0：本机用 127.0.0.1 访问，朋友的手机用同一 WiFi 的局域网 IP 访问（分享链接）
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    actual = httpd.server_address[1]
    _PORT = actual
    url = f"http://127.0.0.1:{actual}/"
    print(f"✅ 验收台已启动：{url}")
    print(f"   局域网内（朋友手机同 WiFi）可访问：http://{_lan_ip()}:{actual}/")
    print("   按 Ctrl+C 退出。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
        httpd.shutdown()


def main(argv=None):
    ap = argparse.ArgumentParser(description="活动规划 Agent · 图形化验收台")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)
    serve(args.port, open_browser=not args.no_open)


# ===========================================================================
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DecisionMate · 拎得清的本地活动 Agent</title>
  <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: { sans: ['"Inter"', '-apple-system', 'BlinkMacSystemFont', '"PingFang SC"', 'sans-serif'] },
          colors: { brand: { 50: '#fff1f2', 100: '#ffe4e6', 400: '#fb7185', 500: '#f43f5e', 600: '#e11d48', 900: '#881337' } },
          animation: { 'blob': 'blob 10s infinite', 'shimmer': 'shimmer 2.5s linear infinite', 'float': 'float 6s ease-in-out infinite', 'fade-in-up': 'fadeInUp .5s ease both' },
          keyframes: {
            blob: { '0%, 100%': { transform: 'translate(0px, 0px) scale(1)' }, '33%': { transform: 'translate(30px, -50px) scale(1.1)' }, '66%': { transform: 'translate(-20px, 20px) scale(0.95)' } },
            shimmer: { 'from': { backgroundPosition: '200% 0' }, 'to': { backgroundPosition: '-200% 0' } },
            float: { '0%, 100%': { transform: 'translateY(0)' }, '50%': { transform: 'translateY(-10px)' } },
            fadeInUp: { 'from': { opacity: 0, transform: 'translateY(8px)' }, 'to': { opacity: 1, transform: 'translateY(0)' } }
          },
          boxShadow: { 'premium': '0 10px 40px -10px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.03)', 'premium-hover': '0 20px 40px -10px rgba(0,0,0,0.12), 0 1px 3px rgba(0,0,0,0.03)', 'glow-rose': '0 0 20px rgba(244, 63, 94, 0.4)' }
        }
      }
    }
  </script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
    body { background-color: #fafafa; -webkit-font-smoothing: antialiased; }
    [v-cloak] { display: none; }
    .fade-step-enter-active, .fade-step-leave-active { transition: all 0.6s cubic-bezier(0.2, 0.8, 0.2, 1); }
    .fade-step-enter-from { opacity: 0; transform: translateY(20px) scale(0.98); }
    .fade-step-leave-to { opacity: 0; transform: translateY(-20px) scale(0.98); position: absolute; width: 100%; }
    .list-enter-active, .list-leave-active { transition: all 0.5s ease; }
    .list-enter-from, .list-leave-to { opacity: 0; transform: translateX(-15px); }
    .no-scrollbar::-webkit-scrollbar { display: none; }
    .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
    .glass-nav { background: rgba(255, 255, 255, 0.6); backdrop-filter: blur(24px) saturate(180%); -webkit-backdrop-filter: blur(24px) saturate(180%); border-bottom: 1px solid rgba(255, 255, 255, 0.5); }
    .text-gradient-animated { background: linear-gradient(to right, #f43f5e, #f97316, #f43f5e); background-size: 200% auto; color: transparent; -webkit-background-clip: text; animation: shimmer 3s linear infinite; }
  </style>
</head>

<body class="text-slate-800 relative overflow-x-hidden">
  <div class="fixed inset-0 overflow-hidden pointer-events-none -z-10">
    <div class="absolute top-[-10%] left-[-10%] w-96 h-96 bg-rose-200/40 rounded-full mix-blend-multiply filter blur-[80px] animate-blob"></div>
    <div class="absolute top-[-10%] right-[-10%] w-96 h-96 bg-orange-200/40 rounded-full mix-blend-multiply filter blur-[80px] animate-blob" style="animation-delay: 2s"></div>
    <div class="absolute bottom-[-20%] left-[20%] w-[500px] h-[500px] bg-pink-200/30 rounded-full mix-blend-multiply filter blur-[100px] animate-blob" style="animation-delay: 4s"></div>
  </div>

  <div id="app" v-cloak class="min-h-screen flex flex-col relative">

    <header class="fixed top-0 w-full z-50 glass-nav transition-all duration-300">
      <div class="max-w-5xl mx-auto px-6 h-16 flex items-center justify-between">
        <div class="flex items-center gap-3">
          <div class="w-8 h-8 rounded-full bg-slate-900 text-white flex items-center justify-center text-sm font-black shadow-lg">AI</div>
          <span class="font-bold tracking-tight text-[15px]">Decision<span class="text-slate-400 font-normal">Mate</span></span>
        </div>
        <div class="hidden md:flex bg-slate-200/50 p-1 rounded-full border border-white/50 backdrop-blur-md">
          <button @click="activeTab = 'I'" :class="['px-5 py-1.5 rounded-full text-[13px] font-semibold transition-all duration-300', activeTab === 'I' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-800']">交互模式</button>
          <button @click="activeTab = 'D'" :class="['px-5 py-1.5 rounded-full text-[13px] font-semibold transition-all duration-300', activeTab === 'D' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-800']">演示验收</button>
        </div>
        <div class="flex items-center gap-5">
          <div v-if="trust" class="hidden sm:flex flex-col items-end justify-center" title="信任账户：你每确认它一次，它下次敢替你多定一点">
            <div class="text-[10px] font-bold text-slate-400 uppercase tracking-widest leading-none mb-1">🤝 信任 {{ trust.label || '' }}</div>
            <div class="font-black text-sm leading-none tracking-tighter text-slate-900">{{ trust.score }}<span class="text-slate-400 font-normal text-[11px]">/100</span></div>
          </div>
          <div class="flex flex-col items-end justify-center">
            <div class="text-[10px] font-bold text-slate-400 uppercase tracking-widest leading-none mb-1">撬动 GMV</div>
            <div class="font-black text-lg leading-none tracking-tighter text-slate-900">¥{{ Math.round(gmv).toLocaleString() }}</div>
          </div>
        </div>
      </div>
    </header>

    <main class="flex-1 w-full max-w-3xl mx-auto px-6 pt-32 pb-40 relative">

      <div v-show="activeTab === 'I'" class="relative">

        <div class="flex items-center gap-2 mb-12">
          <template v-for="(step, index) in steps" :key="index">
            <div @click="goto(index)" :class="['h-1 rounded-full transition-all duration-500 cursor-pointer', currentStep === index ? 'w-12 bg-slate-900' : index < maxReached ? 'w-4 bg-slate-300 hover:bg-slate-400' : 'w-4 bg-slate-200 opacity-50 pointer-events-none']"></div>
          </template>
          <span class="ml-3 text-[11px] font-bold uppercase tracking-widest text-slate-400">{{ steps[currentStep] }}</span>
        </div>

        <div class="relative min-h-[400px]">
          <transition name="fade-step">

            <!-- 步骤 1: 输入 -->
            <div v-if="currentStep === 0" class="absolute w-full top-0 left-0">
              <div class="mb-12">
                <h1 class="text-4xl md:text-5xl font-extrabold tracking-tight text-slate-900 leading-[1.1] mb-6">
                  一句话，<br><span class="text-gradient-animated">把这一局安排明白。</span>
                </h1>
                <p class="text-slate-500 text-lg leading-relaxed max-w-xl font-light">
                  一个<b class="font-semibold text-slate-700">拎得清</b>的 AI：能办的直接办，拿不准的给建议，<b class="font-semibold text-slate-700">只有它真不知道的才停下来问你</b>。绝不丢一堆链接让你自己挑。
                </p>
              </div>

              <div class="relative group">
                <div class="absolute -inset-1 bg-gradient-to-r from-rose-400 via-orange-400 to-rose-400 rounded-[2rem] blur opacity-20 group-hover:opacity-40 transition duration-1000 group-hover:duration-200"></div>
                <div class="relative bg-white/80 backdrop-blur-xl border border-white/60 rounded-[2rem] shadow-premium p-2">
                  <textarea v-model="form.goal" rows="3" class="w-full bg-transparent p-6 text-lg text-slate-800 placeholder-slate-300 outline-none resize-none font-medium leading-relaxed" placeholder="描述你的想法，例如：今天下午想和老婆孩子出去玩几小时，别离家太远，老婆在减肥..."></textarea>
                  <div class="flex items-center justify-between p-2 mt-2 bg-slate-50/50 rounded-2xl border border-slate-100">
                    <div class="flex gap-4 px-4">
                      <label class="flex items-center gap-2 text-[13px] font-semibold text-slate-600">
                        <span class="text-slate-400">👥</span>
                        <input v-model.number="form.partySize" type="number" min="1" max="12" class="w-8 bg-transparent border-b border-slate-300 focus:border-rose-500 outline-none text-center font-bold text-slate-900">
                      </label>
                      <div class="w-px h-4 bg-slate-200 my-auto"></div>
                      <label class="flex items-center gap-2 text-[13px] font-semibold text-slate-600 cursor-pointer hover:text-slate-900 transition" title="勾选后用高德真实 POI（需配置 key），否则用内置示例库">
                        <input v-model="form.useLlm" type="checkbox" class="accent-slate-900 w-4 h-4 cursor-pointer"> 真实地理数据
                      </label>
                      <div class="w-px h-4 bg-slate-200 my-auto"></div>
                      <label class="flex items-center gap-2 text-[13px] font-semibold text-slate-600 cursor-pointer hover:text-slate-900 transition" title="开启后执行阶段会演示满座/超时/配送失败回滚等异常兜底">
                        <input v-model="form.exceptions" type="checkbox" class="accent-slate-900 w-4 h-4 cursor-pointer"> 异常演练
                      </label>
                    </div>
                    <button @click="startPlan" :disabled="isAnalyzing" class="bg-slate-900 text-white font-bold py-3 px-8 rounded-xl shadow-lg hover:bg-rose-500 hover:shadow-glow-rose hover:-translate-y-0.5 transition-all duration-300 disabled:opacity-50 disabled:hover:transform-none">
                      <span v-if="isAnalyzing" class="flex items-center gap-2">
                        <svg class="animate-spin h-4 w-4 text-white" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
                        思考中...
                      </span>
                      <span v-else>生成方案 ✨</span>
                    </button>
                  </div>
                </div>
              </div>

              <!-- 真实定位条 -->
              <div class="mt-5 flex items-center gap-2 flex-wrap">
                <span :class="['text-[13px] font-semibold px-1', locOk ? 'text-emerald-600' : 'text-slate-500']">{{ locStatus }}</span>
                <button @click="detectLoc" class="text-[12px] font-bold text-slate-500 hover:text-rose-500 bg-white border border-slate-200 rounded-full px-3 py-1 transition active:scale-95">↻ 重新定位</button>
                <div class="flex items-center gap-1 ml-auto">
                  <input v-model="addrInput" @keyup.enter="lockAddr" placeholder="或手动输入地点，如 国贸" class="text-[12px] bg-white border border-slate-200 rounded-full px-4 py-1.5 outline-none focus:border-rose-300 w-44">
                  <button @click="lockAddr" class="text-[12px] font-bold text-white bg-slate-900 hover:bg-rose-500 rounded-full px-4 py-1.5 transition active:scale-95">锁定</button>
                </div>
              </div>

              <div v-if="errorMsg" class="mt-4 text-sm text-rose-500 font-semibold pl-2">{{ errorMsg }}</div>

              <div v-if="memorySummary" class="mt-6 bg-indigo-50/70 border border-indigo-100 rounded-2xl px-5 py-3 text-[13px] text-indigo-700 font-medium flex items-center justify-between">
                <span>🧠 我还记得你：{{ memorySummary }}</span>
                <button @click="forgetMe" class="text-indigo-400 hover:text-rose-500 text-xs font-bold">忘记我</button>
              </div>

              <div class="mt-8">
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-widest block mb-3 pl-2">试试这些灵感</span>
                <div class="flex flex-wrap gap-2">
                  <button v-for="eg in examples" @click="form.goal = eg" class="bg-white/60 backdrop-blur-sm border border-slate-200 hover:border-rose-300 text-slate-600 hover:text-rose-600 rounded-full px-5 py-2 text-[13px] font-medium shadow-sm hover:shadow transition-all active:scale-95">{{ eg }}</button>
                </div>
              </div>
            </div>

            <!-- 步骤 2: 方案拍板 -->
            <div v-else-if="currentStep === 1 && planData" class="absolute w-full top-0 left-0 pb-32">
              <div class="mb-10">
                <h2 class="text-3xl font-extrabold tracking-tight text-slate-900 mb-2">我做好了决定，请过目。</h2>
                <p class="text-slate-500 text-[15px]">我替你处理了 <b class="text-emerald-600 font-semibold">{{ countDispoAll('auto') }}</b> 件、给了 <b class="text-amber-600 font-semibold">{{ countDispoAll('suggest') }}</b> 条建议，只回头问你 <b class="text-rose-500 font-semibold">{{ countDispoAll('ask') }}</b> 件——这几件只有你知道，我不替你瞎猜。</p>
              </div>

              <div class="space-y-6">
                <transition-group name="list">
                  <div v-for="(dec, idx) in planData.decisions" :key="dec.id" :class="['relative transition-all duration-500 rounded-3xl p-6 md:p-8', isResolved(dec.status) ? 'bg-transparent border border-slate-200 opacity-60' : getCardStyle(dec.disposition)]" :style="{ transitionDelay: `${idx * 80}ms` }">
                    <div class="flex items-start justify-between gap-4 mb-4">
                      <div class="flex items-center gap-4">
                        <div :class="['w-10 h-10 rounded-2xl flex items-center justify-center text-xl shrink-0', getIconBg(dec.disposition, dec.status)]">{{ getIcon(dec.type) }}</div>
                        <h3 :class="['text-lg font-bold tracking-tight', isResolved(dec.status) ? 'text-slate-500' : 'text-slate-900']">{{ dec.description }}</h3>
                      </div>
                      <div class="shrink-0" v-html="getMinimalBadge(dec.disposition, dec.status)"></div>
                    </div>

                    <div v-if="dec.chosen" class="pl-14">
                      <div class="inline-flex items-center gap-2 bg-slate-100/80 px-4 py-2 rounded-xl text-[14px] text-slate-800 font-semibold">
                        <span class="text-emerald-500 text-lg leading-none">✓</span> {{ dec.chosen.label }}
                      </div>
                    </div>

                    <div v-if="dec.options && dec.options.length && !isResolved(dec.status) && dec.type === 'choose_restaurant'" class="pl-14 mt-4">
                      <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-3">筛选候选池</div>
                      <div class="flex flex-wrap gap-2">
                        <span v-for="opt in dec.options.slice(0,4)" :key="opt.id" class="text-[12px] px-4 py-2 rounded-xl border border-slate-200 bg-white text-slate-600 font-medium hover:border-slate-300 transition-colors cursor-default shadow-sm">
                          {{ opt.label }} <span v-if="opt.per_capita" class="text-slate-400 ml-1">¥{{Math.round(opt.per_capita)}}</span>
                        </span>
                      </div>
                    </div>

                    <div v-if="!isResolved(dec.status)" class="pl-14 mt-6">
                      <div v-if="dec.disposition === 'ask'" class="space-y-3">
                        <div class="flex flex-wrap gap-2">
                          <button v-for="opt in askOptions(dec.type)" :key="opt" @click="pickAsk(dec.id, opt)"
                                  :class="['px-4 py-2.5 rounded-xl text-[14px] font-semibold border transition active:scale-95', answers[dec.id] === opt ? 'bg-rose-500 text-white border-rose-500 shadow-sm' : 'bg-white text-slate-600 border-slate-200 hover:border-rose-300']">
                            {{ opt }}
                          </button>
                          <button v-if="askOptions(dec.type).length" @click="pickCustom(dec)"
                                  :class="['px-4 py-2.5 rounded-xl text-[14px] font-semibold border transition active:scale-95', customAsk[dec.id] ? 'bg-slate-900 text-white border-slate-900' : 'bg-white text-slate-500 border-slate-200 hover:border-slate-300']">
                            ✏️ 自己填
                          </button>
                        </div>
                        <div v-if="customAsk[dec.id] || !askOptions(dec.type).length" class="relative animate-float">
                          <div class="absolute inset-y-0 left-4 flex items-center pointer-events-none"><span class="text-rose-500 text-lg">✦</span></div>
                          <input v-model="answers[dec.id]" @keyup.enter="submitAnswers" class="w-full bg-white/80 backdrop-blur border-2 border-rose-200 focus:border-rose-500 focus:ring-4 focus:ring-rose-500/10 rounded-2xl pl-12 pr-6 py-3.5 text-[15px] font-medium text-slate-900 outline-none transition-all shadow-sm placeholder-rose-300/80" placeholder="自己写：如 人均150内 / 18:00前到家">
                        </div>
                      </div>
                      <label v-if="dec.disposition === 'suggest'" class="group flex items-center gap-4 cursor-pointer">
                        <div class="relative flex items-center justify-center w-6 h-6">
                          <input type="checkbox" v-model="suggestions[dec.id]" class="peer appearance-none w-6 h-6 border-2 border-amber-300 rounded-lg checked:bg-amber-500 checked:border-amber-500 transition-all cursor-pointer">
                          <svg class="absolute text-white pointer-events-none opacity-0 peer-checked:opacity-100 w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"></path></svg>
                        </div>
                        <span class="text-[15px] font-bold text-amber-800 group-hover:text-amber-600 transition-colors">完美，就按这个办</span>
                      </label>
                    </div>

                    <div class="pl-14 mt-6">
                      <details class="group cursor-pointer">
                        <summary class="text-[12px] font-semibold text-slate-400 hover:text-slate-600 list-none flex items-center gap-1 transition-colors">
                          <span class="text-xs transition-transform group-open:rotate-90">▶</span> 查看 AI 思考路径
                        </summary>
                        <div class="mt-3 bg-slate-50 border border-slate-100 rounded-xl p-4 text-[13px] text-slate-500 leading-relaxed space-y-2">
                          <div class="flex items-start gap-2 pb-2 border-b border-slate-200/60">
                            <span class="font-semibold text-slate-700 shrink-0">置信度 {{ (dec.confidence * 100).toFixed(0) }}%</span>
                            <span class="text-slate-400">·</span>
                            <span class="text-slate-500">{{ dec.confidence_basis }}</span>
                          </div>
                          <div>{{ dec.reasoning }}</div>
                          <div v-if="dec.counterfactual" class="text-rose-400/90 bg-rose-50/60 rounded-lg px-3 py-2 mt-1"><b class="font-semibold">反事实：</b>{{ dec.counterfactual }}</div>
                        </div>
                      </details>
                    </div>
                  </div>
                </transition-group>
              </div>

              <!-- 时间线（含倒排 + 排不开提示） -->
              <div v-if="planData.timeline && planData.timeline.length" class="mt-10 bg-white rounded-3xl shadow-sm border border-slate-100 p-6 md:p-8">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-5">⏱️ 智能时间线（按"几点到家"倒排）</div>
                <div class="space-y-3">
                  <div v-for="(s, i) in planData.timeline" :key="i" class="flex gap-4 items-start">
                    <div class="text-[13px] font-mono font-bold text-slate-400 w-24 shrink-0 pt-0.5">{{ s.start }}<span v-if="s.end !== s.start">–{{ s.end }}</span></div>
                    <div class="flex-1 border-l-2 border-slate-100 pl-4 pb-2">
                      <div :class="['text-[14px] font-bold', s.title.includes('⚠️') ? 'text-rose-500' : 'text-slate-800']">{{ s.title }}</div>
                      <div class="text-[12px] text-slate-400 mt-0.5">{{ s.reason }}</div>
                    </div>
                  </div>
                </div>
                <div v-if="planData.tips && planData.tips.length" class="mt-6 pt-5 border-t border-slate-100 space-y-2">
                  <div v-for="(t, i) in planData.tips" :key="i" class="text-[13px] text-slate-500 leading-relaxed">{{ t }}</div>
                </div>
              </div>

              <!-- 真实路线地图（高德实景，仅"真实地理数据"开启时出） -->
              <div v-if="mapUrl" class="mt-8 bg-white rounded-3xl shadow-sm border border-slate-100 p-4">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-3 px-2">🗺️ 真实路线（高德实景 · 家①→玩②→吃③，评委可当场搜证）</div>
                <img :src="mapUrl" @error="mapUrl = ''" class="w-full rounded-2xl" alt="高德真实路线图">
              </div>

              <div class="fixed bottom-8 left-1/2 -translate-x-1/2 w-[calc(100%-3rem)] max-w-3xl z-40 bg-slate-900/90 backdrop-blur-2xl rounded-3xl p-3 shadow-2xl border border-white/10 flex items-center justify-between">
                <div class="text-white text-sm font-medium px-4 flex items-center gap-3">
                  <div v-if="unansweredAsks() > 0" class="relative flex h-3 w-3"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-rose-400 opacity-75"></span><span class="relative inline-flex rounded-full h-3 w-3 bg-rose-500"></span></div>
                  <div v-else class="w-2 h-2 rounded-full bg-emerald-500"></div>
                  {{ unansweredAsks() > 0 ? `还有 ${unansweredAsks()} 件待你选` : '都齐了，可以往下走。' }}
                </div>
                <button @click="submitAnswers" :disabled="isSubmittingAnswers" class="bg-white text-slate-900 font-extrabold py-3 px-8 rounded-2xl hover:scale-[0.98] transition-transform active:scale-95 disabled:opacity-50 disabled:hover:scale-100">{{ isSubmittingAnswers ? '重新规划中...' : '确认方案 →' }}</button>
              </div>
            </div>

            <!-- 步骤 3: 协同反馈（收集，不直接执行）-->
            <div v-else-if="currentStep === 2" class="absolute w-full top-0 left-0 pb-20">
              <div class="mb-10 text-center">
                <div class="w-16 h-16 rounded-3xl bg-indigo-100 text-indigo-500 mx-auto flex items-center justify-center text-3xl mb-6 shadow-sm">👥</div>
                <h2 class="text-3xl font-extrabold tracking-tight text-slate-900 mb-3">还想调整？提点要求再生成</h2>
                <p class="text-slate-500 text-[15px] max-w-lg mx-auto">把链接发给同行的人各自改，或自己加要求。AI 合并后会先把<b class="text-slate-700">新方案给你过目</b>，满意了你再决定执行。</p>
              </div>

              <div v-if="shareUrl" class="bg-white rounded-2xl shadow-sm border border-slate-100 p-5 mb-6 flex items-center gap-3">
                <span class="text-[12px] font-bold text-slate-400 shrink-0">📲 分享链接</span>
                <input :value="shareUrl" readonly class="flex-1 bg-slate-50 border border-slate-100 rounded-lg px-3 py-2 text-[12px] text-slate-500 font-mono outline-none">
                <button @click="copyShare" class="bg-slate-900 text-white text-[12px] font-bold px-4 py-2 rounded-lg hover:bg-rose-500 transition">{{ copied ? '已复制' : '复制' }}</button>
              </div>

              <div class="bg-white rounded-[2rem] shadow-premium border border-slate-100 p-8 mb-8">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-5">加要求 / 模拟好友反馈</div>
                <div class="flex gap-2 mb-5">
                  <input v-model="newConstraint" @keyup.enter="refine" placeholder="直接打字加要求，如：换便宜点的 / 我对花生过敏 / 想吃清淡点" class="flex-1 bg-slate-50 border border-slate-200 rounded-xl px-4 py-3 text-[14px] outline-none focus:border-rose-300">
                  <button @click="refine" class="bg-slate-900 text-white font-bold px-5 rounded-xl hover:bg-rose-500 transition active:scale-95 text-[14px]">加上</button>
                </div>
                <div class="flex flex-wrap gap-3 mb-6">
                  <button @click="wifeFar" class="bg-slate-50 hover:bg-indigo-50 hover:text-indigo-600 border border-slate-200 text-slate-600 font-semibold rounded-xl px-5 py-2.5 transition-all active:scale-95 text-[14px]">👩 妻子嫌远，换最近的</button>
                  <button @click="friendUpscale" class="bg-slate-50 hover:bg-indigo-50 hover:text-indigo-600 border border-slate-200 text-slate-600 font-semibold rounded-xl px-5 py-2.5 transition-all active:scale-95 text-[14px]">🧑 朋友想换一家</button>
                  <button @click="allergy" class="bg-slate-50 hover:bg-rose-50 hover:text-rose-600 border border-slate-200 text-slate-600 font-semibold rounded-xl px-5 py-2.5 transition-all active:scale-95 text-[14px]">⚠️ 有人海鲜过敏</button>
                </div>

                <div v-if="edits.length > 0" class="space-y-3 p-4 bg-slate-50/50 rounded-2xl border border-slate-100">
                  <div class="text-xs font-bold text-slate-400 mb-2">待合并队列：</div>
                  <transition-group name="list">
                    <div v-for="(edit, idx) in edits" :key="idx" class="flex items-center justify-between bg-white px-4 py-3 rounded-xl shadow-sm border border-slate-100">
                      <div class="flex items-center gap-3">
                        <div class="w-8 h-8 rounded-full bg-gradient-to-br from-indigo-100 to-purple-100 text-indigo-700 font-black text-xs flex items-center justify-center">{{ edit.author[0] }}</div>
                        <div><span class="text-[13px] font-bold text-slate-700 mr-2">{{ edit.author }}</span><span class="text-[13px] text-slate-500">{{ edit.type === 'constraint' ? `提出新要求："${edit.constraint}"` : `要求换店 → ${edit._label}` }}</span></div>
                      </div>
                      <button @click="edits.splice(idx, 1)" class="text-slate-300 hover:text-rose-500 transition-colors p-1">✕</button>
                    </div>
                  </transition-group>
                </div>
              </div>

              <div v-if="errorMsg" class="mb-4 text-sm text-rose-500 font-semibold pl-2">{{ errorMsg }}</div>
              <div class="flex gap-4">
                <button @click="goExecute" class="flex-1 bg-white border border-slate-200 text-slate-600 hover:text-slate-900 font-bold py-4 rounded-2xl hover:border-slate-300 transition-all active:scale-95 shadow-sm">无需调整，直接执行</button>
                <button @click="regenerate" :disabled="edits.length === 0 || isReviewing" class="flex-1 bg-slate-900 text-white font-extrabold py-4 rounded-2xl shadow-xl hover:shadow-2xl hover:-translate-y-0.5 transition-all disabled:opacity-50 disabled:hover:transform-none active:scale-95">{{ isReviewing ? '生成中...' : '让 AI 合并，生成新方案 →' }}</button>
              </div>
            </div>

            <!-- 步骤 4: 定稿确认（看新方案 + 出行提醒 → 继续加要求 或 确认执行）-->
            <div v-else-if="currentStep === 3 && planData" class="absolute w-full top-0 left-0 pb-32">
              <div class="mb-8">
                <h2 class="text-3xl font-extrabold tracking-tight text-slate-900 mb-2">融合反馈后的新方案 <span class="text-slate-400 text-2xl">v{{ planData.version }}</span></h2>
                <p class="text-slate-500 text-[15px]">你过目一下。还有不满意的，下面接着加要求再生成；满意了再执行——<b class="text-slate-700">在你点头前，我不会下单。</b></p>
              </div>

              <!-- AI 如何处理了反馈 -->
              <div v-if="reviewResult && (reviewResult.auto_merged.length || reviewResult.conflicts.length)" class="bg-white rounded-3xl shadow-sm border border-slate-100 p-6 mb-6">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-3">🤖 我这样处理了大家的反馈</div>
                <div v-for="(m, i) in reviewResult.auto_merged" :key="'m'+i" class="text-[13px] text-slate-600 mb-1"><span class="text-emerald-500 font-bold">✓ 已合并</span> {{ m.author }}：{{ m.constraint || m.label }} <span class="text-slate-400">— {{ m.merged_note }}</span></div>
                <div v-for="(c, i) in reviewResult.conflicts" :key="'c'+i" class="text-[13px] text-slate-600 mt-2"><span class="text-amber-500 font-bold">⚖ 撞车</span> 采纳「{{ c.suggestion ? c.suggestion.label : '—' }}」（{{ c.suggestion_owner }}）<div class="text-[12px] text-slate-400 whitespace-pre-line pl-5">{{ c.reason }}</div></div>
              </div>

              <!-- 新方案决定（只读） -->
              <div class="space-y-3 mb-6">
                <div v-for="dec in planData.decisions" :key="dec.id" class="bg-white rounded-2xl border border-slate-100 shadow-sm p-5 flex items-start gap-4">
                  <div class="w-9 h-9 rounded-xl bg-slate-50 flex items-center justify-center text-lg shrink-0">{{ getIcon(dec.type) }}</div>
                  <div class="flex-1 min-w-0">
                    <div class="text-[14px] font-bold text-slate-800">{{ dec.description }}</div>
                    <div v-if="dec.chosen" class="text-[13px] text-slate-500 mt-0.5">→ {{ dec.chosen.label }}</div>
                  </div>
                  <div class="shrink-0 self-center" v-html="getMinimalBadge(dec.disposition, dec.status)"></div>
                </div>
              </div>

              <!-- 时间线 -->
              <div v-if="planData.timeline && planData.timeline.length" class="bg-white rounded-3xl shadow-sm border border-slate-100 p-6 md:p-8 mb-6">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-5">⏱️ 行程时间线</div>
                <div class="space-y-3">
                  <div v-for="(s, i) in planData.timeline" :key="i" class="flex gap-4 items-start">
                    <div class="text-[13px] font-mono font-bold text-slate-400 w-24 shrink-0 pt-0.5">{{ s.start }}<span v-if="s.end !== s.start">–{{ s.end }}</span></div>
                    <div class="flex-1 border-l-2 border-slate-100 pl-4 pb-2">
                      <div :class="['text-[14px] font-bold', s.title.includes('⚠️') ? 'text-rose-500' : 'text-slate-800']">{{ s.title }}</div>
                      <div class="text-[12px] text-slate-400 mt-0.5">{{ s.reason }}</div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- 出行提醒 -->
              <div v-if="planData.tips && planData.tips.length" class="bg-gradient-to-br from-rose-50/70 to-orange-50/70 rounded-3xl border border-rose-100/60 p-6 md:p-8 mb-6">
                <div class="text-[11px] font-bold text-rose-400 uppercase tracking-widest mb-4">🤝 出发前，几句出行提醒</div>
                <div class="space-y-2.5">
                  <div v-for="(t, i) in planData.tips" :key="i" class="text-[14px] text-slate-600 leading-relaxed">{{ t }}</div>
                </div>
              </div>

              <!-- 真实路线地图 -->
              <div v-if="mapUrl" class="bg-white rounded-3xl shadow-sm border border-slate-100 p-4 mb-8">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-3 px-2">🗺️ 真实路线（高德实景，可当场搜证）</div>
                <img :src="mapUrl" @error="mapUrl = ''" class="w-full rounded-2xl" alt="高德真实路线图">
              </div>

              <!-- 继续加要求（循环再生成）-->
              <div class="bg-white rounded-[2rem] shadow-premium border border-slate-100 p-6 mb-6">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-4">还不满意？接着加要求，再生成一版</div>
                <div class="flex gap-2 mb-3">
                  <input v-model="newConstraint" @keyup.enter="refine" placeholder="如：换便宜点的 / 离地铁近一点 / 想吃辣的" class="flex-1 bg-slate-50 border border-slate-200 rounded-xl px-4 py-3 text-[14px] outline-none focus:border-rose-300">
                  <button @click="refine" class="bg-slate-100 text-slate-700 font-bold px-5 rounded-xl hover:bg-slate-200 transition active:scale-95 text-[14px]">加上</button>
                </div>
                <div class="flex flex-wrap gap-2 mb-3">
                  <button @click="wifeFar" class="bg-slate-50 hover:bg-indigo-50 border border-slate-200 text-slate-600 font-semibold rounded-lg px-4 py-2 transition active:scale-95 text-[13px]">👩 换最近的</button>
                  <button @click="friendUpscale" class="bg-slate-50 hover:bg-indigo-50 border border-slate-200 text-slate-600 font-semibold rounded-lg px-4 py-2 transition active:scale-95 text-[13px]">🧑 换一家</button>
                  <button @click="allergy" class="bg-slate-50 hover:bg-rose-50 border border-slate-200 text-slate-600 font-semibold rounded-lg px-4 py-2 transition active:scale-95 text-[13px]">⚠️ 海鲜过敏</button>
                </div>
                <div v-if="edits.length > 0" class="space-y-2">
                  <div v-for="(edit, idx) in edits" :key="idx" class="flex items-center justify-between bg-slate-50/70 px-4 py-2 rounded-xl text-[13px]">
                    <span class="text-slate-600"><b class="text-slate-700">{{ edit.author }}</b>：{{ edit.type === 'constraint' ? edit.constraint : '换店 → ' + edit._label }}</span>
                    <button @click="edits.splice(idx, 1)" class="text-slate-300 hover:text-rose-500 p-1">✕</button>
                  </div>
                  <button @click="regenerate" :disabled="isReviewing" class="w-full mt-2 bg-slate-900 text-white font-bold py-3 rounded-xl hover:bg-rose-500 transition active:scale-95 disabled:opacity-50">{{ isReviewing ? '再生成中...' : '🔄 按新要求再生成一版' }}</button>
                </div>
              </div>

              <div class="fixed bottom-8 left-1/2 -translate-x-1/2 w-[calc(100%-3rem)] max-w-3xl z-40 bg-slate-900/90 backdrop-blur-2xl rounded-3xl p-3 shadow-2xl border border-white/10 flex items-center justify-between">
                <div class="text-white text-sm font-medium px-4">方案 v{{ planData.version }} · 满意就执行，否则上面接着改</div>
                <button @click="goExecute" class="bg-white text-slate-900 font-extrabold py-3 px-8 rounded-2xl hover:scale-[0.98] transition-transform active:scale-95">✅ 方案可行，开始执行 →</button>
              </div>
            </div>

            <!-- 步骤 5: 执行 -->
            <div v-else-if="currentStep === 4" class="absolute w-full top-0 left-0">
              <div class="mb-8">
                <h2 class="text-3xl font-extrabold tracking-tight text-slate-900 mb-2">正在为你调度执行。</h2>
                <p class="text-slate-500 text-[15px]">真实跑「查→订→买→送→发」闭环，含满座/超时重试/配送失败回滚补偿。</p>
              </div>

              <div class="bg-white rounded-[2rem] shadow-premium border border-slate-100 p-6 md:p-8">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mb-5">⚙️ 执行过程 · 查 → 订 → 买 → 送 → 发</div>
                <div class="space-y-2.5 max-h-[420px] overflow-y-auto no-scrollbar">
                  <div v-for="(log, i) in execLogs" :key="i" v-html="log" class="animate-fade-in-up"></div>
                  <div v-if="isExecuting" class="flex items-center gap-2 text-slate-400 text-[13px] pt-2">
                    <span class="w-2 h-2 bg-rose-400 rounded-full animate-ping"></span> 正在调度…
                  </div>
                </div>
              </div>

              <!-- 商业小结：真实下单算出的连带率 -->
              <div v-if="business" class="mt-8 grid grid-cols-2 md:grid-cols-4 gap-4 animate-fade-in-up">
                <div class="bg-white rounded-2xl border border-slate-100 shadow-sm p-5 text-center"><div class="text-2xl font-black text-slate-900">{{ business.sku_count }}</div><div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mt-1">连带品类</div></div>
                <div class="bg-white rounded-2xl border border-slate-100 shadow-sm p-5 text-center"><div class="text-2xl font-black text-slate-900">¥{{ business.gmv }}</div><div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mt-1">单局客单</div></div>
                <div class="bg-white rounded-2xl border border-slate-100 shadow-sm p-5 text-center"><div class="text-2xl font-black text-rose-500">{{ business.multiple }}×</div><div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mt-1">vs 单点外卖</div></div>
                <div class="bg-white rounded-2xl border border-slate-100 shadow-sm p-5 text-center"><div class="text-2xl font-black text-slate-900">{{ business.party }}</div><div class="text-[11px] font-bold text-slate-400 uppercase tracking-widest mt-1">服务人数</div></div>
              </div>

              <div v-if="!isExecuting && execLogs.length > 0" class="mt-8 flex justify-center gap-6 animate-fade-in-up">
                <button v-if="hasCard" @click="openCard" class="text-sm font-bold text-slate-500 hover:text-slate-900 transition-colors">📲 查看可分享方案卡</button>
                <button @click="resetFlow" class="text-sm font-bold text-slate-500 hover:text-slate-900 flex items-center gap-2 transition-colors">
                  <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                  开启新会话
                </button>
              </div>
            </div>

          </transition>
        </div>
      </div>

      <!-- ============ 演示验收模式（接真实 /api/run + /api/selftest）============ -->
      <div v-show="activeTab === 'D'" class="mt-4">
        <div class="mb-8">
          <h2 class="text-3xl font-extrabold tracking-tight text-slate-900 mb-2">演示验收台</h2>
          <p class="text-slate-500 text-[15px]">对照 PRD 9 条「必须演出来的瞬间」核验真实输出，外加 93 项内部逻辑自检。评委可当场点。</p>
        </div>
        <div class="flex flex-wrap gap-3 mb-6">
          <button @click="runDemo('family')" :disabled="demoLoading" class="bg-slate-900 text-white font-bold px-6 py-3 rounded-xl hover:bg-rose-500 transition disabled:opacity-50">跑家庭场景</button>
          <button @click="runDemo('friends')" :disabled="demoLoading" class="bg-white border border-slate-200 text-slate-700 font-bold px-6 py-3 rounded-xl hover:border-slate-300 transition disabled:opacity-50">跑朋友场景</button>
          <button @click="runSelftest" :disabled="stLoading" class="bg-white border border-slate-200 text-slate-700 font-bold px-6 py-3 rounded-xl hover:border-slate-300 transition disabled:opacity-50">跑自检 93 项</button>
          <span v-if="stResult" :class="['self-center font-bold text-sm', stResult.ok ? 'text-emerald-600' : 'text-rose-500']">{{ stResult.ok ? '✅' : '❌' }} 自检 {{ stResult.passed }}/{{ stResult.total }}</span>
        </div>

        <div v-if="demoLoading" class="text-slate-400 text-sm">跑真实 demo 中…（首次接真高德会稍慢）</div>

        <div v-if="checklist.length" class="grid md:grid-cols-3 gap-3 mb-8">
          <div v-for="c in checklist" :key="c.id" :class="['rounded-2xl border p-4', c.pass ? 'bg-emerald-50/50 border-emerald-100' : 'bg-rose-50/50 border-rose-100']">
            <div class="flex items-center gap-2 mb-1"><span>{{ c.pass ? '✅' : '❌' }}</span><span class="text-[12px] font-bold text-slate-400">#{{ c.id }} · {{ c.scenario }}</span></div>
            <div class="text-[13px] font-semibold text-slate-700 leading-snug">{{ c.title }}</div>
            <div v-if="!c.pass" class="text-[11px] text-rose-400 mt-1">缺：{{ (c.missing||[]).join('、') }}</div>
          </div>
        </div>

        <div v-if="demoText" class="rounded-[2rem] overflow-hidden shadow-2xl border border-slate-800 bg-slate-900/95 p-8">
          <div class="font-mono text-[12px] leading-relaxed text-slate-300 max-h-[420px] overflow-y-auto no-scrollbar whitespace-pre-wrap">{{ demoText }}</div>
        </div>
      </div>

    </main>
  </div>

  <script>
    const { createApp, ref, onMounted } = Vue;
    const post = async (url, body) => (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })).json();
    const get = async (url) => (await fetch(url)).json();

    createApp({
      setup() {
        const activeTab = ref('I');
        const currentStep = ref(0);
        const maxReached = ref(0);
        const steps = ['输入意图', '方案确认', '协同反馈', '定稿确认', '执行完毕'];
        const gmv = ref(0);
        const trust = ref(null);
        const errorMsg = ref('');
        const memorySummary = ref('');

        const form = ref({ goal: '', partySize: 3, useLlm: true, exceptions: true });
        const realData = ref(false);
        const mapUrl = ref('');
        const mapNonce = ref(0);
        const refreshMap = () => {
          if (realData.value && sid.value) { mapNonce.value++; mapUrl.value = '/api/map?sid=' + encodeURIComponent(sid.value) + '&t=' + mapNonce.value; }
          else mapUrl.value = '';
        };
        const examples = ['今天下午带老婆孩子出去玩，别太远，老婆在减肥', '周末四个朋友小聚，想吃顿好的，有人无辣不欢'];

        // 真实定位
        const loc = ref('');
        const area = ref('');
        const locOk = ref(false);
        const amapOn = ref(false);
        const locStatus = ref('📍 正在获取你的位置…');
        const addrInput = ref('');
        const detectLoc = () => {
          if (!amapOn.value) { locStatus.value = '📍 未配置高德 key，用内置示例库演示'; return; }
          if (!navigator.geolocation) { locStatus.value = '📍 浏览器不支持定位，可手动输入地点'; return; }
          locStatus.value = '📍 定位中…';
          navigator.geolocation.getCurrentPosition(async pos => {
            const r = await post('/api/geo', { lng: pos.coords.longitude, lat: pos.coords.latitude });
            if (r.ok) { loc.value = r.loc; area.value = r.area; locOk.value = true; locStatus.value = '📍 已定位：' + r.area + '　（按你这儿搜附近）'; }
            else { locStatus.value = '📍 ' + (r.error || '定位失败') + '，可手动输入地点'; }
          }, () => { locStatus.value = '📍 定位被拒绝/失败，可手动输入地点（如 国贸）'; }, { timeout: 8000, enableHighAccuracy: true });
        };
        const lockAddr = async () => {
          const a = addrInput.value.trim(); if (!a) return;
          locStatus.value = '📍 锁定中…';
          const r = await post('/api/geo', { address: a });
          if (r.ok) { loc.value = r.loc; area.value = r.area; locOk.value = true; locStatus.value = '📍 已锁定：' + r.area + '　（按这儿搜附近）'; }
          else { locStatus.value = '📍 ' + (r.error || '找不到该地点，换个说法'); }
        };
        onMounted(async () => {
          try { const s = await get('/api/llm_status'); amapOn.value = !!(s.amap && s.amap.enabled); } catch (e) {}
          if (amapOn.value) detectLoc(); else locStatus.value = '📍 未配置高德 key，用内置示例库演示';
        });

        const sid = ref('');
        const isAnalyzing = ref(false);
        const planData = ref(null);
        const answers = ref({});
        const suggestions = ref({});
        const isSubmittingAnswers = ref(false);

        const shareUrl = ref('');
        const copied = ref(false);
        const edits = ref([]);
        const newConstraint = ref('');
        const isReviewing = ref(false);
        const reviewResult = ref(null);

        const isExecuting = ref(false);
        const execLogs = ref([]);
        const business = ref(null);
        const hasCard = ref(false);

        // 演示验收
        const demoLoading = ref(false);
        const demoText = ref('');
        const checklist = ref([]);
        const stLoading = ref(false);
        const stResult = ref(null);

        const restDecision = () => planData.value ? planData.value.decisions.find(d => d.type === 'choose_restaurant') : null;
        const candidates = () => { const r = restDecision(); return r && r.options ? r.options : []; };

        // ---- 步骤1：真实分析 ----
        const startPlan = async () => {
          errorMsg.value = '';
          if (!form.value.goal.trim()) { errorMsg.value = '先说一句你的目标吧'; return; }
          isAnalyzing.value = true;
          try {
            const r = await post('/api/start', { goal: form.value.goal, party_size: form.value.partySize, exceptions: form.value.exceptions, use_llm: form.value.useLlm, loc: loc.value || '' });
            if (!r.ok) { errorMsg.value = r.error || '分析失败'; isAnalyzing.value = false; return; }
            sid.value = r.sid;
            planData.value = r.plan;
            trust.value = r.trust;
            realData.value = !!r.llm_used;
            refreshMap();
            gmv.value = r.plan.gmv_estimate || 0;
            memorySummary.value = (r.memory && r.memory.summary) || '';
            answers.value = {}; suggestions.value = {};
            planData.value.decisions.forEach(d => { if (d.disposition === 'suggest') suggestions.value[d.id] = true; });
            reach(1);
            window.scrollTo({ top: 0, behavior: 'smooth' });
          } catch (e) { errorMsg.value = '服务异常：' + e; }
          isAnalyzing.value = false;
        };

        const forgetMe = async () => { await post('/api/forget', {}); memorySummary.value = ''; };

        // ---- 步骤2：真实回答 + 重新规划 ----
        const submitAnswers = async () => {
          if (isSubmittingAnswers.value) return;
          isSubmittingAnswers.value = true;
          try {
            const r = await post('/api/answer', { sid: sid.value, answers: answers.value, suggestions: suggestions.value });
            if (r.ok) {
              planData.value = r.plan;
              gmv.value = r.plan.gmv_estimate || gmv.value;
              if (r.trust) trust.value = r.trust;
              refreshMap();
              await loadShare();
              reach(2);
              window.scrollTo({ top: 0, behavior: 'smooth' });
            } else { errorMsg.value = r.error; }
          } catch (e) { errorMsg.value = '' + e; }
          isSubmittingAnswers.value = false;
        };

        // ---- 步骤3：真实评审 ----
        const loadShare = async () => { try { const r = await get('/api/shareurl?sid=' + encodeURIComponent(sid.value)); if (r.ok) shareUrl.value = r.url; } catch (e) {} };
        const copyShare = () => { navigator.clipboard && navigator.clipboard.writeText(shareUrl.value); copied.value = true; setTimeout(() => copied.value = false, 1500); };

        const pushEdit = (e) => { if (!edits.value.find(x => x.author === e.author && x.type === e.type && x._label === e._label)) edits.value.push(e); };
        const wifeFar = () => { const c = candidates().slice().sort((a, b) => (a.distance_km || 99) - (b.distance_km || 99))[0]; if (c) pushEdit({ author: '妻子', weight: 0.9, type: 'restaurant', after_id: c.id, _label: c.label }); };
        const friendUpscale = () => { const c = candidates().slice().sort((a, b) => (b.per_capita || 0) - (a.per_capita || 0))[0]; if (c) pushEdit({ author: '朋友', weight: 0.5, type: 'restaurant', after_id: c.id, _label: c.label }); };
        const allergy = () => pushEdit({ author: '好友A', weight: 0.6, type: 'constraint', constraint: '我对海鲜过敏', _label: '海鲜过敏' });
        // 我自己加要求 → 调 /api/refine 立即用高德重搜重排（识别改活动/菜系/距离/预算/过敏/辣度）
        const refine = async () => {
          const t = newConstraint.value.trim(); if (!t || isReviewing.value) return;
          isReviewing.value = true; errorMsg.value = '';
          try {
            const r = await post('/api/refine', { sid: sid.value, text: t });
            if (r.ok) {
              planData.value = r.plan; gmv.value = r.plan.gmv_estimate || gmv.value;
              reviewResult.value = { auto_merged: (r.applied || []).map(a => ({ author: '我', constraint: a, merged_note: '已据此用高德重搜重排' })), conflicts: [] };
              newConstraint.value = ''; refreshMap(); reach(3);
              window.scrollTo({ top: 0, behavior: 'smooth' });
            } else { errorMsg.value = r.error; }
          } catch (e) { errorMsg.value = '' + e; }
          isReviewing.value = false;
        };

        // 合并反馈 → 生成新方案 → 进"定稿确认"（不直接执行；用户看过、可继续加要求，满意了才执行）
        const regenerate = async () => {
          if (!edits.value.length) return;
          errorMsg.value = '';
          isReviewing.value = true;
          try {
            const payload = edits.value.map(e => e.type === 'constraint'
              ? { author: e.author, weight: e.weight, type: 'constraint', constraint: e.constraint }
              : { author: e.author, weight: e.weight, type: 'restaurant', after_id: e.after_id, note: '' });
            const rv = await post('/api/review', { sid: sid.value, edits: payload });
            if (!rv.ok) { errorMsg.value = rv.error; isReviewing.value = false; return; }
            reviewResult.value = { auto_merged: rv.auto_merged, conflicts: rv.conflicts };
            // 撞车默认采纳"带权重的建议"，定稿出 v_next（裁决权仍在用户：不满意可继续加要求再生成）
            const resolutions = {};
            (rv.conflicts || []).forEach((c, i) => resolutions[String(i)] = 'suggestion');
            const rs = await post('/api/resolve', { sid: sid.value, resolutions });
            planData.value = rs.ok ? rs.plan : rv.merged_plan;
            gmv.value = planData.value.gmv_estimate || gmv.value;
            refreshMap();
            edits.value = [];            // 这一轮已并入，清空队列，等下一轮
            reach(3);                    // 去"定稿确认"看新方案
            window.scrollTo({ top: 0, behavior: 'smooth' });
          } catch (e) { errorMsg.value = '' + e; }
          isReviewing.value = false;
        };

        // ---- 步骤5：真实执行 ----
        const goExecute = async () => {
          reach(4);
          window.scrollTo({ top: 0, behavior: 'smooth' });
          isExecuting.value = true;
          execLogs.value = [];
          try {
            const r = await post('/api/execute', { sid: sid.value });
            if (!r.ok) { execLogs.value = ['❌ ' + (r.error || '执行失败')]; isExecuting.value = false; return; }
            const lines = (r.text || '').split('\n');
            // 逐行揭示，营造"在替你跑腿"的过程感（已美化成浅色行、隐藏内部ID/分隔线）
            let i = 0;
            const tick = () => {
              if (i < lines.length) {
                const h = prettyExec(lines[i]); i++;
                if (h) { execLogs.value.push(h); setTimeout(tick, 60); } else { tick(); }
              } else { gmv.value = r.gmv; business.value = r.business; hasCard.value = r.has_card; if (r.trust) trust.value = r.trust; isExecuting.value = false; }
            };
            tick();
          } catch (e) { execLogs.value = ['❌ ' + e]; isExecuting.value = false; }
        };

        const openCard = () => window.open('/api/card?sid=' + encodeURIComponent(sid.value), '_blank');

        // ---- 演示验收 ----
        const runDemo = async (scenario) => {
          demoLoading.value = true; demoText.value = '';
          try {
            const r = await get(`/api/run?scenario=${scenario}&conflict=suggestion`);
            if (r.ok) { demoText.value = r.text; checklist.value = r.checklist || []; gmv.value = r.gmv || gmv.value; }
          } catch (e) {}
          demoLoading.value = false;
        };
        const runSelftest = async () => { stLoading.value = true; try { stResult.value = await get('/api/selftest'); } catch (e) {} stLoading.value = false; };

        // ---- 流程控制 ----
        const goto = (n) => { if (n <= maxReached.value) currentStep.value = n; };
        const reach = (n) => { maxReached.value = Math.max(maxReached.value, n); goto(n); };
        const resetFlow = () => { maxReached.value = 0; currentStep.value = 0; planData.value = null; edits.value = []; newConstraint.value = ''; reviewResult.value = null; execLogs.value = []; business.value = null; hasCard.value = false; gmv.value = 0; sid.value = ''; shareUrl.value = ''; form.value.goal = ''; errorMsg.value = ''; };

        // ---- 展示辅助 ----
        const getIcon = (type) => (type && type.indexOf('choose_activity') === 0) ? '🎨' : ({ choose_restaurant: '🍽️', send_gift: '🎁', set_budget: '💰', set_return_time: '🕖', nap_window: '😴', child_safety: '🧒' }[type] || '✨');
        const countDispo = (d) => planData.value ? planData.value.decisions.filter(x => x.disposition === d && !isResolved(x.status)).length : 0;
        const countDispoAll = (d) => planData.value ? planData.value.decisions.filter(x => x.disposition === d).length : 0;
        const isResolved = (status) => ['confirmed', 'done'].includes(status);
        // "停下来问"给预设选项，更友好；仍保留"自己填"
        const ASK_OPTIONS = {
          set_budget: ['人均 80 以内', '人均 120 以内', '人均 200 左右', '人均 300 以上'],
          set_return_time: ['17:30 前到家', '19:30 前到家', '21:30 前到家', '23:00 前回家'],
        };
        const askOptions = (type) => ASK_OPTIONS[type] || [];
        const customAsk = ref({});
        const pickAsk = (id, opt) => { answers.value[id] = opt; customAsk.value[id] = false; };
        const pickCustom = (dec) => { customAsk.value[dec.id] = true; if (askOptions(dec.type).includes(answers.value[dec.id])) answers.value[dec.id] = ''; };
        const unansweredAsks = () => planData.value ? planData.value.decisions.filter(d => d.disposition === 'ask' && !isResolved(d.status) && !(answers.value[d.id] || '').trim()).length : 0;
        const getCardStyle = (d) => ({ auto: 'bg-white border border-slate-100 shadow-sm', suggest: 'bg-white border border-amber-200/60 shadow-premium', ask: 'bg-white border-2 border-rose-400 shadow-premium-hover transform -translate-y-1' }[d] || 'bg-white border border-slate-100');
        const getIconBg = (d, status) => { if (isResolved(status)) return 'bg-slate-100 text-slate-400'; return { auto: 'bg-emerald-50 text-emerald-600', suggest: 'bg-amber-100 text-amber-600', ask: 'bg-rose-500 text-white shadow-md' }[d] || 'bg-slate-100'; };
        const getMinimalBadge = (dispo, status) => {
          if (isResolved(status)) return '<span class="text-[11px] font-bold text-emerald-500 uppercase tracking-widest">已拍板</span>';
          return { auto: '<span class="text-[11px] font-bold text-emerald-500 uppercase tracking-widest">✅ 已直接定</span>', suggest: '<span class="text-[11px] font-bold text-amber-500 uppercase tracking-widest bg-amber-50 px-2 py-1 rounded">💡 待你点头</span>', ask: '<span class="text-[11px] font-bold text-rose-500 uppercase tracking-widest flex items-center gap-1"><span class="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse"></span> ❓ 必须你定</span>' }[dispo] || '';
        };
        const _esc = (x) => x.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const prettyExec = (raw) => {
          // 去掉内部 ID（amap_r_xxx / rest_x / gift_x / act_x），别让技术细节漏给用户
          let t = (raw || '').replace(/\s*(amap_[a-z]_[A-Za-z0-9]+|rest_[A-Za-z0-9]+|gift_[A-Za-z0-9]+|act_[A-Za-z0-9]+)/g, '');
          const s = t.trim();
          if (!s) return '';
          if (/^[=╔╚║\s].*[=╗╝║\s]$/.test(s) && /[=╔╚║╗╝]/.test(s)) return '';   // 隐藏 ===== / 边框分隔线
          const step = s.match(/^\[(\d+)\/(\d+)\]\s*(.*)$/);
          if (step) {
            const title = _esc(step[3].replace(/⏳.*$/, '').trim());
            return `<div class="flex items-center gap-3 pt-3 first:pt-0"><span class="w-6 h-6 rounded-lg bg-slate-900 text-white text-[11px] font-black flex items-center justify-center shrink-0">${step[1]}</span><span class="font-bold text-slate-800 text-[15px]">${title}</span></div>`;
          }
          if (s.startsWith('💰')) return `<div class="mt-3 inline-flex items-center gap-2 bg-emerald-50 text-emerald-700 font-bold px-4 py-2 rounded-xl text-[14px]">💰 ${_esc(s.replace(/^💰/, '').trim())}</div>`;
          if (s.startsWith('🎉')) return `<div class="text-slate-700 font-semibold text-[14px] pt-1">${_esc(s)}</div>`;
          if (/^已落地订单|^本次会话/.test(s)) return `<div class="text-[12px] text-slate-400 pl-9">${_esc(s)}</div>`;
          let icon = '·', cls = 'text-slate-500', txt = s;
          if (s.startsWith('✅')) { icon = '✓'; cls = 'text-emerald-600'; txt = s.slice(1).trim(); }
          else if (s.startsWith('❌')) { icon = '✕'; cls = 'text-rose-500'; txt = s.slice(1).trim(); }
          else if (s.startsWith('⚠️')) { icon = '!'; cls = 'text-amber-600'; txt = s.replace(/^⚠️/, '').trim(); }
          else if (s.startsWith('↳')) { icon = '↳'; cls = 'text-slate-400'; txt = s.slice(1).trim(); }
          return `<div class="flex items-start gap-2.5 pl-9"><span class="shrink-0 font-bold ${cls}">${icon}</span><span class="text-[14px] leading-relaxed ${cls}">${_esc(txt)}</span></div>`;
        };

        return {
          activeTab, currentStep, maxReached, steps, gmv, trust, errorMsg, memorySummary,
          loc, area, locOk, locStatus, addrInput, detectLoc, lockAddr,
          form, examples, sid, isAnalyzing, planData, answers, suggestions, isSubmittingAnswers,
          shareUrl, copied, edits, newConstraint, isReviewing, reviewResult, isExecuting, execLogs, business, hasCard, mapUrl,
          demoLoading, demoText, checklist, stLoading, stResult,
          startPlan, forgetMe, submitAnswers, copyShare, wifeFar, friendUpscale, allergy, refine, regenerate, goExecute, openCard, resetFlow, runDemo, runSelftest,
          goto, getIcon, countDispo, countDispoAll, isResolved, getCardStyle, getIconBg, getMinimalBadge,
          askOptions, customAsk, pickAsk, pickCustom, unansweredAsks
        };
      }
    }).mount('#app');
  </script>
</body>
</html>
"""

# ===========================================================================
# 朋友页：通过分享链接打开，看到攒局人的方案、提交自己的一条改动
# ===========================================================================
JOIN_PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>帮朋友改改这个安排</title>
<style>
:root{--b1:#ff8a52;--b2:#ff5d87;--grad:linear-gradient(135deg,#ff8a52,#ff5d87);
 --ink:#1b2030;--ink2:#444b60;--muted:#8a90a4;--line:#e9ebf3;--soft:#f5f6fb;--green:#12a05a;--greenbg:#e6f7ee}
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f3f4f9;color:var(--ink);font:15px/1.6 -apple-system,"PingFang SC",system-ui,sans-serif;padding:16px}
.wrap{max-width:560px;margin:0 auto}
.hd{background:var(--grad);color:#fff;border-radius:16px;padding:18px 20px;margin-bottom:14px}
.hd h1{font-size:18px}.hd p{font-size:13px;opacity:.92;margin-top:4px}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.card h2{font-size:14px;color:var(--ink2);margin-bottom:10px}
.cur{font-size:13.5px;color:var(--ink2)}.cur b{color:var(--ink)}
.tl{margin-top:10px;background:var(--soft);border-radius:10px;padding:10px 12px;font-size:13px}
.tl .r{display:flex;gap:10px;padding:3px 0}.tl .t{color:var(--b2);font-weight:700;min-width:100px}
label{display:block;font-size:13px;color:var(--ink2);margin:10px 0 4px}
input,select{width:100%;border:1px solid var(--line);border-radius:9px;padding:9px 11px;font-size:14px}
.row{display:flex;gap:10px}.row>*{flex:1}
.btn{background:var(--grad);color:#fff;border:none;border-radius:10px;padding:12px;font-size:15px;font-weight:700;width:100%;margin-top:14px;cursor:pointer}
.seg{display:flex;gap:8px;margin-top:6px}
.seg button{flex:1;border:1px solid var(--line);background:#fff;border-radius:9px;padding:8px;cursor:pointer;font-size:13.5px}
.seg button.on{border-color:var(--b2);color:var(--b2);font-weight:700}
.ok{background:var(--greenbg);color:var(--green);border-radius:10px;padding:12px;font-weight:700;margin-top:12px}
.hidden{display:none}.muted{color:var(--muted);font-size:12.5px}
</style></head>
<body><div class="wrap">
  <div class="hd"><h1>🙋 帮朋友改改这个安排</h1><p id="goal">加载中…</p></div>
  <div class="card"><h2>当前方案</h2><div class="cur" id="cur"></div><div class="tl" id="tl"></div></div>
  <div class="card">
    <h2>你想改什么？</h2>
    <div class="seg" id="seg">
      <button class="on" data-t="restaurant" onclick="segPick(this)">换个餐厅</button>
      <button data-t="constraint" onclick="segPick(this)">加个要求</button>
    </div>
    <label>你的名字</label><input id="who" placeholder="如：小张">
    <div id="restBox"><label>换成哪家（按离攒局人位置远近列出）</label><select id="rest"></select></div>
    <div id="consBox" class="hidden"><label>你的要求</label><input id="cons" placeholder="如：我对海鲜过敏 / 想吃辣的"></div>
    <label>附言（可选）</label><input id="note" placeholder="如：那家有点远">
    <button class="btn" onclick="submit()">提交我的改动</button>
    <div id="done" class="ok hidden"></div>
    <div class="muted" style="margin-top:8px">提交后，攒局人会在 ta 那边收到你的改动，由 ta 拍板合并。你可以提交多条。</div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const SID=new URLSearchParams(location.search).get('sid')||'';
let TYPE='restaurant',CANDS=[];
function segPick(b){document.querySelectorAll('#seg button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  TYPE=b.dataset.t;$('restBox').classList.toggle('hidden',TYPE!=='restaurant');$('consBox').classList.toggle('hidden',TYPE!=='constraint');}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function facts(o){let b=[];if(o.per_capita!=null)b.push('人均'+Math.round(o.per_capita));if(o.distance_km!=null)b.push(o.distance_km+'km');return b.length?'（'+b.join('，')+'）':'';}
async function load(){
  if(!SID){$('goal').textContent='链接无效';return;}
  const r=await(await fetch('/api/plan?sid='+encodeURIComponent(SID))).json();
  if(!r.ok){$('goal').textContent=r.error||'链接已过期';return;}
  $('goal').textContent='“'+r.goal+'”　·　'+r.party+' 人';
  const chosen=r.plan.decisions.filter(d=>d.chosen).map(d=>'<b>'+esc(d.chosen.label)+'</b>').join('　·　');
  $('cur').innerHTML=chosen||'（暂无）';
  $('tl').innerHTML=(r.plan.timeline||[]).map(s=>`<div class="r"><span class="t">${esc(s.start)}${s.end&&s.end!==s.start?'–'+esc(s.end):''}</span><span>${esc(s.title)}</span></div>`).join('');
  CANDS=r.candidates||[];
  $('rest').innerHTML=CANDS.map(o=>`<option value="${esc(o.id)}">${esc(o.label)}${facts(o)}</option>`).join('');
}
async function submit(){
  const who=$('who').value.trim();if(!who){alert('先填你的名字');return;}
  const body={sid:SID,author:who,weight:0.5,type:TYPE,note:$('note').value.trim()};
  if(TYPE==='constraint'){body.constraint=$('cons').value.trim();if(!body.constraint){alert('填一下你的要求');return;}}
  else{body.after_id=$('rest').value;}
  const r=await(await fetch('/api/submit_edit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(!r.ok){alert(r.error);return;}
  $('done').textContent='✅ 已提交：'+r.label+'（攒局人会收到，目前共 '+r.received+' 条）';
  $('done').classList.remove('hidden');$('cons').value='';$('note').value='';
}
load();
</script>
</body></html>
"""

if __name__ == "__main__":
    main()
