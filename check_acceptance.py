#!/usr/bin/env python3
"""验收自检：对照 PRD「附：Demo 必须演出来的瞬间」逐条核验真实输出是否真的演出来了。

与 agent/selftest.py 的区别：
  · selftest  —— 检查【内部逻辑】（灵魂字段、置信度规则、事务一致性）；开发者视角。
  · 本脚本    —— 跑真实 Demo，检查【屏幕上真的出现了】验收清单的每一个瞬间；评委视角。

用法：
    python3 check_acceptance.py
全绿 -> 退出码 0；任一项缺失 -> 打印缺了什么 + 退出码 1。
（图形化验收台见：python3 -m agent.web）
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

os.environ["NO_COLOR"] = "1"   # 必须在导入 display 之前关色，保证子串匹配干净

from agent.acceptance_spec import CHECKLIST, DIMENSIONS, evaluate  # noqa: E402
from agent.orchestrator import AutoResponder, orchestrate          # noqa: E402
from agent.scenarios import get_scenario                           # noqa: E402


def run(scenario_name: str) -> str:
    scn = get_scenario(scenario_name)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        orchestrate(scn, AutoResponder(scn), fast=True)
    return buf.getvalue()


def main() -> None:
    outputs = {"family": run("family"), "friends": run("friends")}

    print("=" * 64)
    print(" 验收自检：对照 PRD 附《Demo 必须演出来的瞬间》逐条核验")
    print("=" * 64)
    fails = 0
    for scn in ("family", "friends"):
        for r in evaluate(outputs[scn], scn):
            mark = "✅ PASS" if r["pass"] else "❌ FAIL"
            print(f"\n[{r['id']}] {mark}  {r['title']}  （场景:{scn}）")
            if r["pass"]:
                print(f"      证据：{r['evidence'][:90]}")
            else:
                fails += 1
                print(f"      缺少应出现：{r['missing']}")

    print("\n" + "=" * 64)
    print(" 四个评分维度 → 靠哪些验收项支撑")
    print("=" * 64)
    for name, how in DIMENSIONS:
        print(f"  · {name}：{how}")

    print("\n" + "=" * 64)
    if fails == 0:
        print(" 结果：9/9 验收瞬间全部在真实 Demo 输出中出现 ✅")
        print(" 配合 `python3 -m agent.selftest`（内部逻辑 93 项）即为双重达标。")
    else:
        print(f" 结果：{9 - fails}/9 通过，{fails} 项缺失 ❌（见上方）")
        sys.exit(1)


if __name__ == "__main__":
    main()
