"""自动演示（无人值守跑通整条端到端闭环）。

用法：
    python -m agent.demo                # 家庭主场景
    python -m agent.demo --scenario friends   # 朋友局泛化
    python -m agent.demo --slow         # 体验真实工具延迟

把一个场景从那句话一路演到：协同评审 → 全员满意 → 所有单子下完 → 计划发出去，
中途故意触发'餐厅满了'+'超时重试'+'步骤失败回滚'，展示兜底。
"""
from __future__ import annotations

import argparse

from .orchestrator import AutoResponder, orchestrate
from .scenarios import get_scenario


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="活动规划执行 Agent · 自动演示")
    ap.add_argument("--scenario", default="family", choices=["family", "friends"],
                    help="演示场景（默认 family）")
    ap.add_argument("--slow", action="store_true", help="体验真实工具延迟（默认快速）")
    args = ap.parse_args(argv)

    scenario = get_scenario(args.scenario)
    orchestrate(scenario, AutoResponder(scenario), fast=not args.slow,
                card_path=f"plan_card_{scenario.name}.html")


if __name__ == "__main__":
    main()
