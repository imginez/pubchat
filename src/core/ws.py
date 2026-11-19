"""
PubChat WebSocket 层。

Hub 类管理在线连接，handler() 分发 WS 消息到各处理函数。
"""
import asyncio
import json
import secrets

from core.auth import handle_auth, welcome_payload
from core.agent_modes import (DEFAULT_MODE, MODES, agent_payload,
                              mode_label, prompts_for)
from core.protocol import (
    LOBBY, TYPE_AUTH, TYPE_JOIN, TYPE_MESSAGE, TYPE_HISTORY, TYPE_ROOMS,
    TYPE_PRESENCE, TYPE_PROFILE, TYPE_AGENT_RENAME, TYPE_AGENT_CREATE,
    TYPE_AGENT_REMOVE, TYPE_AGENT_SET_MODE, TYPE_AGENT_ROTATE_KEY, TYPE_ONBOARDED, TYPE_PING,
    TYPE_ROOM_MEMBERS, TYPE_TASK_STREAM, TYPE_TASK_LIST, TYPE_TYPING,
    TYPE_MEMBER_LIST, TYPE_MEMBER_REMOVE, TYPE_MEMBER_AGENT_LIMIT,
    TYPE_AGENT_DM_OPEN, TYPE_MESSAGE_ACK, TYPE_DELIVERY,
    TYPE_MEMBER_RESET_KEY, TYPE_DEVICES_LIST, TYPE_DEVICE_REVOKE,
    TYPE_SETTINGS_GET, TYPE_SETTINGS_SET,
    MENTION_ALL_TOKENS, ERR_AUTH_REQUIRED, ERR_BAD_MODE, ERR_AGENT_LIMIT,
    ERR_NO_PERM, ERR_UNKNOWN, ERR_BAD_REPLY,
)
from core.rooms import can_access, rooms_for, me_of
from core.agent_dm import handle_agent_dm_open
from core.delivery import AckLedger, handle_delivery, handle_message_ack
from core.instance_ws import (handle_device_revoke, handle_devices_list,
                              handle_member_reset_key, handle_settings_get,
                              handle_settings_set)
from core.member_admin import (handle_member_agent_limit, handle_member_list,
                               handle_member_remove, seat_full)
from core.rooms_ws import (handle_room_create, handle_room_rename,
                           handle_room_archive, handle_room_leave,
                           handle_room_invite, handle_instance_invite)
from core.store import now_iso, jd
from core.tasks_ws import (handle_task_stream, handle_task_list,
                           handle_room_members, room_participants)


class Hub:
    """WebSocket 连接管理器。"""
    def __init__(self, store=None):
        self.clients = {}          # ws -> ident
        self.by_name = {}          # client_id -> set(ws)
        self.store = store
        self.loop = None           # 主事件循环；管理口踢人时把 close 投递回来用
        self.acks = AckLedger()    # 读回执：内存态，重启清零

    def add(self, ws, ident):
        self.clients[ws] = ident
        self.by_name.setdefault(ident["client_id"], set()).add(ws)

    def remove(self, ws):
        ident = self.clients.pop(ws, None)
        if ident:
            s = self.by_name.get(ident["client_id"])
            if s:
                s.discard(ws)
                if not s:
                    self.by_name.pop(ident["client_id"], None)
        return ident

    def online(self):
        return sorted(self.by_name.keys())

    async def send(self, ws, obj):
        try:
            await ws.send(jd(obj))
        except Exception:
            pass

    async def broadcast(self, ident, obj):
        """按房间路由：只发给能动这个房间的人"""
        room = obj.get("room")
        for ws, other in list(self.clients.items()):
            if room and not can_access(other, room, self.store):
                continue
            await self.send(ws, obj)

    async def presence(self, store):
        members = []
        for u in store.users():
            members.append({"client_id": u["client_id"], "name": u["name"],
                            "kind": "human", "online": u["client_id"] in self.by_name})
        for a in store.all_agents():
            members.append({"client_id": a["client_id"], "name": a["name"],
                            "kind": "agent", "owner": a["owner"],
                            "mode": a.get("mode") or DEFAULT_MODE,
                            "online": a["client_id"] in self.by_name})
        await self.broadcast(None, {"type": "presence", "members": members,
                                    "online": self.online(), "ts": now_iso()})


def parse_mentions(body, store):
    """@显示名 或 @client_id -> 被提及者 client_id 列表"""
    found = []
    for a in store.all_agents():
        for token in (a["name"], a["client_id"]):
            if token and ("@" + token) in body:
                found.append(a["client_id"])
                break
    for u in store.users():
        for token in (u["name"], u["client_id"]):
            if token and ("@" + token) in body:
                found.append(u["client_id"])
                break
    return list(dict.fromkeys(found))


MAX_QUOTE_EXCERPT = 120


def attach_quotes(store, msgs):
    """给每条消息补上被引用消息的名字与摘要。

    引用只带 id 的话，客户端得再发一次 history 才能画出引用块，历史页就会 N+1 查。
    """
    wanted = {m["reply_to"] for m in msgs if m.get("reply_to")}
    if not wanted:
        return [dict(m, reply_to_name=None, reply_to_excerpt=None) for m in msgs]
    by_id = store.messages_by_ids(wanted)
    out = []
    for m in msgs:
        q = by_id.get(m.get("reply_to"))
        excerpt = None
        if q:
            excerpt = (q["content"] or "").replace("\n", " ").strip()
            if len(excerpt) > MAX_QUOTE_EXCERPT:
                excerpt = excerpt[:MAX_QUOTE_EXCERPT] + "…"
        out.append(dict(m, reply_to_name=q["sender_name"] if q else None,
                        reply_to_excerpt=excerpt))
    return out


async def handle_message(ws, store, hub, ident, data):
    """处理消息发送。"""
    body = (data.get("content") or "").strip()
    room = (data.get("room") or LOBBY).strip()
    if not body:
        await hub.send(ws, {"type": "error", "code": "EMPTY", "message": "空消息"})
        return
    if not can_access(ident, room, store):
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "你不在这个房间里"})
        return
    if ident.get("role") == "readonly":
        await hub.send(ws, {"type": "error", "code": "READONLY",
                            "message": "你是只读成员，不能发消息"})
        return
    reply_to = data.get("reply_to")
    if reply_to in (None, "", 0, "0"):
        reply_to = None
    else:
        quoted = store.message(reply_to)
        # 跨房引用等于把另一个房间的正文搬进这个房间，绕过房间隔离，所以只许引本房。
        if not quoted or quoted["room"] != room:
            await hub.send(ws, {"type": "error", "code": ERR_BAD_REPLY,
                                "message": "引用的消息不存在，或不在这个房间里"})
            return
        reply_to = quoted["id"]
    mentions = parse_mentions(body, store)
    if any(tok in body for tok in MENTION_ALL_TOKENS):
        # @全体成员：本房能被 @ 到的人（人类 + 智能体）全算上
        mentions = [m["client_id"] for m in room_participants(store, ident, room)
                    if m["client_id"] != ident["client_id"]]
    msg = store.add_message(room, ident["client_id"], ident["name"], body, mentions,
                            reply_to=reply_to)
    await hub.broadcast(ident, {"type": "message", **attach_quotes(store, [msg])[0]})
    await hub.send(ws, {"type": "ack", "message_id": msg["id"], "status": "ok"})


async def handle_typing(ws, store, hub, ident, data):
    """输入中状态：只广播、不落库、不回 ack（前端不该看到一条「正在输入」的消息）。

    C→S: {"type":"typing","room":"lobby","message_id":43,"state":"start"|"stop"}
    S→C: {"type":"typing","room":..,"from":"alice_a1","name":"Alice-1",
          "message_id":43,"state":"start","ts":"..."}
    """
    room = str(data.get("room") or LOBBY).strip() or LOBBY
    if not can_access(ident, room, store):
        return await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                                   "message": "你不在这个房间里"})
    state = "stop" if str(data.get("state") or "").lower() == "stop" else "start"
    raw_mid = data.get("message_id")
    try:
        mid = int(raw_mid) if raw_mid is not None else None
    except (TypeError, ValueError):
        mid = None
    await hub.broadcast(ident, {
        "type": TYPE_TYPING, "room": room,
        "from": ident["client_id"], "name": ident.get("name") or ident["client_id"],
        "message_id": mid, "state": state, "ts": now_iso(),
    })


async def handle_profile(ws, store, hub, ident, data, log):
    """本人改：显示名、登录名（可选）、一级 key。改完让前端重新登录。"""
    if ident["kind"] != "human":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM, "message": "只有本人能改自己的账号"})
        return
    name = (data.get("name") or "").strip()[:20]
    key1 = (data.get("key") or "").strip()
    new_cid = (data.get("username") or ident["client_id"]).strip().lower()
    if not name:
        await hub.send(ws, {"type": "error", "code": "EMPTY_NAME", "message": "显示名不能为空"})
        return
    if len(key1) < 8:
        await hub.send(ws, {"type": "error", "code": "WEAK_KEY", "message": "一级 key 至少 8 位"})
        return
    from core.protocol import valid_client_id
    if not valid_client_id(new_cid):
        await hub.send(ws, {"type": "error", "code": "BAD_USERNAME",
                            "message": "登录名只能小写字母/数字/下划线，2-32 位"})
        return
    old_cid = ident["client_id"]
    if new_cid != old_cid and store.user(new_cid):
        await hub.send(ws, {"type": "error", "code": "USERNAME_TAKEN",
                            "message": "这个登录名已被占用"})
        return
    old_name = ident["name"]
    store.profile_human(old_cid, new_cid, name, key1)
    for a in store.agents_of(new_cid):
        prefix = old_name + "-"
        tail = a["name"][len(prefix):]
        if a["name"].startswith(prefix) and tail.isdigit():
            store.rename_agent(a["client_id"], name + a["name"][len(old_name):])
    log("profile", old_cid, "->", new_cid, f"({name})")
    await hub.send(ws, {"type": "account", "ok": True, "username": new_cid,
                        "name": name, "key": key1, "relogin": True})
    await hub.presence(store)


async def handle_agent_rename(ws, store, hub, ident, data, log):
    """本人给名下智能体起名（只改显示名，client_id/二级 key 不动）。"""
    acid = (data.get("client_id") or "").strip().lower()
    name = (data.get("name") or "").strip()[:20]
    a = store.agent(acid)
    if not a:
        await hub.send(ws, {"type": "error", "code": "NO_AGENT", "message": "没有这个智能体"})
        return
    if a["owner"] != me_of(ident):
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM, "message": "这不是你的智能体"})
        return
    if not name:
        await hub.send(ws, {"type": "error", "code": "EMPTY_NAME", "message": "名字不能为空"})
        return
    store.rename_agent(acid, name)
    for other in hub.clients.values():
        if other.get("client_id") == acid:
            other["name"] = name
    log("agent rename", acid, "->", name)
    await hub.send(ws, {"type": "agent_renamed", "client_id": acid, "name": name})
    await hub.presence(store)


async def handle_agent_create(ws, store, hub, ident, data, log):
    """本人给自己加一个智能体位（受 users.agent_limit 约束，0＝不限）。client_id 取 <owner>_aN，N 取最小空位。"""
    if ident["kind"] != "human":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "只有本人能加自己的智能体"})
        return
    if ident.get("role") == "readonly":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "只读成员不能加智能体位"})
        return
    mode = (data.get("mode") or "").strip().lower() or DEFAULT_MODE
    if mode not in MODES:
        await hub.send(ws, {"type": "error", "code": ERR_BAD_MODE,
                            "message": "接入方式只能是 online 或 inbox"})
        return
    owner = me_of(ident)
    blocked, current, limit = seat_full(store, owner)
    if blocked:
        await hub.send(ws, {"type": "error", "code": ERR_AGENT_LIMIT,
                            "message": "你的智能体席位已满（%d/%d），请让房主调高上限"
                                       % (current, limit)})
        return
    used = set()
    for a in store.agents_of(owner):
        cid = a["client_id"]
        prefix = owner + "_a"
        if cid.startswith(prefix) and cid[len(prefix):].isdigit():
            used.add(int(cid[len(prefix):]))
    n = 1
    while n in used:
        n += 1
    acid = "%s_a%d" % (owner, n)
    u = store.user(owner)
    base = (u["name"] if u else ident["name"]) or owner
    name = (data.get("name") or "").strip()[:20] or ("%s-%d" % (base, n))[:20]
    k2 = secrets.token_urlsafe(12)
    store.add_agent(acid, name, owner, k2, mode)
    log("agent create", acid, "by", owner, "mode", mode)
    await hub.send(ws, {"type": "agent_created",
                        "agent": agent_payload(store.agent(acid))})
    await hub.presence(store)


async def handle_agent_rotate_key(ws, store, hub, ident, data, log):
    """本人给自己的智能体换二级 Key（0.8.15）：旧 Key 立刻失效，在线连接踢下线。

    以前只有管理口 /api/admin/agent/reset-key 能做，云端用户没有管理员凭证就换不了；
    这条帧让「软件里就能换」跟改名/删除一样顺手。
    """
    if ident["kind"] != "human":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "只有本人能换自己智能体的 Key"})
        return
    if ident.get("role") == "readonly":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "只读成员不能换 Key"})
        return
    acid = (data.get("client_id") or "").strip().lower()
    a = store.agent(acid)
    if not a:
        await hub.send(ws, {"type": "error", "code": "NO_AGENT", "message": "没有这个智能体"})
        return
    if a["owner"] != me_of(ident):
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM, "message": "这不是你的智能体"})
        return
    new_key = store.reset_agent_key(acid)
    for sock, other in list(hub.clients.items()):
        if other.get("client_id") == acid:
            hub.remove(sock)
            try:
                await sock.close(1011, "key rotated")
            except Exception:
                pass
    log("agent rotate key", acid)
    await hub.send(ws, {"type": "agent_key_rotated", "client_id": acid, "key2": new_key})


async def handle_agent_remove(ws, store, hub, ident, data, log):
    """本人删一个智能体位：清房间成员关系、踢下线；历史消息保留。"""
    if ident["kind"] != "human":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "只有本人能删自己的智能体"})
        return
    if ident.get("role") == "readonly":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "只读成员不能删智能体位"})
        return
    acid = (data.get("client_id") or "").strip().lower()
    a = store.agent(acid)
    if not a:
        await hub.send(ws, {"type": "error", "code": "NO_AGENT", "message": "没有这个智能体"})
        return
    if a["owner"] != me_of(ident):
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM, "message": "这不是你的智能体"})
        return
    store.remove_agent(acid)
    for sock, other in list(hub.clients.items()):
        if other.get("client_id") == acid:
            hub.remove(sock)
            try:
                await sock.close(1011, "agent removed")
            except Exception:
                pass
    log("agent remove", acid)
    await hub.send(ws, {"type": "agent_removed", "client_id": acid})
    await hub.presence(store)


async def handle_agent_set_mode(ws, store, hub, ident, data, log):
    """本人给自己的智能体切换接入方式。只改标签，不动权限与在线状态。"""
    if ident["kind"] != "human":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "只有本人能改自己的智能体"})
        return
    if ident.get("role") == "readonly":
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                            "message": "只读成员不能改智能体接入方式"})
        return
    acid = (data.get("client_id") or "").strip().lower()
    a = store.agent(acid)
    if not a:
        await hub.send(ws, {"type": "error", "code": "NO_AGENT", "message": "没有这个智能体"})
        return
    if a["owner"] != me_of(ident):
        await hub.send(ws, {"type": "error", "code": ERR_NO_PERM, "message": "这不是你的智能体"})
        return
    mode = (data.get("mode") or "").strip().lower()
    if mode not in MODES:
        await hub.send(ws, {"type": "error", "code": ERR_BAD_MODE,
                            "message": "接入方式只能是 online 或 inbox"})
        return
    a["mode"] = mode
    store.set_agent_mode(acid, mode)
    log("agent mode", acid, "->", mode)
    await hub.send(ws, {"type": "agent_mode", "client_id": acid, "mode": mode,
                        "mode_label": mode_label(mode),
                        "prompts": prompts_for(a)})
    await hub.presence(store)


async def handle_onboarded_skip(ws, store, hub, ident):
    if ident["kind"] == "human":
        store.mark_onboarded(ident["client_id"])
        await hub.send(ws, {"type": "onboarded"})
    else:
        await hub.send(ws, {"type": "onboarded"})


async def handler(ws, store, hub, log, settings=None, inst_getter=None,
                  inst_saver=None):
    """WS 消息分发主循环。

    inst_getter / inst_saver 由宿主注入：settings_get / settings_set 需要读写
    instance.json，WS 层自己够不着。没注入时这两帧回 ERR_UNSUPPORTED。
    """
    ident = None
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        first = json.loads(raw)
    except Exception:
        return
    if first.get("type") == TYPE_JOIN:
        from core.members import handle_join_ws
        await handle_join_ws(ws, store, hub, first, log, settings)
        return
    if first.get("type") != TYPE_AUTH:
        await hub.send(ws, {"type": "error", "code": ERR_AUTH_REQUIRED,
                            "message": "首帧必须是 auth"})
        return
    ident = await handle_auth(ws, store, hub, first)
    if not ident:
        return
    last_read = first.get("last_read")
    if not isinstance(last_read, dict):
        last_read = {}

    hub.add(ws, ident)
    log("+", ident["kind"], ident["client_id"], f"({len(hub.clients)} 在线)")
    await hub.send(ws, welcome_payload(store, ident, last_read))
    await hub.presence(store)

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            t = data.get("type")
            if t == TYPE_MESSAGE:
                await handle_message(ws, store, hub, ident, data)
            elif t == TYPE_HISTORY:
                room = (data.get("room") or LOBBY).strip()
                if not can_access(ident, room, store):
                    await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                                        "message": "你不在这个房间里"})
                    continue
                msgs = store.history(room, int(data.get("limit") or 60), data.get("since"),
                                     data.get("before"))
                await hub.send(ws, {"type": "history", "room": room,
                                    "messages": attach_quotes(store, msgs),
                                    "before": data.get("before") or None})
            elif t == TYPE_ROOMS:
                lr = data.get("last_read")
                await hub.send(ws, welcome_payload(
                    store, ident, lr if isinstance(lr, dict) else last_read))
            elif t == TYPE_PRESENCE:
                await hub.presence(store)
            elif t == TYPE_PROFILE:
                await handle_profile(ws, store, hub, ident, data, log)
            elif t == TYPE_AGENT_RENAME:
                await handle_agent_rename(ws, store, hub, ident, data, log)
            elif t == TYPE_AGENT_CREATE:
                await handle_agent_create(ws, store, hub, ident, data, log)
            elif t == TYPE_AGENT_REMOVE:
                await handle_agent_remove(ws, store, hub, ident, data, log)
            elif t == TYPE_AGENT_SET_MODE:
                await handle_agent_set_mode(ws, store, hub, ident, data, log)
            elif t == TYPE_AGENT_ROTATE_KEY:
                await handle_agent_rotate_key(ws, store, hub, ident, data, log)
            elif t == TYPE_MEMBER_LIST:
                await handle_member_list(ws, store, hub, ident, data)
            elif t == TYPE_MEMBER_REMOVE:
                await handle_member_remove(ws, store, hub, ident, data, log)
            elif t == TYPE_MEMBER_AGENT_LIMIT:
                await handle_member_agent_limit(ws, store, hub, ident, data, log)
            elif t == TYPE_MEMBER_RESET_KEY:
                await handle_member_reset_key(ws, store, hub, ident, data, log)
            elif t == TYPE_DEVICES_LIST:
                await handle_devices_list(ws, store, hub, ident, data, log)
            elif t == TYPE_DEVICE_REVOKE:
                await handle_device_revoke(ws, store, hub, ident, data, log)
            elif t == TYPE_SETTINGS_GET:
                await handle_settings_get(ws, store, hub, ident, data, log,
                                          inst_getter)
            elif t == TYPE_SETTINGS_SET:
                await handle_settings_set(ws, store, hub, ident, data, log,
                                          inst_getter, inst_saver)
            elif t == TYPE_AGENT_DM_OPEN:
                await handle_agent_dm_open(ws, store, hub, ident, data, log)
            elif t == TYPE_ONBOARDED:
                await handle_onboarded_skip(ws, store, hub, ident)
            elif t == "room_create":
                await handle_room_create(ws, store, hub, ident, data, settings, log)
            elif t == "room_rename":
                await handle_room_rename(ws, store, hub, ident, data, settings, log)
            elif t == "room_archive":
                await handle_room_archive(ws, store, hub, ident, data, settings, log)
            elif t == "room_leave":
                await handle_room_leave(ws, store, hub, ident, data, settings, log)
            elif t == "room_invite":
                await handle_room_invite(ws, store, hub, ident, data, settings, log)
            elif t == "instance_invite":
                await handle_instance_invite(ws, store, hub, ident, data, settings, log)
            elif t == TYPE_ROOM_MEMBERS:
                await handle_room_members(ws, store, hub, ident, data)
            elif t == TYPE_TASK_STREAM:
                await handle_task_stream(ws, store, hub, ident, data)
            elif t == TYPE_TYPING:
                await handle_typing(ws, store, hub, ident, data)
            elif t == TYPE_TASK_LIST:
                await handle_task_list(ws, store, hub, ident, data)
            elif t == TYPE_MESSAGE_ACK:
                await handle_message_ack(ws, store, hub, ident, data)
            elif t == TYPE_DELIVERY:
                await handle_delivery(ws, store, hub, ident, data)
            elif t == TYPE_PING:
                await hub.send(ws, {"type": "pong", "ts": now_iso()})
            else:
                await hub.send(ws, {"type": "error", "code": ERR_UNKNOWN,
                                    "message": "未知类型 %s" % t})
    except Exception as exc:
        log("conn error", ident["client_id"], exc)
    finally:
        hub.remove(ws)
        await hub.presence(store)
        log("-", ident["client_id"], f"({len(hub.clients)} 在线)")
