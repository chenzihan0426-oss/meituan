"""LLM 接入（OpenAI 兼容，标准库直连，零依赖）。

PRD §12 允许"LLM 调用用一个简单封装"。本模块只负责：读配置、发请求、把回复里的 JSON 抠出来。
任何失败都抛 LLMError，由调用方决定回退本地库（PRD §8：出事也要优雅稳住）。

配置优先级：环境变量 > agent/llm_config.json。中转站一般是 OpenAI 格式，填三件套即可：
    {"enabled": true, "base_url": "https://你的中转站/v1", "api_key": "sk-...",
     "model": "claude-3-5-sonnet-20241022"}
环境变量：AGENT_LLM_BASE_URL / AGENT_LLM_API_KEY / AGENT_LLM_MODEL / AGENT_LLM_ENABLED
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "llm_config.json")
CACHE_DIR = os.path.join(_HERE, ".llm_cache")   # 命中即秒回、可离线复演（Demo 上保险）


class LLMError(Exception):
    pass


def _load_config() -> dict:
    cfg = {"enabled": False, "base_url": "", "api_key": "", "model": "claude-3-5-sonnet-20241022",
           "temperature": 0.5, "timeout": 45}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    # 环境变量覆盖
    if os.environ.get("AGENT_LLM_BASE_URL"):
        cfg["base_url"] = os.environ["AGENT_LLM_BASE_URL"]
    if os.environ.get("AGENT_LLM_API_KEY"):
        cfg["api_key"] = os.environ["AGENT_LLM_API_KEY"]
    if os.environ.get("AGENT_LLM_MODEL"):
        cfg["model"] = os.environ["AGENT_LLM_MODEL"]
    if os.environ.get("AGENT_LLM_ENABLED"):
        cfg["enabled"] = os.environ["AGENT_LLM_ENABLED"].lower() in ("1", "true", "yes")
    # 高德 / 默认位置也可走环境变量（部署时线上没有 llm_config.json）
    if os.environ.get("AGENT_AMAP_KEY"):
        cfg["amap_key"] = os.environ["AGENT_AMAP_KEY"]
    if os.environ.get("AGENT_HOME_LOCATION"):
        cfg["home_location"] = os.environ["AGENT_HOME_LOCATION"]
    if os.environ.get("AGENT_HOME_AREA"):
        cfg["home_area"] = os.environ["AGENT_HOME_AREA"]
    if cfg.get("api_key") and cfg.get("base_url"):
        cfg.setdefault("enabled", True)
    return cfg


def status() -> dict:
    cfg = _load_config()
    return {"enabled": is_enabled(), "model": cfg.get("model", ""),
            "base_url_set": bool(cfg.get("base_url")), "key_set": bool(cfg.get("api_key"))}


def is_enabled() -> bool:
    cfg = _load_config()
    return bool(cfg.get("enabled") and cfg.get("base_url") and cfg.get("api_key"))


def _ssl_context(cfg: dict) -> ssl.SSLContext:
    """构造 SSL 上下文。默认验证证书，但挂一个真实可用的 CA bundle——
    修复 macOS python.org 版'找不到根证书(CERTIFICATE_VERIFY_FAILED)'的常见问题。
    verify_ssl=false 时关闭验证（不推荐，仅在中转站证书异常时兜底）。
    """
    if cfg.get("verify_ssl") is False:
        return ssl._create_unverified_context()
    ctx = ssl.create_default_context()
    candidates = []
    try:
        import certifi  # 多数 Python 自带；不算新增 pip 依赖
        candidates.append(certifi.where())
    except Exception:
        pass
    candidates += ["/etc/ssl/cert.pem", "/usr/local/etc/openssl@3/cert.pem"]
    for path in candidates:
        try:
            if path and os.path.exists(path):
                ctx.load_verify_locations(path)
                return ctx
        except Exception:
            continue
    return ctx   # 退回系统默认（已 create_default_context）


def _extract_json(text: str):
    """从模型回复里抠出 JSON（容忍 ```json 包裹、前后废话）。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    # 退一步：找第一个 { 或 [ 到对应结尾
    for op, cl in (("[", "]"), ("{", "}")):
        i, j = s.find(op), s.rfind(cl)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    raise LLMError("模型未返回可解析的 JSON")


def _cache_key(model: str, system: str, user: str, temp) -> str:
    raw = f"{model}\x00{temp}\x00{system}\x00{user}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def chat_json(system: str, user: str, *, temperature: float | None = None,
              max_tokens: int = 2000) -> dict | list:
    """调用 OpenAI 兼容 /chat/completions，返回解析后的 JSON（dict 或 list）。失败抛 LLMError。

    带磁盘缓存：相同(model,system,user,temperature)命中即秒回、可离线复演——
    现场网络抖/中转站超时也不影响已演示过的请求（PRD §8：出事也优雅）。
    """
    cfg = _load_config()
    if not is_enabled():
        raise LLMError("LLM 未配置（缺 base_url 或 api_key，或 enabled=false）")
    temp = cfg.get("temperature", 0.5) if temperature is None else temperature
    use_cache = cfg.get("cache", True)
    ckey = _cache_key(cfg["model"], system, user, temp)
    cpath = os.path.join(CACHE_DIR, ckey + ".json")
    if use_cache and os.path.exists(cpath):
        try:
            with open(cpath, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    url = cfg["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temp,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 45),
                                    context=_ssl_context(cfg)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        raise LLMError(f"HTTP {e.code}：{body}")
    except Exception as e:  # noqa  超时/网络/解析
        raise LLMError(f"请求失败：{e}")
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        raise LLMError(f"返回结构异常：{str(data)[:160]}")
    parsed = _extract_json(content)
    if use_cache:   # 落盘，供下次秒回 / 离线复演
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False)
        except Exception:
            pass
    return parsed
