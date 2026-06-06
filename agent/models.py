"""核心数据结构（PRD §3）——项目的灵魂。

⚠️ 硬性约束（PRD §3.1）：在任何迭代中都禁止为了'先跑通'而删掉
   Decision 上的 confidence / confidence_basis / cost / reasoning / disposition。
   它们就是本项目的认知亮点本身。每个 Decision 创建时都会做自检（见 __post_init__）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import itertools


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class Cost(str, Enum):
    """错了的代价。"""
    LOW = "low"     # 订错餐厅可换
    MID = "mid"     # 有点肉疼但可挽回
    HIGH = "high"   # 钱花错了难收回 / 全盘崩


class Disposition(str, Enum):
    """处置方式——由 confidence × cost 推导得出（见 rules.derive_disposition）。"""
    AUTO = "auto"        # 直接做：置信度高 且 代价低
    SUGGEST = "suggest"  # 给建议，由用户拍板：置信度/代价中等
    ASK = "ask"          # 停下来问：置信度低 或 代价高


class Status(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    DONE = "done"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class EditType(str, Enum):
    REPLACE = "replace"
    ADD = "add"
    REMOVE = "remove"


# ---------------------------------------------------------------------------
# 候选项
# ---------------------------------------------------------------------------
_id_counter = itertools.count(1)


def _next_id(prefix: str) -> str:
    return f"{prefix}_{next(_id_counter)}"


@dataclass
class Option:
    """一个候选项：餐厅 / 活动 / 礼物，或一个取值（预算档位、到家时间）。

    decision-relevant 字段都放进 data（人均、是否儿童友好、是否低卡、过敏原、
    距离、是否排队、等位时长……）——大脑靠这些字段算置信度（PRD §5 硬要求 1）。
    """
    label: str
    kind: str = "generic"                  # restaurant / activity / gift / value
    data: dict[str, Any] = field(default_factory=dict)
    price: float = 0.0                     # 用于 GMV 估算
    id: str = field(default_factory=lambda: _next_id("opt"))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


# ---------------------------------------------------------------------------
# Decision —— 系统的核心对象
# ---------------------------------------------------------------------------
SOUL_FIELDS = ("confidence", "confidence_basis", "cost", "reasoning", "disposition")


@dataclass
class Decision:
    type: str                              # choose_restaurant / choose_activity / send_gift ...
    description: str
    options: list[Option] = field(default_factory=list)
    chosen: Optional[Option] = None

    # ↓↓↓ 灵魂字段，禁止删除（PRD §3.1）↓↓↓
    confidence: float = 0.0                # 0~1，我有多确定用户会认可
    confidence_basis: str = ""             # 依据：基于什么常识/约束/谁的反馈
    cost: Cost = Cost.LOW                  # 错了的代价
    reasoning: str = ""                    # 为什么这么选、为什么排除别的
    disposition: Disposition = Disposition.SUGGEST  # 由 confidence × cost 推导
    # ↑↑↑ 灵魂字段，禁止删除 ↑↑↑

    status: Status = Status.PENDING
    id: str = field(default_factory=lambda: _next_id("dec"))

    # 评审过程留痕：置信度被谁的反馈修正过（PRD §3.1 末）
    confidence_history: list[str] = field(default_factory=list)
    # 反事实：如果我自作主张（不停下来问/不给建议）可能的坏结果——把"懂分寸"的价值讲具体
    counterfactual: str = ""
    # 信任账户'默认填'：根据历史预填的答案（仍是可改的提问，不锁死）
    prefill: str = ""
    # 活动相对'吃饭'的时序：before（饭前）/ after（饭后）——支持'吃完饭再去唱K'这类顺序
    when: str = "before"

    def __post_init__(self) -> None:
        # 自检：灵魂字段必须齐全（PRD 要求每次改完代码自检）
        for f in SOUL_FIELDS:
            if not hasattr(self, f):
                raise AssertionError(f"Decision 缺少灵魂字段 {f}！（违反 PRD §3.1）")
        self.confidence = float(self.confidence)

    def update_confidence(self, new_conf: float, basis: str) -> None:
        """置信度会更新（PRD §3.1）：被某人的反馈确认或推翻后，重算。

        灵魂不变式（PRD §0/§3.1）：confidence 一旦变化，disposition 必须随之重算，
        否则会出现'0.20 低置信却仍显示✅直接定'的自相矛盾。仅在决定仍未定（PENDING）时重算——
        已被 A 回答/确认（CONFIRMED）的决定不应被打回'直接做'。
        """
        old = self.confidence
        self.confidence = float(new_conf)
        self.confidence_basis = basis
        self.confidence_history.append(f"{old:.2f} -> {new_conf:.2f}：{basis}")
        if self.status == Status.PENDING:
            from .rules import derive_disposition  # 惰性导入，避免与 rules 循环依赖
            self.disposition = derive_disposition(self.confidence, self.cost)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
@dataclass
class Slot:
    """带时间的行程片段，含'为什么这样排'的理由。"""
    start: str
    end: str
    title: str
    reason: str = ""


@dataclass
class Plan:
    version: int = 1
    decisions: list[Decision] = field(default_factory=list)
    timeline: list[Slot] = field(default_factory=list)
    open_questions: list[Decision] = field(default_factory=list)   # ask 类，拦在执行前
    gmv_estimate: float = 0.0
    tips: list = field(default_factory=list)   # 出行/饮食温馨提示（[{icon,text}]）

    def find(self, decision_id: str) -> Optional[Decision]:
        for d in self.decisions:
            if d.id == decision_id:
                return d
        return None

    def find_by_type(self, dtype: str) -> Optional[Decision]:
        for d in self.decisions:
            if d.type == dtype:
                return d
        return None

    def recompute_open_questions(self) -> None:
        self.open_questions = [d for d in self.decisions
                               if d.disposition == Disposition.ASK and d.status == Status.PENDING]


# ---------------------------------------------------------------------------
# 协同评审结构（PRD §3.3）
# ---------------------------------------------------------------------------
@dataclass
class Participant:
    id: str                                # "老婆" / "朋友甲" / "朋友乙"
    weight: float = 0.5                    # 意见权重，影响'采纳其改动'的置信度（PRD §4.2）
    note: str = ""                         # 这个人的画像/诉求，便于解释


@dataclass
class Edit:
    """局里某个人对方案的一条改动。每条改动必须带 author（PRD §7.3 硬要求）。"""
    author: Participant
    target_decision: str                   # 改的是哪个决定（decision.type）
    type: EditType
    after: Optional[Option] = None         # 改成什么
    before: Optional[Option] = None        # 改之前
    note: str = ""                         # 附言，如'这家太远了'
    constraint: Optional[str] = None       # add 型可携带一条新约束，如'我对海鲜过敏'
    merged_note: str = ""                  # 合并后真实产生的效果（诚实呈现，见 review）
    actionable: bool = True                # 该约束是否真被引擎采纳（否则只是'已记录'）
    id: str = field(default_factory=lambda: _next_id("edit"))


@dataclass
class Conflict:
    """多人改到同一个决定 -> 撞车。"""
    target_decision: str
    competing_edits: list[Edit]
    agent_suggestion: Optional[Option] = None
    suggestion_reason: str = ""
    suggested_edit: Optional[Edit] = None        # 建议采纳的是哪条改动
    resolution: Optional[Edit] = None            # A 最终选了谁的（未裁为 None）

    def suggestion_owner(self) -> str:
        return self.suggested_edit.author.id if self.suggested_edit else "（无）"


@dataclass
class ReviewRound:
    plan_version: int
    participants: list[Participant]
    edits: list[Edit] = field(default_factory=list)
    merged_plan: Optional[Plan] = None           # 自动合并无争议改动后的版本
    conflicts: list[Conflict] = field(default_factory=list)
    auto_merged: list[Edit] = field(default_factory=list)  # 被无打扰吸收的改动
