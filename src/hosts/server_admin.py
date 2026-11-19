#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公网宿主的认领与管理面。

分三块，都不依赖本机桌面宿主：
1. bootstrap：5 分钟一次性认领链接 + 6 位终端码，库里只存 SHA-256；
2. 设备凭证：认领成功换长期 secret，请求头 Authorization: Bearer <secret>；
3. 管理 API：/api/state、/api/admin/*，逐 IP 限流 + JSONL 审计 + 15 分钟回滚。

硬约束：不在这里碰 messages 结构；不把 token / 设备 secret 写进审计或日志；
危险设置只写配置并提示重启，绝不自动重启。
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from core.agent_modes import MODES, modes_payload
from core.member_admin import normalize_limit, seat_limit_of
from core.store import CST, MEMBER_LIMIT, jd, now_iso

BOOTSTRAP_TTL = 0                # 0＝认领链接永久有效（仍只能用一次）；>0 为秒数
CODE_ATTEMPTS = 5                # 连续错 5 次整条链接作废
MAX_DEVICES = 10
ADMIN_WINDOW = 60
ADMIN_MAX = 30                   # 每 IP 每分钟 30 次管理请求
CLAIM_WINDOW = 60
CLAIM_MAX = 10                   # 每 IP 每分钟 10 次认领尝试
BOOTSTRAP_MAX = 5                # 每 IP 每分钟 5 次 bootstrap 相关请求（Red Line #7）
DEVICE_MAX = 60                  # 每设备每分钟 60 次请求（Red Line #7）
ROLLBACK_TTL = 900               # 15 分钟内可一键回滚
BODY_LIMIT = 64 * 1024

TRUST_PROXY = False              # Red Line #1: 默认不信任 X-Forwarded-Proto
SERVER_MODE = False              # server 宿主设为 True；local 宿主保持 False

_code_lock = threading.Lock()
_code_mem = {}                   # token_hash -> code（明文只存进程内存，重启即失效）

_rate_lock = threading.Lock()
_rates = {}                      # (bucket, ip) -> [ts, ...]


def hash_secret(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def reset_rate_limits():
    """冒烟测试用：清空内存限流窗口。"""
    with _rate_lock:
        _rates.clear()


def rate_ok(bucket, ip, limit, window):
    key = (bucket, ip or "-")
    now = time.time()
    with _rate_lock:
        arr = [t for t in _rates.get(key, []) if now - t < window]
        if len(arr) >= limit:
            _rates[key] = arr
            return False
        arr.append(now)
        _rates[key] = arr
        return True


def is_loopback(ip):
    ip = (ip or "").strip()
    return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")


def real_ip(peer_ip, headers):
    """反代后 peer 是回环，真实来源看 X-Real-IP / X-Forwarded-For 第一跳。"""
    if not is_loopback(peer_ip):
        return peer_ip or "unknown"
    for name in ("X-Real-IP", "X-Forwarded-For"):
        raw = (headers.get(name) or "").strip()
        if raw:
            return raw.split(",")[0].strip() or peer_ip
    return peer_ip or "unknown"


def transport_ok(peer_ip, headers):
    """管理 API 只接受安全信道的请求。

    - local 宿主（SERVER_MODE=False）：本机回环直接放行（没有反代场景）。
    - server 宿主（SERVER_MODE=True）：回环也可能来自 nginx 转发，
      必须同时满足 X-Forwarded-Proto: https（或 --trust-proxy 开启 + HTTPS）。
    - 非回环：必须 --trust-proxy 且 X-Forwarded-Proto: https。
    """
    proto = (headers.get("X-Forwarded-Proto") or "").strip().lower()
    if SERVER_MODE:
        if is_loopback(peer_ip):
            return proto == "https"
        if not TRUST_PROXY:
            return False
        return proto == "https"
    if is_loopback(peer_ip):
        return True
    if not TRUST_PROXY:
        return False
    return proto == "https"


def request_base(headers, fallback=""):
    """邀请/认领链接的基地址。PUBCHAT_PUBLIC_URL 是部署时的权威口径，优先使用。"""
    env_public = (os.environ.get("PUBCHAT_PUBLIC_URL") or "").strip().rstrip("/")
    if env_public:
        return env_public
    host = (headers.get("X-Forwarded-Host") or headers.get("Host") or "").strip()
    proto = (headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    if host:
        return proto + "://" + host
    return (fallback or "").rstrip("/")


def audit(data_dir, device, ip, action, result, detail="", store=None):
    """一条一行 JSONL；凭证类内容一律不进审计。
    Red Line #5: 文件权限 600、只追加；同时写 DB 审计表。"""
    rec = {
        "ts": now_iso(),
        "device": str(device or "")[:40],
        "ip": str(ip or "")[:64],
        "action": str(action or "")[:80],
        "result": str(result or "")[:20],
        "detail": str(detail or "")[:300],
    }
    path = Path(data_dir) / ("admin-audit-%s.jsonl" % datetime.now(CST).strftime("%Y%m"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(jd(rec) + "\n")
        if os.name == "posix":
            try:
                os.chmod(str(path), 0o600)
            except OSError:
                pass
    except OSError:
        pass
    if store is not None:
        try:
            store.add_audit(device, ip, action, result, detail)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
def generate_bootstrap(store):
    """作废所有未用链接，生成新的一次性 token + 6 位码。
    建议1 修复：code 只存进程内存，DB 不存哈希（100 万候选秒破裸 SHA-256）。
    重启即失效，本来也是重启就重新生成。
    BOOTSTRAP_TTL=0 表示链接永久有效（仍为一次性，用过即废）。"""
    store.clear_bootstraps()
    token = secrets.token_urlsafe(32)
    code = "%06d" % secrets.randbelow(1000000)
    if BOOTSTRAP_TTL and BOOTSTRAP_TTL > 0:
        expires_iso = (datetime.now(CST) +
                       timedelta(seconds=BOOTSTRAP_TTL)).isoformat(timespec="seconds")
    else:
        # schema 里 expires_at 是 NOT NULL，用远期哨兵值表示「永久有效」
        # （必须带 +08:00：handle_claim 用 aware 时间比较）
        expires_iso = "9999-12-31T23:59:59+08:00"
    token_hash = hash_secret(token)
    store.add_bootstrap(token_hash, "", expires_iso)
    with _code_lock:
        _code_mem[token_hash] = code
    return token, code, expires_iso


def startup_bootstrap(store, log, base_url="", force=False):
    """服务器启动时调用。已有可用设备就不生成；force 用于 --new-claim 恢复。
    Red Line #7: 启动时清一次过期 bootstrap 行。"""
    store.clear_expired_bootstraps()
    if not force and store.active_device_count() > 0:
        return None
    token, code, expires = generate_bootstrap(store)
    base = (base_url or "").strip().rstrip("/")
    link = (base + "/claim#b=" + token) if base else "/claim#b=" + token
    if BOOTSTRAP_TTL and BOOTSTRAP_TTL > 0:
        log("管理认领链接（%s 内有效，仅一次）：%s"
            % (("5 分钟" if BOOTSTRAP_TTL == 300 else "%d 秒" % BOOTSTRAP_TTL), link))
    else:
        log("管理认领链接（永久有效，仅一次）：%s" % link)
    log("6 位码：%s" % code)
    if not link.lower().startswith("https://"):
        log("警告：本次链接未加密，仅限内网或已由反代终止 HTTPS 的场景。")
    if BOOTSTRAP_TTL and BOOTSTRAP_TTL > 0:
        log("链接到期、已使用，或码连续错 5 次都会作废；重启服务器会重新生成。")
    else:
        log("链接已使用或码连续错 5 次会作废；重启服务器会重新生成。")
    return link, code


def device_name_from_ua(ua):
    ua = ua or ""
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Android" in ua:
        return "Android 设备"
    if "Windows" in ua:
        return "Windows 设备"
    if "Macintosh" in ua:
        return "Mac"
    if "Linux" in ua:
        return "Linux 设备"
    return "新设备"


def handle_claim(store, data_dir, payload, ip, user_agent=""):
    """POST /api/claim 的实现。返回 (status, object)。
    Red Line #2: token 只从 POST body 接收（前端从 URL fragment 取后立刻清掉）。
    Red Line #3: 作废/过期/已用三态都留审计行。
    Red Line #7: bootstrap 层限流。
    建议2 修复：所有失败统一模糊提示 + attempts_left 数字，不泄露状态。"""
    if not rate_ok("claim", ip, CLAIM_MAX, CLAIM_WINDOW):
        audit(data_dir, "", ip, "claim", "429", "rate limited", store=store)
        return 429, {"error": "请求太频繁，请稍后再试"}
    if not rate_ok("bootstrap", ip, BOOTSTRAP_MAX, CLAIM_WINDOW):
        audit(data_dir, "", ip, "claim", "429", "bootstrap rate limited", store=store)
        return 429, {"error": "请求太频繁，请稍后再试"}

    token = str(payload.get("token") or "").strip()
    code = str(payload.get("code") or "").strip()
    name = str(payload.get("name") or "").strip()[:30]
    if not token:
        return 400, {"error": "认领链接不完整"}
    if not re.fullmatch(r"\d{6}", code):
        return 400, {"error": "请输入终端上显示的 6 位数字"}

    token_hash = hash_secret(token)
    row = store.find_bootstrap_by_hash(token_hash)
    generic_err = {"error": "链接无效或已失效，请在服务器上重新生成"}

    if not row or row.get("used"):
        audit(data_dir, "", ip, "claim", "403", "used or unknown", store=store)
        return 403, generic_err

    # 从进程内存取 code（建议1：不存 DB 哈希）
    with _code_lock:
        expected_code = _code_mem.get(token_hash)
    if not expected_code:
        # 重启后内存丢失，或已被清理
        audit(data_dir, "", ip, "claim", "403", "code not in memory", store=store)
        return 403, generic_err

    # expires_at 为 NULL＝永久有效（一次性已在 used 标记里保证）
    expires = None
    if row.get("expires_at"):
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except (ValueError, TypeError):
            expires = None
        # 防御：老库可能存不带时区的字符串，统一按 CST 处理
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=CST)
    if expires and datetime.now(CST) > expires:
        store.burn_bootstrap(row["id"])
        with _code_lock:
            _code_mem.pop(token_hash, None)
        audit(data_dir, "", ip, "claim", "403", "expired", store=store)
        return 403, generic_err

    if not hmac.compare_digest(expected_code, code):
        info = store.bump_bootstrap_attempts(row["id"])
        left = max(0, CODE_ATTEMPTS - int(info.get("attempts") or 0))
        if left <= 0:
            store.burn_bootstrap(row["id"])
            with _code_lock:
                _code_mem.pop(token_hash, None)
            audit(data_dir, "", ip, "claim", "403",
                  "code burned after 5 misses", store=store)
            return 403, generic_err
        audit(data_dir, "", ip, "claim", "403", "wrong code", store=store)
        return 403, {"error": "验证码不对", "attempts_left": left}

    if store.active_device_count() >= MAX_DEVICES:
        return 409, {"error": "设备已达上限（%d 台），请先在管理面板吊销旧设备"
                              % MAX_DEVICES}
    if not store.claim_bootstrap(row["id"]):
        with _code_lock:
            _code_mem.pop(token_hash, None)
        audit(data_dir, "", ip, "claim", "403", "race lost", store=store)
        return 403, generic_err

    # 认领成功，清理内存中的 code
    with _code_lock:
        _code_mem.pop(token_hash, None)

    secret = secrets.token_urlsafe(32)
    device_name = name or device_name_from_ua(user_agent)
    did = store.add_device(device_name, hash_secret(secret), created_by="bootstrap")
    audit(data_dir, "#%d %s" % (did, device_name), ip, "claim", "ok",
          "device created", store=store)
    return 200, {
        "ok": True,
        "device": {"id": did, "name": device_name},
        "secret": secret,
        "note": "设备凭证只显示这一次；浏览器会自动保存在本 origin，请勿截图或转发给他人。",
    }


# --------------------------------------------------------------------------- #
# 实例身份 / 回滚
# --------------------------------------------------------------------------- #
def load_instance(data_dir, name="PubChat"):
    p = Path(data_dir) / "instance.json"
    if p.is_file():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and obj.get("instance_id"):
                return obj
        except (OSError, ValueError):
            pass
    inst = {"instance_id": str(uuid.uuid4()), "name": name}
    save_instance(data_dir, inst)
    return inst


def save_instance(data_dir, inst):
    p = Path(data_dir) / "instance.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(jd(inst), encoding="utf-8")
    os.replace(tmp, p)


def _rollback_path(data_dir):
    return Path(data_dir) / "admin-rollback.json"


def snapshot_rollback(data_dir, inst, action):
    rec = {
        "ts": now_iso(),
        "expires_at": (datetime.now(CST) + timedelta(seconds=ROLLBACK_TTL))
                       .isoformat(timespec="seconds"),
        "action": action,
        "instance": {"name": inst.get("name", ""),
                     "require_approval": bool(inst.get("require_approval")),
                     "allow_room_create": bool(inst.get("allow_room_create", True))},
    }
    p = _rollback_path(data_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(jd(rec), encoding="utf-8")
    os.replace(tmp, p)
    return rec


def read_rollback(data_dir):
    """返回仍在 15 分钟内的快照；过期文件按"已转正"忽略。"""
    p = _rollback_path(data_dir)
    if not p.is_file():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        exp = datetime.fromisoformat(rec["expires_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if datetime.now(CST) > exp:
        return None
    return rec


def clear_rollback(data_dir):
    try:
        _rollback_path(data_dir).unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# 设备认证
# --------------------------------------------------------------------------- #
def authenticate(store, headers):
    raw = (headers.get("Authorization") or "").strip()
    if not raw.lower().startswith("bearer "):
        return None
    secret = raw[7:].strip()
    if not secret:
        return None
    dev = store.device_by_hash(hash_secret(secret))
    if dev:
        store.touch_device(dev["id"])
        return dev
    # 0.8.17：管理权跟账号走，不跟机器走（需求方：不要认领那套）。
    # 一级 Key 属于 role=owner 的账号时，直接当管理员；普通成员的 key1 不算。
    for u in store.users_with_role():
        if u.get("key1") and u["key1"] == secret and (u.get("role") or "") == "owner":
            return {"id": "owner-account", "label": "主人账号",
                    "client_id": u["client_id"], "kind": "account"}
    return None


# --------------------------------------------------------------------------- #
# 管理 API
# --------------------------------------------------------------------------- #
def users_state(store, hub, data_dir=None, device=None, ip=None):
    out = []
    for u in store.users_with_role():
        agents = store.agents_of(u["client_id"])
        out.append({
            "client_id": u["client_id"], "name": u["name"],
            "display_name": u.get("display_name") or u["name"],
            "is_admin": bool(u.get("is_admin")), "key1": u["key1"],
            "role": u.get("role") or "member",
            "source": u.get("source") or "local",
            "status": u.get("status") or "active",
            "created_at": u.get("created_at"),
            "online": u["client_id"] in hub.by_name,
            "agent_count": len(agents),
            "agent_limit": seat_limit_of(store, u["client_id"]),
            "agents": [{"client_id": a["client_id"], "name": a["name"],
                        "key2": a["key2"], "mode": a.get("mode") or "online"}
                       for a in agents],
        })
    if data_dir is not None:
        audit(data_dir, device or "", ip or "", "key_list_read", "ok",
              "read %d users' keys" % len(out), store=store)
    return out


def rooms_state(store):
    users = {u["client_id"]: u for u in store.users()}
    ids = ["own-" + cid for cid in users]
    for rid in store.room_ids():
        if rid in ids or rid == "lobby" or rid.startswith("dm-"):
            continue
        ids.append(rid)
    out = []
    for rid in ids:
        members = []
        for m in store.room_members_of(rid):
            u = users.get(m["user_id"])
            if u:
                members.append({"client_id": u["client_id"], "name": u["name"]})
        row = store.room(rid) if not rid.startswith("own-") else None
        if rid.startswith("own-"):
            owner_cid = rid[4:]
            owner = users.get(owner_cid)
            kind, title = "own", (owner["name"] if owner else owner_cid) + " 的个人房"
        elif row:
            owner_cid, kind = row["owner"], "custom"
            title = row["title"] or rid
        else:
            owner_cid, kind, title = None, "custom", "共享房间 · " + rid
        item = {"room_id": rid, "kind": kind, "title": title,
                "owner": owner_cid, "members": members}
        if row:
            item.update({"visibility": row.get("visibility") or "invite",
                         "archived": int(row.get("archived") or 0)})
        out.append(item)
    return out


def invites_state(store):
    now = datetime.now(CST)
    out = []
    for inv in store.list_invites():
        expired = False
        if inv.get("expires_at"):
            try:
                expired = datetime.fromisoformat(inv["expires_at"]) < now
            except ValueError:
                pass
        out.append({
            "id": inv["id"], "role": inv["role"],
            "expires_at": inv["expires_at"], "used": inv["used"],
            "status": "used" if inv["used"] else ("expired" if expired else "active"),
            "created_at": inv["created_at"],
        })
    return out


def public_meta(inst, version):
    return {"ok": True, "name": inst.get("name", "PubChat"),
            "fingerprint": str(inst.get("instance_id", ""))[:12],
            "version": version}


def _make_user(store, name, cid=None, is_admin=0, role="member", source="public"):
    name = (name or "").strip()[:20]
    if not name:
        raise ValueError("显示名不能为空")
    cid = (cid or "").strip().lower()
    if not cid:
        base = re.sub(r"[^a-z0-9]", "", name.lower()) or "user"
        cid = base[:24]
        i = 1
        while store.user(cid):
            i += 1
            cid = "%s%d" % (base[:20], i)
    from core.protocol import valid_client_id
    if not valid_client_id(cid):
        raise ValueError("登录名只能小写字母/数字/下划线，2-32 位")
    if store.user(cid):
        raise ValueError("登录名已被占用")
    import secrets as _secrets
    key1 = _secrets.token_urlsafe(12)
    store.add_user(cid, name, key1, is_admin, role=role, source=source)
    agents = []
    for i in range(1, 4):
        acid = "%s_a%d" % (cid, i)
        k2 = _secrets.token_urlsafe(12)
        store.add_agent(acid, "%s-%d" % (name, i), cid, k2)
        agents.append({"client_id": acid, "name": "%s-%d" % (name, i), "key2": k2})
    return {"client_id": cid, "name": name, "key1": key1, "role": role,
            "source": source, "agents": agents}


def _state(store, hub, data_dir, web_dir, inst, version, port, base_url, device,
           admin_port=None, ip=""):
    devs = []
    for d in store.list_devices():
        devs.append({"id": d["id"], "name": d["name"], "revoked": bool(d["revoked"]),
                     "created_at": d["created_at"], "last_seen": d["last_seen"],
                     "created_by": d.get("created_by") or "",
                     "current": bool(device and d["id"] == device.get("id"))})
    rb = read_rollback(data_dir)
    dev_tag = "#%s %s" % (device.get("id"), device.get("name") or "") if device else ""
    return {
        "ok": True, "version": version,
        "initialized": bool(store.users()),
        "users": users_state(store, hub, data_dir, dev_tag, ip),
        "agent_modes": modes_payload(),
        "port": port, "admin_port": admin_port or port,
        "addresses": {"local": (base_url or "") + "/" if base_url else "/",
                      "lan": [(base_url or "") + "/"] if base_url else []},
        "data_dir": str(data_dir), "web_dir": str(web_dir),
        "online": len(hub.clients),
        "tunnel": {"state": "off", "mode": "manual",
                   "url": base_url or None, "available": False,
                   "detail": "服务器部署的 HTTPS 由反向代理提供，本程序不管理隧道"},
        "instance": {
            "instance_id": inst.get("instance_id"),
            "name": inst.get("name", "PubChat"),
            "fingerprint": str(inst.get("instance_id", ""))[:12],
            "require_approval": bool(inst.get("require_approval")),
        },
        "invites": invites_state(store),
        "rooms": rooms_state(store),
        "member_limit": MEMBER_LIMIT,
        "member_count": store.user_count(),
        "mode": "server", "serving": True, "remote_url": "",
        "devices": devs, "device_max": MAX_DEVICES,
        "rollback": ({"available": True, "action": rb["action"],
                      "expires_at": rb["expires_at"]} if rb else {"available": False}),
    }


def handle_admin(store, hub, data_dir, web_dir, get_instance, save_instance_fn,
                 method, path, payload, headers, peer_ip, version, port,
                 admin_port=None):
    """
    处理 /api/state 与 /api/admin/*。返回 (status, obj)。
    自己完成：传输门 -> 限流 -> Bearer 设备认证 -> 审计 -> 分发。
    Red Line #4: 一律 Bearer，不给 cookie 会话。
    Red Line #7: 三层限流（bootstrap 在 handle_claim，这里做 IP + 设备）。
    """
    data_dir = Path(data_dir)
    ip = real_ip(peer_ip, headers)
    base = request_base(headers)
    action = path + ((" " + str(payload.get("action"))) if payload.get("action") else "")

    if not transport_ok(peer_ip, headers):
        audit(data_dir, "", ip, action, "403", "plain http from non-loopback",
              store=store)
        return 403, {"error": "管理接口只接受 HTTPS 反代或本机回环访问"}

    if not rate_ok("admin", ip, ADMIN_MAX, ADMIN_WINDOW):
        audit(data_dir, "", ip, action, "429", "rate limited", store=store)
        return 429, {"error": "操作太频繁（每 IP 每分钟 %d 次），请稍后再试" % ADMIN_MAX}

    device = authenticate(store, headers)
    if not device:
        audit(data_dir, "", ip, action, "401", "no valid device credential",
              store=store)
        return 401, {"error": "这台设备还没有认领管理权，或凭证已失效"}

    if not rate_ok("device_%s" % device.get("id"), ip, DEVICE_MAX, ADMIN_WINDOW):
        audit(data_dir, "#%s" % device.get("id"), ip, action, "429",
              "device rate limited", store=store)
        return 429, {"error": "操作太频繁，请稍后再试"}

    dev_tag = "#%s %s" % (device.get("id"), device.get("name") or "")
    inst = get_instance()

    def done(status, obj, detail=""):
        audit(data_dir, dev_tag, ip, action, "ok" if status < 400 else str(status),
              detail, store=store)
        return status, obj

    try:
        if method == "GET":
            if path == "/api/state":
                return done(200, _state(store, hub, data_dir, web_dir, inst,
                                        version, port, base, device, admin_port, ip=ip))
            if path == "/api/admin/devices":
                return done(200, _state(store, hub, data_dir, web_dir, inst,
                                        version, port, base, device, admin_port, ip=ip))
            if path == "/api/admin/rooms":
                return done(200, {"ok": True, "rooms": rooms_state(store)})
            if path == "/api/admin/rollback":
                rb = read_rollback(data_dir)
                return done(200, {"ok": True, "rollback": rb})
            return 404, {"error": "not found"}

        if path == "/api/admin/user":
            first = not store.users()
            u = _make_user(store, payload.get("name"), payload.get("client_id"),
                           is_admin=1 if (payload.get("is_admin") or first) else 0,
                           role="owner" if first else "member", source="local")
            return done(200, {"ok": True, "user": u,
                              "first_user": first}, "cid=%s" % u["client_id"])

        if path == "/api/admin/invite":
            act = (payload.get("action") or "create").strip()
            if act == "create":
                role = (payload.get("role") or "member").strip()
                if role not in ("member", "readonly"):
                    return done(400, {"error": "角色必须是 member 或 readonly"})
                raw_hours = payload.get("hours")
                try:
                    hours = 24 if raw_hours in (None, "") else int(raw_hours)
                except (TypeError, ValueError):
                    return done(400, {"error": "有效期只能是整数小时，0＝永久"})
                if hours != 0 and (hours < 1 or hours > 720):
                    return done(400, {"error": "有效期需在 1-720 小时之间（0＝永久）"})
                raw_count = payload.get("count")
                count = 1 if raw_count in (None, "") else int(raw_count)
                if count < 1 or count > 20:
                    return done(400, {"error": "单批数量需在 1-20 之间"})
                expires = None if hours == 0 else (
                    datetime.now(CST) + timedelta(hours=hours)).isoformat(
                    timespec="seconds")
                items = []
                for _ in range(count):
                    token = secrets.token_urlsafe(24)
                    inv_id = store.add_invite(hash_secret(token), role, expires, "admin")
                    items.append({"id": inv_id, "token": token, "role": role,
                                  "expires_at": expires,
                                  "url": (base or "") + "/join.html#t=" + token})
                return done(200, {"ok": True, "invites": items, "invite": items[0],
                                  "has_public": bool(base), "note": ""},
                            "count=%d role=%s" % (count, role))
            if act == "delete":
                inv_id = int(payload.get("id") or 0)
                if inv_id:
                    store.remove_invite(inv_id)
                return done(200, {"ok": True}, "id=%d" % inv_id)
            return done(400, {"error": "未知操作"})

        if path == "/api/admin/rooms":
            return done(200, {"ok": True, "rooms": rooms_state(store)})

        if path in ("/api/admin/room/rename", "/api/admin/room/archive"):
            room = (payload.get("room") or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", room):
                return done(400, {"error": "房间名不合法"})
            row = store.room(room)
            if not row:
                return done(404, {"error": "房间不存在"})
            if path.endswith("/rename"):
                title = (payload.get("title") or "").strip()[:20]
                if len(title) < 2:
                    return done(400, {"error": "房间名需 2–20 个字"})
                store.rename_room(room, title)
                return done(200, {"ok": True, "rooms": rooms_state(store)},
                            "rename %s" % room)
            store.archive_room(room)
            return done(200, {"ok": True, "rooms": rooms_state(store)},
                        "archive %s" % room)

        if path == "/api/admin/room/members":
            act = (payload.get("action") or "list").strip()
            room = (payload.get("room") or "").strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", room):
                return done(400, {"error": "房间名不合法"})
            known = {r["room_id"] for r in rooms_state(store)}
            if room not in known:
                return done(404, {"error": "房间不存在或不需要单独配置"})
            if act == "add":
                raw_ids = payload.get("client_ids")
                if raw_ids is None:
                    raw_ids = [payload.get("client_id")]
                if not isinstance(raw_ids, list):
                    return done(400, {"error": "client_ids 必须是数组"})
                added = []
                for cid in raw_ids:
                    cid = (cid or "").strip().lower()
                    u = store.user(cid) if cid else None
                    if not u or (u.get("status") or "active") != "active":
                        return done(404, {"error": "成员不存在：%s" % cid})
                    store.add_room_member(room, cid)
                    added.append(cid)
                return done(200, {"ok": True, "room": room, "added": added,
                                  "rooms": rooms_state(store)}, "add=%s" % ",".join(added))
            if act == "remove":
                cid = (payload.get("client_id") or "").strip().lower()
                if not cid:
                    return done(400, {"error": "缺少 client_id"})
                store.remove_room_member(room, cid)
                return done(200, {"ok": True, "room": room,
                                  "rooms": rooms_state(store)}, "remove=%s" % cid)
            if act == "list":
                info = next(r for r in rooms_state(store) if r["room_id"] == room)
                return done(200, {"ok": True, "room": info})
            return done(400, {"error": "未知操作"})

        if path in ("/api/admin/member/approve", "/api/admin/member/reject"):
            cid = (payload.get("client_id") or "").strip().lower()
            u = store.user(cid)
            if not u:
                return done(404, {"error": "找不到该成员"})
            if path.endswith("approve"):
                store.set_user_status(cid, "active")
                return done(200, {"ok": True, "status": "active"}, "cid=%s" % cid)
            if (u.get("role") or "") == "owner":
                return done(403, {"error": "不能移除服务器主人（房主）"})
            store.remove_user(cid)
            return done(200, {"ok": True, "status": "rejected"}, "cid=%s" % cid)

        if path == "/api/admin/member/role":
            cid = (payload.get("client_id") or "").strip()
            role = (payload.get("role") or "").strip()
            if role not in ("owner", "member", "readonly"):
                return done(400, {"error": "角色必须是 owner/member/readonly"})
            u = store.user(cid)
            if not u:
                return done(404, {"error": "找不到该成员"})
            if (u.get("role") or "") == "owner" and role != "owner":
                # 普通成员和房主的区别：房主不能被降级（否则没人管得住服务器）
                return done(403, {"error": "房主（owner）不能被降级或移除"})
            store.set_user_role(cid, role)
            for ws, ident in list(hub.clients.items()):
                if ident.get("client_id") == cid:
                    ident["role"] = role
            return done(200, {"ok": True}, "cid=%s role=%s" % (cid, role))

        if path == "/api/admin/member/limit":
            cid = (payload.get("client_id") or "").strip().lower()
            if not store.user(cid):
                return done(404, {"error": "找不到该成员"})
            value, bad = normalize_limit(payload.get("limit"))
            if bad:
                return done(400, {"error": bad})
            store.set_user_agent_limit(cid, value)
            return done(200, {"ok": True, "client_id": cid, "agent_limit": value},
                        "cid=%s agent_limit=%d" % (cid, value))

        if path == "/api/admin/member/remove":
            cid = (payload.get("client_id") or "").strip()
            u = store.user(cid)
            if not u:
                return done(404, {"error": "找不到该成员"})
            if (u.get("role") or "") == "owner":
                # 普通成员不能把房主踢出去；管理口也不允许误删唯一 owner
                return done(403, {"error": "不能移除服务器主人（房主）"})
            to_kick = []
            for ws, ident in list(hub.clients.items()):
                if ident.get("client_id") == cid or ident.get("owner") == cid:
                    to_kick.append(ws)
            for ws in to_kick:
                try:
                    hub.remove(ws)
                    if hasattr(ws, "transport"):
                        ws.transport.close()
                except Exception:
                    pass
            store.remove_user(cid)
            return done(200, {"ok": True}, "cid=%s" % cid)

        if path == "/api/admin/member/reset-key":
            cid = (payload.get("client_id") or "").strip()
            if not store.user(cid):
                return done(404, {"error": "找不到该成员"})
            new_key = store.reset_user_key(cid)
            return done(200, {"ok": True, "key1": new_key}, "cid=%s" % cid)

        if path == "/api/admin/agent/reset-key":
            acid = (payload.get("client_id") or "").strip()
            if not store.agent(acid):
                return done(404, {"error": "找不到该智能体"})
            new_key = store.reset_agent_key(acid)
            return done(200, {"ok": True, "key2": new_key}, "cid=%s" % acid)

        if path == "/api/admin/agent/mode":
            acid = (payload.get("client_id") or "").strip()
            mode = (payload.get("mode") or "").strip().lower()
            if mode not in MODES:
                return done(400, {"error": "接入方式只能是 online/inbox"})
            if not store.agent(acid):
                return done(404, {"error": "找不到该智能体"})
            store.set_agent_mode(acid, mode)
            return done(200, {"ok": True, "client_id": acid, "mode": mode},
                        "cid=%s mode=%s" % (acid, mode))

        if path == "/api/admin/instance":
            before = {"name": inst.get("name", ""),
                      "require_approval": bool(inst.get("require_approval")),
                      "allow_room_create": bool(inst.get("allow_room_create", True))}
            changed = False
            new_name = (payload.get("name") or "").strip()
            if new_name:
                inst["name"] = new_name[:30]
                changed = True
            if "require_approval" in payload:
                inst["require_approval"] = bool(payload.get("require_approval"))
                changed = True
            if "allow_room_create" in payload:
                inst["allow_room_create"] = bool(payload.get("allow_room_create"))
                changed = True
            if changed:
                snapshot_rollback(data_dir, before, "instance")
                save_instance_fn(inst)
            return done(200, {"ok": True, "instance": inst,
                              "rollback": read_rollback(data_dir)},
                        "name=%s approval=%s rooms=%s" % (
                            inst.get("name"),
                            bool(inst.get("require_approval")),
                            bool(inst.get("allow_room_create", True))))

        if path == "/api/admin/devices":
            act = (payload.get("action") or "list").strip()
            if act == "list":
                return done(200, _state(store, hub, data_dir, web_dir, inst,
                                        version, port, base, device, admin_port, ip=ip))
            if act == "invite":
                token, code, expires = generate_bootstrap(store)
                link = (base or "") + "/claim#b=" + token
                return done(200, {"ok": True, "link": link, "code": code,
                                  "expires_at": expires, "ttl": BOOTSTRAP_TTL,
                                  "note": "5 分钟内有效、仅一次；给要授权的设备打开"},
                            "new bootstrap")
            if act == "revoke":
                did = int(payload.get("id") or 0)
                if not did:
                    return done(400, {"error": "缺少设备 id"})
                if not store.revoke_device(did):
                    return done(404, {"error": "设备不存在或已吊销"})
                return done(200, {"ok": True, "revoked": did,
                                  "self": bool(did == device.get("id"))}, "device=#%s" % did)
            return done(400, {"error": "未知操作"})

        if path == "/api/admin/rollback":
            rb = read_rollback(data_dir)
            if not rb:
                return done(409, {"error": "没有可回滚的变更（超过 15 分钟自动转正）"})
            snapshot = rb.get("instance") or {}
            if "name" in snapshot:
                inst["name"] = str(snapshot.get("name") or "")[:30]
            if "require_approval" in snapshot:
                inst["require_approval"] = bool(snapshot.get("require_approval"))
            if "allow_room_create" in snapshot:
                inst["allow_room_create"] = bool(snapshot.get("allow_room_create"))
            save_instance_fn(inst)
            clear_rollback(data_dir)
            return done(200, {"ok": True, "instance": inst}, "rollback %s" % rb.get("action"))

        return 404, {"error": "not found"}
    except ValueError as exc:
        return done(400, {"error": str(exc)})
    except Exception as exc:
        return done(500, {"error": str(exc)})
