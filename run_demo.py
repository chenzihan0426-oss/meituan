#!/usr/bin/env python3
"""便捷入口：python3 run_demo.py [family|friends] [--slow]

等价于 python3 -m agent.demo --scenario <场景>。
交互式体验请用：python3 -m agent.cli
自检请用：       python3 -m agent.selftest
"""
import sys

from agent.demo import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    # 允许位置写法：run_demo.py friends
    if argv and not argv[0].startswith("-"):
        argv = ["--scenario", argv[0]] + argv[1:]
    main(argv)
