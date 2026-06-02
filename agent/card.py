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
        f"""<div class="slot"><div class="time">{_esc(s.start)}<br><span>{_esc(s.end)}</span></div>
        <div class="dot"></div><div class="what"><b>{_esc(s.title)}</b>
        {f'<small>{_esc(s.reason)}</small>' if s.reason else ''}</div></div>"""
        for s in plan.timeline)

    items = "".join(
        f"""<div class="item"><span class="ic">{ic}</span>
        <div><b>{_esc(lbl)}</b>{f'<small>{_esc(facts)}</small>' if facts else ''}</div></div>"""
        for ic, lbl, facts in _chosen_items(plan))

    gmv_line = (f'<div class="gmv">这趟预计花销约 <b>¥{int(gmv)}</b></div>'
                if gmv else "")
    txt = share_text(plan, send_to)

    return f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>今天下午的安排</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#eef1f7;font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
 display:flex;justify-content:center;padding:18px;color:#222}}
.card{{width:100%;max-width:420px;background:#fff;border-radius:22px;overflow:hidden;
 box-shadow:0 14px 40px rgba(40,60,120,.16)}}
.hd{{background:linear-gradient(135deg,#ff8a5c,#ff5e8a 60%,#c86bff);color:#fff;padding:22px 20px 26px}}
.hd .k{{font-size:12px;opacity:.9;letter-spacing:.1em}}
.hd h1{{font-size:21px;margin:4px 0 10px}}
.hd p{{font-size:14px;opacity:.95}}
.body{{padding:18px 18px 8px}}
.sec{{font-size:12px;color:#9aa1b2;letter-spacing:.08em;margin:14px 4px 8px}}
.slot{{display:flex;gap:10px;align-items:flex-start;position:relative}}
.slot .time{{flex:none;width:46px;text-align:right;font-size:12px;color:#ff5e8a;font-weight:700;line-height:1.25}}
.slot .time span{{color:#c2c7d4;font-weight:400}}
.slot .dot{{flex:none;width:11px;height:11px;border-radius:50%;background:#ff5e8a;margin-top:3px;
 box-shadow:0 0 0 4px rgba(255,94,138,.15)}}
.slot .what{{padding-bottom:14px;border-left:2px dashed #ffd9e3;margin-left:-6px;padding-left:14px;flex:1}}
.slot:last-child .what{{border-left-color:transparent}}
.slot .what b{{font-size:15px}}.slot .what small{{display:block;color:#9aa1b2;font-size:12px;margin-top:2px}}
.item{{display:flex;gap:11px;align-items:center;background:#f7f8fc;border-radius:14px;padding:11px 13px;margin:8px 0}}
.item .ic{{font-size:22px}}.item b{{font-size:15px}}.item small{{display:block;color:#9aa1b2;font-size:12px}}
.gmv{{text-align:center;color:#7a8196;font-size:13px;margin:14px 0 4px}}.gmv b{{color:#ff5e8a;font-size:17px}}
.ft{{padding:6px 18px 20px}}
.btn{{display:block;width:100%;border:0;border-radius:14px;padding:13px;font:inherit;font-weight:700;
 background:linear-gradient(135deg,#ff8a5c,#ff5e8a);color:#fff;cursor:pointer}}
.by{{text-align:center;color:#b8bdcb;font-size:11px;margin-top:12px}}
.toast{{position:fixed;bottom:26px;left:50%;transform:translateX(-50%);background:#222;color:#fff;
 padding:9px 16px;border-radius:20px;font-size:13px;opacity:0;transition:.3s}}
</style></head><body>
<div class="card">
  <div class="hd"><div class="k">AI 助手 · 帮你安排好了</div><h1>今天下午的安排 ☀️</h1><p>{_esc(greeting)}</p></div>
  <div class="body">
    <div class="sec">⏰ 时间线</div>
    {slots}
    {f'<div class="sec">✅ 已订好</div>{items}' if items else ''}
    {gmv_line}
  </div>
  <div class="ft">
    <button class="btn" onclick="cp()">📋 复制分享文案（发给朋友）</button>
    <div class="by">— 一句话，帮你把下午安排明白 —</div>
  </div>
</div>
<div class="toast" id="t">已复制，去粘贴给 TA 吧 ✨</div>
<script>
const SHARE={_json_str(txt)};
function cp(){{navigator.clipboard&&navigator.clipboard.writeText(SHARE);
 var t=document.getElementById('t');t.style.opacity=1;setTimeout(()=>t.style.opacity=0,1600);}}
</script>
</body></html>"""


def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)
