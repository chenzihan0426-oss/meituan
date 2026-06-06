"""执行层 + 异常处理（PRD §8）——多数队伍的死穴 = 我们的得分点。

A 最终确认后，按依赖顺序把动作真的做掉（订餐 → 下礼物 → 送花 → 发计划），
每一步状态可见。异常三件套全做：
  · 餐厅满了 → 自动排队报 ETA / 等位过久则换备选
  · 工具超时 → 自动重试
  · 中途某步失败 → 把同一笔事务里已下的单回滚/补偿（事务完整性）
GMV 计数器随每次成功下单跳动（PRD §10/§11）。
"""
from __future__ import annotations

from . import rules
from .display import (BOLD, CYAN, DIM, GREEN, GREY, RED, RESET, YELLOW, colorize)
from .models import Option, Plan, Status
from .tools import ToolBox, ToolError

QUEUE_TOLERANCE_MIN = 30   # 等位超过这个就换备选


class Executor:
    def __init__(self, plan: Plan, tb: ToolBox, constraints: dict,
                 party_size: int = 3, send_to: str = "小张"):
        self.plan = plan
        self.tb = tb
        self.constraints = constraints
        self.party = party_size
        self.send_to = send_to
        self.step_no = 0
        self.total_steps = self._count_steps()
        self.results: list[str] = []

    # -- 状态可见的打印 ---------------------------------------------------
    def _begin(self, title: str) -> None:
        self.step_no += 1
        print(f"\n{BOLD}[{self.step_no}/{self.total_steps}] {title}{RESET}  "
              f"{DIM}⏳ 执行中…{RESET}")

    def _ok(self, msg: str, amount: float = 0.0) -> None:
        gmv = f"  {CYAN}｜GMV ¥{self.tb.gmv:.0f}{RESET}" if amount else ""
        print(f"      {GREEN}✅ {msg}{RESET}{gmv}")

    def _warn(self, msg: str) -> None:
        print(f"      {YELLOW}⚠️ {msg}{RESET}")

    def _fail(self, msg: str) -> None:
        print(f"      {RED}❌ {msg}{RESET}")

    def _note(self, msg: str) -> None:
        print(f"      {GREY}↳ {msg}{RESET}")

    # -- 通用：带重试的调用（只对 timeout 重试）---------------------------
    def _attempt(self, fn, *args, retries: int = 2, **kwargs):
        last = None
        for i in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except ToolError as ex:
                last = ex
                if ex.kind == "timeout" and i < retries:
                    self._warn(f"{ex.message} → 自动重试（第 {i + 1} 次）")
                    continue
                raise
        raise last

    # -- 主流程 -----------------------------------------------------------
    def run(self) -> dict:
        print(f"\n{BOLD}{CYAN}===== 开始执行（按依赖顺序下单，状态可见）====={RESET}")
        self._exec_activities()
        rest_ref = self._exec_restaurant()
        self._exec_gift(rest_ref)
        self._exec_send_plan()
        return self._summary()

    # 0. 订活动：KTV / 游乐园 / 电影… 逐个预订（含多活动并存）
    def _activity_decisions(self):
        return [d for d in self.plan.decisions
                if d.type.startswith("choose_activity") and d.chosen]

    def _exec_activities(self):
        for a in self._activity_decisions():
            ch = a.chosen
            self._begin(f"预订活动：{ch.label}")
            amount = (ch.price or ch.get("price_per_person") or 0) * self.party
            if amount <= 0:
                self._ok(f"已预约 {ch.label}（免费 / 到场即可）")
                continue
            try:
                res = self._attempt(self.tb.book_activity, ch.get("id") or ch.id, amount)
                self._ok(f"已订 {ch.label}（¥{res['amount']:.0f}）", res["amount"])
            except ToolError as ex:
                self._warn(f"{ch.label} 预订未成（{ex.message}）→ 改到场购票，不影响行程")
                self.results.append(f"{ch.label} 线上预订未成 → 已转到场购票")

    # 1. 订餐厅：满了 → 排队报 ETA / 等位过久换备选 -----------------------
    def _exec_restaurant(self):
        rest_dec = self.plan.find_by_type("choose_restaurant")
        chosen: Option = rest_dec.chosen
        self._begin(f"订餐厅：{chosen.label}")
        try:
            res = self._attempt(self.tb.book_restaurant, chosen.get("id"), self.party)
            self._ok(f"已订 {chosen.label}（¥{res['amount']:.0f}）", res["amount"])
            return chosen
        except ToolError as ex:
            if ex.kind != "full":
                raise
            self._fail(f"{chosen.label} {ex.message}")
            # 兜底 a：先看排队
            q = self.tb.join_queue(chosen.get("id"))
            self._note(f"自动排队：预计等位 {q['eta_minutes']} 分钟")
            if q["eta_minutes"] <= QUEUE_TOLERANCE_MIN:
                self._ok(f"已加入 {chosen.label} 排队，ETA {q['eta_minutes']}min（可接受）")
                return chosen
            # 兜底 b：等位过久 → 换备选（从候选里挑次优且满足约束的）
            self._warn(f"等位 {q['eta_minutes']}min > {QUEUE_TOLERANCE_MIN}min 阈值，改订备选")
            backup = self._pick_backup(rest_dec, exclude_id=chosen.get("id"))
            if backup is None:
                self._fail("没有满足约束的备选，已加入排队")
                return chosen
            self._note(f"备选：{backup.label}（{self._why_backup(backup)}）")
            try:
                res = self._attempt(self.tb.book_restaurant, backup.get("id"), self.party)
                rest_dec.chosen = backup
                rest_dec.update_confidence(0.8, f"首选满了，执行期自动改订备选 {backup.label}")
                self._ok(f"已改订 {backup.label}（¥{res['amount']:.0f}）", res["amount"])
                return backup
            except ToolError as ex2:
                # 连备选也订不上 / 反复超时：优雅兜底为排队，不让异常逃逸崩掉 Demo
                self._fail(f"备选 {backup.label} 也未订上：{ex2.message}")
                q2 = self.tb.join_queue(backup.get("id"))
                rest_dec.chosen = backup
                rest_dec.update_confidence(0.6, f"首选与备选均紧张，已为 {backup.label} 排队")
                self._note(f"已为备选 {backup.label} 加入排队，ETA {q2['eta_minutes']}min")
                self.results.append(f"首选与备选均紧张 → 已为 {backup.label} 排队（ETA {q2['eta_minutes']}min）")
                return backup

    def _pick_backup(self, rest_dec, exclude_id):
        cands = [o for o in rest_dec.options if o.get("id") != exclude_id]
        scored = [(rules.score_restaurant(o, self.constraints)[0], o) for o in cands]
        scored = [(s, o) for s, o in scored if s >= 0.5]   # 仍要满足基本约束
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored else None

    def _why_backup(self, o: Option) -> str:
        bits = []
        if o.get("child_friendly"):
            bits.append("有儿童餐")
        if o.get("has_low_cal"):
            bits.append("有低卡")
        bits.append(f"{o.get('distance_km')}km")
        return "、".join(bits) + "，同样满足约束"

    # 2. 礼物：两笔独立事务——鲜花(下单→送达) 与 展览票。任一笔失败只回滚该笔，互不牵连。
    def _exec_gift(self, rest_ref):
        gift_dec = self.plan.find_by_type("send_gift")
        if gift_dec is None or gift_dec.chosen is None:
            return
        members = gift_dec.chosen.get("members") or []
        if "gift_flowers" in members:
            self._run_flower_txn(rest_ref)
        if "gift_ticket" in members:
            self._run_ticket_txn()

    def _run_flower_txn(self, rest_ref):
        """'送花'事务：下单 → 送到餐厅。任一步（含下单支付失败/送达失败）失败 → 回滚整笔。"""
        committed = []
        try:
            self._begin("下鲜花订单")
            fr = self._attempt(self.tb.order_flowers)
            committed.append(fr["txn"])
            self._ok(f"鲜花已下单（¥{fr['amount']:.0f}）", fr["amount"])

            self._begin(f"安排把鲜花送到 {rest_ref.label}")
            d = self._attempt(self.tb.arrange_delivery, "gift_flowers", rest_ref.label)
            committed.append(d["txn"])
            self._ok(f"配送已安排（¥{d['amount']:.0f}）", d["amount"])
        except ToolError as ex:
            self._fail(f"鲜花事务失败（{ex.kind}）：{ex.message}")
            self._rollback_group(committed, "鲜花")
            self._note("补偿方案：建议到店现买同款向日葵（到店自取，不影响给 TA 的惊喜）")
            self.results.append(f"鲜花事务失败（{ex.kind}）→已退款并改为到店现买（事务保持一致）")

    def _run_ticket_txn(self):
        """'展览票'事务：独立于鲜花，失败也不牵连鲜花。"""
        committed = []
        try:
            self._begin("购买展览票")
            t = self._attempt(self.tb.buy_ticket)
            committed.append(t["txn"])
            self._ok(f"展览票已购（¥{t['amount']:.0f}）", t["amount"])
        except ToolError as ex:
            self._fail(f"购票失败（{ex.kind}）：{ex.message}")
            self._rollback_group(committed, "展览票")
            self._note("补偿方案：改到现场购票或临时换其它礼物")
            self.results.append(f"购票失败（{ex.kind}）→已回滚补偿（事务保持一致）")

    def _rollback_group(self, committed: list, name: str) -> None:
        if not committed:
            return
        total = 0.0
        for txn in committed:
            total += self.tb.rollback(txn)["refunded"]
        self._warn(f"回滚{name}事务：已退款 ¥{total:.0f}（GMV 相应回退至 ¥{self.tb.gmv:.0f}）")

    # 3. 发计划 -----------------------------------------------------------
    def _exec_send_plan(self):
        self._begin(f"把最终方案发给 {self.send_to}")
        summary = f"v{self.plan.version} 方案：" + "；".join(
            f"{d.description}={d.chosen.label}" for d in self.plan.decisions if d.chosen)
        r = self._attempt(self.tb.send_plan, self.send_to, summary)
        self._ok(f"已发送给 {self.send_to}（{r['chars']} 字）")

    def _count_steps(self) -> int:
        n = len(self._activity_decisions())   # 活动（可多个）
        n += 1  # restaurant
        gift = self.plan.find_by_type("send_gift")
        if gift and gift.chosen:
            members = gift.chosen.get("members") or []
            n += ("gift_flowers" in members) + ("gift_ticket" in members) + ("gift_flowers" in members)
        n += 1  # send_plan
        return n

    def _summary(self) -> dict:
        print(f"\n{BOLD}{CYAN}===== 执行完成 ====={RESET}")
        for r in self.results:
            self._note(r)
        print(f"{BOLD}💰 本次会话实际撬动 GMV：{GREEN}¥{self.tb.gmv:.0f}{RESET}")
        print(f"{DIM}   已落地订单：{len(self.tb.ledger)} 笔｜工具调用：{len(self.tb.call_log)} 次{RESET}")
        return {"gmv": self.tb.gmv, "orders": list(self.tb.ledger), "notes": self.results}
