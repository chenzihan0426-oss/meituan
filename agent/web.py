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
    constraints = dict(intent.constraints)
    tb = ToolBox(seed=7, fast=True, use_llm=want_real,
                 forced=dict(INTERACTIVE_FORCED) if exceptions else {})
    plan = build_first_plan(intent, tb, party_size=party)
    sid = _new_session({"intent": intent, "constraints": constraints, "tb": tb,
                        "plan": plan, "party": party, "executed": False, "use_llm": want_real})
    mem = prefs.load()
    return {"ok": True, "sid": sid, "goal": goal,
            "known": intent.known, "unknown": intent.unknown,
            "party": party, "plan": _ser_plan(plan),
            "source": tb.last_source, "llm_used": want_real,
            "memory": {"prefs": mem, "summary": prefs.summary(mem)}}


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
    for d in plan.decisions:
        if d.id in answers and d.disposition == Disposition.ASK:
            txt = str(answers[d.id]).strip()
            if txt:
                _apply_open_answer(d, txt, constraints)
                if d.type == "set_budget":
                    budget_changed = True
        if d.id in suggestions and d.disposition == Disposition.SUGGEST:
            if suggestions[d.id]:
                d.status = Status.CONFIRMED
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
    return {"ok": True, "plan": _ser_plan(plan),
            "candidates": [_ser_opt(o) for o in (plan.find_by_type("choose_restaurant").options
                                                 if plan.find_by_type("choose_restaurant") else [])]}


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
    rest = plan.find_by_type("choose_restaurant")
    cur = rest.chosen if rest else None
    edits, participants, seen = [], [], {}
    for e in raw_edits:
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
    if not edits:
        return {"ok": False, "error": "没有有效改动；可直接执行，或添加至少一条改动"}
    rr = run_review_round(plan, participants, edits, constraints, party_size=party)
    s["rr"] = rr
    return {"ok": True,
            "auto_merged": [{"author": e.author.id, "constraint": e.constraint,
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
    """家 → 玩 → 吃 的坐标点（只取有真实坐标的，供静态地图打点）。"""
    cons = s.get("constraints", {})
    spots = [(amap.search_center(cons), "家")]
    plan = s["plan"]
    act = plan.find_by_type("choose_activity")
    if act and act.chosen and act.chosen.get("location"):
        spots.append((act.chosen.get("location"), "玩"))
    rest = plan.find_by_type("choose_restaurant")
    if rest and rest.chosen and rest.chosen.get("location"):
        spots.append((rest.chosen.get("location"), "吃"))
    return spots


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
            "orders": len(tb.ledger), "has_card": bool(s.get("card_html"))}


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
                    "/api/execute": api_execute, "/api/geo": api_geo,
                    "/api/submit_edit": api_submit_edit, "/api/autonomy": api_autonomy,
                    "/api/forget": api_forget}
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
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>会替你拿主意的活动规划 Agent</title>
<style>
:root{
 --bg:#f3f4f9; --card:#ffffff; --ink:#1b2030; --ink2:#444b60; --muted:#8a90a4;
 --line:#e9ebf3; --soft:#f5f6fb;
 --b1:#ff8a52; --b2:#ff5d87; --grad:linear-gradient(135deg,#ff8a52 0%,#ff5d87 100%);
 --green:#12a05a; --greenbg:#e6f7ee; --amber:#cf7a09; --amberbg:#fff2dd; --amberbg:#fff3df;
 --red:#e23b5a; --redbg:#ffe9ed; --cyan:#0a97a9; --cyanbg:#e1f6f8; --blue:#3a6df0;
 --shadow:0 8px 30px rgba(35,45,90,.07); --shadow2:0 2px 10px rgba(35,45,90,.05);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
a{color:inherit}
/* 顶栏 */
.bar{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.86);backdrop-filter:saturate(1.4) blur(10px);
 border-bottom:1px solid var(--line);padding:10px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px}
.logo{width:36px;height:36px;border-radius:11px;background:var(--grad);display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:0 4px 12px rgba(255,93,135,.35)}
.brand h1{font-size:15.5px;font-weight:800;letter-spacing:-.01em}
.brand p{font-size:11.5px;color:var(--muted)}
.tabs{display:flex;gap:6px;background:var(--soft);padding:4px;border-radius:12px}
.tabs button{border:0;background:transparent;color:var(--muted);font:inherit;font-weight:700;font-size:13px;padding:7px 14px;border-radius:9px;cursor:pointer}
.tabs button.on{background:#fff;color:var(--ink);box-shadow:var(--shadow2)}
.gmvbox{margin-left:auto;text-align:right;background:var(--soft);border-radius:13px;padding:6px 14px}
.gmvbox b{font-size:20px;font-weight:800;background:var(--grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.gmvbox span{display:block;font-size:10.5px;color:var(--muted);letter-spacing:.04em}
main{max-width:880px;margin:0 auto;padding:22px 18px 60px}
/* 卡片通用 */
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;box-shadow:var(--shadow);padding:22px;margin:16px 0}
.card.lite{box-shadow:var(--shadow2)}
.shd{display:flex;align-items:flex-start;gap:12px;margin-bottom:14px}
.shd .no{flex:none;width:30px;height:30px;border-radius:9px;background:var(--grad);color:#fff;font-weight:800;display:flex;align-items:center;justify-content:center;font-size:15px;box-shadow:0 4px 12px rgba(255,93,135,.3)}
.shd h2{font-size:18px;font-weight:800;letter-spacing:-.01em}
.shd p{font-size:13px;color:var(--muted);margin-top:1px}
/* hero */
.hero h2{font-size:24px;font-weight:800;letter-spacing:-.02em;line-height:1.3}
.hero .lead{color:var(--ink2);margin:6px 0 16px;font-size:15px}
.feats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:18px}
.feat{background:var(--soft);border:1px solid var(--line);border-radius:14px;padding:12px 13px}
.feat .fi{font-size:18px}.feat b{display:block;font-size:13.5px;margin:4px 0 2px}.feat span{font-size:12px;color:var(--muted)}
.feat.g{background:var(--greenbg);border-color:#cdeedd}.feat.a{background:var(--amberbg);border-color:#f3e2c0}.feat.r{background:var(--redbg);border-color:#f6d3d9}
textarea,input,select{width:100%;background:var(--soft);border:1.5px solid var(--line);border-radius:13px;padding:12px 14px;font:inherit;color:var(--ink);outline:none;transition:.15s}
textarea{min-height:74px;resize:vertical;font-size:15.5px}
textarea:focus,input:focus,select:focus{border-color:var(--b2);background:#fff;box-shadow:0 0 0 4px rgba(255,93,135,.1)}
.egs{margin-top:9px;font-size:13px;color:var(--muted)}
.eg{display:inline-block;background:#fff;border:1px solid var(--line);border-radius:20px;padding:5px 12px;margin:5px 6px 0 0;cursor:pointer;color:var(--ink2);font-size:12.5px;transition:.12s}
.eg:hover{border-color:var(--b2);color:var(--b2)}
.autorow{display:flex;align-items:center;gap:12px;margin-top:14px;padding:11px 14px;background:var(--soft);border:1px solid var(--line);border-radius:12px;flex-wrap:wrap}
.autorow .auto-l{font-size:13.5px;font-weight:700;color:var(--ink2)}
.autorow input[type=range]{flex:1;min-width:140px;accent-color:var(--b2)}
.autorow .auto-v{font-weight:800;color:var(--b2);min-width:84px;text-align:right}
.membar{margin-top:12px;padding:11px 14px;border-radius:12px;background:#eef3ff;border:1px solid #cfdcff;font-size:13.5px;color:var(--ink2);line-height:1.7}
.membar b{color:var(--blue)}
.membar .forget{color:var(--red);cursor:pointer;font-size:12.5px;margin-left:6px;text-decoration:underline}
.quad{margin-top:14px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.quad-h{font-weight:800;font-size:13.5px;color:var(--ink2);margin-bottom:6px}
.quad-svg{width:100%;height:auto;display:block}
.quad-legend{font-size:12px;color:var(--muted);margin-top:4px}
.quad-legend .lg{font-size:13px}.quad-legend .lg.g{color:var(--green)}.quad-legend .lg.a{color:var(--amber)}.quad-legend .lg.r{color:var(--red)}
.mapwrap{margin-top:14px;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.mapimg{width:100%;display:block}
.mapcap{font-size:12.5px;color:var(--ink2);padding:8px 12px;background:var(--soft);font-weight:600}
.cf{margin-top:8px;padding:9px 12px;border-radius:10px;background:#fff3df;border:1px solid #ffe0b0;color:var(--amber);font-size:13px;line-height:1.6}
.tips{margin-top:14px;background:#f0fbf5;border:1px solid #c4ecd5;border-radius:12px;padding:12px 14px}
.tips:empty{display:none}
.tips-h{font-weight:800;font-size:13.5px;color:var(--green);margin-bottom:8px}
.tip{display:flex;gap:9px;padding:5px 0;font-size:13.5px;color:var(--ink2);line-height:1.65;border-top:1px dashed #d4eede}
.tip:first-of-type{border-top:none}
.tip-i{flex:none;font-size:15px}
.tagline{margin-top:12px;padding:11px 14px;border-radius:12px;background:linear-gradient(135deg,#fff5ef,#ffeef3);border:1px solid #ffd9c9;font-size:14px;color:var(--ink2);line-height:1.7}
.tagline b{color:var(--b2)}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.reveal{animation:rise .5s both cubic-bezier(.2,.7,.2,1)}
.aispeak{position:relative;margin:-2px 0 12px;padding:13px 15px;border-radius:14px;background:linear-gradient(135deg,#fff0e6,#ffe6ef);border:1px solid #ffcdbb;color:var(--ink);font-size:14px;line-height:1.75}
.aispeak b{color:var(--b2)}
.aispeak .aitag{display:block;margin-top:7px;font-size:12px;color:var(--amber);font-weight:700}
.timeline{margin-top:14px;background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.timeline:empty{display:none}
.tl-title{font-weight:800;font-size:13.5px;margin-bottom:8px;color:var(--ink2)}
.tl-row{display:flex;gap:12px;padding:5px 0;font-size:13.5px;border-top:1px dashed var(--line)}
.tl-row:first-of-type{border-top:none}
.tl-time{color:var(--b2);font-weight:700;min-width:104px;font-variant-numeric:tabular-nums}
.tl-act{color:var(--ink2)}
.tl-act b{color:var(--ink)}
.replan{margin-top:14px;padding:12px 14px;border-radius:12px;background:var(--greenbg);border:1px solid #b6e6cc;color:var(--green);font-weight:700;font-size:13.5px}
.replan:empty,.replan.hidden{display:none}
.replan small{display:block;font-weight:500;color:var(--ink2);margin-top:4px}
.sharebox{margin:14px 0;padding:12px 14px;background:var(--soft);border:1px dashed #c9cfe0;border-radius:12px}
.sharebox #shareRow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}
.sharebox #shareUrl{flex:1;min-width:200px;border:1px solid var(--line);border-radius:9px;padding:7px 10px;font-size:12.5px;color:var(--ink2);background:#fff}
.locbar{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-top:14px;padding:10px 12px;background:var(--soft);border:1px solid var(--line);border-radius:12px}
.locbar #locLabel{font-size:13px;font-weight:600;color:var(--ink2)}
.locbar #locLabel.ok{color:var(--green)}
.locbar #locLabel.err{color:var(--red)}
.locbtn{background:#fff;border:1px solid var(--line);border-radius:18px;padding:5px 12px;cursor:pointer;font-size:12.5px;color:var(--ink2);transition:.12s}
.locbtn:hover{border-color:var(--b2);color:var(--b2)}
.locbar #locInput{flex:1;min-width:160px;border:1px solid var(--line);border-radius:9px;padding:6px 10px;font-size:13px}
.opts{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-top:16px}
.opts label{font-size:13.5px;color:var(--ink2);display:flex;align-items:center;gap:7px}
.opts input[type=number]{width:72px;text-align:center}
.chkbox{display:flex;align-items:center;gap:8px;cursor:pointer}
.chkbox input{width:auto;accent-color:var(--b2);transform:scale(1.15)}
.btn{border:0;border-radius:13px;font:inherit;font-weight:800;font-size:15px;padding:13px 24px;cursor:pointer;background:var(--grad);color:#fff;box-shadow:0 6px 18px rgba(255,93,135,.32);transition:.15s}
.btn:hover{transform:translateY(-1px);box-shadow:0 9px 22px rgba(255,93,135,.4)}
.btn:disabled{opacity:.45;cursor:default;box-shadow:none;transform:none}
.btn.sm{font-size:13.5px;padding:9px 16px}
.btn.ghost{background:#fff;color:var(--ink);border:1.5px solid var(--line);box-shadow:none}
.btn.ghost:hover{border-color:var(--b2);color:var(--b2);transform:none}
.btn.ok{background:linear-gradient(135deg,#19b06a,#0c8f78);box-shadow:0 6px 18px rgba(18,160,90,.3)}
.statuspill{font-size:12.5px;color:var(--green);display:inline-flex;align-items:center;gap:5px}
/* 步骤条 */
.steps{display:flex;margin:6px 0 2px;user-select:none}
.node{flex:1;text-align:center;position:relative;padding-top:4px;cursor:default}
.node .sdot{width:34px;height:34px;border-radius:50%;background:#fff;border:2px solid var(--line);color:var(--muted);
 display:flex;align-items:center;justify-content:center;font-weight:800;margin:0 auto 6px;font-size:14px;transition:.2s}
.node .slab{font-size:12.5px;color:var(--muted);font-weight:600}
.node:not(:last-child):after{content:'';position:absolute;top:21px;left:62%;width:76%;height:2.5px;background:var(--line);border-radius:2px}
.node.reachable{cursor:pointer}
.node.on .sdot{background:var(--grad);border-color:transparent;color:#fff;box-shadow:0 5px 14px rgba(255,93,135,.4)}
.node.on .slab{color:var(--ink);font-weight:800}
.node.done .sdot{background:var(--greenbg);border-color:var(--green);color:var(--green)}
.node.done .slab{color:var(--ink2)}
/* 概要 */
.srcline{margin-bottom:12px}
.stats{display:flex;gap:10px;margin:4px 0 14px}
.stat{flex:1;background:var(--soft);border:1px solid var(--line);border-radius:14px;padding:11px 8px;text-align:center}
.stat b{display:block;font-size:24px;font-weight:800;line-height:1}
.stat span{font-size:12px;color:var(--muted)}
.stat.green b{color:var(--green)}.stat.green{background:var(--greenbg);border-color:#cdeedd}
.stat.amber b{color:var(--amber)}.stat.amber{background:var(--amberbg);border-color:#f3e2c0}
.stat.red b{color:var(--red)}.stat.red{background:var(--redbg);border-color:#f6d3d9}
.facts{font-size:13px;line-height:1.7}.known{color:var(--green);font-weight:700}.unknown{color:var(--amber);font-weight:700}
/* 决定卡 */
.dcard{border:1px solid var(--line);border-left:4px solid var(--line);border-radius:15px;padding:15px 16px;margin:11px 0;background:#fff;box-shadow:var(--shadow2)}
.dcard.auto{border-left-color:var(--green)}.dcard.suggest{border-left-color:var(--amber)}.dcard.ask{border-left-color:var(--red)}
.dcard.done{border-left-color:var(--cyan);background:#fcfdfe}
.dhead{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.dic{font-size:20px}.dtit{font-weight:800;font-size:15.5px;flex:1}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.chip{font-size:12px;color:var(--ink2);background:var(--soft);border:1px solid var(--line);border-radius:18px;padding:3px 10px}
.chip.on{background:var(--greenbg);border-color:#bce6cf;color:var(--green);font-weight:700}
.chosen{font-size:14px;margin:8px 0 6px}.chosen b{font-weight:800}
.meter{display:flex;align-items:center;gap:10px;margin:8px 0 4px}
.meter-track{flex:1;height:9px;border-radius:6px;background:var(--soft);overflow:hidden}
.meter-fill{display:block;height:100%;border-radius:6px}
.meter-fill.hi{background:linear-gradient(90deg,#23c47e,#12a05a)}.meter-fill.mid{background:linear-gradient(90deg,#ffc452,#e89309)}.meter-fill.lo{background:linear-gradient(90deg,#ff7a8f,#e23b5a)}
.meter-lab{font-size:12.5px;font-weight:800;white-space:nowrap}.meter-lab.hi{color:var(--green)}.meter-lab.mid{color:var(--amber)}.meter-lab.lo{color:var(--red)}
.basis,.reason{font-size:13px;color:var(--muted);margin-top:3px}.reason{color:var(--ink2)}
.hist{font-size:12px;color:var(--cyan);margin-top:4px}
.pill{display:inline-flex;align-items:center;gap:4px;font-size:12.5px;font-weight:800;padding:4px 11px;border-radius:20px;white-space:nowrap}
.pill.auto{background:var(--greenbg);color:var(--green)}.pill.suggest{background:var(--amberbg);color:var(--amber)}
.pill.ask{background:var(--redbg);color:var(--red)}.pill.ok{background:var(--cyanbg);color:var(--cyan)}
.ask{margin-top:10px;background:var(--redbg);border:1px solid #f6d3d9;border-radius:12px;padding:11px 12px}
.ask-q{color:var(--red);font-size:13px;font-weight:700;margin-bottom:7px}
.ask .fld{background:#fff}
.sug{display:flex;align-items:center;gap:9px;margin-top:10px;background:var(--amberbg);border:1px solid #f3e2c0;border-radius:12px;padding:11px 12px;font-size:13.5px;font-weight:700;color:var(--amber);cursor:pointer}
.sug input{width:auto;accent-color:var(--amber);transform:scale(1.15)}
/* 评审 */
.reviewsum{font-size:13.5px;color:var(--ink2);background:var(--soft);border-radius:12px;padding:10px 13px;margin-bottom:12px}
.qhint{font-size:13px;font-weight:700;margin:6px 0}
.qedits{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}
details.adv{margin:10px 0}details.adv summary{cursor:pointer;color:var(--b2);font-size:13px;font-weight:700}
.advrow{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin-top:10px}
.advrow input,.advrow select{width:auto}.advrow input[type=text],.advrow input:not([type]){min-width:150px}
.editline{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13.5px;padding:7px 0;border-bottom:1px dashed var(--line)}
.editline .who{color:var(--cyan);font-weight:800}.eg2,.editline .eg{color:var(--red);cursor:pointer;font-size:12.5px;font-weight:700}
.merge{color:var(--green);font-size:13.5px;margin:5px 0;font-weight:600}
.noted{color:var(--cyan);font-size:13.5px;margin:5px 0}
.sec-title{font-size:14px;font-weight:800;color:var(--ink);margin:14px 0 6px;padding-left:10px;border-left:3px solid var(--b2)}
.conflict{background:var(--amberbg);border:1px solid #f3e2c0;border-radius:14px;padding:13px 15px;margin:10px 0}
.conflict .sug{display:block;background:none;border:0;padding:0;color:var(--amber);font-weight:800;margin:6px 0}
.conflict .why{color:var(--ink2);font-size:13px;white-space:pre-line;margin:5px 0 9px;line-height:1.6}
.opt{display:flex;align-items:center;gap:9px;background:#fff;border:1.5px solid var(--line);border-radius:11px;padding:10px 12px;margin:6px 0;cursor:pointer;font-size:13.5px;transition:.12s}
.opt:hover{border-color:var(--b2)}
.opt input{accent-color:var(--b2);transform:scale(1.2)}
/* 执行 */
.exec{background:#0f1320;border-radius:15px;padding:16px 18px;font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12.5px;line-height:1.7;color:#cfd6ea}
.exec .e-step{font-weight:800;color:#fff;margin-top:9px}
.e-ok{color:#4ade80}.e-warn{color:#fbbf24}.e-fail{color:#fb7185}.e-note{color:#94a3b8}
.cardcta{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px;align-items:center}
.muted{color:var(--muted);font-size:13px}.hidden{display:none}
/* 演示页 */
.demohead{display:flex;flex-wrap:wrap;gap:14px;align-items:center}
.seg{display:inline-flex;background:var(--soft);border-radius:11px;padding:3px}
.seg button{border:0;background:transparent;color:var(--muted);font:inherit;font-weight:700;font-size:13px;padding:6px 13px;border-radius:8px;cursor:pointer}
.seg button.on{background:#fff;color:var(--ink);box-shadow:var(--shadow2)}
.demowrap{display:grid;grid-template-columns:300px 1fr;gap:16px;margin-top:14px}
.chk{display:flex;gap:9px;padding:9px 10px;border-radius:11px;cursor:pointer}
.chk:hover{background:var(--soft)}.chk .ic{flex:none}.chk.pass .ic{color:var(--green)}.chk.fail .ic{color:var(--red)}
.chk .t{font-size:12.5px}.chk .t small{display:block;color:var(--muted)}
.demoview{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12px;line-height:1.7;color:var(--ink2);max-height:62vh;overflow:auto}
.demoview .sec-title{font-family:inherit}
.flash{animation:fl 1.3s ease}@keyframes fl{0%{background:rgba(255,93,135,.22)}100%{background:transparent}}
@media(max-width:680px){.feats{grid-template-columns:1fr}.demowrap{grid-template-columns:1fr}.gmvbox{order:3}}
</style></head>
<body>
<div class="bar">
  <div class="brand"><div class="logo">🤖</div>
    <div><h1>会替你拿主意的活动 Agent</h1><p>该自己定的定，该问你的问 · 还帮整个"局"协调</p></div></div>
  <div class="tabs"><button id="tabI" class="on" onclick="showTab('I')">✍️ 自己输入</button>
    <button id="tabD" onclick="showTab('D')">🎬 一键演示</button></div>
  <div class="gmvbox"><b id="gmv">¥0</b><span>本次撬动 GMV</span></div>
</div>

<main>
<!-- ============ 交互 ============ -->
<div id="paneI">
  <!-- 步骤1 -->
  <div id="pGoal" class="card hero">
    <h2>说一句话，我帮你把下午安排明白 ☀️</h2>
    <div class="lead">不是给你一堆链接让你自己挑——而是<b>真的把事办完</b>：去哪玩、去哪吃、要不要排队、确认后一键下单、还能发给家人一起定。</div>
    <div class="tagline">🤝 像一个<b>真正懂你的朋友</b>：帮你把位订了、把娃的避辣也想到了——但<b>刷你的卡之前，一定先问你一句</b>。</div>
    <div class="feats">
      <div class="feat g"><div class="fi">✅</div><b>有把握 → 直接定</b><span>常识性的我自己拍板，不烦你</span></div>
      <div class="feat a"><div class="fi">💡</div><b>拿不准 → 给建议</b><span>给方案，最后你点头</span></div>
      <div class="feat r"><div class="fi">❓</div><b>代价高 → 停下来问</b><span>预算/到家时间这种，我不敢替你定</span></div>
    </div>
    <textarea id="goal" placeholder="例如：今天下午想和老婆孩子出去玩几小时，别离家太远，老婆最近在减肥，帮我安排"></textarea>
    <div class="egs">点一下直接填：
      <span class="eg" onclick="setGoal(this)">今天下午和老婆孩子出去玩，别太远，老婆最近在减肥</span>
      <span class="eg" onclick="setGoal(this)">周末四个朋友聚餐，离家别太远，口味不太一样</span>
      <span class="eg" onclick="setGoal(this)">下午带5岁孩子去游乐场，再吃个火锅</span></div>
    <div class="locbar" id="locbar">
      <span id="locLabel" class="muted">📍 正在获取你的位置…</span>
      <button class="locbtn" type="button" onclick="detectLoc()" title="用浏览器定位重新获取">↻ 重新定位</button>
      <input id="locInput" placeholder="或手动输入地点，如：国贸 / 五道口 / 三里屯"
             onkeydown="if(event.key==='Enter')setLocManual()">
      <button class="locbtn" type="button" onclick="setLocManual()">用这个地点</button>
    </div>
    <div class="opts">
      <label>人数 <input id="party" type="number" value="3" min="1" max="12"></label>
      <label class="chkbox" title="AI 听懂你的自然语言 + 高德真实 POI；调不通自动回退本地库"><input id="useLlm" type="checkbox" checked> 🌐 AI 听懂你的话 + 真实数据</label>
      <label class="chkbox" title="执行时故意触发一次满座/超时/配送失败，演示兜底"><input id="exc" type="checkbox" checked> 演示异常兜底</label>
      <span id="llmStatus" class="muted"></span>
      <button class="btn" id="startBtn" onclick="start()" style="margin-left:auto">✨ 帮我安排</button>
    </div>
  </div>

  <div class="steps" id="stepper">
    <div class="node on" data-step="0" onclick="goto(0)"><span class="sdot">1</span><span class="slab">说目标</span></div>
    <div class="node" data-step="1" onclick="goto(1)"><span class="sdot">2</span><span class="slab">看方案·拍板</span></div>
    <div class="node" data-step="2" onclick="goto(2)"><span class="sdot">3</span><span class="slab">邀人评审</span></div>
    <div class="node" data-step="3" onclick="goto(3)"><span class="sdot">4</span><span class="slab">执行下单</span></div>
  </div>

  <!-- 步骤2 -->
  <div id="pPlan" class="card hidden">
    <div class="shd"><div class="no">2</div><div><h2>这是我的方案，你来拍板</h2>
      <p>每个决定都标了「我有多大把握」和「我打算怎么处理」。<b style="color:var(--red)">红色❓需要你回答</b>，黄色💡勾一下是否采纳。</p></div></div>
    <div class="srcline" id="src"></div>
    <div class="stats" id="summary"></div>
    <div class="autorow">
      <span class="auto-l">🎚️ 我希望它替我做主的程度</span>
      <input id="autonomy" type="range" min="0" max="2" step="1" value="1" oninput="setAutonomy(this.value)">
      <span id="autonomyLabel" class="auto-v">平衡</span>
    </div>
    <div id="memoryBanner" class="membar hidden"></div>
    <div id="quadrant"></div>
    <div class="facts"><div id="known"></div><div id="unknown"></div></div>
    <div id="decisions" style="margin-top:8px"></div>
    <div id="mapbox"></div>
    <div id="timeline" class="timeline"></div>
    <div id="tips"></div>
    <div id="replanBanner" class="replan hidden"></div>
    <div style="margin-top:16px"><button class="btn" onclick="submitAnswers()">提交回答，继续 →</button></div>
  </div>

  <!-- 步骤3 -->
  <div id="pReview" class="card hidden">
    <div class="shd"><div class="no">3</div><div><h2>邀人评审（可选 · 升维亮点）</h2>
      <p>攒局不是一个人的事。点下面按钮模拟"局里的人"各自改方案——我自动合并无争议的，撞车处按「内容契合度 × 谁说的」给带理由的建议。</p></div></div>
    <div class="reviewsum" id="reviewSummary"></div>
    <div id="reviewTimeline" class="timeline"></div>
    <div id="reviewTips"></div>
    <div class="sharebox">
      <button class="btn ghost sm" type="button" onclick="shareLink()">📲 生成链接，发给朋友自己改</button>
      <div id="shareRow" class="hidden">
        <input id="shareUrl" onclick="this.select()" title="若手机打不开，把这里的 IP 改成你电脑 WiFi 的 IP">
        <button class="locbtn" type="button" onclick="copyShare()">复制</button>
        <button class="locbtn" type="button" onclick="pullEdits()">🔄 收取</button>
        <span id="shareRecv" class="muted"></span>
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px">朋友同一 WiFi 用手机打开即可改；或你自己另开一个无痕窗口模拟朋友。提交后自动并入下方改动（每 4 秒刷新一次）。</div>
    </div>
    <div class="qhint">① 一键模拟几个人的改动（或用上面的链接让朋友真改）：</div>
    <div class="qedits" id="quickEdits"></div>
    <details class="adv"><summary>高级：自定义一条改动（指定谁、权重、换哪家 / 加什么约束）</summary>
      <div class="advrow">
        <input id="edAuthor" placeholder="谁（如 老婆）" style="width:130px">
        <label class="muted">权重</label><input id="edWeight" type="number" value="0.6" min="0" max="1" step="0.1" style="width:70px">
        <select id="edType" onchange="edTypeChange()" style="width:120px"><option value="restaurant">换餐厅</option><option value="constraint">加约束</option></select>
        <select id="edRest" style="min-width:200px"></select>
        <input id="edConstraint" class="hidden" placeholder="如：我对海鲜过敏">
        <button class="btn ghost sm" onclick="addEdit()">+ 添加</button>
      </div>
    </details>
    <div id="editList"></div>
    <div class="cardcta">
      <button class="btn" id="reviewBtn" onclick="runReview()" disabled>② 运行评审合并 →</button>
      <button class="btn ghost" onclick="execute()">跳过评审，直接执行 →</button>
    </div>
    <div id="autoMerged"></div>
    <div id="conflicts"></div>
    <div id="resolveRow" class="cardcta hidden"><button class="btn" onclick="applyResolve()">③ 应用裁决，生成新版方案 →</button></div>
    <div id="v2Box" class="hidden" style="margin-top:8px">
      <div class="sec-title">新版方案 <span class="muted" id="v2Ver"></span></div>
      <div id="v2Decisions"></div>
      <div class="cardcta"><button class="btn ok" onclick="execute()">确认这版，去执行 →</button></div>
    </div>
  </div>

  <!-- 步骤4 -->
  <div id="pExec" class="card hidden">
    <div class="shd"><div class="no">4</div><div><h2>执行下单（含异常兜底）</h2>
      <p>按依赖顺序真把单下掉，每步状态可见；满座换备选、超时重试、失败回滚补偿，GMV 同步跳动。</p></div></div>
    <div class="exec" id="execOut"></div>
    <div class="cardcta">
      <button class="btn ok" id="cardBtn" style="display:none" onclick="openCard()">📲 打开可分享方案卡（递给老婆 / 发小张）</button>
      <button class="btn ghost" onclick="goto(0)">↺ 换个目标重新来</button>
    </div>
    <div class="muted" id="cardHint" style="display:none;margin-top:6px">这就是"把手机递给老婆看"的那一屏——手机端友好、可一键复制分享文案。</div>
  </div>
</div>

<!-- ============ 演示 ============ -->
<div id="paneD" class="hidden">
  <div class="card lite">
    <div class="muted" style="margin-bottom:10px">不想自己输？这里把两个内置场景从头跑给你看，左侧 9 条验收清单自动打勾。</div>
    <div class="demohead">
      <span class="muted">场景</span><div class="seg" id="scn"><button data-v="family" class="on">家庭</button><button data-v="friends">朋友局</button></div>
      <span class="muted">冲突裁决</span><div class="seg" id="cf"><button data-v="suggestion" class="on">采纳建议</button><button data-v="alt">选另一方</button></div>
      <button class="btn sm" onclick="runDemo()">▶ 运行</button>
      <button class="btn ghost sm" onclick="runSelftest()">自检 selftest</button>
    </div>
  </div>
  <div class="demowrap">
    <div class="card lite"><div style="font-size:13px;color:var(--muted);font-weight:800;margin-bottom:6px">验收清单（PRD 9 条）</div>
      <div id="checklist"></div><div id="score" style="font-weight:800;margin-top:8px"></div>
      <div id="stRes" class="muted" style="margin-top:8px"></div></div>
    <div class="card lite"><div class="demoview" id="demoView"><span class="muted">点「运行」开始</span></div></div>
  </div>
</div>
</main>

<script>
function $(id){return document.getElementById(id);}
function esc(s){return (s==null?'':''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function setGmv(v){$('gmv').textContent='¥'+Math.round(v||0);}
function showTab(t){$('tabI').classList.toggle('on',t==='I');$('tabD').classList.toggle('on',t==='D');
  $('paneI').classList.toggle('hidden',t!=='I');$('paneD').classList.toggle('hidden',t!=='D');}
function setGoal(el){$('goal').value=el.textContent.trim();goto(0);$('goal').focus();}
async function post(url,body){return await(await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();}

let S={sid:null,party:3,edits:[],candidates:[],loc:null,area:null,pulled:0};
let LOC_OK=false;  // 高德是否可用（决定要不要定位）
function setLocLabel(txt,cls){const el=$('locLabel');el.textContent=txt;el.className=(cls||'muted');}
function detectLoc(){
  if(!LOC_OK){setLocLabel('📍 未配置高德，定位不可用（仍可演示）','muted');return;}
  if(!navigator.geolocation){setLocLabel('📍 浏览器不支持定位，请手动输入地点','err');return;}
  setLocLabel('📍 正在获取你的位置…','muted');
  navigator.geolocation.getCurrentPosition(async pos=>{
    const r=await post('/api/geo',{lng:pos.coords.longitude,lat:pos.coords.latitude});
    if(r.ok){S.loc=r.loc;S.area=r.area;setLocLabel('📍 已定位：'+r.area+'（给你搜附近）','ok');}
    else{setLocLabel('📍 '+(r.error||'定位失败')+'，可手动输入地点','err');}
  },err=>{setLocLabel('📍 定位被拒绝/失败，请手动输入地点（如 国贸）','err');},
  {enableHighAccuracy:true,timeout:8000,maximumAge:300000});
}
async function setLocManual(){
  const a=$('locInput').value.trim();if(!a){alert('先输入一个地点');return;}
  if(!LOC_OK){setLocLabel('📍 未配置高德，无法解析地点','err');return;}
  setLocLabel('📍 正在解析「'+a+'」…','muted');
  const r=await post('/api/geo',{address:a});
  if(r.ok){S.loc=r.loc;S.area=r.area;setLocLabel('📍 已锁定：'+r.area+'（给你搜附近）','ok');}
  else{setLocLabel('📍 '+(r.error||'解析失败'),'err');}
}
let cur=0,maxReached=0;
const PANELS=['pGoal','pPlan','pReview','pExec'];
function goto(n){if(n>maxReached)return;cur=n;
  PANELS.forEach((id,i)=>$(id).classList.toggle('hidden',i!==n));
  document.querySelectorAll('.node').forEach((el,i)=>{el.classList.toggle('on',i===cur);
    el.classList.toggle('done',i<maxReached&&i!==cur);el.classList.toggle('reachable',i<=maxReached);});
  window.scrollTo({top:0,behavior:'smooth'});}
function reach(n){maxReached=Math.max(maxReached,n);goto(n);}

const DEC_ICON={choose_activity:'🎡',choose_restaurant:'🍽️',send_gift:'🎁',set_budget:'💰',set_return_time:'🕖',nap_window:'😴',child_safety:'🧒'};
function confMeter(v){const pct=Math.round(v*100);const lvl=v>=0.7?'hi':v>=0.4?'mid':'lo';
  const lab=v>=0.7?'有把握':v>=0.4?'中等':'没把握';
  return `<div class="meter"><div class="meter-track"><i class="meter-fill ${lvl}" style="width:${pct}%"></i></div><span class="meter-lab ${lvl}">${v.toFixed(2)} · ${lab}</span></div>`;}
function dispoBadge(d){return {auto:'<span class="pill auto">✅ 直接定</span>',suggest:'<span class="pill suggest">💡 给建议</span>',ask:'<span class="pill ask">❓ 停下来问</span>'}[d]||'';}
function optFacts(o){if(!o)return'';let b=[];if(o.per_capita!=null)b.push('人均'+Math.round(o.per_capita));
  if(o.child_friendly)b.push(o.kind==='restaurant'?'有儿童餐':'亲子');if(o.has_low_cal)b.push('低卡');
  if(o.has_spicy_option)b.push('辣口');if(o.distance_km!=null)b.push(o.distance_km+'km');
  if(o.allergens&&o.allergens.length)b.push('含'+o.allergens.join('/'));return b.length?'（'+b.join('·')+'）':'';}

function decCard(d,interactive,i){
  const resolved=(d.status==='confirmed'||d.status==='done');
  const ic=DEC_ICON[d.type]||'🤖';
  const badge=resolved?'<span class="pill ok">✅ 你已拍板</span>':dispoBadge(d.disposition);
  const delay=(typeof i==='number')?` style="animation-delay:${(i*0.09).toFixed(2)}s"`:'';
  let h=`<div class="dcard reveal ${resolved?'done':d.disposition}" data-dec="${d.id}"${delay}>`;
  // 情感高光：送礼这条做成 AI 主动开口的对话气泡（路演记忆点）
  if(d.type==='send_gift')
    h+=`<div class="aispeak">💬 “我注意到你提到 ta 最近在减肥——高热量蛋糕可能适得其反，我把礼物换成了<b>一束花 + 一张她一直想看的展的票</b>。更贴心、也不破坏她的计划。<b>要这样吗？</b>”<span class="aitag">这是建议，不是替你定 · 分寸感由你拍板</span></div>`;
  h+=`<div class="dhead"><span class="dic">${ic}</span><span class="dtit">${esc(d.description)}</span>${badge}</div>`;
  if(d.options&&d.options.length&&d.type==='choose_restaurant')
    h+=`<div class="chips">${d.options.slice(0,6).map(o=>`<span class="chip${d.chosen&&o.label===d.chosen.label?' on':''}">${esc(o.label)}${optFacts(o)}</span>`).join('')}</div>`;
  if(d.chosen)h+=`<div class="chosen">→ 选定 <b>${esc(d.chosen.label)}</b></div>`;
  h+=confMeter(d.confidence);
  h+=`<div class="basis">依据：${esc(d.confidence_basis)}</div>`;
  h+=`<div class="reason">${esc(d.reasoning)}</div>`;
  if(d.counterfactual)h+=`<div class="cf">🔮 如果我没拦你：${esc(d.counterfactual)}</div>`;
  (d.history||[]).forEach(x=>h+=`<div class="hist">↻ ${esc(x)}</div>`);
  if(interactive&&!resolved&&d.disposition==='ask')
    h+=`<div class="ask"><div class="ask-q">❓ 需要你回答</div><input class="ask-input fld" data-dec="${d.id}" placeholder="如：人均120以内 / 19:30前到家"></div>`;
  if(interactive&&!resolved&&d.disposition==='suggest')
    h+=`<label class="sug"><input type="checkbox" class="sug-check" data-dec="${d.id}" checked> 采纳这条建议</label>`;
  return h+'</div>';
}
function summaryBanner(decs){
  const a=decs.filter(d=>d.disposition==='auto').length;
  const s=decs.filter(d=>d.disposition==='suggest').length;
  const q=decs.filter(d=>d.disposition==='ask').length;
  return `<div class="stat green"><b>${a}</b><span>✅ 直接定</span></div><div class="stat amber"><b>${s}</b><span>💡 给建议</span></div><div class="stat red"><b>${q}</b><span>❓ 想问你</span></div>`;
}

function renderTimeline(tl){
  if(!tl||!tl.length)return '';
  const rows=tl.map(s=>{
    const span=(s.end&&s.end!==s.start)?esc(s.start)+'–'+esc(s.end):esc(s.start);
    return `<div class="tl-row"><span class="tl-time">${span}</span><span class="tl-act">${esc(s.title)}</span></div>`;
  }).join('');
  return '<div class="tl-title">⏱ 行程时间线（随你定的到家时间自动倒排）</div>'+rows;
}
function renderQuadrant(decs){
  if(!decs||!decs.length)return '';
  const W=330,H=200,PADX=40;
  const xc=c=>PADX+(W-PADX-14)*Math.max(0,Math.min(1,c||0));
  const yc=cost=>({low:H-44,mid:(H-30)/2,high:24})[cost]||((H-30)/2);
  const col=d=>({auto:'#12a05a',suggest:'#cf7a09',ask:'#e23b5a'})[d.disposition]||'#888';
  let body='';
  decs.forEach((d,i)=>{
    const x=xc(d.confidence),y=yc(d.cost)+(i%2?7:-7);
    body+=`<circle cx="${x}" cy="${y}" r="7" fill="${col(d)}" opacity=".9"/>`;
    body+=`<text x="${x+10}" y="${y+4}" font-size="10" fill="#444b60">${esc((d.description||'').slice(0,7))}</text>`;
  });
  return `<div class="quad"><div class="quad-h">🧠 决策大脑：每个决定落在「置信度 × 代价」的哪一格</div>
   <svg viewBox="0 0 ${W} ${H}" class="quad-svg">
    <rect x="${PADX}" y="6" width="${W-PADX-6}" height="${H-30}" fill="none" stroke="#e9ebf3"/>
    <line x1="${PADX}" y1="${(H-30)/2+6}" x2="${W-6}" y2="${(H-30)/2+6}" stroke="#eef0f6" stroke-dasharray="3 3"/>
    <text x="${PADX}" y="${H-4}" font-size="10" fill="#8a90a4">← 没把握　置信度　有把握 →</text>
    <text x="2" y="16" font-size="10" fill="#8a90a4">代价高↑</text>
    <text x="2" y="${H-34}" font-size="10" fill="#8a90a4">代价低</text>
    ${body}
   </svg>
   <div class="quad-legend"><span class="lg g">●</span> 直接定　<span class="lg a">●</span> 给建议　<span class="lg r">●</span> 停下来问</div>
  </div>`;
}
const AUTO_LV=['conservative','balanced','bold'];
const AUTO_TXT={conservative:'保守·多问我',balanced:'平衡',bold:'大胆·多替我定'};
async function setAutonomy(v){
  const level=AUTO_LV[+v]||'balanced';
  $('autonomyLabel').textContent=AUTO_TXT[level];
  if(!S.sid)return;
  const r=await post('/api/autonomy',{sid:S.sid,level});
  if(!r.ok)return;
  $('summary').innerHTML=summaryBanner(r.plan.decisions);
  $('decisions').innerHTML=r.plan.decisions.map((d,i)=>decCard(d,true,i)).join('');
  $('quadrant').innerHTML=renderQuadrant(r.plan.decisions);
}
function showMap(box){
  if(!S.sid){$(box).innerHTML='';return;}
  $(box).innerHTML=`<div class="mapwrap"><img class="mapimg" src="/api/map?sid=${encodeURIComponent(S.sid)}&t=${(new Date()).getTime()}" alt="路线图" onerror="var w=this.closest('.mapwrap');if(w)w.style.display='none'"><div class="mapcap">🗺️ 真实地图路线：家 → 玩 → 吃（高德实景，评委可当场搜证）</div></div>`;
}
async function forgetPrefs(){await post('/api/forget',{});$('memoryBanner').classList.add('hidden');}
function renderTips(tips){
  if(!tips||!tips.length)return '';
  return '<div class="tips"><div class="tips-h">🤝 出发前，几句贴心提醒</div>'+
    tips.map(t=>`<div class="tip"><span class="tip-i">${esc(t.icon||'·')}</span><span>${esc(t.text)}</span></div>`).join('')+'</div>';
}
function replanBannerHtml(plan){
  const f=t=>plan.decisions.find(d=>d.type===t);
  const rest=f('choose_restaurant'),bdg=f('set_budget'),ret=f('set_return_time');
  let head='✅ 已按你的回答重新规划';
  if(rest&&rest.chosen){head+='：餐厅 → <b>'+esc(rest.chosen.label)+'</b>'+(rest.chosen.price?'（人均 ¥'+Math.round(rest.chosen.price)+'）':'');}
  let sub=[];
  if(bdg&&bdg.chosen)sub.push('预算：'+esc(bdg.chosen.label));
  if(ret&&ret.chosen)sub.push('到家：'+esc(ret.chosen.label));
  return head+(sub.length?'<small>'+sub.join('　·　')+'　→　餐厅与行程时间线已随之更新</small>':'');
}

async function start(){
  if(!$('goal').value.trim()){alert('先写一句你的目标');return;}
  const useLlm=$('useLlm').checked;
  $('startBtn').disabled=true;$('startBtn').textContent=useLlm?'查真实数据中…':'分析中…';
  const r=await post('/api/start',{goal:$('goal').value,party_size:+$('party').value,exceptions:$('exc').checked,use_llm:useLlm,loc:S.loc||''});
  $('startBtn').disabled=false;$('startBtn').textContent='✨ 帮我安排';
  if(!r.ok){alert(r.error);return;}
  S.sid=r.sid;S.party=r.party;S.edits=[];$('party').value=r.party;
  const src=(r.source&&r.source.indexOf('高德')>=0)?'<span class="pill ok">🌐 候选来自高德真实 POI · 评委可当场搜证</span>'
    :(r.source==='AI'?'<span class="pill ok">🧠 候选来自 AI 实时生成</span>':'<span class="muted">候选来自本地真实连锁库</span>');
  $('src').innerHTML=src;
  $('summary').innerHTML=summaryBanner(r.plan.decisions);
  $('known').innerHTML='<span class="known">✓ 我读懂的：</span>'+(r.known.length?r.known.map(k=>'<br>　· '+esc(k)).join(''):'（这句话没给明确约束）');
  $('unknown').innerHTML='<span class="unknown">? 我还不知道的（不替你乱填）：</span>'+(r.unknown.length?r.unknown.map(k=>'<br>　· '+esc(k)).join(''):'无');
  $('decisions').innerHTML=r.plan.decisions.map((d,i)=>decCard(d,true,i)).join('');
  $('quadrant').innerHTML=renderQuadrant(r.plan.decisions);
  $('autonomy').value=1;$('autonomyLabel').textContent='平衡';
  showMap('mapbox');
  $('timeline').innerHTML=renderTimeline(r.plan.timeline);
  $('tips').innerHTML=renderTips(r.plan.tips);
  $('replanBanner').classList.add('hidden');
  // 偏好记忆：记得你→横幅提示 + 预填"停下来问"的输入框（仍由你确认，不静默填）
  const mem=r.memory&&r.memory.summary;
  if(mem){
    $('memoryBanner').innerHTML=`🧠 我记得你：<b>${esc(mem)}</b> —— 这次先按这个预填好了，<b>确认或改都行</b><span class="forget" onclick="forgetPrefs()">忘记我的偏好</span>`;
    $('memoryBanner').classList.remove('hidden');
    const p=(r.memory&&r.memory.prefs)||{};
    r.plan.decisions.forEach(d=>{
      const inp=document.querySelector('.ask-input[data-dec="'+d.id+'"]');
      if(!inp)return;
      if(d.type==='set_budget'&&p.budget_per_capita)inp.value='人均 '+Math.round(p.budget_per_capita)+' 以内';
      if(d.type==='set_return_time'&&p.return_time)inp.value=p.return_time;
    });
  }else $('memoryBanner').classList.add('hidden');
  setGmv(r.plan.gmv_estimate);
  reach(1);
}

async function submitAnswers(){
  const answers={},suggestions={};
  document.querySelectorAll('.ask-input').forEach(i=>{if(i.value.trim())answers[i.dataset.dec]=i.value.trim();});
  document.querySelectorAll('.sug-check').forEach(c=>suggestions[c.dataset.dec]=c.checked);
  const r=await post('/api/answer',{sid:S.sid,answers,suggestions});
  if(!r.ok){alert(r.error);return;}
  $('decisions').innerHTML=r.plan.decisions.map((d,i)=>decCard(d,false,i)).join('');
  $('summary').innerHTML='<div class="stat green" style="flex:1"><b>✓</b><span>方案已按你的回答重新规划</span></div>';
  const banner=replanBannerHtml(r.plan);
  $('timeline').innerHTML=renderTimeline(r.plan.timeline);
  $('tips').innerHTML=renderTips(r.plan.tips);
  $('replanBanner').innerHTML=banner;$('replanBanner').classList.remove('hidden');
  setGmv(r.plan.gmv_estimate);
  S.candidates=r.candidates||[];S.pulled=0;
  $('reviewSummary').innerHTML='<div class="replan">'+banner+'</div>';
  $('reviewTimeline').innerHTML=renderTimeline(r.plan.timeline);
  $('reviewTips').innerHTML=renderTips(r.plan.tips);
  buildQuickEdits();
  const sel=$('edRest');sel.innerHTML=S.candidates.map(o=>`<option value="${o.id}">${esc(o.label)}${optFacts(o)}</option>`).join('');
  $('autoMerged').innerHTML='';$('conflicts').innerHTML='';$('resolveRow').classList.add('hidden');$('v2Box').classList.add('hidden');
  reach(2);
}

function buildQuickEdits(){
  const c=S.candidates;const box=$('quickEdits');let b=[];
  const near=c.slice().sort((x,y)=>(x.distance_km??99)-(y.distance_km??99))[0];
  const far=c.slice().sort((x,y)=>(y.distance_km??0)-(x.distance_km??0))[0];
  const sea=c.find(o=>o.allergens&&o.allergens.includes('海鲜'));
  if(near)b.push(`<button class="btn ghost sm" onclick="quickEdit('老婆',0.9,'restaurant','${near.id}','换最近的')">👩 老婆嫌远 → 换最近「${esc(near.label)}」</button>`);
  if(sea)b.push(`<button class="btn ghost sm" onclick="quickEdit('朋友甲',0.5,'restaurant','${sea.id}','想吃海鲜')">🧑 朋友想吃「${esc(sea.label)}」</button>`);
  else if(far&&far!==near)b.push(`<button class="btn ghost sm" onclick="quickEdit('朋友甲',0.5,'restaurant','${far.id}','')">🧑 朋友想换「${esc(far.label)}」</button>`);
  b.push(`<button class="btn ghost sm" onclick="quickEdit('朋友乙',0.5,'constraint','我对海鲜过敏')">➕ 有人海鲜过敏</button>`);
  b.push(`<button class="btn ghost sm" onclick="quickEdit('阿强',0.55,'constraint','我无辣不欢')">➕ 有人无辣不欢</button>`);
  box.innerHTML=b.join('');
}
function quickEdit(author,weight,type,val,note){
  let e={author,weight,type};
  if(type==='constraint')e.constraint=val;
  else{e.after_id=val;const o=S.candidates.find(x=>x.id===val);e._label=o?o.label:val;e.note=note||'';}
  S.edits.push(e);renderEdits();$('reviewBtn').disabled=false;
}
function edTypeChange(){const c=$('edType').value==='constraint';$('edConstraint').classList.toggle('hidden',!c);$('edRest').classList.toggle('hidden',c);}
function addEdit(){
  const name=$('edAuthor').value.trim();if(!name){alert('先填"谁"改的');return;}
  const w=+$('edWeight').value,type=$('edType').value;let e={author:name,weight:w,type};
  if(type==='constraint'){e.constraint=$('edConstraint').value.trim();if(!e.constraint){alert('填写约束内容');return;}}
  else{const o=S.candidates.find(x=>x.id===$('edRest').value);e.after_id=$('edRest').value;e._label=o?o.label:e.after_id;}
  S.edits.push(e);renderEdits();$('edAuthor').value='';$('edConstraint').value='';$('reviewBtn').disabled=false;
}
function renderEdits(){
  $('editList').innerHTML=S.edits.length?('<div class="muted" style="margin:8px 0 2px">已加入的改动：</div>'+S.edits.map((e,i)=>`<div class="editline"><span class="who">${esc(e.author)}</span>
   <span class="muted">权重 ${e.weight}</span> ${e.type==='constraint'?'加约束「'+esc(e.constraint)+'」':'换餐厅 → '+esc(e._label)}
   <span class="eg2" onclick="delEdit(${i})">删除</span></div>`).join('')):'';
}
function delEdit(i){S.edits.splice(i,1);renderEdits();if(!S.edits.length)$('reviewBtn').disabled=true;}

let SHARE_POLL=null;
async function shareLink(){
  if(!S.sid){alert('请先生成方案');return;}
  const r=await(await fetch('/api/shareurl?sid='+encodeURIComponent(S.sid))).json();
  if(!r.ok){alert(r.error);return;}
  $('shareUrl').value=r.url;$('shareRow').classList.remove('hidden');
  if(!SHARE_POLL)SHARE_POLL=setInterval(pullEdits,4000);
  pullEdits();
}
function copyShare(){const i=$('shareUrl');i.select();
  if(navigator.clipboard)navigator.clipboard.writeText(i.value);
  $('shareRecv').textContent='链接已复制，发给朋友吧';}
async function pullEdits(){
  if(!S.sid)return;
  const r=await(await fetch('/api/edits?sid='+encodeURIComponent(S.sid))).json();
  if(!r.ok)return;
  const all=r.edits||[];const fresh=all.slice(S.pulled||0);
  if(fresh.length){fresh.forEach(e=>S.edits.push(e));S.pulled=all.length;
    renderEdits();$('reviewBtn').disabled=false;}
  $('shareRecv').textContent='朋友已提交 '+all.length+' 条改动'+(all.length?'（已并入下方）':'');
}
async function runReview(){
  const r=await post('/api/review',{sid:S.sid,edits:S.edits});
  if(!r.ok){alert(r.error);return;}
  $('autoMerged').innerHTML='<div class="sec-title">合并结果</div>'+r.auto_merged.map(e=>{
    const txt=e.constraint?('「'+esc(e.constraint)+'」'+esc(e.merged_note||'')):('餐厅→'+esc(e.label||''));
    return e.actionable!==false?`<div class="merge">✅ 自动合并（无争议）：${esc(e.author)} 的 ${txt}</div>`
      :`<div class="noted">📝 已记录（无需打扰）：${esc(e.author)} 的 ${txt}</div>`;}).join('');
  $('conflicts').innerHTML=(r.conflicts.length?'<div class="sec-title">撞车待你裁决</div>':'')+r.conflicts.map(c=>{
    let h=`<div class="conflict"><div style="color:var(--amber);font-weight:800">⚠️ ${ {choose_restaurant:'餐厅'}[c.target]||c.target } 撞车了</div>`;
    h+=`<div class="sug">🤖 我的建议：采纳 ${esc(c.suggestion_owner)}的 ${esc(c.suggestion?c.suggestion.label:'')}</div>`;
    h+=`<div class="why">${esc(c.reason)}</div>`;
    c.competing.forEach(e=>{const isSug=e.edit_id===c.suggested_edit_id;
      h+=`<label class="opt"><input type="radio" name="cf${c.idx}" value="${e.edit_id}" ${isSug?'checked':''}> 采纳 <b>${esc(e.author)}</b> 的 ${esc(e.label)}（权重 ${e.weight}）${isSug?' &nbsp;← 我建议这个':''}</label>`;});
    h+=`<label class="opt"><input type="radio" name="cf${c.idx}" value="none"> 都不采纳，保持原样</label>`;
    return h+'</div>';}).join('');
  if(!r.conflicts.length)$('conflicts').innerHTML+='<div class="muted">没有撞车——所有改动都被无争议自动合并了，可直接生成新版。</div>';
  $('resolveRow').classList.remove('hidden');
  $('resolveRow').scrollIntoView({behavior:'smooth',block:'center'});
}
async function applyResolve(){
  const resolutions={};document.querySelectorAll('#conflicts .conflict').forEach((box,i)=>{
    const sel=box.querySelector('input[type=radio]:checked');resolutions[i]=sel?sel.value:'suggestion';});
  const r=await post('/api/resolve',{sid:S.sid,resolutions});
  if(!r.ok){alert(r.error);return;}
  $('v2Box').classList.remove('hidden');$('v2Ver').textContent='(v'+r.plan.version+')';
  $('v2Decisions').innerHTML=r.plan.decisions.map((d,i)=>decCard(d,false,i)).join('')+renderTimeline(r.plan.timeline)+renderTips(r.plan.tips);
  setGmv(r.plan.gmv_estimate);$('v2Box').scrollIntoView({behavior:'smooth',block:'center'});
}

async function execute(){
  const r=await post('/api/execute',{sid:S.sid});
  if(!r.ok){alert(r.error);return;}
  $('execOut').innerHTML=r.text.split('\n').map(line=>{const t=line.trim();
    let cls='';if(/^\[\d+\/\d+\]/.test(t))cls='e-step';else if(t.startsWith('✅'))cls='e-ok';
    else if(t.startsWith('⚠️'))cls='e-warn';else if(t.startsWith('❌'))cls='e-fail';
    else if(t.startsWith('↳')||t.startsWith('💰')||t.startsWith('👤'))cls='e-note';
    return `<div class="${cls}">${esc(line)}</div>`;}).join('');
  animateGmv(r.gmv_trace,r.gmv);
  if(r.has_card){$('cardBtn').style.display='';$('cardHint').style.display='';}
  reach(3);
}
function openCard(){window.open('/api/card?sid='+encodeURIComponent(S.sid),'_blank');}
function animateGmv(trace,final){const seq=(trace&&trace.length)?trace:[final||0];let k=0;
  (function tick(){setGmv(seq[k]||0);k++;if(k<seq.length)setTimeout(tick,260);else setGmv(final||0);})();}

/* 一键演示 */
let scenario='family',conflict='suggestion';
function seg(id,cb){document.querySelectorAll('#'+id+' button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('#'+id+' button').forEach(x=>x.classList.remove('on'));b.classList.add('on');cb(b.dataset.v);});}
seg('scn',v=>{scenario=v;runDemo();});seg('cf',v=>{conflict=v;runDemo();});
async function runDemo(){
  $('demoView').textContent='运行中…';
  const r=await(await fetch(`/api/run?scenario=${scenario}&conflict=${conflict}&loc=${encodeURIComponent(S.loc||'')}`)).json();
  if(!r.ok){$('demoView').textContent='出错：'+(r.error||'');return;}
  renderChecklist(r.checklist);setGmv(r.gmv);
  $('demoView').innerHTML=r.text.split('\n').map(line=>{const t=line.trim();if(!t)return'';
    if(t.includes('║')){const x=t.replace(/[║╔╗╚╝═]/g,'').trim();return x?`<div class="sec-title">${esc(x)}</div>`:'';}
    if(/^[╔╚╗╝═]+$/.test(t))return'';
    let cls='';if(t.startsWith('🤖'))cls='e-step';else if(t.startsWith('✅'))cls='e-ok';
    else if(t.startsWith('⚠️')||t.startsWith('💡'))cls='e-warn';else if(t.startsWith('❌'))cls='e-fail';
    else if(t.startsWith('↳')||t.startsWith('👤')||t.startsWith('置信度')||t.startsWith('代价'))cls='e-note';
    return `<div data-l class="${cls}">${esc(line)}</div>`;}).join('');
}
function renderChecklist(items){const box=$('checklist');box.innerHTML='';let p=0;
  items.forEach(it=>{if(it.pass)p++;const d=document.createElement('div');d.className='chk '+(it.pass?'pass':'fail');
    d.innerHTML=`<div class="ic">${it.pass?'✅':'❌'}</div><div class="t">[${it.id}] ${esc(it.title)}${it.pass?'':'<small>缺：'+esc((it.missing||[]).join('、'))+'</small>'}</div>`;
    d.onclick=()=>{const ev=it.evidence||'';const hit=[...document.querySelectorAll('#demoView [data-l]')].find(x=>x.textContent.includes(ev.slice(0,16)));
      if(hit){hit.scrollIntoView({block:'center'});hit.classList.remove('flash');void hit.offsetWidth;hit.classList.add('flash');}};
    box.appendChild(d);});
  $('score').textContent=p+'/'+items.length+' 通过 '+(p===items.length?'✅':'');$('score').style.color=p===items.length?'var(--green)':'var(--amber)';}
async function runSelftest(){$('stRes').textContent='自检运行中…';const r=await(await fetch('/api/selftest')).json();
  $('stRes').innerHTML=(r.ok?'✅':'❌')+' selftest '+r.passed+'/'+r.total+' 通过';}
edTypeChange();
(async()=>{try{const s=await(await fetch('/api/llm_status')).json();
  const amap=s.amap&&s.amap.enabled, ai=s.enabled;
  LOC_OK=!!amap;
  if(amap||ai){$('useLlm').checked=true;$('useLlm').disabled=false;
    let bits=[];if(amap)bits.push('高德真实 POI');if(ai)bits.push('AI');
    $('llmStatus').innerHTML='<span class="statuspill">● 已接入 '+bits.join(' + ')+'</span>';}
  else{$('useLlm').checked=false;$('useLlm').disabled=true;
    $('llmStatus').innerHTML='<span class="muted">未配置（走本地库）</span>';}
  if(amap){detectLoc();}                       // 有高德就自动尝试定位
  else{setLocLabel('📍 未配置高德 key，定位不可用（仍可用本地库演示）','muted');$('locInput').disabled=true;}
}catch(e){$('useLlm').disabled=true;setLocLabel('📍 定位不可用','muted');}})();
</script>
</body></html>
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
