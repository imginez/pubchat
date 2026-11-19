"""
PubChat 认证。

handle_auth() 处理首帧认证（人类/智能体），welcome_payload() 构造认证成功后的欢迎消息。
"""
from core.agent_modes import agent_payload, modes_payload
from core.protocol import (ERR_BAD_LOGIN, ERR_BAD_AGENT_KEY, ERR_BAD_KIND,
                           ERR_PENDING)
from core.rooms import rooms_for
from core.store import now_iso


def _active(u):
    return (u.get("status") or "active") == "active"


async def handle_auth(ws, store, hub, data):
    """处理首帧认证。返回 ident dict 或 None（失败时已发 error）。"""
    kind = (data.get("kind") or "user").strip()
    if kind == "user":
        cid = (data.get("username") or "").strip().lower()
        key = (data.get("key") or "").strip()
        u = store.user(cid)
        if not u or u["key1"] != key:
            await hub.send(ws, {"type": "error", "code": ERR_BAD_LOGIN,
                                "message": "用户名或一级 key 不对"})
            return None
        if not _active(u):
            await hub.send(ws, {"type": "error", "code": ERR_PENDING,
                                "message": "账号等待房主批准"})
            return None
        return {"kind": "human", "client_id": cid, "name": u["name"],
                "owner": None, "is_admin": bool(u["is_admin"]),
                "role": u.get("role") or "member",
                "onboarded": bool(u.get("onboarded"))}
    if kind == "agent":
        cid = (data.get("client_id") or "").strip().lower()
        key = (data.get("key") or "").strip()
        a = store.agent(cid)
        if not a or a["key2"] != key:
            await hub.send(ws, {"type": "error", "code": ERR_BAD_AGENT_KEY,
                                "message": "智能体 client_id 或二级 key 不对"})
            return None
        owner = store.user(a["owner"])
        if owner and not _active(owner):
            await hub.send(ws, {"type": "error", "code": ERR_PENDING,
                                "message": "账号等待房主批准"})
            return None
        return {"kind": "agent", "client_id": cid, "name": a["name"],
                "owner": a["owner"], "owner_name": owner["name"] if owner else a["owner"],
                "is_admin": False}
    await hub.send(ws, {"type": "error", "code": ERR_BAD_KIND, "message": "kind 必须是 user/agent"})
    return None


def welcome_payload(store, ident, last_read=None):
    """构造认证成功后的欢迎消息（含房间列表、智能体列表等）。"""
    users = store.users()
    rooms = rooms_for(store, ident, users, last_read)
    payload = {"type": "authenticated", "me": ident, "rooms": rooms, "ts": now_iso()}
    if ident["kind"] == "human":
        payload["agents"] = [agent_payload(a)
                             for a in store.agents_of(ident["client_id"])]
        payload["agent_modes"] = modes_payload()
    return payload
