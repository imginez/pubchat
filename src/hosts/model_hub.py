"""模型中转站（批4③）——只读展示，本模块不许有任何写入口。

为什么是「只读」而不是「先做个开关」：需求方要的是看得见的口径（上游有哪些模型、
本实例注册了哪些 Key、分给了哪些智能体），而写入 upstream 的配置属于另一件事，
一旦开了写入口，管理面就要同时负责凭证落盘与审计，这一批不背。

三条硬约束（都由 tests/smoke.py 的 unit_model_hub() 钉住）：
① 上游地址与 Key **只从环境变量读**（``PUBCHAT_MODEL_BASE`` / ``PUBCHAT_MODEL_KEY``），
   不接受调用方传 URL——否则这个只读接口立刻变成 SSRF 探针；也**不落盘、不打印**，
   与需求方的凭据红线同一口径。
② 对上游只发 GET、且永不带 body（`urlopen` 一旦收到 ``data=`` 就变成 POST，
   所以这里根本不传那个参数）。
③ 回包里任何 Key 一律掩码，URL 里的 userinfo 一律剥掉。
"""

import json
import os
import ssl
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ENV_BASE = "PUBCHAT_MODEL_BASE"
ENV_KEY = "PUBCHAT_MODEL_KEY"

TIMEOUT = 6.0            # 管理页等人没意义，上游慢就报慢
MAX_BYTES = 1 << 20      # 1MB：上游回什么都不至于把这台机器压垮
CACHE_TTL = 60.0         # 刷新页面不该每次都打上游
MAX_MODELS = 200
MAX_ID_LEN = 80

_cache = {"at": 0.0, "payload": None}
_cache_lock = threading.Lock()


def upstream_config():
    """上游前缀与 Key 的唯一来源。返回 (base, key)，没配就是空串。"""
    return (os.environ.get(ENV_BASE) or "").strip(), (os.environ.get(ENV_KEY) or "").strip()


def mask(secret):
    """留头 3 尾 2 够分辨是哪个 Key，中间一律不打出去。"""
    s = str(secret or "")
    if not s:
        return ""
    if len(s) <= 6:
        return "*" * len(s)
    return s[:3] + "·" + ("*" * 6) + "·" + s[-2:]


def safe_base(base):
    """能显示的上游地址：剥掉 userinfo（有人会把 key 写进 https://user:pw@host）。"""
    parts = urlsplit(str(base or ""))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = parts.netloc.rsplit("@", 1)[-1]
    return parts.scheme + "://" + host + parts.path


def _models_url(base):
    return str(base).rstrip("/") + "/models"


def fetch_models(base, key):
    """拉上游 /v1/models。任何失败都变成结构化字段，不抛出去——只读页不能因为
    上游挂了就打不开管理口。"""
    if not base:
        return {"ok": False, "state": "unconfigured", "models": [],
                "detail": "未配置上游：设 %s 为完整前缀（例如 https://api.openai.com/v1）" % ENV_BASE}
    if safe_base(base) == "":
        return {"ok": False, "state": "bad_base", "models": [],
                "detail": "%s 需要是 http(s) 完整前缀，例如 https://api.openai.com/v1" % ENV_BASE}
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = Request(_models_url(base), headers=headers, method="GET")
    try:
        with urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as resp:
            status = getattr(resp, "status", 200) or 200
            raw = resp.read(MAX_BYTES)
    except Exception as exc:                                # noqa: BLE001 - 上游怎么炸都不该炸页面
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return {"ok": False, "state": "denied", "models": [],
                    "detail": "上游拒绝该凭证（HTTP %s）" % code}
        return {"ok": False, "state": "error", "models": [],
                "detail": "拉取失败：%s" % type(exc).__name__}
    if status != 200:
        return {"ok": False, "state": "http_%s" % status, "models": [],
                "detail": "上游返回 HTTP %s" % status}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"ok": False, "state": "bad_json", "models": [], "detail": "上游回包不是 JSON"}
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {"ok": False, "state": "no_data", "models": [],
                "detail": "上游回包没有 data 数组"}
    out = []
    for it in items[:MAX_MODELS]:
        mid = str((it or {}).get("id") if isinstance(it, dict) else it)[:MAX_ID_LEN]
        if not mid:
            continue
        out.append({"id": mid,
                    "owned_by": str(it.get("owned_by") or "")[:40] if isinstance(it, dict) else "",
                    "created": it.get("created") if isinstance(it, dict) else None})
    return {"ok": True, "state": "ok", "models": out,
            "detail": "", "truncated": len(items) > MAX_MODELS, "total": len(items)}


def registered_keys(store):
    """本实例已注册的智能体 Key（掩码）与归属——这就是「分配给哪些智能体」的那份真相。"""
    rows = []
    for a in store.all_agents():
        rows.append({
            "client_id": a.get("client_id") or "",
            "name": a.get("name") or "",
            "owner": a.get("owner") or "",
            "key_masked": mask(a.get("key2")),
            "mode": a.get("mode") or "",
            "created_at": a.get("created_at") or "",
        })
    return rows


def owner_quotas(store):
    """每个主人名下的席位数与上限——本仓没有 token 计量，能给的真实数字只有这个。"""
    counts = {}
    for a in store.all_agents():
        counts[a.get("owner") or ""] = counts.get(a.get("owner") or "", 0) + 1
    out = []
    for u in store.users_with_role():
        cid = u.get("client_id") or ""
        out.append({
            "client_id": cid,
            "name": u.get("name") or "",
            "role": u.get("role") or "member",
            "agent_limit": u.get("agent_limit"),
            "agents": counts.get(cid, 0),
        })
    out.sort(key=lambda r: (-r["agents"], r["client_id"]))
    return out


def usage(store):
    """用量口径要说清：本仓没有计费表，这里只有「各智能体发了多少条消息」，
    它不是 token 用量，页面上也必须这么写。"""
    try:
        rows = store.message_counts_by_sender()
    except AttributeError:
        return {"available": False, "by_sender": [],
                "note": "服务端没有按发送者统计的读接口"}
    return {"available": True,
            "by_sender": [{"sender": r.get("sender") or "", "count": int(r.get("count") or 0)}
                          for r in rows][:400],
            "note": "消息条数，不是 token 用量"}


def snapshot(store, force=False):
    """管理页那一屏的全部数据。带 60 秒缓存，刷新页面不该每次打上游。"""
    now = time.time()
    if not force:
        with _cache_lock:
            if _cache["payload"] and now - _cache["at"] < CACHE_TTL:
                return _cache["payload"]
    base, key = upstream_config()
    payload = {
        "ok": True,
        "readOnly": True,
        "upstream": {"configured": bool(base),
                     "display_base": safe_base(base),
                     "has_key": bool(key),
                     "key_masked": mask(key) if base or key else "",
                     "env": [ENV_BASE, ENV_KEY]},
        "models": fetch_models(base, key),
        "agents": registered_keys(store),
        "owners": owner_quotas(store),
        "usage": usage(store),
        "cached_at": now,
    }
    with _cache_lock:
        _cache["at"] = now
        _cache["payload"] = payload
    return payload
