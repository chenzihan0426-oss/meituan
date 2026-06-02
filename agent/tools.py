"""工具层（PRD §5）——配角，全部 Mock，够用就停。

两条硬要求：
  1. 返回数据必须带上决策需要的字段（人均/儿童友好/低卡/过敏原/排队/等位时长……）。
  2. Mock 必须能制造'信息缺失'和'失败'（查不到人均、订位满了、偶发超时）。

为了让 Demo 可复现，ToolBox 用固定种子的 RNG，并支持 forced[...] 强制特定结果。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Mock 数据库（尽量用真实片区、真实价位 —— PRD §5）。家在望京。
# ---------------------------------------------------------------------------
# 餐厅库（Mock，但按菜系铺开，便于按口味选店）。cuisine 串里务必含可被意图匹配的关键词。
RESTAURANTS: list[dict[str, Any]] = [
    {"id": "rest_A", "name": "蓝港·亲子西餐厅", "area": "蓝色港湾", "cuisine": "西餐·牛排",
     "per_capita": 85, "child_friendly": True, "has_low_cal": True, "allergens": [],
     "distance_km": 8.5, "needs_queue": False, "queue_minutes": 0,
     "has_spicy_option": False, "spicy_only": False},
    {"id": "rest_B", "name": "京雅堂·精致粤菜", "area": "王府井", "cuisine": "粤菜·港式",
     "per_capita": 320, "child_friendly": False, "has_low_cal": True, "allergens": ["海鲜"],
     "distance_km": 14.0, "needs_queue": True, "queue_minutes": 30,
     "has_spicy_option": False, "spicy_only": False},
    {"id": "rest_C", "name": "望京·绿叶轻食花园", "area": "望京", "cuisine": "融合轻食·沙拉",
     "per_capita": 95, "child_friendly": True, "has_low_cal": True, "allergens": [],
     "distance_km": 2.1, "needs_queue": False, "queue_minutes": 0,
     "has_spicy_option": False, "spicy_only": False},
    {"id": "rest_D", "name": "合生汇·川味海鲜火锅", "area": "朝阳合生汇", "cuisine": "海鲜火锅",
     "per_capita": 180, "child_friendly": False, "has_low_cal": False, "allergens": ["海鲜"],
     "distance_km": 9.0, "needs_queue": True, "queue_minutes": 40,
     "has_spicy_option": True, "spicy_only": True},
    {"id": "rest_E", "name": "海底捞·火锅（望京店）", "area": "望京", "cuisine": "火锅",
     "per_capita": 130, "child_friendly": True, "has_low_cal": False, "allergens": [],
     "distance_km": 3.5, "needs_queue": True, "queue_minutes": 20,
     "has_spicy_option": True, "spicy_only": False},
    {"id": "rest_F", "name": "将太无二·日料", "area": "国贸", "cuisine": "日料·寿司",
     "per_capita": 220, "child_friendly": False, "has_low_cal": True, "allergens": ["海鲜"],
     "distance_km": 12.0, "needs_queue": False, "queue_minutes": 0,
     "has_spicy_option": False, "spicy_only": False},
    {"id": "rest_G", "name": "很久以前·烧烤（烤串）", "area": "三里屯", "cuisine": "烧烤·烤串",
     "per_capita": 110, "child_friendly": False, "has_low_cal": False, "allergens": [],
     "distance_km": 10.0, "needs_queue": True, "queue_minutes": 25,
     "has_spicy_option": True, "spicy_only": False},
    {"id": "rest_H", "name": "西贝·西北家常菜", "area": "望京", "cuisine": "西北家常菜",
     "per_capita": 90, "child_friendly": True, "has_low_cal": True, "allergens": [],
     "distance_km": 2.8, "needs_queue": False, "queue_minutes": 0,
     "has_spicy_option": True, "spicy_only": False},
    {"id": "rest_I", "name": "绿茶餐厅·融合家常", "area": "朝阳大悦城", "cuisine": "中餐·融合家常",
     "per_capita": 75, "child_friendly": True, "has_low_cal": True, "allergens": [],
     "distance_km": 7.0, "needs_queue": True, "queue_minutes": 15,
     "has_spicy_option": True, "spicy_only": False},
]

# 活动库（Mock）。category 用于按你说的'场所类型'精准匹配（游乐场/博物馆/海洋馆/公园…）。
ACTIVITIES: list[dict[str, Any]] = [
    {"id": "act_amuse1", "name": "蓝色港湾·儿童乐园", "area": "蓝色港湾", "category": "amusement",
     "distance_km": 8.5, "child_friendly": True, "tiring": "mid", "duration_h": 2.5,
     "price_per_person": 120, "near_bar_street": False},
    {"id": "act_amuse2", "name": "欢乐谷·主题乐园", "area": "东四环", "category": "amusement",
     "distance_km": 16.0, "child_friendly": True, "tiring": "high", "duration_h": 4.0,
     "price_per_person": 260, "near_bar_street": False},
    {"id": "act_museum1", "name": "国家自然博物馆·恐龙展", "area": "天桥", "category": "museum",
     "distance_km": 11.0, "child_friendly": True, "tiring": "low", "duration_h": 2.0,
     "price_per_person": 40, "near_bar_street": False},
    {"id": "act_museum2", "name": "中国科技馆", "area": "奥林匹克公园", "category": "museum",
     "distance_km": 12.0, "child_friendly": True, "tiring": "low", "duration_h": 2.5,
     "price_per_person": 30, "near_bar_street": False},
    {"id": "act_aqua", "name": "富国海底世界·海洋馆", "area": "工体", "category": "aquarium",
     "distance_km": 9.0, "child_friendly": True, "tiring": "low", "duration_h": 2.0,
     "price_per_person": 150, "near_bar_street": False},
    {"id": "act_park", "name": "朝阳公园·亲子骑行", "area": "朝阳公园", "category": "park",
     "distance_km": 6.0, "child_friendly": True, "tiring": "high", "duration_h": 2.0,
     "price_per_person": 60, "near_bar_street": False},
    {"id": "act_cinema", "name": "万达影城·亲子场", "area": "望京", "category": "cinema",
     "distance_km": 4.0, "child_friendly": True, "tiring": "low", "duration_h": 2.0,
     "price_per_person": 90, "near_bar_street": False},
    {"id": "act_walk1", "name": "三里屯 citywalk 小吃街", "area": "三里屯", "category": "walk",
     "distance_km": 10.0, "child_friendly": False, "tiring": "mid", "duration_h": 2.0,
     "price_per_person": 0, "near_bar_street": True},
    {"id": "act_walk2", "name": "南锣鼓巷·胡同 citywalk", "area": "南锣鼓巷", "category": "walk",
     "distance_km": 9.0, "child_friendly": True, "tiring": "low", "duration_h": 2.0,
     "price_per_person": 0, "near_bar_street": False},
]

GIFTS: list[dict[str, Any]] = [
    {"id": "gift_cake", "name": "85度C·黑森林蛋糕", "price": 158, "high_calorie": True,
     "kind": "cake"},
    {"id": "gift_flowers", "name": "话梅鲜花·向日葵花束", "price": 129, "high_calorie": False,
     "kind": "flowers"},
    {"id": "gift_ticket", "name": "UCCA 当代艺术展·双人票", "price": 120, "high_calorie": False,
     "kind": "ticket"},
]


class ToolError(Exception):
    """工具失败（满了 / 超时 / 下单失败）。"""
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # full / timeout / payment_failed / delivery_failed
        self.message = message


@dataclass
class ToolBox:
    seed: int = 7
    fast: bool = True             # demo 跑得快一点；置 False 体验真实延迟
    forced: dict[str, str] = field(default_factory=dict)  # 强制结果，用于可复现 Demo
    gmv: float = 0.0
    ledger: list[dict] = field(default_factory=list)      # 已提交事务，供回滚
    call_log: list[str] = field(default_factory=list)
    use_llm: bool = False                                 # True 则查询走 AI（失败回退本地库）
    context: dict = field(default_factory=dict)           # {"goal":..., "constraints":...} 供 AI 用

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self._fired: set[str] = set()   # 记录已触发过的'失败一次'类强制项
        self.dynamic_rest: dict[str, dict] = {}   # AI 生成的餐厅，按 id 存，供下单/排队查回
        self.last_source: str = "本地库"            # 最近一次查询的来源（AI / 本地库）

    def _rest_by_id(self, rest_id: str) -> Optional[dict]:
        """先查 AI 生成的动态餐厅，再查本地库。"""
        if rest_id in self.dynamic_rest:
            return self.dynamic_rest[rest_id]
        return next((x for x in RESTAURANTS if x["id"] == rest_id), None)

    # -- 内部辅助 ---------------------------------------------------------
    def _latency(self, base: float = 0.12):
        if self.fast:
            time.sleep(0.02)
        else:
            time.sleep(base + self.rng.random() * 0.2)

    def _log(self, name: str, detail: str = ""):
        self.call_log.append(f"{name}({detail})")

    def _forced_key(self, kind: str, rest_id: str) -> Optional[str]:
        """返回适用的强制键：优先精确 'kind:rest_id'，否则通配 'kind:*'。"""
        if f"{kind}:{rest_id}" in self.forced:
            return f"{kind}:{rest_id}"
        if f"{kind}:*" in self.forced:
            return f"{kind}:*"
        return None

    def _maybe(self, key: str, prob: float) -> bool:
        """是否触发某异常。forced 优先：
           "ok"   -> 永不触发
           "once" -> 只在第一次触发（用于'超时一次后重试成功'的演示）
           其它真值 -> 每次都触发
        否则按概率（用固定 RNG，可复现）。
        """
        if key in self.forced:
            val = self.forced[key]
            if val == "ok":
                return False
            if val == "once":
                if key in self._fired:
                    return False
                self._fired.add(key)
                return True
            return True
        return self.rng.random() < prob

    # == 查 ===============================================================
    def search_restaurants(self, *, child_friendly: Optional[bool] = None,
                           low_cal: Optional[bool] = None,
                           exclude_allergens: Optional[list[str]] = None,
                           max_per_capita: Optional[float] = None) -> list[dict]:
        self._latency()
        self._log("search_restaurants")
        if self.use_llm and self.context.get("goal"):
            goal, cons = self.context["goal"], self.context.get("constraints", {})
            # 1) 真实高德 POI 优先（评委可当场搜证）
            try:
                from . import amap
                if amap.is_enabled():
                    rows = amap.amap_restaurants(goal, cons)
                    for r in rows:
                        self.dynamic_rest[r["id"]] = r
                    self.last_source = "高德POI(真实)"
                    return [dict(r) for r in rows]
            except Exception:
                pass
            # 2) LLM 实时生成次之
            try:
                from . import llm_tools
                rows = llm_tools.llm_restaurants(goal, cons)
                for r in rows:
                    self.dynamic_rest[r["id"]] = r
                self.last_source = "AI"
                return [dict(r) for r in rows]
            except Exception:
                pass  # 3) 回退本地库（PRD §8：出事也优雅）
        self.last_source = "本地库"
        out = []
        for r in RESTAURANTS:
            if max_per_capita is not None and r["per_capita"] > max_per_capita * 1.5:
                continue
            out.append(dict(r))   # 不按约束过滤死，保留候选以便大脑讲'为什么排除'
        return out

    def search_activities(self) -> list[dict]:
        self._latency()
        self._log("search_activities")
        if self.use_llm and self.context.get("goal"):
            goal, cons = self.context["goal"], self.context.get("constraints", {})
            try:
                from . import amap
                if amap.is_enabled():
                    self.last_source = "高德POI(真实)"
                    return [dict(a) for a in amap.amap_activities(goal, cons)]
            except Exception:
                pass
            try:
                from . import llm_tools
                rows = llm_tools.llm_activities(goal, cons)
                self.last_source = "AI"
                return [dict(a) for a in rows]
            except Exception:
                pass
        self.last_source = "本地库"
        return [dict(a) for a in ACTIVITIES]

    def check_availability(self, rest_id: str) -> dict:
        """有没有位、是否要排队、等位时长。还可能制造'人均未知'的信息缺失。"""
        self._latency()
        self._log("check_availability", rest_id)
        r = self._rest_by_id(rest_id)
        if r is None:
            raise ToolError("not_found", f"查无此店 {rest_id}")
        res = {
            "id": rest_id, "available": True,
            "needs_queue": r["needs_queue"], "queue_minutes": r["queue_minutes"],
            "per_capita": r["per_capita"],
        }
        # 信息缺失：rest_D 偶发查不到人均（-> 触发'问'或'建议'）
        if self._maybe(f"missing_price:{rest_id}", 1.0 if rest_id == "rest_D" else 0.0):
            res["per_capita"] = None
        return res

    def get_route(self, area: str) -> dict:
        self._latency()
        self._log("get_route", area)
        # 以望京为家，给个粗略距离/耗时
        table = {a["area"]: a["distance_km"] for a in ACTIVITIES}
        table.update({r["area"]: r["distance_km"] for r in RESTAURANTS})
        dist = table.get(area, 7.0)
        return {"area": area, "distance_km": dist, "drive_minutes": round(dist * 3 + 6)}

    # == 订 ===============================================================
    def book_restaurant(self, rest_id: str, party: int = 3) -> dict:
        self._latency()
        self._log("book_restaurant", rest_id)
        # 支持 'book_full:*' 通配：无论 A 最终选了哪家，首次下单都'满了'（兜底演示更稳健）
        full_key = self._forced_key("book_full", rest_id)
        if full_key and self._maybe(full_key, 0.0):
            raise ToolError("full", f"{rest_id} 满了，订不上")
        to_key = self._forced_key("timeout:book", rest_id)
        if to_key and self._maybe(to_key, 0.0):
            raise ToolError("timeout", "订位接口超时")
        r = self._rest_by_id(rest_id)
        amount = (r["per_capita"] if r else 100) * party
        txn = {"action": "book_restaurant", "ref": rest_id, "amount": amount}
        self._commit(txn)
        return {"ok": True, "ref": rest_id, "amount": amount, "txn": txn}

    def join_queue(self, rest_id: str) -> dict:
        self._latency()
        self._log("join_queue", rest_id)
        r = self._rest_by_id(rest_id)
        eta = r["queue_minutes"] if r and r.get("queue_minutes") else 25
        forced_eta = self.forced.get(f"queue_eta:{rest_id}", self.forced.get("queue_eta:*"))
        if forced_eta is not None:
            eta = int(forced_eta)
        return {"ok": True, "ref": rest_id, "eta_minutes": eta}

    # == 买 ===============================================================
    def order_cake(self, gift_id: str = "gift_cake") -> dict:
        return self._order_gift(gift_id, "order_cake")

    def order_flowers(self, gift_id: str = "gift_flowers") -> dict:
        return self._order_gift(gift_id, "order_flowers")

    def buy_ticket(self, gift_id: str = "gift_ticket") -> dict:
        return self._order_gift(gift_id, "buy_ticket")

    def _order_gift(self, gift_id: str, action: str) -> dict:
        self._latency()
        self._log(action, gift_id)
        g = next((x for x in GIFTS if x["id"] == gift_id), None)
        if g is None:
            raise ToolError("not_found", f"查无此商品 {gift_id}")
        if self._maybe(f"timeout:{action}", 0.0):
            raise ToolError("timeout", f"{action} 接口超时")
        if self._maybe(f"payment_failed:{action}", 0.0):
            raise ToolError("payment_failed", f"{action} 支付失败")
        txn = {"action": action, "ref": gift_id, "amount": g["price"]}
        self._commit(txn)
        return {"ok": True, "ref": gift_id, "amount": g["price"], "txn": txn}

    # == 送 ===============================================================
    def arrange_delivery(self, item_ref: str, to: str) -> dict:
        self._latency()
        self._log("arrange_delivery", f"{item_ref}->{to}")
        if self._maybe(f"timeout:delivery", 0.0):
            raise ToolError("timeout", "配送调度接口超时")
        if self._maybe(f"delivery_failed", 0.0):
            raise ToolError("delivery_failed", f"运力不足，无法把 {item_ref} 送到 {to}")
        fee = 12
        txn = {"action": "arrange_delivery", "ref": item_ref, "amount": fee, "to": to}
        self._commit(txn)
        return {"ok": True, "ref": item_ref, "amount": fee, "txn": txn}

    # == 发 ===============================================================
    def send_plan(self, to: str, summary: str) -> dict:
        self._latency()
        self._log("send_plan", to)
        return {"ok": True, "to": to, "chars": len(summary)}

    # == 事务 / GMV / 回滚 ================================================
    def _commit(self, txn: dict):
        self.gmv += txn["amount"]
        self.ledger.append(txn)

    def rollback(self, txn: dict) -> dict:
        """补偿/回滚一笔已提交事务：退款，GMV 相应回退（事务完整性）。"""
        self._latency()
        self._log("rollback", txn["action"])
        if txn in self.ledger:
            self.ledger.remove(txn)
            self.gmv -= txn["amount"]
        return {"ok": True, "refunded": txn["amount"], "action": txn["action"]}
