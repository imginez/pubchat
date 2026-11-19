"""
任务流（二期）：智能体把自己的进度以流式片段贴到房间里。

协议（见 docs/protocol.md「任务流」）：
- C→S task_stream {room, task_id?, title?, text?, status?, agent?}
    没有 task_id 就新建一个任务；text 是这次新增的流式片段（追加语义）。
    status: running / done / error，done|error 之后任务不再算「进行中」。
- C→S task_list {room?}  → S→C task_list {tasks:[{...events}]}
- S→C task_state {room, task_id, task:{meta}, event:{...}|null}  按房广播

约束：能进这个房才能发；只读成员不能发；片段最多 2000 字，
每个任务留最近 300 段、每个房间留最近 50 个任务（旧的自动作废）。
"""
import secrets

from core.protocol import (LOBBY, TASK_RUNNING, TASK_DONE, TASK_ERROR,
                           TASK_STATUSES, ERR_NO_PERM, ERR_READ_ONLY,
                           ERR_UNKNOWN)
from core.rooms import can_access, rooms_for, me_of
from core.store import now_iso

MAX_TASK_TEXT = 2000
MAX_TITLE = 60


def room_participants(store, ident, room, hub=None):
    """房间里该被 @ 到的人：人类 + 他们的智能体。带在线状态。"""
    online = set(hub.by_name.keys()) if hub is not None else set()
    users = {u["client_id"]: u for u in store.users()
             if (u.get("status") or "active") == "active"}
    agents = store.all_agents()
    ids = []

    def add_human(cid):
        if cid and cid in users and cid not in ids:
            ids.append(cid)

    def add_agents_of(cid):
        for a in agents:
            if a["owner"] == cid and a["client_id"] not in ids:
                ids.append(a["client_id"])

    if room == LOBBY:
        order = [c for c, u in users.items() if (u.get("role") or "member") == "owner"]
        order += [c for c in users if c not in order]
        for cid in order:
            add_human(cid)
            add_agents_of(cid)
    elif room.startswith("own-"):
        owner = room[4:]
        add_human(owner)
        add_agents_of(owner)
    elif room.startswith("dm-"):
        try:
            a, b = room[3:].split("-", 1)
        except ValueError:
            a = b = ""
        for cid in (a, b):
            add_human(cid)
            add_agents_of(cid)
    elif room.startswith("adm-"):
        # 智能体私聊房：只有两个智能体本人 + 各自的主人
        from core.agent_dm import split_slug
        for acid in (split_slug(room) or ()):
            if acid not in ids:
                ids.append(acid)
            add_human((store.agent(acid) or {}).get("owner"))
    else:
        for r in store.room_members_of(room):
            add_human(r["user_id"])
            add_agents_of(r["user_id"])
        # 实例主人能进任何房间，也算能被 @ 到的人
        for cid, u in users.items():
            if (u.get("role") or "member") == "owner":
                add_human(cid)

    out = []
    for cid in ids:
        if cid in users:
            u = users[cid]
            out.append({"client_id": cid, "name": u["name"], "kind": "human",
                        "role": u.get("role") or "member", "owner": None,
                        "online": cid in online})
        else:
            a = next((x for x in agents if x["client_id"] == cid), None)
            if not a:
                continue
            out.append({"client_id": cid, "name": a["name"], "kind": "agent",
                        "role": "agent", "owner": a["owner"],
                        "online": cid in online})
    return out


def _meta(task):
    """广播用的任务元信息（不带 events，流式时每段都要发，越小越好）。"""
    return {"id": task["id"], "room": task["room"], "title": task["title"],
            "owner": task["owner"], "owner_name": task["owner_name"],
            "status": task["status"], "created_at": task["created_at"],
            "updated_at": task["updated_at"]}


def _event_view(row):
    return {"id": row["id"], "agent": row["agent"], "agent_name": row["agent_name"],
            "kind": row["kind"], "text": row["text"], "ts": row["ts"]}


async def handle_task_stream(ws, store, hub, ident, data):
    """收到一段流式进度：落库 + 按房广播（不走 ack，避免和消息 ack 撞）。"""
    room = (data.get("room") or LOBBY).strip()
    if not can_access(ident, room, store):
        return await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                                   "message": "你不在这个房间里"})
    if ident.get("role") == "readonly":
        return await hub.send(ws, {"type": "error", "code": ERR_READ_ONLY,
                                   "message": "你是只读成员，不能发任务进度"})
    status = (data.get("status") or TASK_RUNNING).strip()
    if status not in TASK_STATUSES:
        status = TASK_RUNNING
    tid = (data.get("task_id") or "").strip()[:40]
    title = (data.get("title") or "").strip()[:MAX_TITLE]
    old = store.task(tid) if tid else None
    if tid and old and old["room"] != room:
        return await hub.send(ws, {"type": "error", "code": ERR_UNKNOWN,
                                   "message": "任务号不属于这个房间"})
    if not tid:
        tid = "t-" + secrets.token_hex(6)
        if not title:
            title = "任务 " + tid[-4:]
        status = TASK_RUNNING
    me = me_of(ident)
    task = store.upsert_task(tid, room, title, me, ident.get("name") or me, status)
    text = data.get("text")
    if text is None:
        text = data.get("delta")
    text = str(text or "")[:MAX_TASK_TEXT]
    event = None
    if text.strip():
        kind = str(data.get("kind") or "delta")[:12]
        store.add_task_event(tid, ident["client_id"],
                             ident.get("name") or ident["client_id"], kind, text)
        event = {"agent": ident["client_id"],
                 "agent_name": ident.get("name") or ident["client_id"],
                 "kind": kind, "text": text}
    await hub.broadcast(ident, {"type": "task_state", "room": room,
                                "task_id": tid, "task": _meta(task),
                                "event": event, "ts": now_iso()})


async def handle_task_list(ws, store, hub, ident, data):
    """一次拿全：进行中的 + 最近的已结束（带最近 60 段流）。"""
    room = (data.get("room") or "").strip()
    if room:
        if not can_access(ident, room, store):
            return await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                                       "message": "你不在这个房间里"})
        rooms = [room]
    else:
        rooms = [r["slug"] for r in rooms_for(store, ident, store.users())
                 if can_access(ident, r["slug"], store)]
    tasks = store.tasks_for_rooms(rooms, limit=20, events_limit=60)
    await hub.send(ws, {"type": "task_list", "tasks": tasks,
                        "room": room or None, "ts": now_iso()})


async def handle_room_members(ws, store, hub, ident, data):
    """@ 补全用：这个房里能被 @ 到的人。"""
    room = (data.get("room") or LOBBY).strip()
    if not can_access(ident, room, store):
        return await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                                   "message": "你不在这个房间里"})
    await hub.send(ws, {"type": "room_members", "room": room,
                        "members": room_participants(store, ident, room, hub),
                        "ts": now_iso()})
