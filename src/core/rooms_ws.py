"""
房间 v2 的 WS 处理：建房 / 改名 / 归档 / 邀请 / 退房。

设计约束（见 docs/superpowers/specs/2026-11-08-mobile-parity-wechat-ui-design.md）：
- 房间默认仅邀请；没有 room_join，进房只能由房主邀请或凭房间邀请 token。
- 普通成员每人最多 3 个未归档房间；实例 owner 不受限。
- 房间邀请严格一次性，token 只存哈希。
"""
import re
import secrets
from datetime import datetime, timedelta

from core.rooms import rooms_for
from core.store import CST, _hash_token

ROOM_LIMIT = 3
ROOM_TITLE_MAX = 20
ROOM_INVITE_MAX = 20
ROOM_INVITE_HOURS = 24
INVITE_HOURS_CHOICES = (24, 48, 0)
RESERVED = ("lobby", "own-", "dm-", "adm-")


def _expires_at(hours=None):
    """邀请有效期：24 / 48 / 0（永久，返回 None）。非法值回落 24。"""
    try:
        value = int(hours)
    except (TypeError, ValueError):
        value = ROOM_INVITE_HOURS
    if value not in INVITE_HOURS_CHOICES:
        value = ROOM_INVITE_HOURS
    if value == 0:
        return None
    return (datetime.now(CST) + timedelta(hours=value)).isoformat(timespec="seconds")


def _me(ident):
    return ident.get("owner") or ident.get("client_id")


def _is_instance_owner(ident):
    return ident.get("kind") == "human" and ident.get("role") == "owner"


def _valid_title(title):
    title = (title or "").strip()[:ROOM_TITLE_MAX]
    return title if len(title) >= 2 else ""


def _new_slug(store, title):
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:20] or "room"
    for _ in range(6):
        rid = "g-" + base + "-" + secrets.token_hex(3)
        if not any(rid.startswith(p) for p in RESERVED) and not store.room(rid):
            return rid
    return "g-room-" + secrets.token_hex(6)


def _room_view(store, ident, rid):
    users = store.users()
    for r in rooms_for(store, ident, users):
        if r["slug"] == rid:
            return r
    return {"slug": rid, "kind": "shared", "title": rid}


def create_room(store, ident, title, allow=True, limit=ROOM_LIMIT):
    """返回 (payload, code, message)。payload 含 room + rooms 快照。"""
    if ident.get("kind") != "human":
        return None, "NO_PERM", "智能体不能建房"
    if ident.get("role") == "readonly":
        return None, "NO_PERM", "只读成员不能建房"
    if not allow and not _is_instance_owner(ident):
        return None, "ROOM_CREATE_OFF", "管理员关闭了建房"
    name = _valid_title(title)
    if not name:
        return None, "BAD_TITLE", "房间名需 2–%d 个字" % ROOM_TITLE_MAX
    me = _me(ident)
    if not _is_instance_owner(ident) and store.count_owned_rooms(me) >= limit:
        return None, "ROOM_LIMIT", "每人最多 %d 个房间" % limit
    rid = _new_slug(store, name)
    row = store.create_room(rid, name, me)
    room = {"slug": rid, "kind": "shared", "title": name,
            "owner": me, "archived": 0}
    return {"room": room, "rooms": rooms_for(store, ident, store.users())}, None, None


def rename_room(store, ident, rid, title):
    row = store.room(rid)
    if not row or row.get("archived"):
        return None, "NO_ROOM", "房间不存在"
    if not (_is_instance_owner(ident) or row.get("owner") == _me(ident)):
        return None, "NO_PERM", "只有房主能改名"
    name = _valid_title(title)
    if not name:
        return None, "BAD_TITLE", "房间名需 2–%d 个字" % ROOM_TITLE_MAX
    store.rename_room(rid, name)
    return {"room": {"slug": rid, "title": name},
            "rooms": rooms_for(store, ident, store.users())}, None, None


def archive_room(store, ident, rid):
    row = store.room(rid)
    if not row or row.get("archived"):
        return None, "NO_ROOM", "房间不存在"
    if not (_is_instance_owner(ident) or row.get("owner") == _me(ident)):
        return None, "NO_PERM", "只有房主能归档"
    store.archive_room(rid)
    return {"room": rid, "rooms": rooms_for(store, ident, store.users())}, None, None


def leave_room(store, ident, rid):
    row = store.room(rid)
    if not row or row.get("archived"):
        return None, "NO_ROOM", "房间不存在"
    me = _me(ident)
    if row.get("owner") == me:
        return None, "NO_PERM", "房主不能退出，请归档或移交"
    if not store.is_room_member(rid, me):
        return None, "NO_ROOM", "你不在这个房间"
    store.remove_room_member(rid, me)
    return {"room": rid, "rooms": rooms_for(store, ident, store.users())}, None, None


def invite_to_room(store, ident, rid, count=1, hours=None):
    row = store.room(rid)
    if not row or row.get("archived"):
        return None, "NO_ROOM", "房间不存在"
    if not (_is_instance_owner(ident) or row.get("owner") == _me(ident)):
        return None, "NO_PERM", "只有房主能邀请"
    try:
        count = int(count or 1)
    except (TypeError, ValueError):
        return None, "TOO_MANY", "数量必须是数字"
    if count < 1 or count > ROOM_INVITE_MAX:
        return None, "TOO_MANY", "一次最多 %d 条" % ROOM_INVITE_MAX
    expires = _expires_at(hours)
    items = []
    for _ in range(count):
        token = secrets.token_urlsafe(24)
        store.add_invite(_hash_token(token), "member", expires, _me(ident),
                         room_id=rid)
        items.append({"token": token, "url": "/join.html#t=" + token,
                      "expires_at": expires})
    return {"room": rid, "invites": items}, None, None


def invite_to_instance(store, ident, count=1, hours=None):
    """邀请新成员加入整个实例（不带房间）。只有实例 owner 能生成。"""
    if not _is_instance_owner(ident):
        return None, "NO_PERM", "只有服务器主人能邀请"
    try:
        count = int(count or 1)
    except (TypeError, ValueError):
        return None, "TOO_MANY", "数量必须是数字"
    if count < 1 or count > ROOM_INVITE_MAX:
        return None, "TOO_MANY", "一次最多 %d 条" % ROOM_INVITE_MAX
    expires = _expires_at(hours)
    items = []
    for _ in range(count):
        token = secrets.token_urlsafe(24)
        store.add_invite(_hash_token(token), "member", expires, _me(ident),
                         room_id="")
        items.append({"token": token, "url": "/join.html#t=" + token,
                      "expires_at": expires})
    return {"invites": items}, None, None


# ------------------------------ WS 包装 ------------------------------ #

async def handle_room_create(ws, store, hub, ident, data, settings=None, log=None):
    inst = settings or {}
    payload, code, msg = create_room(
        store, ident, data.get("title"),
        allow=bool(inst.get("allow_room_create", True)))
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    await hub.send(ws, {"type": "room_created", **payload})
    await hub.broadcast(None, {"type": "rooms-changed"})


async def handle_room_rename(ws, store, hub, ident, data, settings=None, log=None):
    payload, code, msg = rename_room(store, ident, (data.get("room") or "").strip(),
                                     data.get("title"))
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    await hub.send(ws, {"type": "room_renamed", **payload})
    await hub.broadcast(None, {"type": "rooms-changed"})


async def handle_room_archive(ws, store, hub, ident, data, settings=None, log=None):
    payload, code, msg = archive_room(store, ident, (data.get("room") or "").strip())
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    await hub.send(ws, {"type": "room_archived", **payload})
    await hub.broadcast(None, {"type": "rooms-changed"})


async def handle_room_leave(ws, store, hub, ident, data, settings=None, log=None):
    payload, code, msg = leave_room(store, ident, (data.get("room") or "").strip())
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    await hub.send(ws, {"type": "room_left", **payload})
    await hub.broadcast(None, {"type": "rooms-changed"})


async def handle_room_invite(ws, store, hub, ident, data, settings=None, log=None):
    payload, code, msg = invite_to_room(store, ident,
                                        (data.get("room") or "").strip(),
                                        data.get("count", 1),
                                        data.get("hours"))
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    await hub.send(ws, {"type": "room_invites", **payload})


async def handle_instance_invite(ws, store, hub, ident, data, settings=None, log=None):
    payload, code, msg = invite_to_instance(store, ident, data.get("count", 1),
                                            data.get("hours"))
    if code:
        return await hub.send(ws, {"type": "error", "code": code, "message": msg})
    await hub.send(ws, {"type": "instance_invites", **payload})
