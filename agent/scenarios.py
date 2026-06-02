"""场景预设（PRD §12 步骤 7：朋友场景泛化——同一套代码切 2 男 2 女）。

每个场景提供：那句话目标、局里的人（含 weight）、给 Demo 用的预设改动与强制项、
以及 ask 类决定的预设回答 / 冲突的预设裁决（让 Demo 能无人值守跑通）。
真实交互时（cli.py）这些'预设回答'被真人输入替代。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .models import Edit, EditType, Option, Participant
from .planner import _opt_from_restaurant
from .tools import RESTAURANTS


def _rest(rid: str) -> Option:
    return _opt_from_restaurant(next(r for r in RESTAURANTS if r["id"] == rid))


@dataclass
class Scenario:
    name: str
    goal: str
    participants: list[Participant]
    party_size: int = 3
    send_to: str = "小张"
    forced: dict = field(default_factory=dict)
    # 评审改动构造器：拿到第一版 plan，产出局里各人的改动
    edits_builder: Callable = None
    # ask 类决定的预设回答（decision.type -> 人话回答）
    ask_answers: dict = field(default_factory=dict)
    # 冲突预设裁决：decision.type -> "suggestion"(采纳建议) / author.id / "self"
    conflict_choices: dict = field(default_factory=dict)


# ===========================================================================
# 场景一：家庭（主场景）
# ===========================================================================
def _family_edits(participants: dict, plan) -> list[Edit]:
    wife = participants["老婆"]
    friend_a = participants["朋友甲"]
    friend_b = participants["朋友乙"]
    rest_dec = plan.find_by_type("choose_restaurant")
    cur = rest_dec.chosen
    return [
        # 老婆：把餐厅换成更近的 C 店（附言：A 店有点远）
        Edit(author=wife, target_decision="choose_restaurant", type=EditType.REPLACE,
             before=cur, after=_rest("rest_C"), note="A 店有点远"),
        # 朋友甲：把餐厅换成 D 店（海鲜火锅，人均偏高）
        Edit(author=friend_a, target_decision="choose_restaurant", type=EditType.REPLACE,
             before=cur, after=_rest("rest_D"), note="想吃海鲜火锅"),
        # 朋友乙：新增约束「我对海鲜过敏」（无争议，自动合并 + 剔除海鲜店）
        Edit(author=friend_b, target_decision="constraint", type=EditType.ADD,
             constraint="我对海鲜过敏"),
    ]


FAMILY = Scenario(
    name="family",
    goal="今天下午想和老婆孩子出去玩几小时，别离家太远，老婆最近在减肥，帮我安排",
    participants=[
        Participant("老婆", weight=0.9, note="意见权重高；在减肥，倾向清淡低卡、别太远"),
        Participant("朋友甲", weight=0.5, note="一起约的朋友，口味重，爱海鲜火锅"),
        Participant("朋友乙", weight=0.5, note="一起约的朋友，对海鲜过敏"),
    ],
    party_size=3,
    send_to="小张",
    forced={
        # 演示异常三件套（可复现）。用通配键，无论 A 最终采纳哪家店，首次下单都会'满了'：
        "book_full:*": "once",             # 首次订位'满了'（仅一次）→ 触发兜底
        "queue_eta:*": "45",               # 等位 45min > 30min 阈值 → 改订备选
        "timeout:order_flowers": "once",   # 下花单超时一次 → 自动重试成功
        "delivery_failed": "always",       # 送花到店失败 → 回滚花单 + 补偿
    },
    edits_builder=_family_edits,
    ask_answers={
        "set_budget": "人均 120 以内就行，不用上精致餐厅",
        "set_return_time": "19:30 前到家",
    },
    conflict_choices={"choose_restaurant": "suggestion"},   # A 采纳 Agent 建议（C 店）
)


# ===========================================================================
# 场景二：朋友局（泛化，2 男 2 女、口味不一）—— PRD 验收 #9，一句带过
# ===========================================================================
def _friends_edits(participants: dict, plan) -> list[Edit]:
    qiang = participants["阿强"]      # 无辣不欢
    mei = participants["小美"]        # 减脂中，要低卡
    wei = participants["大伟"]        # 海鲜爱好者
    zhen = participants["阿珍"]       # 怕辣，吃清淡
    rest_dec = plan.find_by_type("choose_restaurant")
    cur = rest_dec.chosen
    return [
        # 餐厅撞车：小美要轻食(C) vs 大伟要海鲜火锅(D)
        Edit(author=mei, target_decision="choose_restaurant", type=EditType.REPLACE,
             before=cur, after=_rest("rest_C"), note="减脂中，想吃轻食"),
        Edit(author=wei, target_decision="choose_restaurant", type=EditType.REPLACE,
             before=cur, after=_rest("rest_D"), note="好久没吃海鲜火锅了"),
        # 口味分歧：阿强要辣、阿珍怕辣（无争议，各自纳入口味偏好，正面体现'口味不一'）
        Edit(author=qiang, target_decision="constraint", type=EditType.ADD,
             constraint="我无辣不欢，至少有个辣口菜"),
        Edit(author=zhen, target_decision="constraint", type=EditType.ADD,
             constraint="我怕辣，麻烦点清淡的"),
    ]


FRIENDS = Scenario(
    name="friends",
    goal="周六晚上四个人聚餐，两男两女，离家别太远，口味不太一样，帮我安排",
    participants=[
        Participant("阿强", weight=0.55, note="无辣不欢"),
        Participant("小美", weight=0.6, note="减脂中，要低卡"),
        Participant("大伟", weight=0.5, note="海鲜爱好者"),
        Participant("阿珍", weight=0.55, note="怕辣，吃得清淡"),
    ],
    party_size=4,
    send_to="群里",
    forced={
        "timeout:book:*": "once",          # 首次订位超时一次（无论哪家）→ 自动重试成功
    },
    edits_builder=_friends_edits,
    ask_answers={
        "set_budget": "人均 150 左右可以",
        "set_return_time": "23:00 前各自回家就行",
    },
    conflict_choices={"choose_restaurant": "suggestion"},
)


SCENARIOS = {"family": FAMILY, "friends": FRIENDS}


def get_scenario(name: str) -> Scenario:
    if name not in SCENARIOS:
        raise SystemExit(f"未知场景 {name}，可选：{', '.join(SCENARIOS)}")
    return SCENARIOS[name]
