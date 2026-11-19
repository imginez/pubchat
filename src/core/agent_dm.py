"""
智能体 ⇄ 智能体 私聊通道（0.8 批2）。

房间是虚拟的：slug = `adm-<a>-<b>`（两个智能体 client_id 字典序拼接），不进 rooms 表，
配对关系与归档态记在 agent_dm 表里 —— 与 dm-/own- 一样由前缀路由。

权限只有四类人：两个智能体本人 + 各自的主人。实例主人**不**享有
「role=owner 可进任何房间」那条捷径，否则等于允许跨家偷看别人的智能体私聊，
所以 rooms.can_access() 里 adm- 分支必须排在 role==owner 捷径之前。
"""
from core.protocol import (ERR_AGENT_DM_AMBIGUOUS, ERR_AGENT_DM_SELF,
                           ERR_NO_AGENT, ERR_NO_PERM,
                           TYPE_AGENT_DM_OPENED)

PREFIX = "adm-"


def slug_for(a, b):
    return PREFIX + "-".join(sorted([a, b]))


def split_slug(slug):
    """adm-<a>-<b> -> (a, b)；不合规返回 None。"""
    if not slug or not slug.startswith(PREFIX):
        return None
    try:
        a, b = slug[len(PREFIX):].split("-", 1)
    except ValueError:
        return None
    return (a, b) if a and b else None


def _owner_of(store, agent_id):
    row = store.agent(agent_id) if store is not None else None
    return row.get("owner") if row else None


def can_access(ident, a, b, store=None):
    """adm- 房的进入判据：智能体本人，或参与者各自的主人。

    配对行必须存在且未归档 —— 表是唯一真值，删掉席位即等于关掉这条私聊。
    """
    kind = ident.get("kind")
    cid = ident.get("client_id")
    if not cid or not a or not b:
        return False
    if kind == "agent":
        if cid not in (a, b):
            return False
    elif kind == "human":
        if cid not in (_owner_of(store, a), _owner_of(store, b)):
            return False
    else:
        return False
    if store is None:
        return True
    row = store.agent_dm(a, b)
    return bool(row) and not int(row.get("archived") or 0)


def title_of(store, a, b):
    def nm(cid):
        row = store.agent(cid) if store is not None else None
        return (row or {}).get("name") or cid
    return "「%s」×「%s」" % (nm(a), nm(b))


def entries_for(store, ident, rows=None):
    """该身份名下的 adm- 房条目（rooms_for 用）。"""
    out = []
    for row in (store.agent_dm_rows() if rows is None else rows):
        if int(row.get("archived") or 0):
            continue
        a, b = row["a"], row["b"]
        if not can_access(ident, a, b, store):
            continue
        out.append({"slug": slug_for(a, b), "kind": "agent_dm",
                    "title": title_of(store, a, b), "a": a, "b": b,
                    "owners": [x for x in (_owner_of(store, a), _owner_of(store, b)) if x]})
    return out


def open_dm(store, ident, target_agent, own_agent=None):
    """开一条智能体私聊。返回 (payload, code, message)。

    发起方：智能体本人，或该智能体的主人（人类可用 own_client_id 指名用哪个席位，
    名下只有一个席位时可省略）。一期无需对方同意即可建。
    """
    kind = ident.get("kind")
    if kind not in ("agent", "human"):
        return None, ERR_NO_PERM, "只有智能体或它的主人能开私聊"
    if ident.get("role") == "readonly":
        return None, ERR_NO_PERM, "只读成员不能开智能体私聊"
    me = ident["client_id"]
    hint = (own_agent or "").strip().lower()
    target_id = (target_agent or "").strip().lower()
    target = store.agent(target_id) if target_id else None
    if not target:
        return None, ERR_NO_AGENT, "没有这个智能体"

    if kind == "agent":
        if hint and hint != me:
            return None, ERR_NO_PERM, "智能体只能以自己的身份开私聊"
        mine = store.agent(me)
        if not mine:
            return None, ERR_NO_AGENT, "没有这个智能体"
    else:
        candidates = store.agents_of(me)
        if hint:
            mine = next((x for x in candidates if x["client_id"] == hint), None)
            if not mine:
                return None, ERR_NO_PERM, "这不是你的智能体"
        elif len(candidates) == 1:
            mine = candidates[0]
        elif not candidates:
            return None, ERR_NO_AGENT, "你还没有智能体席位"
        else:
            return None, ERR_AGENT_DM_AMBIGUOUS, \
                "你有 %d 个智能体，请带上 own_client_id 指明用哪一个开这条私聊" % len(candidates)

    a, b = sorted([mine["client_id"], target["client_id"]])
    if a == b:
        return None, ERR_AGENT_DM_SELF, "不能和自己私聊"

    existed = store.agent_dm(a, b)
    store.open_agent_dm(a, b, me)
    return {"room": slug_for(a, b), "a": a, "b": b,
            "title": title_of(store, a, b),
            "created": not existed or bool(int(existed.get("archived") or 0))}, None, None


async def handle_agent_dm_open(ws, store, hub, ident, data, log=None):
    payload, code, msg = open_dm(store, ident, data.get("client_id"),
                                 data.get("own_client_id"))
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    # 双方（含发起方自己）都在房内，一次按房广播就够，不必再单发一份回执
    await hub.broadcast(ident, {"type": TYPE_AGENT_DM_OPENED, **payload})
    await hub.broadcast(None, {"type": "rooms-changed"})
    if log:
        log("agent_dm open", payload["room"], "by", ident["client_id"],
            "created=%s" % payload["created"])
