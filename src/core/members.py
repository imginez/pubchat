"""
PubChat 成员管理。

统一的加入逻辑（邀请码校验、建号、建智能体），供 WS 和 HTTP 两条路径调用。
"""
import secrets
from datetime import datetime

from core.protocol import (
    valid_client_id,
    ERR_EMPTY_TOKEN, ERR_EMPTY_NAME, ERR_BAD_CID, ERR_CID_TAKEN,
    ERR_FULL, ERR_BAD_INVITE, ERR_EXPIRED, ERR_USED,
)
from core.store import now_iso, _hash_token, CST, MEMBER_LIMIT

# 建号时预置的智能体位数量。只是默认值，不是上限——不够用可以一直加。
DEFAULT_AGENT_SLOTS = 3


def add_agent_slot(store, owner, name=None):
    """给某成员新开一个智能体位，编号取第一个空位，数量不限。

    返回 {"client_id", "name", "key2"}。
    """
    used = {a["client_id"] for a in store.agents_of(owner)}
    seq = 1
    while "%s_a%d" % (owner, seq) in used:
        seq += 1
    acid = "%s_a%d" % (owner, seq)
    owner_row = store.user(owner) or {}
    label = (name or "").strip()[:20]
    if not label:
        label = "%s-%d" % (owner_row.get("name") or owner, seq)
    key2 = secrets.token_urlsafe(12)
    store.add_agent(acid, label[:20], owner, key2)
    return {"client_id": acid, "name": label[:20], "key2": key2}


def create_user_with_agents(store, cid, name, role="member", source="public",
                            status="active"):
    """创建用户及其默认智能体位（3 个，之后可自行加，不限个数）。返回 (key1, agents_list)。"""
    key1 = secrets.token_urlsafe(12)
    store.add_user(cid, name, key1, is_admin=0, role=role, source=source,
                   status=status)
    agents = [add_agent_slot(store, cid) for _ in range(DEFAULT_AGENT_SLOTS)]
    return key1, [{k: a[k] for k in ("client_id", "name")} for a in agents]



def validate_join_request(store, token, name, cid, ip=None, check_rate_limit=None):
    """校验加入请求。返回 (invite, error_code, error_msg)。
    error_code 为 None 表示校验通过。
    """
    if not token:
        return None, ERR_EMPTY_TOKEN, "缺少邀请码"
    if not name:
        return None, ERR_EMPTY_NAME, "请填写显示名"
    if not cid:
        return None, "EMPTY_CID", "请填写登录名"
    if not valid_client_id(cid):
        return None, ERR_BAD_CID, "登录名只能小写字母/数字/下划线，2-32 位"
    if store.user(cid):
        return None, ERR_CID_TAKEN, "登录名已被占用"
    if MEMBER_LIMIT and store.user_count() >= MEMBER_LIMIT:
        return None, ERR_FULL, "成员已达上限（%d）" % MEMBER_LIMIT

    if ip and check_rate_limit and not check_rate_limit(ip):
        return None, "RATE_LIMITED", "加入尝试过于频繁，请稍后再试"

    token_hash = _hash_token(token)
    invite = store.find_invite_by_hash(token_hash)
    if not invite:
        return None, ERR_BAD_INVITE, "邀请码无效"

    now_dt = datetime.now(CST)
    if invite.get("expires_at"):
        exp = datetime.fromisoformat(invite["expires_at"])
        if now_dt > exp:
            return None, ERR_EXPIRED, "邀请码已过期"

    if invite["used"] >= 1:
        return None, ERR_USED, "邀请码已使用"

    return invite, None, None


async def handle_join_ws(ws, store, hub, data, log, settings=None):
    """通过 WebSocket 处理加入请求。首帧 type=join，验证邀请码后建号，返回凭据。
    实例开启 require_approval 时先建待审账号，等房主批准后才能登录。
    """
    token = (data.get("token") or "").strip()
    name = (data.get("name") or "").strip()[:20]
    cid = (data.get("client_id") or "").strip().lower()

    invite, err_code, err_msg = validate_join_request(store, token, name, cid)
    if err_code:
        await hub.send(ws, {"type": "error", "code": err_code, "message": err_msg})
        return

    role = invite.get("role") or "member"
    if not store.claim_invite(invite["id"]):
        await hub.send(ws, {"type": "error", "code": ERR_USED, "message": "邀请码已使用"})
        return
    require_approval = bool((settings or {}).get("require_approval"))
    status = "pending" if require_approval else "active"
    key1, agents = create_user_with_agents(store, cid, name, role=role,
                                           source="public", status=status)
    room_id = (invite.get("room_id") or "").strip()
    if room_id:
        store.add_room_member(room_id, cid)

    log("join", cid, "via invite #%d" % invite["id"],
        "role=%s status=%s room=%s" % (role, status, room_id or "-"))
    if require_approval:
        await hub.send(ws, {"type": "pending", "ok": True, "status": "pending",
                            "client_id": cid, "name": name, "key1": key1,
                            "message": "已提交，等待房主批准"})
        return
    await hub.send(ws, {"type": "joined", "ok": True,
                        "client_id": cid, "name": name, "key1": key1,
                        "role": role, "agents": agents})
