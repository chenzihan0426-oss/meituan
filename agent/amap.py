"""高德（AMap）真实 POI 搜索——把"查餐厅/查活动"接上真实数据（PRD §5：尽量结合真实数据）。

返回的候选带**真实店名/地址/人均/评分/离家距离**（评委当场可搜证）；
而决策需要但高德不直接给的字段（是否儿童友好/低卡/辣口/过敏原/排队）由名称+品类**启发式推断**。
任何失败都抛 AmapError，由 ToolBox 回退到 LLM / 本地库（PRD §8：出事也优雅）。

围绕"家"（默认望京）做周边搜索，所以"别离家太远"的距离是**真实**的。带磁盘缓存，命中秒回、可离线复演。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.parse
import urllib.request

from . import llm  # 复用配置加载 + SSL 上下文

_AROUND = "https://restapi.amap.com/v5/place/around"
_REGEO = "https://restapi.amap.com/v3/geocode/regeo"     # 逆地理：坐标 → 地名
_GEOCODE = "https://restapi.amap.com/v3/geocode/geo"     # 正向：地址 → 坐标
_WEATHER = "https://restapi.amap.com/v3/weather/weatherInfo"  # 实时天气
_STATIC = "https://restapi.amap.com/v3/staticmap"             # 静态地图（返回 PNG）
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".amap_cache")


# ---------------------------------------------------------------------------
# 坐标转换：浏览器 navigator.geolocation 给的是 WGS-84（GPS），高德用 GCJ-02（火星
# 坐标），在国内差几百米。下面是公开的 WGS-84 → GCJ-02 变换，本地算、不联网。
# ---------------------------------------------------------------------------
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _tf_lat(x: float, y: float) -> float:
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tf_lng(x: float, y: float) -> float:
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """GPS 坐标 → 高德坐标。境外原样返回。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _tf_lat(lng - 105.0, lat - 35.0)
    dlng = _tf_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sm) * math.pi)
    dlng = (dlng * 180.0) / (_A / sm * math.cos(radlat) * math.pi)
    return round(lng + dlng, 6), round(lat + dlat, 6)


class AmapError(Exception):
    pass


def _cfg() -> dict:
    return llm._load_config()


def _key() -> str:
    return os.environ.get("AGENT_AMAP_KEY") or _cfg().get("amap_key", "")


def is_enabled() -> bool:
    return bool(_key())


def status() -> dict:
    return {"enabled": is_enabled(), "home": _cfg().get("home_area", "望京")}


# ---------------------------------------------------------------------------
def _get(params: dict) -> dict:
    params = {**params, "key": _key()}
    url = _AROUND + "?" + urllib.parse.urlencode(params)
    ckey = hashlib.sha256(url.encode()).hexdigest()[:32]
    cpath = os.path.join(CACHE_DIR, ckey + ".json")
    if _cfg().get("cache", True) and os.path.exists(cpath):
        try:
            with open(cpath, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        with urllib.request.urlopen(url, timeout=_cfg().get("timeout", 30),
                                    context=llm._ssl_context({})) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa
        raise AmapError(f"高德请求失败：{e}")
    if str(data.get("status")) != "1":
        raise AmapError(f"高德返回错误：{data.get('info')}（{data.get('infocode')}）")
    if _cfg().get("cache", True):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
    return data


def _api(base: str, params: dict, cache: bool = True) -> dict:
    """通用高德 v3 GET（regeo/geocode 等），带磁盘缓存。失败抛 AmapError。"""
    params = {**params, "key": _key()}
    url = base + "?" + urllib.parse.urlencode(params)
    ckey = hashlib.sha256(url.encode()).hexdigest()[:32]
    cpath = os.path.join(CACHE_DIR, ckey + ".json")
    if cache and _cfg().get("cache", True) and os.path.exists(cpath):
        try:
            with open(cpath, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    try:
        with urllib.request.urlopen(url, timeout=_cfg().get("timeout", 30),
                                    context=llm._ssl_context({})) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa
        raise AmapError(f"高德请求失败：{e}")
    if str(data.get("status")) != "1":
        raise AmapError(f"高德返回错误：{data.get('info')}（{data.get('infocode')}）")
    if cache and _cfg().get("cache", True):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
    return data


def regeo(loc: str) -> str:
    """逆地理编码：'lng,lat'(GCJ-02) → 人话地名（如 '北京市朝阳区国贸'）。"""
    data = _api(_REGEO, {"location": loc, "radius": 200, "extensions": "base"})
    rc = data.get("regeocode") or {}
    fmt = rc.get("formatted_address")
    if isinstance(fmt, str) and fmt:
        return fmt
    comp = rc.get("addressComponent") or {}
    parts = [comp.get("district"), comp.get("township")]
    return "".join(p for p in parts if isinstance(p, str)) or "你的位置"


def geocode(address: str, city: str = "北京") -> str | None:
    """正向编码：地址/地名 → 'lng,lat'(GCJ-02)。查不到返回 None。

    带 city 约束避免重名地点跨城误匹配（如'五道口'），默认按'家'所在的北京。
    """
    if not address.strip():
        return None
    data = _api(_GEOCODE, {"address": address.strip(), "city": city})
    geos = data.get("geocodes") or []
    if geos and geos[0].get("location"):
        return geos[0]["location"]
    return None


def resolve_gps(lng: float, lat: float) -> dict:
    """浏览器 GPS(WGS-84) → {'loc': 'lng,lat'(GCJ-02), 'area': 地名}。"""
    glng, glat = wgs84_to_gcj02(float(lng), float(lat))
    loc = f"{glng},{glat}"
    try:
        area = regeo(loc)
    except Exception:
        area = "你的位置"
    return {"loc": loc, "area": area}


def weather(loc: str) -> dict | None:
    """loc='lng,lat'(GCJ-02) → 实时天气 {weather,temp,wind,city}；取不到返回 None。"""
    try:
        rc = _api(_REGEO, {"location": loc, "extensions": "base"}).get("regeocode") or {}
        adcode = (rc.get("addressComponent") or {}).get("adcode")
        if not adcode or isinstance(adcode, list):
            return None
        data = _api(_WEATHER, {"city": adcode, "extensions": "base"})
        lives = data.get("lives") or []
        if not lives:
            return None
        w = lives[0]
        return {"weather": w.get("weather"), "temp": _num(w.get("temperature")),
                "wind": w.get("windpower"), "city": w.get("city")}
    except Exception:
        return None


def staticmap_png(spots: list) -> bytes:
    """spots: [(loc 'lng,lat'(GCJ02), label单字), ...] → 一张带标记+路线的地图 PNG。

    用现有 Web 服务 key 即可（不需要 JS 地图 key）。带磁盘缓存，离线可复演。
    """
    spots = [(loc, lab) for loc, lab in spots if loc]
    if not spots:
        raise AmapError("无坐标可画")
    markers = "|".join(f"large,,{lab}:{loc}" for loc, lab in spots)
    params = {"size": "750*360", "scale": "2", "markers": markers, "key": _key()}
    if len(spots) >= 2:
        params["paths"] = "6,0x3a6df0,1,,:" + ";".join(loc for loc, _ in spots)
    url = _STATIC + "?" + urllib.parse.urlencode(params)
    ckey = hashlib.sha256(url.encode()).hexdigest()[:32]
    cpath = os.path.join(CACHE_DIR, ckey + ".png")
    if _cfg().get("cache", True) and os.path.exists(cpath):
        with open(cpath, "rb") as f:
            return f.read()
    data = None
    last = None
    for _ in range(3):                     # 静态地图偶发 TLS EOF，带 UA + 重试更稳
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=_cfg().get("timeout", 30),
                                        context=llm._ssl_context({})) as resp:
                data = resp.read()
            break
        except Exception as e:  # noqa
            last = e
            data = None
    if data is None:
        raise AmapError(f"高德静态地图请求失败：{last}")
    if data[:1] == b"{":                   # 错误时返回 JSON，不是图
        raise AmapError("高德静态地图返回错误（非图片）")
    if _cfg().get("cache", True):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cpath, "wb") as f:
                f.write(data)
        except Exception:
            pass
    return data


def search_center(constraints: dict | None) -> str:
    """搜索中心：优先用户实时定位，否则回退配置里的'家'（望京）。"""
    cons = constraints or {}
    return cons.get("user_location") or _cfg().get("home_location", "116.4709,39.9966")


def _num(s, default=None):
    if s is None:
        return default
    m = re.search(r"\d+(\.\d+)?", str(s))
    return float(m.group()) if m else default


# ---------------------------------------------------------------------------
# 餐厅
# ---------------------------------------------------------------------------
# 把口味映射到高德餐饮 typecode，搜得更准（外国餐厅050200/西餐050201/日料050202/
# 韩餐050203/粤菜050103/火锅050117）。有 typecode 的口味就用它精确搜。
CUISINE_TYPECODE = {
    "火锅": "050117", "日料": "050202", "西餐": "050201", "粤菜": "050103",
}


def _cuisine(name: str, typ: str) -> str:
    """优先用高德 type 串里的精确品类段判菜系（比关键词可靠）。"""
    for seg in reversed(re.split(r"[;；|]", typ)):
        if "火锅" in seg:
            return "火锅"
        if "日本料理" in seg or "寿司" in seg:
            return "日料"
        if "韩国料理" in seg:
            return "韩餐"
        if "西餐" in seg or "墨西哥" in seg or "意式" in seg or "法式" in seg or "牛排" in seg:
            return "西餐"
        if "广东" in seg or "粤" in seg:
            return "粤菜"
        if "快餐" in seg:
            return "中餐"
    s = name + " " + typ
    for kw, cui in (("火锅", "火锅"), ("烧烤", "烧烤"), ("烤肉", "烧烤"), ("川菜", "川菜"),
                    ("湘菜", "湘菜"), ("素食", "轻食"), ("沙拉", "轻食"), ("轻食", "轻食")):
        if kw in s:
            return cui
    seg = [x for x in re.split(r"[;；]", typ) if x]
    return seg[-1] if seg else "中餐"


def _infer_restaurant(poi: dict) -> dict:
    name = poi.get("name", "")
    typ = poi.get("type", "")
    biz = poi.get("business", {}) if isinstance(poi.get("business"), dict) else {}
    s = name + " " + typ
    cui = _cuisine(name, typ)
    pc = _num(biz.get("cost"))
    if pc is None:
        pc = {"日料": 160, "西餐": 150, "火锅": 120, "烧烤": 110, "粤菜": 150,
              "川菜": 90, "湘菜": 90, "轻食": 75, "中餐": 80}.get(cui, 85)
    rating = _num(biz.get("rating"))
    child = (any(k in s for k in ("亲子", "儿童", "乐园", "披萨", "西贝", "外婆家",
                                  "绿茶", "快餐", "海底捞", "家常")) or
             cui in ("西餐", "中餐", "火锅", "粤菜", "轻食")) and \
            not any(k in s for k in ("酒吧", "清吧", "居酒屋", "夜店"))
    low_cal = any(k in s for k in ("轻食", "沙拉", "健康", "素食", "蒸", "粥", "日料"))
    spicy = any(k in s for k in ("火锅", "川", "麻辣", "烧烤", "水煮", "湘", "重庆", "冒菜", "串"))
    spicy_only = any(k in s for k in ("川菜", "麻辣烫", "水煮", "冒菜", "串串", "湘菜"))
    allergens = ["海鲜"] if any(k in s for k in ("海鲜", "寿司", "刺身", "日本料理", "潮汕")) else []
    dist = _num(poi.get("distance"), 0) or 0
    needs_q = bool(rating and rating >= 4.5)
    return {
        "id": "amap_r_" + str(poi.get("id", name)),
        "name": name, "area": poi.get("adname") or poi.get("address", ""), "cuisine": cui,
        "per_capita": pc, "child_friendly": child, "has_low_cal": low_cal,
        "allergens": allergens, "distance_km": round(dist / 1000, 1),
        "needs_queue": needs_q, "queue_minutes": 20 if needs_q else 0,
        "has_spicy_option": spicy, "spicy_only": spicy_only,
        "rating": rating, "address": poi.get("address", ""),
        "location": poi.get("location"),   # 'lng,lat'(GCJ02)，供地图打点
    }


def amap_restaurants(goal: str, constraints: dict, n: int = 10) -> list[dict]:
    loc = search_center(constraints)
    pref = constraints.get("cuisine_pref")
    types = "050000"   # 餐饮服务
    if pref and pref in CUISINE_TYPECODE:
        types = CUISINE_TYPECODE[pref]   # 用精确 typecode 严格搜该口味
        kw = pref
    elif pref:
        kw = pref
    elif constraints.get("need_low_cal"):
        kw = "轻食 餐厅"
    elif constraints.get("need_child_friendly"):
        kw = "亲子餐厅"
    else:
        kw = "美食"
    radius = int((constraints.get("max_distance_km") or 10) * 1000)
    radius = max(3000, min(50000, radius))
    data = _get({"location": loc, "keywords": kw, "types": types,
                 "radius": radius, "page_size": min(20, max(5, n)),
                 "show_fields": "business"})
    out = [_infer_restaurant(p) for p in (data.get("pois") or []) if p.get("name")]
    if not out:
        raise AmapError("高德无餐厅结果")
    if pref:   # 点名了口味：把对口的排前面，保证候选里有它（大脑再据此打分）
        out.sort(key=lambda r: pref not in (r.get("cuisine") or ""))
    return out


# ---------------------------------------------------------------------------
# 活动
# ---------------------------------------------------------------------------
_ACT_KW = {
    "amusement": ("儿童乐园", "080300"), "museum": ("博物馆", "140100"),
    "aquarium": ("海洋馆", ""), "park": ("公园", "110101"),
    "cinema": ("电影院", "080601"), "internet": ("网咖", ""),
    "ktv": ("KTV", ""), "walk": ("购物中心", "060100"),
}
_ACT_PRICE = {"amusement": 120, "museum": 40, "aquarium": 150, "park": 0,
              "cinema": 60, "internet": 30, "ktv": 80, "walk": 0}
_ACT_TIRING = {"amusement": "mid", "museum": "low", "aquarium": "low",
               "park": "mid", "cinema": "low", "internet": "low",
               "ktv": "low", "walk": "mid"}


def amap_activities(goal: str, constraints: dict, n: int = 8) -> list[dict]:
    loc = search_center(constraints)
    pref = constraints.get("activity_pref")
    raw = constraints.get("activity_raw")
    if pref and pref in _ACT_KW:
        cat = pref
        kw, types = _ACT_KW[pref]                 # 已知类别：用精确关键词/typecode
    elif raw:
        cat = pref or "custom"
        kw, types = raw, ""                        # 没有现成类别：按你的原话搜（如'蹦床''密室'）
    else:
        cat = "amusement"
        kw, types = _ACT_KW["amusement"]           # 真的没说想去哪：才回退游乐场
    # 博物馆/海洋馆这类目的地稀疏，放宽搜索半径（值得多走点）
    sparse = cat in ("museum", "aquarium", "amusement")
    base = (constraints.get("max_distance_km") or 12)
    radius = int(max(15000 if sparse else 8000, base * 1000))
    radius = min(50000, radius)
    params = {"location": loc, "keywords": kw, "radius": radius,
              "page_size": min(15, max(5, n)), "show_fields": "business"}
    if types:
        params["types"] = types
    data = _get(params)
    out = []
    for poi in (data.get("pois") or []):
        if not poi.get("name"):
            continue
        biz = poi.get("business", {}) if isinstance(poi.get("business"), dict) else {}
        dist = _num(poi.get("distance"), 0) or 0
        out.append({
            "id": "amap_a_" + str(poi.get("id", poi["name"])),
            "name": poi["name"], "area": poi.get("adname") or poi.get("address", ""),
            "category": cat, "distance_km": round(dist / 1000, 1),
            "child_friendly": "酒吧" not in (poi.get("name", "") + poi.get("type", "")),
            "tiring": _ACT_TIRING.get(cat, "mid"), "duration_h": 2.0,
            "price_per_person": int(_num(biz.get("cost"), _ACT_PRICE.get(cat, 60)) or 0),
            "near_bar_street": "酒吧" in poi.get("address", ""),
            "rating": _num(biz.get("rating")), "address": poi.get("address", ""),
            "location": poi.get("location"),   # 'lng,lat'(GCJ02)，供地图打点
        })
    if not out:
        raise AmapError("高德无活动结果")
    return out
