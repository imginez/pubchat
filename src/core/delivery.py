"""
读回执（0.8 批4）：谁真的收到了这条消息。

协议：
- C→S message_ack  {room, ids:[...]}        客户端宣告「这几条我处理完了」
- S→C message_acked {room, id, by, acked:[...], ts}   房内广播（含 ack 的人自己）
- C→S delivery     {room, id}               一条消息的送达查询
- S→C delivery     {room, id, from, acked:[...], members:[...], ts}

为什么只在内存里攒、不落库：读回执是「这一刻谁在线并确认过」，不是消息本体。
重启即清零，客户端重连后重新 ack 就行；落库反而会把已读状态做成第二份真相，
和现有的 last_read（客户端每次自己上报、服务端不持久化）打架。

关键口径：**作者自己的 ack 不算送达**。pubchat_done 要的是「别人收到了」，
所以 delivery 把 from 一起回给调用方，由调用方把作者从 acked 里剔掉再判 >0。
"""
from core.protocol import (LOBBY, TYPE_MESSAGE_ACKED, TYPE_DELIVERY,
                           ERR_NO_PERM, ERR_NOT_FOUND)
from core.rooms import can_access
from core.store import now_iso
from core.tasks_ws import room_participants

MAX_ACK_IDS = 50          # 一帧最多带多少条，防止客户端拿它当批量刷库的工具
ACK_ROOM_KEEP = 20000     # 内存里最多留多少条消息的 ack，超了按插入顺序淘汰


class AckLedger:
    """message_id -> {client_id}。单进程内存态，够用且故意不做持久化。"""

    def __init__(self, keep=ACK_ROOM_KEEP):
        self._d = {}
        self.keep = keep

    def add(self, mid, cid):
        """返回 True 表示这是一次新确认（重复 ack 不再广播，免得刷屏）。"""
        s = self._d.setdefault(int(mid), set())
        if cid in s:
            return False
        s.add(cid)
        if len(self._d) > self.keep:
            for stale in list(self._d)[:len(self._d) - self.keep]:
                self._d.pop(stale, None)
        return True

    def acked(self, mid):
        return sorted(self._d.get(int(mid), ()))


async def handle_message_ack(ws, store, hub, ident, data):
    room = (data.get("room") or LOBBY).strip()
    if not can_access(ident, room, store):
        return await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                                   "message": "你不在这个房间里"})
    raw = data.get("ids")
    if raw is None and data.get("id") is not None:
        raw = [data.get("id")]
    if not isinstance(raw, (list, tuple)):
        return await hub.send(ws, {"type": "error", "code": ERR_NOT_FOUND,
                                   "message": "ids 要是数组"})
    me = ident["client_id"]
    for mid in list(raw)[:MAX_ACK_IDS]:
        msg = store.message(mid)
        # 只认本房的 id：跨房 ack 会把别的房间的送达名单污染掉。
        if not msg or msg["room"] != room:
            continue
        if not hub.acks.add(msg["id"], me):
            continue
        await hub.broadcast(ident, {"type": TYPE_MESSAGE_ACKED, "room": room,
                                    "id": msg["id"], "by": me,
                                    "acked": hub.acks.acked(msg["id"]),
                                    "ts": now_iso()})


async def handle_delivery(ws, store, hub, ident, data):
    room = (data.get("room") or LOBBY).strip()
    if not can_access(ident, room, store):
        return await hub.send(ws, {"type": "error", "code": ERR_NO_PERM,
                                   "message": "你不在这个房间里"})
    msg = store.message(data.get("id"))
    if not msg or msg["room"] != room:
        return await hub.send(ws, {"type": "error", "code": ERR_NOT_FOUND,
                                   "message": "这条消息不存在，或不在这个房间里"})
    members = room_participants(store, ident, room, hub)
    await hub.send(ws, {"type": TYPE_DELIVERY, "room": room, "id": msg["id"],
                        "from": msg["from"], "acked": hub.acks.acked(msg["id"]),
                        "members": [m["client_id"] for m in members],
                        "ts": now_iso()})
