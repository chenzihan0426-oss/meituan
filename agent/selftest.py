"""自检（守 PRD §3.1 灵魂字段 + §0 主轴规则 + 验收清单关键瞬间）。

运行：python3 -m agent.selftest
不依赖任何第三方测试框架；任一断言失败则非零退出。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

from . import rules
from .intent import parse_goal
from .models import (Cost, Decision, Disposition, Edit, EditType, Option,
                     Participant, SOUL_FIELDS, Status)
from .orchestrator import AutoResponder, orchestrate
from .planner import build_first_plan
from .review import run_review_round
from .scenarios import FAMILY, get_scenario
from .tools import ToolBox

_checks = 0
_fails = 0


def check(cond: bool, msg: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
        print(f"  ✗ FAIL: {msg}")
    else:
        print(f"  ✓ {msg}")


def test_disposition_matrix():
    print("[1] 主轴：confidence × cost -> disposition（PRD §0）")
    D = rules.derive_disposition
    check(D(0.92, Cost.LOW) == Disposition.AUTO, "高置信 + 低代价 -> 直接做")
    check(D(0.55, Cost.LOW) == Disposition.SUGGEST, "中置信 + 低代价 -> 给建议")
    check(D(0.55, Cost.MID) == Disposition.SUGGEST, "中置信 + 中代价 -> 给建议")
    check(D(0.30, Cost.HIGH) == Disposition.ASK, "低置信 + 高代价 -> 停下来问")
    check(D(0.95, Cost.HIGH) == Disposition.ASK, "高代价压倒一切 -> 停下来问")
    check(D(0.20, Cost.LOW) == Disposition.ASK, "低置信压倒一切 -> 停下来问")


def test_soul_fields_present():
    print("[2] 守灵魂字段：每个 Decision 都齐全且非空（PRD §3.1 禁删）")
    intent = parse_goal(get_scenario("family").goal)
    plan = build_first_plan(intent, ToolBox(fast=True))
    for d in plan.decisions:
        for f in SOUL_FIELDS:
            check(hasattr(d, f), f"{d.type} 含字段 {f}")
        check(isinstance(d.confidence, float), f"{d.type}.confidence 是 float")
        check(bool(d.confidence_basis), f"{d.type}.confidence_basis 非空")
        check(bool(d.reasoning), f"{d.type}.reasoning 非空")
        check(d.disposition == rules.derive_disposition(d.confidence, d.cost),
              f"{d.type}.disposition 确由 confidence×cost 推导")


def test_acceptance_moments():
    print("[3] 验收清单关键瞬间都在第一版 Plan 里（PRD 附）")
    intent = parse_goal(get_scenario("family").goal)
    plan = build_first_plan(intent, ToolBox(fast=True))
    dispo = {d.type: d.disposition for d in plan.decisions}
    check(dispo.get("child_safety") == Disposition.AUTO, "#1 高置信直接定（孩子常识）")
    check(dispo.get("set_budget") == Disposition.ASK, "#2 低置信停下来问（预算）")
    check(dispo.get("set_return_time") == Disposition.ASK, "#2 低置信停下来问（到家时间）")
    check(dispo.get("nap_window") == Disposition.SUGGEST, "#3 中置信给建议（午睡口子）")
    gift = plan.find_by_type("send_gift")
    check(gift is not None and gift.disposition == Disposition.SUGGEST,
          "#5 情感高光是'建议'非'擅改'（送礼 disposition=suggest）")


def test_end_to_end():
    print("[4] 端到端跑通 + 群体评审 + 异常兜底 + GMV（family）")
    scn = get_scenario("family")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = orchestrate(scn, AutoResponder(scn), fast=True)
    plan = out["plan"]
    check(plan.version == 2, "经过一轮再规划 -> v2")
    rest = plan.find_by_type("choose_restaurant")
    # 评审里采纳了老婆的 C 店（weight 高 + 更契合）；执行期 C 满了 -> 兜底改订蓝港
    check(rest.chosen.get("id") in ("rest_C", "rest_A"),
          "餐厅经评审/兜底后落在合理候选上")
    check(len(rest.confidence_history) >= 1, "置信度被'谁的反馈'更新过（PRD §3.1）")
    check(out["gmv"] > 0, f"GMV 计数器有累加（¥{out['gmv']:.0f}）")
    # 回滚演示：花单失败后退款，落地订单应为 2 笔（餐厅 + 展览票）
    check(len(out["result"]["orders"]) == 2, "配送失败后回滚花单，落地订单=2（事务一致）")
    check(any("退款" in n for n in out["result"]["notes"]), "执行记录里有回滚/补偿痕迹")


def test_friends_generalization():
    print("[5] 朋友场景泛化：同一套代码不漏家庭味（PRD 验收 #9）")
    scn = get_scenario("friends")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = orchestrate(scn, AutoResponder(scn), fast=True)
    types = {d.type for d in out["plan"].decisions}
    check("nap_window" not in types, "4 个成人不应出现'午睡口子'")
    check("child_safety" not in types, "成人局不应出现'孩子常识约束'")
    check(out["gmv"] > 0, f"朋友局也撬动 GMV（¥{out['gmv']:.0f}）")


def test_disposition_invariant_after_feedback():
    print("[6] 灵魂不变式：置信度一变，disposition 必须随之重算（PRD §3.1）")
    d = Decision(type="x", description="x", confidence=0.9, confidence_basis="b",
                 cost=Cost.LOW, reasoning="r",
                 disposition=rules.derive_disposition(0.9, Cost.LOW))
    check(d.disposition == Disposition.AUTO, "初始高置信低代价 -> AUTO")
    d.update_confidence(0.2, "被推翻")
    check(d.disposition == Disposition.ASK,
          "置信度跌到 0.20 后 disposition 自动变 ASK（不再自相矛盾）")
    # 已确认的决定不应被打回'直接做'
    d2 = Decision(type="y", description="y", confidence=0.3, confidence_basis="b",
                  cost=Cost.HIGH, reasoning="r", disposition=Disposition.ASK,
                  status=Status.CONFIRMED)
    d2.update_confidence(0.9, "A 已回答")
    check(d2.disposition == Disposition.ASK,
          "已确认(CONFIRMED)的决定不被 update_confidence 重算回 AUTO")


def test_review_surfaces_refuted_choice():
    print("[7] 评审中被推翻的高代价选择会被'拎出来问'（PRD §0：低置信→停下来问）")
    intent = parse_goal(FAMILY.goal)
    plan = build_first_plan(intent, ToolBox(fast=True))
    rest = plan.find_by_type("choose_restaurant")
    seafood = next((o for o in rest.options if o.get("allergens")), None)
    check(seafood is not None, "候选里存在含海鲜的店可供构造场景")
    rest.chosen = seafood
    rest.confidence = 0.9
    rest.disposition = Disposition.AUTO
    p = Participant("朋友乙", weight=0.5)
    e = Edit(author=p, target_decision="constraint", type=EditType.ADD, constraint="我对海鲜过敏")
    rr = run_review_round(plan, [p], [e], dict(intent.constraints))
    m = rr.merged_plan.find_by_type("choose_restaurant")
    check(m.confidence < 0.4, f"含海鲜的已选项被推翻，置信度跌到 {m.confidence:.2f}")
    check(m.disposition == Disposition.ASK, "其 disposition 自动变 ASK（而非仍显示'直接定'）")
    check(any(q.type == "choose_restaurant" for q in rr.merged_plan.open_questions),
          "该决定被重新拎进 open_questions 交还给 A")


def test_exception_robustness():
    print("[8] 异常鲁棒性：恶劣 forced 组合也不崩、且事务一致（PRD §8）")
    nasty = {**FAMILY.forced, "payment_failed:buy_ticket": "always", "book_full:*": "always"}
    tb = ToolBox(seed=7, fast=True, forced=nasty)
    scn = get_scenario("family")
    crashed = False
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            out = orchestrate(scn, AutoResponder(scn), fast=True, tb=tb)
    except Exception as ex:  # noqa
        crashed = True
        out = None
    check(not crashed, "备选也满 + 购票支付失败时，执行不崩（优雅兜底）")
    if out:
        # 餐厅最终要么订上要么排队；GMV 不应为负，且账本与 GMV 一致
        ledger_sum = sum(t["amount"] for t in tb.ledger)
        check(abs(ledger_sum - tb.gmv) < 1e-6, "回滚后 GMV 与账本始终一致（无悬挂订单）")
        check(tb.gmv >= 0, "GMV 不为负")


def main():
    os.environ.setdefault("NO_COLOR", "1")
    test_disposition_matrix()
    test_soul_fields_present()
    test_acceptance_moments()
    test_end_to_end()
    test_friends_generalization()
    test_disposition_invariant_after_feedback()
    test_review_surfaces_refuted_choice()
    test_exception_robustness()
    print(f"\n==== 自检结果：{_checks - _fails}/{_checks} 通过 ====")
    if _fails:
        print(f"❌ {_fails} 项失败")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
