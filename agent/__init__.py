"""一个'知道该替你决定多少'、且服务整个'局'的本地活动规划执行 Agent。

主轴：对每个决定按 置信度 × 代价 决定 做 / 建议 / 问。
升维：从个人助理 -> 群体决策协调器（多人协同评审 + 再规划）。

详见 PRD。本包只依赖 Python 标准库，开箱即跑。
"""

__all__ = ["models", "rules", "tools", "intent", "planner", "review", "execution", "display", "scenarios"]
