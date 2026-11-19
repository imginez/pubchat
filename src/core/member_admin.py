"""
成员管理：实例 owner 对成员的名单、移出与智能体席位上限。

权限判据只有 `role == "owner"` 一种。房间房主（rooms.owner）不在这里获得
删账号或改上限的权力，否则多个小房主可以互删。
"""
from core.agent_modes import DEFAULT_MODE
from core.protocol import (ERR_AGENT_LIMIT, ERR_BAD_LIMIT, ERR_NOT_FOUND,
                           ERR_NO_PERM)
import asyncio
import json

from core.rooms import me_of

AGENT_LIMIT_MAX = 20
KICK_REASON = "已被移出服务器"


def kick_socket(ws, loop=None):
    """管理口同步线程里关闭一个在线连接。

    websockets >= 14 的 ServerConnection 没有 transport 属性，直接
    ws.transport.close() 是空操作；优先把 close 投递回事件循环，老版本才退回
    transport。
    """
    if loop is not None:
        async def _do():
            try:
                await ws.send(json.dumps({"type": "kicked",
                                          "reason": KICK_REASON,
                                          "message": KICK_REASON},
                                         ensure_ascii=False))
            except Exception:
                pass
            try:
                await ws.close(1011, KICK_REASON)
            except Exception:
                pass
        try:
            asyncio.run_coroutine_threadsafe(_do(), loop)
            return True
        except Exception:
            pass
    if hasattr(ws, "transport"):
        try:
            ws.transport.close()
            return True
        except Exception:
            pass
    return False


def _is_instance_owner(ident):
    return ident.get("kind") == "human" and ident.get("role") == "owner"


def seat_limit_of(store, owner):
    """该成员的智能体席位上限。0 或读不到 = 不限额（维持历史行为）。"""
    u = store.user(owner)
    if not u:
        return 0
    try:
        return int(u.get("agent_limit"))
    except (TypeError, ValueError):
        return 0


def seat_full(store, owner):
    """返回 (受限, 现有席位数, 上限)。"""
    limit = seat_limit_of(store, owner)
    current = len(store.agents_of(owner))
    return (bool(limit) and current >= limit), current, limit


def member_rows(store, hub):
    hub_names = getattr(hub, "by_name", {}) or {}
    out = []
    for u in store.users_with_role():
        cid = u["client_id"]
        agents = [{"client_id": a["client_id"], "name": a["name"],
                   "mode": a.get("mode") or DEFAULT_MODE,
                   "online": a["client_id"] in hub_names}
                  for a in store.agents_of(cid)]
        out.append({"client_id": cid, "name": u.get("name") or cid,
                    "role": u.get("role") or "member",
                    "status": u.get("status") or "active",
                    "online": cid in hub_names,
                    "agent_limit": seat_limit_of(store, cid),
                    "agents": agents})
    return out


def list_members(store, hub, ident):
    """返回 (payload, code, message)。"""
    if not _is_instance_owner(ident):
        return None, ERR_NO_PERM, "只有服务器主人能看成员名单"
    return {"members": member_rows(store, hub)}, None, None


def remove_member(store, ident, client_id):
    cid = (client_id or "").strip().lower()
    if not _is_instance_owner(ident):
        return None, ERR_NO_PERM, "只有服务器主人能移出成员"
    if not cid or not store.user(cid):
        return None, ERR_NOT_FOUND, "没有这个成员"
    if cid in (me_of(ident), ident.get("client_id")):
        return None, ERR_NO_PERM, "不能把自己移出服务器"
    targets = [cid] + [a["client_id"] for a in store.agents_of(cid)]
    removed_agents = [a["client_id"] for a in store.agents_of(cid)]
    store.remove_user(cid)
    return {"client_id": cid, "targets": targets,
            "agents": removed_agents}, None, None


def normalize_limit(limit):
    """返回 (数值, 错误文案)。0＝不限额，上界 AGENT_LIMIT_MAX。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return None, "席位上限只能是 0–%d 的整数（0＝不限）" % AGENT_LIMIT_MAX
    if value < 0 or value > AGENT_LIMIT_MAX:
        return None, "席位上限只能是 0–%d（0＝不限）" % AGENT_LIMIT_MAX
    return value, None


def set_agent_limit(store, ident, client_id, limit):
    cid = (client_id or "").strip().lower()
    if not _is_instance_owner(ident):
        return None, ERR_NO_PERM, "只有服务器主人能调整席位上限"
    if not cid or not store.user(cid):
        return None, ERR_NOT_FOUND, "没有这个成员"
    value, bad = normalize_limit(limit)
    if bad:
        return None, ERR_BAD_LIMIT, bad
    store.set_user_agent_limit(cid, value)
    return {"client_id": cid, "agent_limit": value}, None, None


# ------------------------------ WS 包装 ------------------------------ #

async def handle_member_list(ws, store, hub, ident, data=None):
    payload, code, msg = list_members(store, hub, ident)
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    await hub.send(ws, {"type": "member_list", **payload})


async def handle_member_remove(ws, store, hub, ident, data, log=None):
    payload, code, msg = remove_member(store, ident, data.get("client_id"))
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    targets = set(payload["targets"])
    for sock, other in list(hub.clients.items()):
        if other.get("client_id") not in targets:
            continue
        await hub.send(sock, {"type": "kicked", "reason": KICK_REASON,
                              "message": KICK_REASON})
        hub.remove(sock)
        try:
            await sock.close(1011, KICK_REASON)
        except Exception:
            pass
    if log:
        log("member remove", payload["client_id"], "by", me_of(ident),
            "agents=%d" % len(payload["agents"]))
    await hub.send(ws, {"type": "member_removed", "client_id": payload["client_id"],
                        "agents": payload["agents"]})
    await hub.broadcast(None, {"type": "rooms-changed"})
    await hub.presence(store)


async def handle_member_agent_limit(ws, store, hub, ident, data, log=None):
    payload, code, msg = set_agent_limit(store, ident, data.get("client_id"),
                                         data.get("limit"))
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    if log:
        log("member agent_limit", payload["client_id"], "=", payload["agent_limit"],
            "by", me_of(ident))
    await hub.send(ws, {"type": "member_agent_limit_set",
                        "client_id": payload["client_id"],
                        "limit": payload["agent_limit"],
                        "agent_limit": payload["agent_limit"]})
    await hub.presence(store)
