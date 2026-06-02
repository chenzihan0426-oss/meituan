"""验收规格（单一事实来源）——PRD 附《Demo 必须演出来的瞬间》。

check_acceptance.py（命令行）与 agent/web.py（网页验收台）都从这里读，避免两处不一致。
每条 = (编号, 标题, 场景, [必须出现的子串...], [禁止出现的子串...])。
"""
from __future__ import annotations

CHECKLIST = [
    ("1", "高置信直接定（孩子常识：避辣/避酒吧街）", "family",
     ["孩子相关常识约束", "普遍育儿常识", "✅ 直接定"], []),
    ("2", "低置信停下来问（预算 + 到家时间）", "family",
     ["预算完全未知", "整条时间线", "❓ 停下来问"], []),
    ("3", "中置信给建议（午睡口子）", "family",
     ["下午留不留午睡口子", "💡 给建议"], []),
    ("4", "内部冲突协调（娃 vs 老婆，找两全的店）", "family",
     ["内部冲突：娃要", "两全"], []),
    ("5", "情感高光（减肥→花+票，是建议非擅改）", "family",
     ["送高热量蛋糕可能适得其反", "一束花 + 一张她一直想看的展的票", "我只建议，不擅改"], []),
    ("6", "协同评审升维（多人改+署名+自动合并+带weight的冲突建议+再规划v2）", "family",
     ["协同评审回合", "📥 收到改动", "自动合并（无争议）", "冲突待裁决",
      "🤖 建议：采纳", "weight=", "再规划：新版方案 v2"], []),
    ("7", "异常恢复（满了→排队/换备选 + 超时重试 + 失败回滚补偿）", "family",
     ["满了，订不上", "自动排队：预计等位", "改订备选", "自动重试", "回滚鲜花事务", "已退款"], []),
    ("8", "确认后一键执行 + 状态可见 + GMV 跳动 + send_plan", "family",
     ["开始执行", "执行中", "GMV ¥", "已发送给 小张"], []),
    ("9", "泛化到朋友场景（2男2女口味不一，同一套代码）", "friends",
     ["场景：friends", "已纳入口味偏好", "实际撬动 GMV"], ["午睡口子", "孩子相关常识"]),
]

DIMENSIONS = [
    ("创新性", "①按置信度决定自动到什么程度（验收 #1/#2/#3）；②升维群体协调器（#6）；③补全用户没说的（#5）"),
    ("完整性", "查订买送发闭环 + 协同评审回合（#6）+ 异常兜底/回滚（#7）"),
    ("应用效果", "内部冲突协调（#4）+ 多人分歧协调（#6）+ 该问才问（#2）+ 情感体贴（#5）"),
    ("商业价值", "一句话撬动门票+餐厅+礼物多笔交易；GMV 计数器（#8）；组局比个人客单更高（#9）"),
]


def evaluate(text: str, scenario: str):
    """对某场景的输出文本逐条核验，返回 [{id,title,scenario,pass,missing,evidence}]。"""
    results = []
    for cid, title, scn, must, must_not in CHECKLIST:
        if scn != scenario:
            continue
        missing = [s for s in must if s not in text]
        bad = [s for s in must_not if s in text]
        ok = not missing and not bad
        evidence = ""
        if ok and must:
            evidence = next((ln.strip() for ln in text.splitlines() if must[0] in ln), must[0])
        results.append({"id": cid, "title": title, "scenario": scn, "pass": ok,
                        "missing": missing + [f"不该出现:{b}" for b in bad], "evidence": evidence})
    return results
