"""把最终方案渲染成一张"递给老婆/发小张"的移动端可分享卡片（自包含 HTML）。

这是赛题最有画面感的一句——"把手机递给老婆/发给朋友看好不好"——的落地物：
不是给评委看的 JSON，而是家人愿意打开的卡片，还能一键复制分享文案。
纯字符串渲染，无第三方依赖。
"""
from __future__ import annotations

import html as _html

from .models import Plan

_ICON = {
    "choose_activity": "🎡", "choose_restaurant": "🍽️", "send_gift": "🎁",
    "set_budget": "💰", "set_return_time": "🕖", "nap_window": "😴",
    "child_safety": "🧒",
}


def _esc(s) -> str:
    return _html.escape("" if s is None else str(s))


def _chosen_items(plan: Plan):
    """挑出要展示给家人看的'实物'决定（活动/餐厅/礼物）。"""
    order = ["choose_activity", "choose_restaurant", "send_gift"]
    items = []
    for t in order:
        d = plan.find_by_type(t)
        if d and d.chosen:
            facts = []
            o = d.chosen
            if o.get("per_capita") is not None:
                facts.append(f"人均 ¥{int(o.get('per_capita'))}")
            if o.get("distance_km") is not None:
                facts.append(f"{o.get('distance_km')}km")
            if o.get("child_friendly"):
                facts.append("亲子友好")
            if o.get("has_low_cal"):
                facts.append("有低卡")
            items.append((_ICON.get(t, "•"), o.label, "、".join(facts)))
    return items


def share_text(plan: Plan, send_to: str = "家人") -> str:
    """给'发给小张'用的纯文本分享文案。"""
    lines = [f"搞定了！今天下午这么安排："]
    for s in plan.timeline:
        lines.append(f"· {s.start}–{s.end} {s.title}")
    items = _chosen_items(plan)
    if items:
        lines.append("")
        lines.append("订好的：" + "，".join(lbl for _, lbl, _ in items))
    return "\n".join(lines)


def render_plan_card(plan: Plan, *, send_to: str = "家人",
                     greeting: str | None = None, gmv: float | None = None) -> str:
    if greeting is None:
        # 第一屏先说给"接收者"听（情感内核），而不是先甩日程（评审第二轮反馈）
        family = any(plan.find_by_type(t) for t in ("child_safety", "send_gift", "nap_window"))
        if family:
            greeting = "亲爱的，今天下午你什么都不用操心，只管美美地出现就好 💛 剩下的我都排好了 👇"
        else:
            greeting = "今天不用谁费心张罗，人来就行 🙌 我都安排好了 👇 有意见随时改"

    slots = "".join(
        f'<div class="slot"><div class="time">{_esc(s.start)}<br><span>{_esc(s.end)}</span></div>'
        f'<div class="track"><div class="dot"></div><div class="line"></div></div>'
        f'<div class="what"><b>{_esc(s.title)}</b>'
        f'{f"<small>{_esc(s.reason)}</small>" if s.reason else ""}</div></div>'
        for s in plan.timeline)

    items = "".join(
        f'<div class="item"><span class="ic">{ic}</span>'
        f'<div><b>{_esc(lbl)}</b>{f"<small>{_esc(facts)}</small>" if facts else ""}</div></div>'
        for ic, lbl, facts in _chosen_items(plan))

    gmv_line = (f'<div class="gmv"><b>¥{int(gmv)}</b>预计总花销</div>' if gmv else "")
    txt = share_text(plan, send_to)

    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>今天下午的安排</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#f8fafc;font-family:'Inter',-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
 -webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%;display:flex;justify-content:center;padding:24px 16px;color:#0f172a}}
.card{{width:100%;max-width:400px;background:#fff;border-radius:32px;position:relative;overflow:hidden;border:1px solid #f1f5f9;
 box-shadow:0 20px 40px -10px rgba(0,0,0,0.08),0 1px 3px rgba(0,0,0,0.03)}}
.blob{{position:absolute;top:-60px;right:-60px;width:220px;height:220px;
 background:linear-gradient(135deg,#fecdd3,#fed7aa);filter:blur(50px);border-radius:50%;opacity:.6;pointer-events:none}}
.content{{position:relative;z-index:1;padding:32px 24px}}
.k{{font-size:11px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:.1em;margin-bottom:12px}}
h1{{font-size:26px;font-weight:900;letter-spacing:-.02em;margin:0 0 16px;color:#0f172a;line-height:1.2}}
.greeting{{font-size:14px;color:#475569;line-height:1.6;margin-bottom:32px;font-weight:500;background:#f8fafc;padding:16px;border-radius:20px;border:1px solid #f1f5f9}}
.sec{{font-size:11px;font-weight:800;color:#94a3b8;text-transform:uppercase;letter-spacing:.1em;margin:0 0 20px}}
.slot{{display:flex;gap:16px;margin-bottom:0}}
.time{{flex:none;width:44px;text-align:right;font-size:14px;font-weight:800;color:#f43f5e;line-height:1.2;letter-spacing:-.02em}}
.time span{{display:block;font-weight:500;color:#94a3b8;font-size:12px;margin-top:2px}}
.track{{display:flex;flex-direction:column;align-items:center;margin-top:4px}}
.dot{{flex:none;width:10px;height:10px;border-radius:50%;background:#fff;border:2.5px solid #f43f5e;box-shadow:0 0 0 3px #fff1f2}}
.line{{flex:1;width:2px;background:#f1f5f9;margin-top:4px;margin-bottom:4px}}
.slot:last-child .line{{background:transparent}}
.what{{flex:1;padding-bottom:24px}}
.what b{{display:block;font-size:15px;font-weight:700;color:#0f172a;margin-bottom:4px}}
.what small{{display:block;font-size:13px;color:#64748b;line-height:1.5;font-weight:400}}
.item{{display:flex;gap:14px;align-items:center;background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:16px;margin-bottom:12px;box-shadow:0 4px 6px -1px rgba(0,0,0,0.02)}}
.item .ic{{font-size:24px}}
.item b{{display:block;font-size:14px;font-weight:700;color:#0f172a}}
.item small{{display:block;font-size:12px;color:#64748b;margin-top:2px;font-weight:500}}
.gmv{{text-align:center;font-size:11px;color:#94a3b8;font-weight:700;text-transform:uppercase;letter-spacing:.1em;margin:32px 0 24px}}
.gmv b{{display:block;color:#0f172a;font-size:22px;font-weight:900;letter-spacing:-.02em;margin-bottom:2px}}
.ft{{padding:0 24px 32px;position:relative;z-index:1}}
.btn{{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;background:#0f172a;color:#fff;border:none;
 padding:18px;border-radius:20px;font-size:15px;font-weight:700;font-family:inherit;cursor:pointer;transition:.2s;box-shadow:0 10px 25px -5px rgba(15,23,42,0.3)}}
.btn:active{{transform:scale(.96)}}
.by{{text-align:center;color:#cbd5e1;font-size:11px;margin-top:16px;font-weight:600;letter-spacing:.05em}}
.toast{{position:fixed;bottom:40px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,0.9);
 backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);color:#fff;padding:12px 24px;border-radius:100px;
 font-size:13px;font-weight:600;opacity:0;transition:.3s;box-shadow:0 10px 25px -5px rgba(0,0,0,0.2);pointer-events:none}}
</style></head><body>
<div class="card">
  <div class="blob"></div>
  <div class="content">
    <div class="k">AI 决策搭子已就绪</div>
    <h1>今天下午的安排 ☀️</h1>
    <p class="greeting">{_esc(greeting)}</p>
    <div class="sec">⏰ 时间线</div>
    {slots}
    {f'<div class="sec" style="margin-top:8px">✅ 已订好</div>{items}' if items else ''}
    {gmv_line}
  </div>
  <div class="ft">
    <button class="btn" onclick="cp()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
      复制分享文案（发给朋友）
    </button>
    <div class="by">一句话，帮你把下午安排明白</div>
  </div>
</div>
<div class="toast" id="t">已复制，去粘贴给 TA 吧 ✨</div>
<script>
const SHARE={_json_str(txt)};
function cp(){{if(navigator.clipboard)navigator.clipboard.writeText(SHARE);
 var t=document.getElementById('t');t.style.opacity=1;setTimeout(()=>t.style.opacity=0,2000);}}
</script>
</body></html>"""


def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)
