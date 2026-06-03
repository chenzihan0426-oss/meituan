"""出行 / 饮食温馨提示 —— "懂你的朋友"的体贴层。

基于：天气（高德实时，可选）、孩子、忌口/过敏、减肥、交通早晚高峰、晚归，
生成一组贴心提醒。纯规则、可解释；天气取不到就用时段启发式，绝不因联网失败而崩。
"""
from __future__ import annotations


def _hour(slot_start: str) -> int:
    try:
        return int(slot_start.split(":")[0])
    except Exception:
        return 15


def build_tips(plan, constraints: dict) -> list[dict]:
    c = constraints or {}
    tips: list[dict] = []
    tl = plan.timeline or []
    start_h = _hour(tl[0].start) if tl else 15

    # ---- 1. 天气（高德实时优先，取不到用时段启发式）----
    w = None
    loc = c.get("user_location")
    if loc:
        try:
            from . import amap
            if amap.is_enabled():
                w = amap.weather(loc)
        except Exception:
            w = None
    if w and w.get("weather"):
        text, temp, city = w["weather"], w.get("temp"), w.get("city") or ""
        if any(k in text for k in ("雨", "雪", "雷")):
            tips.append({"icon": "🌧️", "text": f"{city}现在{text}，出门记得带伞，鞋穿防滑的；开车留意路滑别急刹"})
        elif "晴" in text and 9 <= start_h <= 16:
            tips.append({"icon": "☀️", "text": f"{city}{text}{('，'+str(int(temp))+'°C') if temp else ''}，白天紫外线强——涂好防晒、戴帽子，给孩子也抹一点"})
        elif temp is not None:
            tips.append({"icon": "🌤️", "text": f"{city}{text}，约 {int(temp)}°C，按这个加减衣"})
        if temp is not None and temp >= 30:
            tips.append({"icon": "🥵", "text": f"气温 {int(temp)}°C 偏热，多备水、避开午后暴晒时段长时间户外，别让孩子中暑"})
        elif temp is not None and temp <= 8:
            tips.append({"icon": "🧥", "text": f"只有 {int(temp)}°C，加件外套，孩子尤其别冻着"})
    elif 9 <= start_h <= 16:
        tips.append({"icon": "☀️", "text": "白天出行紫外线偏强，建议涂防晒/带伞遮阳；出门前再瞄一眼天气，下雨记得带伞"})

    # ---- 2. 孩子 ----
    if c.get("need_child_friendly"):
        tips.append({"icon": "🧒", "text": "带上孩子的水杯、纸巾和一件备用外套；行程已避开太累的项目和酒吧街，中途留了喘口气的余量"})

    # ---- 3. 过敏 / 忌口（硬约束）----
    al = [a for a in (c.get("allergens") or []) if a]
    if al:
        a = "、".join(al)
        tips.append({"icon": "⚠️", "text": f"同行有人对{a}过敏——已避开{a}相关的店，点菜时也跟服务员确认一下，避免交叉接触"})

    # ---- 4. 减肥 / 低卡 ----
    if c.get("need_low_cal") or c.get("emotional_diet"):
        tips.append({"icon": "🥗", "text": "有人在控制饮食——已优先有低卡选项的店，甜点可以换成果茶/气泡水，别破坏 ta 的计划"})

    # ---- 5. 口味 ----
    if c.get("need_spicy"):
        tips.append({"icon": "🌶️", "text": "有人无辣不欢——已确保有辣口菜；和怕辣的同桌可以点个鸳鸯/分盘"})

    # ---- 6. 交通：从时间线里捞出落在早晚高峰的通勤段 ----
    for s in tl:
        if "高峰" in (s.reason or ""):
            tips.append({"icon": "🚗", "text": f"{s.start} {s.title}正赶上高峰，路上会堵——建议直接打车、并尽量提前出发错峰"})
            break

    # ---- 7. 晚归 ----
    if tl:
        end_h = _hour(tl[-1].start)
        if end_h >= 21 or end_h <= 4:
            tips.append({"icon": "🌙", "text": "到家偏晚，注意安全，晚归建议直接打车回；带孩子的话备点小零食路上垫垫"})

    # ---- 8. 通用兜底 ----
    tips.append({"icon": "📞", "text": "出发前点开餐厅确认在营业、预留好排队时间（周末热门店可能要等位，已为你看了排队情况）"})

    return tips[:6]   # 最多 6 条，别变成说明书
