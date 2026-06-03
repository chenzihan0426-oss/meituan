"""跨会话偏好记忆——"越用越懂你"（支撑商业价值里的数据护城河叙事）。

把你上次确认过的预算/到家时间/忌口记到本地文件，下次**预填**进"停下来问"的输入框、
并显式告诉你"上次你说…这次先按这个？"——**仍由你确认，绝不静默当默认值**
（守住主轴：不默默替你填）。仅用于交互 web 流，不影响自动演示 / 验收。
"""
from __future__ import annotations

import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".prefs.json")
_KEYS = ("budget_per_capita", "return_time", "allergens", "avoid_spicy", "need_low_cal")


def load() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save(d: dict) -> dict:
    cur = load()
    for k in _KEYS:
        v = d.get(k)
        if v not in (None, "", []):
            cur[k] = v
    try:
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
    except Exception:
        pass
    return cur


def clear() -> None:
    try:
        os.remove(_PATH)
    except Exception:
        pass


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
