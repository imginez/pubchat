#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PubChat —— 服务端宿主（公网多实例模式）

本文件是 deprecated 薄壳，实际逻辑已迁移到 core/ 和 hosts/server.py。
保留此文件以兼容旧启动方式。
"""
import asyncio
import json
import os
import uuid
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

try:
    from websockets.asyncio.server import serve as ws_serve
except Exception:  # pragma: no cover
    from websockets.server import serve as ws_serve

# Core imports - re-exported for backward compatibility
from core.store import Store, now_iso, jd, MEMBER_LIMIT, RATE_LIMIT_WINDOW, RATE_LIMIT_MAX, CST, _hash_token
from core.protocol import LOBBY, valid_client_id
from core.rooms import can_access, rooms_for, me_of, pair, dm_slug, own_slug
from core.auth import handle_auth, welcome_payload
from core.ws import Hub, handler as ws_handler, parse_mentions, handle_message
from core.members import handle_join_ws, create_user_with_agents, validate_join_request

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
WEB = ROOT / "web"

CTYPES = {
    "html": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "application/javascript; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "svg": "image/svg+xml",
    "png": "image/png",
    "ico": "image/x-icon",
    "woff2": "font/woff2",
}

_rate_attempts = {}
INSTANCE = None
hub = Hub()
STORE = None


def set_runtime(data_dir=None, web_dir=None, root_dir=None):
    global ROOT, DATA, WEB
    if root_dir:
        ROOT = Path(root_dir).resolve()
    if data_dir:
        DATA = Path(data_dir).resolve()
    if web_dir:
        WEB = Path(web_dir).resolve()
    DATA.mkdir(parents=True, exist_ok=True)


def web_asset(path):
    rel = (path or "/").lstrip("/")
    if rel in ("", "index.html"):
        rel = "index.html"
    base = WEB.resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def static_response(path):
    target = web_asset(path)
    if target is None:
        return 403, {"Content-Type": CTYPES["json"]}, b'{"error":"forbidden"}'
    if not target.is_file():
        return 404, {"Content-Type": CTYPES["json"]}, b'{"error":"not found"}'
    body = target.read_bytes()
    ctype = CTYPES.get(target.suffix.lstrip(".").lower(), "application/octet-stream")
    return 200, {"Content-Type": ctype, "Cache-Control": "no-store"}, body


def log(*a):
    print("[pubchat]", *a, flush=True)


def load_instance():
    global INSTANCE
    p = DATA / "instance.json"
    if p.is_file():
        try:
            INSTANCE = json.loads(p.read_text(encoding="utf-8"))
            return INSTANCE
        except Exception:
            pass
    INSTANCE = {
        "instance_id": str(uuid.uuid4()),
        "name": "PubChat",
    }
    p.write_text(jd(INSTANCE), encoding="utf-8")
    return INSTANCE


def get_instance():
    if INSTANCE is None:
        load_instance()
    return INSTANCE


def save_instance(obj):
    """把实例设置写回 instance.json（原子替换）。

    0.8.24：手机端设置页（settings_set 帧）要能改服务器名称/加入审批/允许建房，
    WS 层拿不到 saver，由宿主从这里注入 core.ws.handler。
    """
    global INSTANCE
    DATA.mkdir(parents=True, exist_ok=True)
    p = DATA / "instance.json"
    INSTANCE = dict(obj or {})
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(jd(INSTANCE), encoding="utf-8")
    os.replace(tmp, p)
    return INSTANCE


def check_rate_limit(ip):
    now = datetime.now(CST).timestamp()
    _rate_attempts_local = globals().get("_rate_attempts", {})
    attempts = _rate_attempts_local.get(ip, [])
    attempts = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    if len(attempts) >= RATE_LIMIT_MAX:
        return False
    attempts.append(now)
    _rate_attempts_local[ip] = attempts
    globals()["_rate_attempts"] = _rate_attempts_local
    return True


class HTTP(BaseHTTPRequestHandler):
    server_version = "pubchat/1.0"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = jd(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        p = Path(path)
        try:
            rel = p.resolve().relative_to(WEB.resolve())
        except Exception:
            rel = Path(str(path).lstrip("/"))
        code, headers, body = static_response("/" + str(rel).replace("\\", "/"))
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"ok": True, "ts": now_iso(),
                                    "online": len(hub.clients)})
        if path in ("/", "/index.html"):
            return self._file(WEB / "index.html")
        target = web_asset(path)
        if target is None:
            return self._json(403, {"error": "forbidden"})
        return self._file(target)

    def do_POST(self):
        return self._json(404, {"error": "not found"})


def boot():
    global STORE
    DATA.mkdir(parents=True, exist_ok=True)
    prev = globals().get("STORE")
    if prev is not None:
        try:
            prev.conn.close()
        except Exception:
            pass
    STORE = Store(DATA / "pubchat.db")
    hub.store = STORE
    load_instance()
    return STORE


DB_COLUMNS = {
    "users": {"client_id", "name", "key1"},
    "agents": {"client_id", "name", "owner", "key2"},
    "messages": {"id", "room", "sender", "sender_name", "body", "mentions", "created_at"},
}


def validate_db(path):
    p = Path(path)
    if not p.is_file():
        return "找不到文件：%s" % p
    try:
        conn = sqlite3.connect(str(p))
    except Exception as exc:
        return "打不开这个文件：%s" % exc
    try:
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as exc:
            return "不是有效的 SQLite 文件：%s" % exc
        missing = set(DB_COLUMNS) - tables
        if missing:
            return "不是 PubChat 数据库（缺表：%s）" % "、".join(sorted(missing))
        for table, cols in DB_COLUMNS.items():
            have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            lack = cols - have
            if lack:
                return "数据库版本不对（%s 表缺列：%s）" % (table, "、".join(sorted(lack)))
        if not conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return "这个库里没有任何用户"
        return None
    finally:
        conn.close()


async def handler(ws):
    inst = get_instance()
    settings = {"require_approval": bool(inst.get("require_approval"))}
    # 0.8.24：把实例设置的读写注入 WS 层，settings_get/settings_set 才算数。
    await ws_handler(ws, STORE, hub, log, settings,
                     inst_getter=get_instance, inst_saver=save_instance)


def main():
    """公网宿主入口。"""
    import os
    boot()
    pub_port = int(os.environ.get("PUBCHAT_PORT") or 8865)
    log(f"starting public server on :{pub_port}")
    return ws_serve(handler, "0.0.0.0", pub_port)


if __name__ == "__main__":
    asyncio.run(main())
