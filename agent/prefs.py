"""跨会话偏好记忆 + 信任账户——"越用越懂你、越信越敢替你做主"。

这是本作品的脑洞主线之一：把"该替你决定多少"从一个**静态规则**，变成一段
**会成长的信任关系**——
  · 刚认识：信任低 → 重要的事都先问你（保守）；
  · 你每确认一次它的判断 → 信任 +分；
  · 信任够了 → 它自动获得更多自主权，并把你**反复确认过的事直接替你定**（错了随时撤）。

偏好与信任都记在本地 .prefs.json（已 gitignore）。仅用于交互 web 流，不影响演示/验收。
"""
from __future__ import annotations

import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".prefs.json")
_KEYS = ("budget_per_capita", "return_time", "allergens", "avoid_spicy", "need_low_cal")


# ---------------------------------------------------------------------------
def _read() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write(d: dict) -> None:
    try:
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


# ---- 偏好 -----------------------------------------------------------------
def load() -> dict:
    p = _read()
    return {k: p.get(k) for k in _KEYS if p.get(k) not in (None, "", [])}


def save(d: dict) -> dict:
    p = _read()
    for k in _KEYS:
        v = d.get(k)
        if v not in (None, "", []):
            p[k] = v
    _write(p)
    return load()


def summary(p: dict) -> str:
    bits = []
    if p.get("budget_per_capita"):
        bits.append(f"人均 ≤{int(p['budget_per_capita'])}")
    if p.get("allergens"):
        bits.append("忌口 " + "/".join(p["allergens"]))
    if p.get("return_time"):
        bits.append(f"常按「{p['return_time']}」到家")
    if p.get("need_low_cal"):
        bits.append("偏清淡低卡")
    return "、".join(bits)


def clear() -> None:
    try:
        os.remove(_PATH)
    except Exception:
        pass


# ---- 信任账户 -------------------------------------------------------------
def load_trust() -> dict:
    t = _read().get("_trust") or {}
    return {"score": int(t.get("score", 0)), "confirms": dict(t.get("confirms", {}))}


def save_trust(t: dict) -> None:
    p = _read()
    p["_trust"] = {"score": max(0, min(100, int(t.get("score", 0)))),
                   "confirms": dict(t.get("confirms", {}))}
    _write(p)


def trust_level(score: int) -> str:
    """信任分 → 它能替你做主的程度。"""
    if score >= 70:
        return "bold"
    if score >= 30:
        return "balanced"
    return "conservative"


def trust_label(score: int) -> str:
    return {"bold": "大胆 · 大部分敢替你定",
            "balanced": "平衡 · 常识自己定、要紧的问你",
            "conservative": "谨慎 · 才认识，重要的都先问你"}[trust_level(score)]


def bump_trust(confirmed_asks: int, accepted_suggestions: int,
               confirmed_types: list) -> dict:
    """用户每确认一次 Agent 的判断，信任就长一点；并记下'这类决定被确认过几次'。"""
    t = load_trust()
    t["score"] = min(100, t["score"] + confirmed_asks * 9 + accepted_suggestions * 5)
    for ty in confirmed_types:
        t["confirms"][ty] = t["confirms"].get(ty, 0) + 1
    save_trust(t)
    return t
