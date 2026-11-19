"""
PubChat 房间与权限。

按房路由与越权拒收：can_access() 校验身份是否有权进入某房间，
rooms_for() 返回该身份能看到/能进的房间列表。
"""
from core import agent_dm
from core.protocol import LOBBY


def pair(a, b):
    return "-".join(sorted([a, b]))


def dm_slug(a, b):
    return "dm-" + pair(a, b)


def own_slug(u):
    return "own-" + u


def me_of(ident):
    """人类或智能体 -> 归属人的 client_id"""
    return ident.get("owner") or ident.get("client_id")


def can_access(ident, slug, store=None):
    """校验身份是否有权进入某房间。

    第一级是实例成员（能登录）；第二级是房间成员（room_members）。
    大厅公开、私聊按双方、自己的专属房天然可进；房主可进任何房间，
    其余房间必须命中 room_members。
    adm-（智能体 ⇄ 智能体）是例外：只认两个智能体本人和各自的主人，
    实例主人也不许跨家进别人的智能体私聊，所以分支必须排在 owner 捷径前面。
    """
    if slug == LOBBY:
        return True
    if ident.get("kind") == "system":
        return False
    if store is not None:
        row = store.room(slug)
        if row and row.get("archived"):
            return False
    if slug.startswith(agent_dm.PREFIX):
        pair_ids = agent_dm.split_slug(slug)
        if not pair_ids:
            return False
        return agent_dm.can_access(ident, pair_ids[0], pair_ids[1], store)
    me = me_of(ident)
    if ident.get("role") == "owner":
        return True
    if slug == own_slug(me):
        return True
    if slug.startswith("dm-"):
        try:
            a, b = slug[3:].split("-", 1)
        except ValueError:
            return False
        return me in (a, b)
    if store is not None and store.is_room_member(slug, me):
        return True
    return False


def rooms_for(store, ident, users, last_read=None):
    """该身份能看到/能进的房间（大厅 + 自己房 + 私聊 + 被拉进的共享房）"""
    me = me_of(ident)
    out = [{"slug": LOBBY, "kind": "lobby", "title": "公共大厅"}]
    out.append({"slug": own_slug(me), "kind": "own", "title": "我的房间"})
    for u in users:
        if u["client_id"] == me:
            continue
        out.append({"slug": dm_slug(me, u["client_id"]), "kind": "dm",
                    "title": u["name"]})
    seen = {r["slug"] for r in out}
    if store is not None:
        for rid in store.rooms_of_user(me):
            if rid in seen or rid == LOBBY or rid.startswith("dm-"):
                continue
            if rid.startswith("own-"):
                u = store.user(rid[4:])
                title = (u["name"] if u else rid[4:]) + " 的个人房"
                owner = rid[4:]
            else:
                row = store.room(rid)
                if not row or row.get("archived"):
                    continue
                title = row.get("title") or ("共享房间 · " + rid)
                owner = row.get("owner")
            out.append({"slug": rid, "kind": "shared", "title": title,
                        "owner": owner})
            seen.add(rid)
        try:
            for r in agent_dm.entries_for(store, ident):
                if r["slug"] in seen:
                    continue
                out.append(r)
                seen.add(r["slug"])
        except Exception:
            pass
    try:
        last = store.last_ts_map()
        last_id = store.last_id_map()
    except Exception:
        last, last_id = {}, {}
    last_read = last_read or {}
    for r in out:
        r["last_ts"] = last.get(r["slug"])
        r["last_id"] = last_id.get(r["slug"])
        try:
            r["unread"] = store.unread_count(r["slug"], last_read.get(r["slug"]),
                                            ident["client_id"])
        except Exception:
            r["unread"] = 0
    return out
