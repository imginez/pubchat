#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pubchat 本机版宿主（Windows 单 exe 入口的实际实现）

对外一个口：  页面 + /ws + /healthz     （默认 8765，0.0.0.0）
本机一个口：  首启向导 / 管理 / 二维码  （默认 8765+1，仅 127.0.0.1）

管理口除了绑回环，还校验 Host 与 Origin，防 DNS rebinding / CSRF。
数据目录默认 %LOCALAPPDATA%\\PubChat；exe 同级已有可写 pubchat-data/ 时才走便携模式。
"""
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

# PyInstaller 的窗口模式没有控制台：sys.stdout/stderr 是 None，
# print 会直接抛 AttributeError。先兜底成 devnull，再记下有没有真控制台。
HAS_CONSOLE = sys.stdout is not None
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import server
import tunnel
from core.agent_modes import MODES, modes_payload
from core.member_admin import kick_socket, normalize_limit, seat_limit_of
from hosts import update_check
from websockets.datastructures import Headers
from websockets.http11 import Response

VERSION = "0.8.28"
APP = "PubChat"
MAX_INSTANCES = 20          # 本机 + 远端合计上限
REASON = {200: "OK", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
          409: "Conflict", 500: "Internal Server Error", 503: "Service Unavailable"}
TUNNEL = None                     # tunnel.Tunnel，main() 里按端口创建
SERVING = True                    # 本机是否监听对外口；remote 纯客户端模式为 False


def _set_serving(value):
    global SERVING
    SERVING = bool(value)


def _fatal_popup(title, text):
    """窗口模式下没有控制台，致命错误至少让人看见。"""
    print("[pubchat] %s：%s" % (title, text), flush=True)
    if HAS_CONSOLE or os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return ROOT_DIR


def bundle_dir():
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return ROOT_DIR


def resolve_data_dir(args):
    """
    返回 (data_dir, error_message)。
    优先级：CLI 参数 > 指针文件 > 便携模式 > 默认目录
    评审要求：--data-dir 必须永远最高优先
    """
    if args.data_dir:
        return Path(args.data_dir).expanduser().resolve(), None

    # 指针文件：在默认目录里记录实际数据位置
    from hosts.windows_setup import read_pointer
    target, err, path = read_pointer()
    if err and path is not None:
        # 指针存在但目标不可用 → 返回错误，不静默回落（评审硬要求 #1）
        # 报错到 UI 措辞，别只抛 OSError
        return None, err
    if target is not None and err is None:
        return target, None
    # 指针不存在或损坏但无法恢复 → 走后续逻辑

    portable = app_dir() / "pubchat-data"
    if args.portable:
        return portable, None
    # 便携模式：同级目录已经存在 pubchat-data/ 且可写，才沿用
    if portable.is_dir() and os.access(str(portable), os.W_OK):
        return portable, None
    return _default_data_base(), None


def _default_data_base():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP


def _settings_path():
    return server.DATA / "settings.json"


def load_settings():
    p = _settings_path()
    if not p.is_file():
        return {
            "mode": "local",
            "remote_url": "",
            "instances": [],
            "autostart": False,
            "shortcut": False,
            "domain_prompted": False,
            "update": {
                "manifest_url": "",
                "last_checked": "",
                "last_result": "",
                "dismissed_version": ""
            }
        }
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 兼容老配置：补上缺失的键
        if "autostart" not in cfg:
            cfg["autostart"] = False
        if "shortcut" not in cfg:
            cfg["shortcut"] = False
        if "domain_prompted" not in cfg:
            cfg["domain_prompted"] = False
        if "update" not in cfg:
            cfg["update"] = {
                "manifest_url": "",
                "last_checked": "",
                "last_result": "",
                "dismissed_version": ""
            }
        # 注意：不要在这里给老配置补「instances: []」。老版本只有 remote_url，
        # normalize_instances 要看到「没有 instances 键」才会把它迁成远端实例；
        # 补了空数组就再也分不清「老配置」和「用户删光了实例」。
        return cfg
    except (json.JSONDecodeError, OSError):
        return {}


def save_settings(cfg):
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 多实例登记（纯客户端，不动服务端 schema）
# --------------------------------------------------------------------------- #
def _instance_name_from_url(url):
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    return host[:30] or "远端实例"


def normalize_instances(cfg):
    """
    返回 (实例列表, 是否刚从老配置迁移)。本机实例恒在第一位且不可移除。

    老版本只有一个 remote_url；这里一次性迁成 remote 实例，但不改文件 ——
    调用方 save_settings 才落盘。用户删掉这条后 instances 已是空数组，
    不会再被 remote_url 迁回来。
    """
    raw = cfg.get("instances")
    migrated = False
    if not isinstance(raw, list):
        raw = []
        old = str(cfg.get("remote_url") or "").strip()
        if old:
            url, bad = _normalize_remote_url(old)
            if url and not bad:
                raw.append({"id": "remote-legacy", "kind": "remote",
                            "name": _instance_name_from_url(url), "url": url})
        migrated = True

    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        iid = str(item.get("id") or "").strip()
        if not iid or iid == "local" or item.get("kind") == "local" or iid in seen:
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        seen.add(iid)
        name = str(item.get("name") or "").strip()[:30] or _instance_name_from_url(url)
        out.append({"id": iid, "kind": "remote", "name": name, "url": url})
    out.insert(0, {"id": "local", "kind": "local", "name": "本机", "url": ""})
    return out, migrated


def instances_state(cfg):
    return normalize_instances(cfg)[0]


def _normalize_remote_url(raw):
    from hosts import remote_probe
    return remote_probe.normalize_url(raw)


def add_instance(cfg, raw_url, name=""):
    """新增远端实例。返回 (新增项, 完整列表)。"""
    url, bad = _normalize_remote_url(raw_url)
    if bad:
        raise ValueError(bad)
    items, _ = normalize_instances(cfg)
    if len(items) >= MAX_INSTANCES:
        raise ValueError("实例最多 %d 个" % MAX_INSTANCES)
    for it in items:
        if it["kind"] == "remote" and it["url"].lower() == url.lower():
            raise ValueError("这个实例已经添加过了")
    item = {"id": "remote-" + secrets.token_hex(3), "kind": "remote",
            "name": (name or "").strip()[:30] or _instance_name_from_url(url),
            "url": url}
    items.append(item)
    cfg["instances"] = items
    return item, items


def update_instance(cfg, iid, name):
    """只允许改名字；地址变更请删掉重加（避免自检/登录态错位）。"""
    iid = (iid or "").strip()
    if iid == "local":
        raise ValueError("本机实例不能改名")
    name = (name or "").strip()[:30]
    if not name:
        raise ValueError("名字不能为空")
    items, _ = normalize_instances(cfg)
    for it in items:
        if it["id"] == iid:
            it["name"] = name
            cfg["instances"] = items
            return it, items
    raise ValueError("找不到这个实例")


def remove_instance(cfg, iid):
    """只删本地登记，不动远端数据。删掉当前纯客户端模式指向的那条会退回本机模式。"""
    iid = (iid or "").strip()
    if iid == "local":
        raise ValueError("本机实例不能移除")
    items, _ = normalize_instances(cfg)
    removed = None
    keep = []
    for it in items:
        if it["id"] == iid:
            removed = it
        else:
            keep.append(it)
    if removed is None:
        raise ValueError("找不到这个实例")
    cfg["instances"] = keep
    if (cfg.get("mode") == "remote"
            and str(cfg.get("remote_url") or "").strip() == removed["url"]):
        cfg["mode"] = "local"
        cfg["remote_url"] = ""
    return removed, keep


def ensure_instance(cfg, url):
    """老「服务器连接」保存地址时顺手登记成实例，保证壳里能找到它。"""
    url, bad = _normalize_remote_url(url)
    if bad or not url:
        return None
    items, _ = normalize_instances(cfg)
    for it in items:
        if it["url"].lower() == url.lower():
            return it
    if len(items) >= MAX_INSTANCES:
        return None
    item = {"id": "remote-" + secrets.token_hex(3), "kind": "remote",
            "name": _instance_name_from_url(url), "url": url}
    items.append(item)
    cfg["instances"] = items
    return item


def resolve_web_dir(args):
    if args.web_dir:
        return Path(args.web_dir).expanduser().resolve()
    return bundle_dir() / "web"


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips, key=lambda x: (not x.startswith(("192.168.", "10.")), x))


# --------------------------------------------------------------------------- #
# 账号
# --------------------------------------------------------------------------- #
def make_user(name, cid=None, is_admin=0, key1=None, role="member", source="local"):
    name = (name or "").strip()[:20]
    if not name:
        raise ValueError("显示名不能为空")
    cid = (cid or "").strip().lower()
    if not cid:
        base = re.sub(r"[^a-z0-9]", "", name.lower()) or "user"
        cid = base[:24]
        i = 1
        while server.STORE.user(cid):
            i += 1
            cid = "%s%d" % (base[:20], i)
    if not server.valid_client_id(cid):
        raise ValueError("登录名只能小写字母/数字/下划线，2-32 位")
    if server.STORE.user(cid):
        raise ValueError("登录名已被占用")
    key1 = (key1 or "").strip() or secrets.token_urlsafe(12)
    if len(key1) < 8:
        raise ValueError("密码至少 8 位")
    server.STORE.add_user(cid, name, key1, is_admin, role=role, source=source)
    # 默认只建账号本身，不自动建智能体（需求方 2026-11-14 要求：首次只生成聊天室主人一个人的账号密码）。
    # 需要智能体席位时在管理页「智能体席位」手动添加。
    return {"client_id": cid, "name": name, "key1": key1, "role": role,
            "source": source, "agents": []}


def users_state():
    out = []
    for u in server.STORE.users_with_role():
        agents = server.STORE.agents_of(u["client_id"])
        out.append({
            "client_id": u["client_id"], "name": u["name"],
            "display_name": u.get("display_name") or u["name"],
            "is_admin": bool(u.get("is_admin")), "key1": u["key1"],
            "role": u.get("role") or "member",
            "source": u.get("source") or "local",
            "status": u.get("status") or "active",
            "created_at": u.get("created_at"),
            "online": u["client_id"] in server.hub.by_name,
            "agent_count": len(agents),
            "agent_limit": seat_limit_of(server.STORE, u["client_id"]),
            "agents": [{"client_id": a["client_id"], "name": a["name"],
                        "key2": a["key2"], "mode": a.get("mode") or "online"}
                       for a in agents],
        })
    return out


def rooms_state():
    """管理页可配置的房间：每位成员的个人房 + 已有的自定义共享房。"""
    users = {u["client_id"]: u for u in server.STORE.users()}
    ids = ["own-" + cid for cid in users]
    for rid in server.STORE.room_ids():
        if rid in ids or rid == "lobby" or rid.startswith("dm-"):
            continue
        ids.append(rid)
    out = []
    for rid in ids:
        members = []
        for m in server.STORE.room_members_of(rid):
            u = users.get(m["user_id"])
            if u:
                members.append({"client_id": u["client_id"], "name": u["name"]})
        if rid.startswith("own-"):
            owner_cid = rid[4:]
            owner = users.get(owner_cid)
            kind, title = "own", (owner["name"] if owner else owner_cid) + " 的个人房"
        else:
            owner_cid, kind, title = None, "custom", rid
        out.append({"room_id": rid, "kind": kind, "title": title,
                    "owner": owner_cid, "members": members})
    return out


def install_db_file(src):
    """校验后把老库接管为当前库。现有库会先备份到 pubchat.db.bak-<时间>。"""
    err = server.validate_db(src)
    if err:
        raise ValueError(err)
    target = server.DATA / "pubchat.db"
    backup = None
    if target.exists():
        backup = server.DATA / ("pubchat.db.bak-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(target, backup)
    tmp = server.DATA / "import-tmp.db"
    shutil.copy2(src, tmp)
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(target) + suffix).unlink()
        except FileNotFoundError:
            pass
    prev = getattr(server, "STORE", None)
    if prev is not None:
        try:
            prev.conn.close()
        except Exception:
            pass
    os.replace(tmp, target)
    return backup


# --------------------------------------------------------------------------- #
# 可达性自测与加入
# --------------------------------------------------------------------------- #
def check_reachability(port):
    """检测公网入口是否可用。返回 (has_public, base_url, note)。"""
    if TUNNEL:
        st = TUNNEL.status()
        if st.get("state") == "on" and st.get("url"):
            return True, st["url"], ""
    ips = lan_ips()
    lan_url = "http://%s:%d/" % (ips[0], port) if ips else "http://127.0.0.1:%d/" % port
    return False, lan_url, "没有公网入口，邀请码仅同 WiFi 可用。如需远程访问，请先开启公网隧道。"


def fingerprint():
    inst = server.get_instance()
    return inst["instance_id"][:12]


def handle_join(payload, client_ip="unknown"):
    """处理加入请求。payload 需含 token, name, client_id。返回 (code, response_dict)。"""
    if not server.check_rate_limit(client_ip):
        return 429, {"error": "操作太频繁，请稍后再试"}
    token = (payload.get("token") or "").strip()
    name = (payload.get("name") or "").strip()[:20]
    cid = (payload.get("client_id") or "").strip().lower()
    if not token:
        return 400, {"error": "缺少邀请码"}
    if not name:
        return 400, {"error": "请填写显示名"}
    if not cid:
        return 400, {"error": "请填写登录名"}
    if not server.valid_client_id(cid):
        return 400, {"error": "登录名只能小写字母/数字/下划线，2-32 位"}
    if server.STORE.user(cid):
        return 409, {"error": "登录名已被占用"}
    if server.MEMBER_LIMIT and server.STORE.user_count() >= server.MEMBER_LIMIT:
        return 403, {"error": "成员已达上限（%d）" % server.MEMBER_LIMIT}
    token_hash = server._hash_token(token)
    invite = server.STORE.find_invite_by_hash(token_hash)
    if not invite:
        return 403, {"error": "邀请码无效"}
    now = datetime.now(server.CST)
    if invite.get("expires_at"):
        exp = datetime.fromisoformat(invite["expires_at"])
        if now > exp:
            return 403, {"error": "邀请码已过期"}
    if invite["used"] >= 1:
        return 403, {"error": "邀请码已使用"}
    if not server.STORE.claim_invite(invite["id"]):
        return 403, {"error": "邀请码已使用"}
    role = invite.get("role") or "member"
    u = make_user(name, cid, is_admin=0, role=role, source="public")
    server.log("join", cid, "via invite #%d" % invite["id"], "role=%s" % role)
    return 200, {"ok": True, "user": u}


# --------------------------------------------------------------------------- #
# 管理口一次性链接（DB 版）
# --------------------------------------------------------------------------- #
# token 放 URL fragment（不进日志、不发 Referer），库里只存 SHA-256，5 分钟有效，
# 兑换成功立刻作废（used=1）。明文只出现在 URL #t= 后，不落盘、不进查询串。
# 状态码：401 无 token 或无效 / 410 已过期 / 409 已使用。
ADMIN_OPEN_TTL = max(1, int(os.environ.get("PUBCHAT_ADMIN_TTL") or 300))  # 测试可调


def write_admin_handoff(link, expires_iso, ttl, data_dir=None):
    """把最新一次性链接写到 <数据目录>/admin-open.json（给人/桌面壳读取的交接文件）。
    明文只在这一处落盘；兑换成功后立刻删除，不再留底。"""
    target = Path(data_dir) if data_dir else server.DATA
    try:
        (target / "admin-open.json").write_text(
            json.dumps({
                "url": link,
                "expires_at": expires_iso,
                "expires_in": ttl,
                "note": "一次性管理链接：打开后 token 立即作废，过期请重新生成",
            }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def delete_admin_handoff():
    try:
        (server.DATA / "admin-open.json").unlink()
    except OSError:
        pass


def generate_admin_link(port, data_dir=None, kid="local"):
    """生成一次性管理链接（DB 版）。返回 (URL, token) 元组。

    token 存 DB 的 admin_token 表，只存 sha256，5 分钟有效，单次使用。
    明文只出现在 URL #t= 后，不落盘、不进查询串。
    同时写 <数据目录>/admin-open.json 供桌面壳读取。"""
    from core.store import _hash_token
    token = secrets.token_urlsafe(24)
    token_hash = _hash_token(token)
    expires = datetime.now(server.CST) + timedelta(seconds=ADMIN_OPEN_TTL)
    expires_iso = expires.isoformat(timespec="seconds")
    server.STORE.add_admin_token(token_hash, expires_iso, kid=kid)
    link = "http://127.0.0.1:%d/admin.html#t=%s" % (port, token)
    if data_dir:
        write_admin_handoff(link, expires_iso, ADMIN_OPEN_TTL, data_dir)
    server.log("admin_token", "generate", "kid=%s" % kid)
    return link, token


# --------------------------------------------------------------------------- #
# 管理口
# --------------------------------------------------------------------------- #
class AdminHTTP(server.HTTP):
    """只绑 127.0.0.1 的管理面。Host/Origin 双校验，防 DNS rebinding 与跨站提交。"""

    allowed_hosts = set()
    allowed_origins = set()
    public_port = 0

    def _guard(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.allowed_hosts:
            self._json(403, {"error": "forbidden host"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in self.allowed_origins:
            self._json(403, {"error": "forbidden origin"})
            return False
        return True

    def _send(self, code, headers, body):
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Static resources that can be loaded without token (to avoid redirect loop)
    # 桌面壳相关的页面也必须能直接加载：内置窗口导航带不了 Authorization 头，
    # 页面本身不含数据，真正的数据与动作仍由 /api/* 的 Bearer 凭证把关。
    _public_static = {"admin.html", "admin.js", "device.js", "qrcode.js",
                      "style.css", "weui-tokens.css", "config.js",
                      "shell.html", "settings.html", "instances.html",
                      "about.html", "server.html", "members.html",
                      "setup.html", "invite.js", "members.js",
                      "settings.html", "instances.html", "server.html",
                      "manage.html", "manage.js",
                      "splash.js", "guide.js", "notice.js"}

    # 重新生成一次性链接的限频（滑动窗口，单进程内足够；管理口只绑回环）
    _gen_times = []
    _gen_lock = threading.Lock()
    _GEN_WINDOW = 60
    _GEN_LIMIT = 5

    def _locked(self, path):
        """没有有效设备凭证时的统一拒绝：API 给 401 JSON，页面分两种情况：
        - 静态资源（admin.js/device.js/qrcode.js/style.css/config.js/admin.html）：允许无 token 加载
        - 其他页面（/, /index.html 等）：给 admin.html，由页面的 JS 渲染「粘贴管理链接」引导
        """
        if path.startswith("/api/"):
            return self._json(401, {"error": "需要设备凭证：请用一次性链接打开管理口",
                                    "code": "admin_locked"})
        # 提取文件名（去掉路径和 query）
        from urllib.parse import urlparse
        fname = urlparse(path).path.rsplit("/", 1)[-1]
        # 静态资源允许无 token 加载
        if fname in self._public_static:
            target = server.web_asset("/" + fname)
            if target is not None and target.is_file():
                code, headers, body = server.static_response("/" + fname)
                return self._send(code, headers, body)
        # 其他页面：给 admin.html，由页面的 JS 渲染「粘贴管理链接」引导（不再 302 回自身）
        guide = server.web_asset("/admin.html")
        if guide is not None and guide.is_file():
            code, headers, body = server.static_response("/admin.html")
            return self._send(code, headers, body)
        return self._json(404, {"error": "admin.html not found"})

    def _render_paste_link_page(self):
        """渲染「粘贴管理链接」引导页，让用户粘贴一次性管理链接。"""
        body = (
            "<!DOCTYPE html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>PubChat · 请先粘贴管理链接</title>"
            "<body style=\"font:15px/1.7 system-ui;margin:0;display:grid;place-items:center;"
            "min-height:100vh;background:#f6f7f9;color:#222\">"
            "<div style=\"max-width:480px;padding:28px;background:#fff;border-radius:14px;"
            "box-shadow:0 8px 30px rgba(0,0,0,.08)\">"
            "<h2 style=\"margin:0 0 12px\">请先粘贴管理链接</h2>"
            "<p>这是 PubChat 管理面板。首次访问需要一次性管理链接。</p>"
            "<p style=\"color:#4e5969;font-size:14px\">请在服务器启动时的控制台找到类似这样的链接：</p>"
            "<code style=\"display:block;background:#f7f8fa;padding:10px;border-radius:8px;"
            "font-size:12px;word-break:break-all;color:#1d2129;margin:8px 0 16px\">"
            "http://127.0.0.1:8766/admin.html#t=xxxxxxxxxxxxxxxx</code>"
            "<p style=\"color:#86909c;font-size:13px\">复制该链接到浏览器地址栏打开，即可完成设备认领并进入管理面板。</p>"
            "<p style=\"color:#86909c;font-size:12px;margin-top:16px\">链接 5 分钟内有效，仅可使用一次。</p>"
            "</div></body></html>"
        ).encode("utf-8")
        return self._send(200, {"Content-Type": "text/html; charset=utf-8",
                                "Cache-Control": "no-store"}, body)

    def _has_session(self):
        """检查请求是否携带有效的设备 Bearer token。"""
        from hosts import server_admin
        auth_header = self.headers.get("Authorization") or ""
        if not auth_header.lower().startswith("bearer "):
            return False
        secret = auth_header[7:].strip()
        if not secret:
            return False
        from core.store import _hash_token
        dev = server.STORE.device_by_hash(_hash_token(secret))
        if dev:
            server.STORE.touch_device(dev["id"])
            return True
        # 0.8.17：管理权跟账号走，不跟机器走（需求方：不要认领那套）。
        # role=owner 账号的一级 Key 直接当管理员凭据，普通成员不算。
        for u in server.STORE.users_with_role():
            if u.get("key1") and u["key1"] == secret and (u.get("role") or "") == "owner":
                return True
        return False

    def _page(self, name):
        target = server.web_asset("/" + name)
        if target is None or not target.is_file():
            return self._json(404, {"error": "not found"})
        code, headers, body = server.static_response("/" + name)
        return self._send(code, headers, body)

    def do_GET(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"ok": True, "port": self.public_port})
        if not self._has_session():
            return self._locked(path)
        if path in ("/", "/index.html"):
            return self._page("setup.html" if not server.STORE.users() else "admin.html")
        if path == "/api/state":
            return self._json(200, self.state())
        if path == "/api/deploy":
            return self._json(200, self._deploy_state())
        if path == "/api/instances":
            cfg = load_settings()
            return self._json(200, {"ok": True, "instances": instances_state(cfg)})
        # 模型中转站（批4③）：只有 GET 这一支。上游地址与 Key 只在 model_hub 里
        # 从环境变量取，请求体与查询参数一概不参与——do_POST 里没有这个 path 的分支，
        # 冒烟 unit_model_hub() 就照这条判据钉死「无写入口」。
        if path == "/api/model-hub":
            from hosts import model_hub
            q = urlparse(self.path).query or ""
            return self._json(200, model_hub.snapshot(server.STORE, force="force=1" in q))
        target = server.web_asset(path)
        if target is None:
            return self._json(403, {"error": "forbidden"})
        return self._file(target)

    def do_POST(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        # /api/import 是二进制文件上传，单独处理
        if path == "/api/import":
            if not self._has_session():
                return self._json(401, {"error": "需要设备凭证：请用一次性链接打开管理口",
                                        "code": "admin_locked"})
            return self._import()
        # 其他端点解析 JSON payload
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            payload = {}
        # Token 兑换端点：不需要设备凭证，用来创建凭证
        if path == "/api/admin/token/verify":
            return self._token_verify(payload)
        # 其他端点（含重新生成管理链接）需要设备凭证
        if not self._has_session():
            # 本机宿主首次引导：还没有账号时的初始化、账号确认/改密，
            # 回环 + Origin 校验通过就放行（改密仍要校验旧密码）。
            from hosts import server_admin as _sa
            bootstrap_ok = (not _sa.SERVER_MODE and path in
                            ("/api/setup", "/api/me/name", "/api/me/password",
                             "/api/shell-claim"))
            if not bootstrap_ok:
                return self._json(401, {"error": "需要设备凭证：请用一次性链接打开管理口",
                                        "code": "admin_locked"})
        if path == "/api/admin/token/generate":
            return self._token_generate(payload)
        try:
            if path == "/api/shell-claim":
                # 桌面外壳自动登录（本机部署完成页调用）：管理口只绑回环，
                # 同源请求才走得到这里；生成一条设备凭证给壳，前端存 localStorage。
                secret = secrets.token_urlsafe(32)
                from core.store import _hash_token
                server.STORE.add_device("桌面外壳", _hash_token(secret),
                                        created_by="desktop-shell")
                return self._json(200, {"ok": True, "secret": secret})
            if path == "/api/setup":
                if server.STORE.users():
                    return self._json(409, {"error": "已经初始化过了"})
                auto = bool(payload.get("auto"))
                name = (payload.get("name") or "").strip()
                cid = (payload.get("client_id") or "").strip().lower()
                key = (payload.get("key") or "").strip()
                if auto:
                    # 本机部署自动模式：账号和密码都自动生成，完成页展示后由用户改
                    name = name or "聊天室主人"
                    cid = cid or "admin"
                    if server.STORE.user(cid):
                        cid = "admin_" + secrets.token_hex(3)
                    if not server.valid_client_id(cid):
                        cid = "admin"
                    key = key or secrets.token_urlsafe(12)
                else:
                    if len(key) < 8:
                        return self._json(400, {"error": "密码至少 8 位"})
                    if not server.valid_client_id(cid):
                        return self._json(400,
                                          {"error": "登录名只能小写字母/数字/下划线，2-32 位"})
                u = make_user(name, cid, is_admin=1, key1=key, role="owner")
                server.STORE.mark_onboarded(u["client_id"])
                return self._json(200, {"ok": True, "user": u, "auto": auto})
            if path == "/api/me/name":
                # 首次登录的「确认账户名」：本机模式改 owner 的显示名
                u = next((x for x in server.STORE.users()
                          if (x.get("role") or "") == "owner"), None)
                if not u:
                    return self._json(404, {"error": "还没有管理员账号"})
                new_name = (payload.get("name") or "").strip()[:20]
                if len(new_name) < 1:
                    return self._json(400, {"error": "名字不能为空"})
                server.STORE.set_user_name(u["client_id"], new_name)
                for ws, ident in list(server.hub.clients.items()):
                    if ident.get("client_id") == u["client_id"]:
                        ident["name"] = new_name
                return self._json(200, {"ok": True, "name": new_name})
            if path == "/api/me/password":
                # 首次登录的「修改密码」：校验旧密码后换新
                u = next((x for x in server.STORE.users()
                          if (x.get("role") or "") == "owner"), None)
                if not u:
                    return self._json(404, {"error": "还没有管理员账号"})
                old = (payload.get("old") or "").strip()
                new = (payload.get("new") or "").strip()
                if old != (u.get("key1") or ""):
                    return self._json(403, {"error": "旧密码不对"})
                if len(new) < 8:
                    return self._json(400, {"error": "新密码至少 8 位"})
                if new == old:
                    return self._json(400, {"error": "新密码不能和旧密码相同"})
                server.STORE.set_user_key(u["client_id"], new)
                return self._json(200, {"ok": True, "key1": new,
                                        "client_id": u["client_id"]})
            if path == "/api/admin/user":
                u = make_user(payload.get("name"), payload.get("client_id"),
                              is_admin=1 if payload.get("is_admin") else 0)
                return self._json(200, {"ok": True, "user": u})
            if path == "/api/admin/tunnel":
                action = (payload.get("action") or "status").strip()
                if TUNNEL is None:
                    return self._json(500, {"error": "公网模块未启动"})
                if action == "start":
                    return self._json(200, {"ok": True, "tunnel": TUNNEL.start()})
                if action == "stop":
                    return self._json(200, {"ok": True, "tunnel": TUNNEL.stop()})
                if action == "config":
                    cfg = TUNNEL.save_config(
                        mode=payload.get("mode"),
                        token=payload.get("token"),
                        public_url=payload.get("public_url"),
                        auto=payload.get("auto"),
                        clear_token=bool(payload.get("clear_token")),
                    )
                    return self._json(200, {"ok": True, "config": cfg,
                                            "tunnel": TUNNEL.status()})
                return self._json(200, {"ok": True, "tunnel": TUNNEL.status()})
            if path == "/api/admin/reachability":
                has_pub, url, note = check_reachability(self.public_port)
                return self._json(200, {"ok": True, "has_public": has_pub,
                                        "url": url, "note": note})
            if path == "/api/admin/invite":
                action = (payload.get("action") or "create").strip()
                if action == "create":
                    role = (payload.get("role") or "member").strip()
                    if role not in ("member", "readonly"):
                        return self._json(400, {"error": "角色必须是 member 或 readonly"})
                    raw_hours = payload.get("hours")
                    try:
                        hours = 24 if raw_hours in (None, "") else int(raw_hours)
                    except (TypeError, ValueError):
                        return self._json(400, {"error": "有效期只能是 24/48 或 0（永久）"})
                    raw_count = payload.get("count")
                    count = 1 if raw_count in (None, "") else int(raw_count)
                    if hours != 0 and (hours < 1 or hours > 720):
                        return self._json(400, {"error": "有效期需在 1-720 小时之间，0＝永久"})
                    if count < 1 or count > 20:
                        return self._json(400, {"error": "单批数量需在 1-20 之间"})
                    # 0＝永久：与云端管理口同口径，expires 落 NULL
                    expires = (None if hours == 0 else
                               (datetime.now(server.CST) +
                                timedelta(hours=hours)).isoformat(timespec="seconds"))
                    has_pub, base_url, note = check_reachability(self.public_port)
                    items = []
                    for _ in range(count):
                        token = secrets.token_urlsafe(24)
                        inv_id = server.STORE.add_invite(
                            server._hash_token(token), role, expires, "admin")
                        items.append({
                            "id": inv_id, "token": token, "role": role,
                            "expires_at": expires,
                            "url": base_url.rstrip("/") + "/join.html#t=" + token,
                        })
                    return self._json(200, {
                        "ok": True,
                        "invites": items,
                        "invite": items[0],
                        "has_public": has_pub, "note": note,
                    })
                if action == "delete":
                    inv_id = int(payload.get("id") or 0)
                    if inv_id:
                        server.STORE.remove_invite(inv_id)
                    return self._json(200, {"ok": True})
                return self._json(400, {"error": "未知操作"})
            if path == "/api/admin/rooms":
                return self._json(200, {"ok": True, "rooms": rooms_state()})
            if path == "/api/admin/room/members":
                action = (payload.get("action") or "list").strip()
                room = (payload.get("room") or "").strip().lower()
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", room):
                    return self._json(400, {"error": "房间名不合法"})
                known = {r["room_id"] for r in rooms_state()}
                if room not in known:
                    return self._json(404, {"error": "房间不存在或不需要单独配置"})
                if action == "add":
                    raw_ids = payload.get("client_ids")
                    if raw_ids is None:
                        raw_ids = [payload.get("client_id")]
                    if not isinstance(raw_ids, list):
                        return self._json(400, {"error": "client_ids 必须是数组"})
                    added = []
                    for cid in raw_ids:
                        cid = (cid or "").strip().lower()
                        u = server.STORE.user(cid) if cid else None
                        if not u or (u.get("status") or "active") != "active":
                            return self._json(404, {"error": "成员不存在：%s" % cid})
                        server.STORE.add_room_member(room, cid)
                        added.append(cid)
                    return self._json(200, {"ok": True, "room": room, "added": added,
                                            "rooms": rooms_state()})
                if action == "remove":
                    cid = (payload.get("client_id") or "").strip().lower()
                    if not cid:
                        return self._json(400, {"error": "缺少 client_id"})
                    server.STORE.remove_room_member(room, cid)
                    return self._json(200, {"ok": True, "room": room,
                                            "rooms": rooms_state()})
                if action == "list":
                    info = next(r for r in rooms_state() if r["room_id"] == room)
                    return self._json(200, {"ok": True, "room": info})
                return self._json(400, {"error": "未知操作"})
            if path in ("/api/admin/member/approve", "/api/admin/member/reject"):
                cid = (payload.get("client_id") or "").strip().lower()
                u = server.STORE.user(cid)
                if not u:
                    return self._json(404, {"error": "找不到该成员"})
                if path.endswith("approve"):
                    server.STORE.set_user_status(cid, "active")
                    return self._json(200, {"ok": True, "status": "active"})
                server.STORE.remove_user(cid)
                return self._json(200, {"ok": True, "status": "rejected"})
            if path == "/api/admin/member/role":
                cid = (payload.get("client_id") or "").strip()
                role = (payload.get("role") or "").strip()
                if role not in ("owner", "member", "readonly"):
                    return self._json(400, {"error": "角色必须是 owner/member/readonly"})
                u = server.STORE.user(cid)
                if not u:
                    return self._json(404, {"error": "找不到该成员"})
                server.STORE.set_user_role(cid, role)
                for ws, ident in list(server.hub.clients.items()):
                    if ident.get("client_id") == cid:
                        ident["role"] = role
                return self._json(200, {"ok": True})
            if path == "/api/admin/member/limit":
                cid = (payload.get("client_id") or "").strip().lower()
                if not server.STORE.user(cid):
                    return self._json(404, {"error": "找不到该成员"})
                value, bad = normalize_limit(payload.get("limit"))
                if bad:
                    return self._json(400, {"error": bad})
                server.STORE.set_user_agent_limit(cid, value)
                return self._json(200, {"ok": True, "client_id": cid,
                                        "agent_limit": value})
            if path == "/api/admin/member/remove":
                cid = (payload.get("client_id") or "").strip()
                u = server.STORE.user(cid)
                if not u:
                    return self._json(404, {"error": "找不到该成员"})
                if (u.get("role") or "") == "owner":
                    return self._json(403, {"error": "不能移除服务器主人（房主）"})
                to_kick = []
                for ws, ident in list(server.hub.clients.items()):
                    if ident.get("client_id") == cid or ident.get("owner") == cid:
                        to_kick.append(ws)
                loop = getattr(server.hub, "loop", None)
                for ws in to_kick:
                    try:
                        server.hub.remove(ws)
                        kick_socket(ws, loop)
                    except Exception:
                        pass
                server.STORE.remove_user(cid)
                return self._json(200, {"ok": True})
            if path == "/api/admin/member/reset-key":
                cid = (payload.get("client_id") or "").strip()
                u = server.STORE.user(cid)
                if not u:
                    return self._json(404, {"error": "找不到该成员"})
                new_key = server.STORE.reset_user_key(cid)
                return self._json(200, {"ok": True, "key1": new_key})
            if path == "/api/admin/agent/reset-key":
                acid = (payload.get("client_id") or "").strip()
                a = server.STORE.agent(acid)
                if not a:
                    return self._json(404, {"error": "找不到该智能体"})
                new_key = server.STORE.reset_agent_key(acid)
                return self._json(200, {"ok": True, "key2": new_key})
            if path == "/api/admin/agent/mode":
                acid = (payload.get("client_id") or "").strip()
                mode = (payload.get("mode") or "").strip().lower()
                if mode not in MODES:
                    return self._json(400, {"error": "接入方式只能是 online/inbox"})
                if not server.STORE.agent(acid):
                    return self._json(404, {"error": "找不到该智能体"})
                server.STORE.set_agent_mode(acid, mode)
                return self._json(200, {"ok": True, "client_id": acid, "mode": mode})
            if path == "/api/admin/instance":
                inst = server.get_instance()
                changed = False
                new_name = (payload.get("name") or "").strip()
                if new_name:
                    inst["name"] = new_name[:30]
                    changed = True
                if "require_approval" in payload:
                    inst["require_approval"] = bool(payload.get("require_approval"))
                    changed = True
                if changed:
                    p = server.DATA / "instance.json"
                    p.write_text(server.jd(inst), encoding="utf-8")
                return self._json(200, {"ok": True, "instance": inst})
            if path == "/api/instances":
                return self._handle_instances(payload)
            if path == "/api/deploy":
                return self._handle_deploy(payload)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            return self._json(500, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def _handle_instances(self, payload):
        action = (payload.get("action") or "").strip()
        cfg = load_settings()
        if action == "add":
            item, items = add_instance(cfg, payload.get("url"), payload.get("name"))
            save_settings(cfg)
            return self._json(200, {"ok": True, "instance": item, "instances": items})
        if action == "update":
            item, items = update_instance(cfg, payload.get("id"), payload.get("name"))
            save_settings(cfg)
            return self._json(200, {"ok": True, "instance": item, "instances": items})
        if action == "remove":
            removed, items = remove_instance(cfg, payload.get("id"))
            save_settings(cfg)
            return self._json(200, {"ok": True, "removed": removed, "instances": items,
                                    "mode": cfg.get("mode", "local"),
                                    "remote_url": cfg.get("remote_url", "")})
        return self._json(400, {"error": "未知操作：%s" % action})

    def _drain_body(self):
        """把请求体读干净再回错误码。

        不回读就关连接，Windows 上发的是 RST 而不是 FIN：调用方还在按 Content-Length
        往下写，拿到的是 WinError 10053（连接中止），看不见我们真正回的那个 409/400。
        只在「已经决定拒绝、但客户端还在传」这条路必要，所以只在这两个分支前调。
        """
        remaining = int(self.headers.get("Content-Length") or 0)
        while remaining > 0:
            chunk = self.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _import(self):
        """接收原始 SQLite 字节流。不能先按 JSON 读 body，否则上传会被吞掉。"""
        if server.STORE.users():
            self._drain_body()
            return self._json(409, {"error": "已经有数据了。要覆盖请关掉程序，用 --import 导入（会先备份）"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 200 * 1024 * 1024:
            if 0 < n <= 200 * 1024 * 1024 * 4:
                self._drain_body()   # 超大（>800MB）不陪它耗带宽，让连接自己断
            return self._json(400, {"error": "文件为空或超过 200MB"})
        # 换库后旧 admin_device 行会随旧库消失，用本次请求的 Bearer 重新挂回去，
        # 否则导入成功后管理页立刻变 401。
        auth = self.headers.get("Authorization") or ""
        secret = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        upload = server.DATA / "upload.db"
        try:
            with open(upload, "wb") as f:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                return self._json(400, {"error": "上传中断，文件不完整"})
            backup = install_db_file(upload)
            server.boot()
            imported = True
        except ValueError as exc:
            server.boot()
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            server.boot()
            return self._json(500, {"error": str(exc)})
        finally:
            try:
                upload.unlink()
            except FileNotFoundError:
                pass
        if secret:
            from core.store import _hash_token
            secret_hash = _hash_token(secret)
            if not server.STORE.device_by_hash(secret_hash):
                server.STORE.add_device("本地管理浏览器", secret_hash, created_by="import")
        return self._json(200, {"ok": True, "users": len(server.STORE.users()),
                                "backup": str(backup) if backup else None})

    def _token_generate(self, payload):
        """Generate a one-time admin access token. Returns link with token in fragment.

        The token is 5-minute TTL, single-use. Only the hash is stored; plaintext
        is returned once in the link and never logged. Requires a valid device
        credential (caller already enforced by do_POST) and is rate limited.
        """
        now = time.monotonic()
        with self._gen_lock:
            self._gen_times[:] = [t for t in self._gen_times if now - t < self._GEN_WINDOW]
            if len(self._gen_times) >= self._GEN_LIMIT:
                retry = max(1, int(self._GEN_WINDOW - (now - self._gen_times[0])))
                return self._json(429, {"error": "生成太频繁，请 %d 秒后再试" % retry,
                                        "code": "rate_limited", "retry_after": retry})
            self._gen_times.append(now)

        from core.store import _hash_token
        kid = (payload.get("kid") or "local")[:40]
        token = secrets.token_urlsafe(24)
        token_hash = _hash_token(token)
        expires = (datetime.now(server.CST) + timedelta(seconds=ADMIN_OPEN_TTL))
        expires_iso = expires.isoformat(timespec="seconds")
        server.STORE.add_admin_token(token_hash, expires_iso, kid=kid)
        admin_port = self.server.server_address[1]
        link = "http://127.0.0.1:%d/admin.html#t=%s" % (admin_port, token)
        write_admin_handoff(link, expires_iso, ADMIN_OPEN_TTL)
        server.log("admin_token", "generate", "kid=%s" % kid)
        return self._json(200, {
            "ok": True,
            "link": link,
            "expires_at": expires_iso,
            "ttl": ADMIN_OPEN_TTL,
            "note": "此链接 5 分钟内有效，仅可使用一次。打开后自动完成设备认领。",
        })

    def _token_verify(self, payload):
        """Verify a one-time admin token and bootstrap a device credential.

        Status codes:
        - 200: token valid, device created, secret returned
        - 401: no token provided or token not found
        - 410: token expired
        - 409: token already used
        """
        from core.store import _hash_token
        token = (payload.get("token") or "").strip()
        if not token:
            return self._json(401, {"error": "缺少管理令牌"})

        token_hash = _hash_token(token)
        row = server.STORE.find_admin_token_by_hash(token_hash)

        if not row:
            return self._json(401, {"error": "管理令牌无效"})

        # 过期判定必须在 used 之前：启动时 clear_expired_admin_tokens 会把过期行
        # 标成 used=1，否则重启后过期链接会错报成 409「已使用」。
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except (ValueError, TypeError):
            return self._json(410, {"error": "此令牌已过期"})

        if datetime.now(server.CST) > expires:
            server.STORE.use_admin_token(row["id"])
            return self._json(410, {"error": "此令牌已过期"})

        if row["used"]:
            return self._json(409, {"error": "此令牌已被使用"})

        if not server.STORE.use_admin_token(row["id"]):
            return self._json(409, {"error": "此令牌已被使用"})

        device_secret = secrets.token_urlsafe(32)
        device_name = "本地管理浏览器"
        did = server.STORE.add_device(device_name, _hash_token(device_secret),
                                      created_by="admin_token")
        # 明文链接只在兑换前存在：成功后把交接文件删掉
        delete_admin_handoff()
        server.log("admin_token", "verify", "device=#%d kid=%s" % (did, row.get("kid", "")))
        return self._json(200, {
            "ok": True,
            "device": {"id": did, "name": device_name},
            "secret": device_secret,
            "note": "设备凭证只显示这一次，已自动保存在本浏览器。",
        })

    def state(self):
        inst = server.get_instance()
        invites_raw = server.STORE.list_invites()
        now = datetime.now(server.CST)
        invites = []
        for inv in invites_raw:
            expired = False
            if inv["expires_at"]:
                try:
                    expired = datetime.fromisoformat(inv["expires_at"]) < now
                except ValueError:
                    pass
            invites.append({
                "id": inv["id"], "role": inv["role"],
                "expires_at": inv["expires_at"],
                "used": inv["used"],
                "status": "used" if inv["used"] else ("expired" if expired else "active"),
                "created_at": inv["created_at"],
            })
        cfg = load_settings()
        return {
            "ok": True,
            "version": VERSION,
            "initialized": bool(server.STORE.users()),
            "users": users_state(),
            "agent_modes": modes_payload(),
            "port": self.public_port,
            "admin_port": self.server.server_address[1],
            "addresses": {
                "local": "http://127.0.0.1:%d/" % self.public_port,
                "lan": ["http://%s:%d/" % (ip, self.public_port) for ip in lan_ips()],
            },
            "data_dir": str(server.DATA),
            "web_dir": str(server.WEB),
            "online": len(server.hub.clients),
            "tunnel": TUNNEL.status() if TUNNEL else
            {"state": "off", "url": None, "available": False, "detail": "公网模块未启动"},
            "instance": {
                "instance_id": inst["instance_id"],
                "name": inst.get("name", "PubChat"),
                "fingerprint": inst["instance_id"][:12],
                "require_approval": bool(inst.get("require_approval")),
            },
            "invites": invites,
            "rooms": rooms_state(),
            "member_limit": server.MEMBER_LIMIT,
            "member_count": server.STORE.user_count(),
            "mode": cfg.get("mode", "local"),
            "remote_url": cfg.get("remote_url", ""),
            "instances": instances_state(cfg),
            "serving": bool(SERVING),
            "restart_needed": bool(remote_target(cfg)) == bool(SERVING),
        }

    def _deploy_state(self):
        cfg = load_settings()
        runtime = mode_runtime(cfg)
        return {
            "ok": True,
            "mode": runtime["mode"],
            "remote_url": runtime["remote_url"],
            "serving": runtime["serving"],
            "restart_needed": runtime["restart_needed"],
            "autostart": cfg.get("autostart", False),
            "shortcut": cfg.get("shortcut", False),
            "domain_prompted": cfg.get("domain_prompted", False),
            "data_dir": str(server.DATA),
            "version": VERSION,
            "update": cfg.get("update", {}),
        }

    def _handle_deploy(self, payload):
        action = (payload.get("action") or "").strip()
        cfg = load_settings()

        if action == "set_mode":
            mode = (payload.get("mode") or "local").strip()
            if mode not in ("local", "remote"):
                return self._json(400, {"error": "mode 必须是 local 或 remote"})
            cfg["mode"] = mode
            save_settings(cfg)
            return self._json(200, {"ok": True, "mode": mode,
                                    "restart_needed": bool(remote_target(cfg)) == bool(SERVING)})

        if action == "set_remote":
            from hosts import remote_probe
            raw = (payload.get("url") or "").strip()
            url, bad = remote_probe.normalize_url(raw) if raw else ("", "")
            if bad:
                return self._json(400, {"error": bad})
            cfg["remote_url"] = url
            cfg["mode"] = "remote" if url else "local"
            if url:
                ensure_instance(cfg, url)
            save_settings(cfg)
            # 这里不代替用户再探一次：前端「保存并切换」只在三段自检通过后才可点。
            # 探过了反而多一次网络往返，且失败时容易把用户锁死在「切不过去」。
            return self._json(200, {"ok": True, "mode": cfg["mode"], "remote_url": url,
                                    "instances": instances_state(cfg),
                                    "relogin": bool(url),
                                    "restart_needed": bool(remote_target(cfg)) == bool(SERVING)})

        if action == "test_remote":
            # 三段自检 DNS / TCP / WS 分开报（HEAD 200 不代表 WS 通，
            # 会误判「能连」）。实现放在 hosts/remote_probe.py，冒烟能对真实端口注故障。
            from hosts import remote_probe
            res = remote_probe.probe(payload.get("url") or "")
            stages = {"dns": res["dns"], "tcp": res["tcp"], "ws": res["ws"]}
            return self._json(200, {
                "ok": True,
                "reachable": res["reachable"],
                "result": {
                    "reachable": res["reachable"],
                    "stages": stages,
                    "stage": res["stage"],
                    "note": res["note"] or res["detail"],
                    "url": res.get("url", ""),
                },
                "error": "" if res["reachable"] else (res["detail"] or res["note"]),
            })

        if action == "set_manifest":
            url = (payload.get("url") or "").strip()
            if url and not update_check.is_https(url):
                return self._json(400, {"error": "清单地址必须使用 HTTPS（http:// 一律拒收）"})
            if "update" not in cfg:
                cfg["update"] = {}
            old_host = (cfg["update"].get("trusted_host") or "").strip().lower()
            cfg["update"]["manifest_url"] = url
            host = update_check.host_of(url)
            # TOFU：换域名不当场生效。先回 need_confirm，前端拿到完整域名让用户确认，
            # 确认过的 check_update 带 confirm_host 再来一次才会写 trusted_host
            need_confirm = bool(host) and host != old_host
            cfg["update"]["last_result"] = ("待确认发布源域名：%s" % host) if need_confirm else ""
            if not host:
                cfg["update"]["trusted_host"] = ""
                cfg["update"].pop("manifest", None)
                cfg["update"]["has_update"] = False
            save_settings(cfg)
            return self._json(200, {"ok": True, "host": host, "need_confirm": need_confirm,
                                    "changed_from": old_host if (host and old_host) else ""})

        if action == "check_update":
            cfg, res = update_check.apply_check(
                cfg, VERSION, confirm_host=(payload.get("confirm_host") or "").strip())
            save_settings(cfg)
            out = {k: res.get(k) for k in ("ok", "code", "error", "need_confirm", "host",
                                          "has_update", "latest", "note", "channel",
                                          "released_at")}
            out["current"] = VERSION
            out["dismissed"] = cfg["update"].get("dismissed_version", "")
            if res["ok"]:
                out["ends"] = res["manifest"]["ends"]
            return self._json(200, out)

        if action == "dismiss_update":
            version = (payload.get("version") or "").strip()
            if "update" not in cfg:
                cfg["update"] = {}
            cfg["update"]["dismissed_version"] = version
            if version and cfg["update"].get("latest_version") == version:
                cfg["update"]["has_update"] = False
            save_settings(cfg)
            return self._json(200, {"ok": True, "dismissed_version": version})

        if action == "set_autostart":
            from hosts.windows_setup import set_autostart, autostart_enabled
            enable = bool(payload.get("enable", False))
            ok, err = set_autostart(enable, data_dir=str(server.DATA))
            if not ok:
                return self._json(500, {"error": err})
            cfg["autostart"] = enable
            save_settings(cfg)
            return self._json(200, {"ok": True, "autostart": autostart_enabled()})

        if action == "set_shortcut":
            from hosts.windows_setup import set_shortcut, shortcut_exists
            enable = bool(payload.get("enable", False))
            ok, err = set_shortcut(enable, data_dir=str(server.DATA))
            if not ok:
                return self._json(500, {"error": err})
            cfg["shortcut"] = enable
            save_settings(cfg)
            return self._json(200, {"ok": True, "shortcut": shortcut_exists()})

        if action == "set_data_dir":
            from hosts.windows_setup import pick_data_dir
            new_dir = (payload.get("dir") or "").strip()
            if not new_dir:
                return self._json(400, {"error": "缺少 dir 参数"})
            ok, err, new_db = pick_data_dir(new_dir)
            if not ok:
                return self._json(500, {"error": err})
            # ok=True 时 err 可能是「旧目录里还剩几个文件」这种提醒，不是失败
            return self._json(200, {"ok": True, "data_dir": new_dir,
                                    "db": new_db, "note": err or ""})

        if action == "get_shortcut_state":
            from hosts.windows_setup import autostart_enabled, shortcut_exists, read_pointer
            target, ptr_err, _ = read_pointer()
            return self._json(200, {
                "ok": True,
                "autostart": autostart_enabled(),
                "shortcut": shortcut_exists(),
                "pointer_target": str(target) if target else None,
                "pointer_error": ptr_err,
            })

        if action == "set_domain":
            domain = (payload.get("domain") or "").strip()
            if domain:
                cfg["domain"] = domain
                save_settings(cfg)
            return self._json(200, {"ok": True, "domain": domain})

        if action == "mark_domain_prompted":
            cfg["domain_prompted"] = True
            save_settings(cfg)
            return self._json(200, {"ok": True})

        if action == "reset_firstrun":
            firstrun_path = server.DATA / "firstrun.json"
            deleted = False
            if firstrun_path.is_file():
                try:
                    firstrun_path.unlink()
                    deleted = True
                except OSError:
                    pass
            return self._json(200, {"ok": True, "deleted": deleted,
                                    "note": "下次启动时会重新弹出首启对话框" if deleted else "没有找到 firstrun.json"})

        return self._json(400, {"error": "未知操作：%s" % action})


def start_admin(port):
    ips = {"127.0.0.1", "localhost", "::1", "[::1]"}
    AdminHTTP.allowed_hosts = {"%s:%d" % (h, port) for h in ips if h != "[::1]"} | {"[::1]:%d" % port}
    origins = {"http://%s:%d" % (h, port) for h in ("127.0.0.1", "localhost")} | {"http://[::1]:%d" % port}
    AdminHTTP.allowed_origins = {o.lower() for o in origins}
    srv = ThreadingHTTPServer(("127.0.0.1", port), AdminHTTP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- #
# 对外单端口
# --------------------------------------------------------------------------- #
def waiting_page(admin_port):
    body = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>PubChat 尚未初始化</title>"
        "<body style=\"font:15px/1.7 system-ui;margin:0;display:grid;place-items:center;"
        "min-height:100vh;background:#f6f7f9;color:#222\">"
        "<div style=\"max-width:420px;padding:28px;background:#fff;border-radius:14px;"
        "box-shadow:0 8px 30px rgba(0,0,0,.08)\">"
        "<h2 style=\"margin:0 0 8px\">聊天室还没初始化</h2>"
        "<p>请在这台电脑上打开 PubChat 控制台（启动时自动弹出，"
        "或用启动窗口里的一次性管理链接）完成管理员设置。</p>"
        "<p style=\"color:#888\">PubChat 本机版</p></div></body></html>"
    ).encode("utf-8")
    return 503, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}, body


def make_process_request(admin_port):
    """对外口只放行聊天页需要的文件，以及 /mobile/ 下的手机版静态资源。
    用文件名的白名单而不是 URL 黑名单：Windows 大小写不敏感、`..` 和编码变体
    都绕不过这一层，管理页/向导/管理 API 永远不会从公网口漏出去。"""
    public_files = {"index.html", "app.js", "config.js", "style.css",
                    "weui-tokens.css", "join.html", "m.html",
                    # 管理页（settings.html + 子页）：鉴权靠 owner 账号的一级 Key，
                    # 没有 key1 进来也只是个 401 的空壳，放行安全
                    "settings.html", "instances.html", "server.html",
                    "manage.html", "manage.js",
                    "splash.js", "guide.js", "notice.js"}

    def json_response(code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return Response(code, REASON.get(code, "OK"),
                        Headers([("Content-Type", "application/json; charset=utf-8"),
                                 ("Content-Length", str(len(body))),
                                 ("Connection", "close")]),
                        body)

    def process_request(connection, request):
        path = urlparse(request.path).path
        if path in ("/ws", "/pub/ws"):
            return None                      # 交给 websockets 升级
        if path == "/healthz":
            return json_response(200, {"ok": True, "online": len(server.hub.clients)})
        if path == "/api/instance":
            inst = server.get_instance()
            return json_response(200, {
                "ok": True,
                "name": inst.get("name", "PubChat"),
                "fingerprint": inst["instance_id"][:12],
            })
        # 更新公告：与公网宿主（hosts/server.py）同一套语义——没文件/空 → 204，
        # 有 → 200 原样透传，不加工；本机这边认 server.DATA，这样「数据目录搬家」
        # 以后公告仍然跟着实例走。
        if path == "/api/notice":
            raw = None
            try:
                notice = Path(server.DATA) / "notice.json"
                if notice.is_file():
                    raw = notice.read_text(encoding="utf-8")
            except Exception:
                raw = None
            if not raw:
                return Response(204, "No Content",
                                Headers([("Content-Length", "0"), ("Connection", "close")]), b"")
            try:
                obj = json.loads(raw)
            except Exception:
                return json_response(500, {"error": "notice_malformed"})
            if not isinstance(obj, dict):
                return json_response(500, {"error": "notice_malformed"})
            return json_response(200, obj)
        # 手机版 H5 的构建产物把资源写成根路径 /app/static/...（uni-app 默认
        # 打包口径）。原包不动，这里把 /app/... 映射到 /mobile/app/...，
        # 仍然受下面目录边界检查约束。
        if path == "/app" or path.startswith("/app/"):
            path = "/mobile" + path
        # 手机版 UI（原样复用桌面安装包里的 H5）：只托管 /mobile/ 目录内的
        # 静态文件；web_asset 会规范化 ..，这里再按真实路径卡一次目录边界，
        # 免得 /mobile/../admin.html 这种变体从对外口漏出管理页。
        if path.startswith("/mobile/"):
            target = server.web_asset(path)
            inside = False
            if target is not None and target.is_file():
                try:
                    target.resolve().relative_to((server.WEB / "mobile").resolve())
                    inside = True
                except ValueError:
                    inside = False
            if not inside:
                return json_response(404, {"error": "not found"})
            code, headers, body = server.static_response(path)
            # 手机版首页：桌面聊天已经在同一 origin 存了 pubchat.user/key 的，
            # 这里补一份手机版认的 pubchat.session，切过去就不用再登录一次。
            # 只改响应字节，web/mobile 里的原包不动。
            if code == 200 and path == "/mobile/app/index.html":
                page = body.decode("utf-8", "replace")
                seed = (
                    "<script>(function(){try{"
                    "if(localStorage.getItem('pubchat.session'))return;"
                    "var u=localStorage.getItem('pubchat.user'),"
                    "k=localStorage.getItem('pubchat.key');"
                    "if(!u||!k)return;"
                    "localStorage.setItem('pubchat.session',JSON.stringify("
                    "{type:'object',data:{server:location.origin,"
                    "user:u,key:k,name:u}}));"
                    "}catch(e){}})();</script>"
                )
                if "<head>" in page:
                    page = page.replace("<head>", "<head>" + seed, 1)
                else:
                    page = seed + page
                body = page.encode("utf-8")
            headers = dict(headers)
            headers["Content-Length"] = str(len(body))
            headers["Connection"] = "close"
            return Response(code, REASON.get(code, "OK"), Headers(list(headers.items())), body)
        target = server.web_asset(path)
        if target is None or not target.is_file() or target.name.lower() not in public_files:
            return json_response(404, {"error": "not found"})
        if target.name.lower() == "index.html" and not server.STORE.users():
            code, headers, body = waiting_page(admin_port)
        else:
            code, headers, body = server.static_response(path)
        headers = dict(headers)
        headers["Content-Length"] = str(len(body))
        headers["Connection"] = "close"
        return Response(code, REASON.get(code, "OK"), Headers(list(headers.items())), body)

    return process_request


async def run_public(host, port, admin_port, stop_event=None, started=None):
    try:
        server.hub.loop = asyncio.get_running_loop()
    except Exception:
        pass
    async with server.ws_serve(server.handler, host, port,
                               process_request=make_process_request(admin_port)):
        if started is not None:
            started.set()
        if stop_event is None:
            await asyncio.Future()
        else:
            while not stop_event.is_set():
                await asyncio.sleep(0.25)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def remote_target(cfg):
    """部署模式是 remote 且填了地址 → 返回该地址；否则 None（本机部署）。"""
    if (cfg or {}).get("mode") != "remote":
        return None
    return str((cfg or {}).get("remote_url") or "").strip() or None


def mode_runtime(cfg):
    """
    把「设置里选的模式」和「此刻真实的运行状态」一起回给前端。

    模式切换要重启才生效（监听不监听对外口是启动时定的），所以必须让前端能看出
    「已经改了但还没生效」，否则用户以为切完了，实际还在连本机。
    """
    want_remote = bool(remote_target(cfg))
    return {
        "mode": (cfg or {}).get("mode", "local"),
        "remote_url": (cfg or {}).get("remote_url", ""),
        "serving": bool(SERVING),
        "restart_needed": want_remote == bool(SERVING),
    }


def _background_update_check():
    """
    启动后在后台查一次发布源（每日一次，设计稿 §6.4）。

    放线程里跑是的实现提醒：不能拖窗口启动时间。
    失败只写 update.last_result，「关于」页显示，不弹窗、不打扰（§8）。
    """
    try:
        cfg = load_settings()
        if not update_check.start_check(cfg, VERSION):
            return
        cfg, res = update_check.apply_check(cfg, VERSION)
        save_settings(cfg)
        if res.get("need_confirm"):
            print("[pubchat] 更新通道待用户确认发布源域名：%s" % res.get("host"), flush=True)
        elif res.get("ok") and res.get("has_update"):
            print("[pubchat] 发现新版本 v%s（当前 v%s），请到「关于」页查看"
                  % (res.get("latest"), VERSION), flush=True)
    except Exception as exc:                       # 更新通道坏了不能影响主流程
        print("[pubchat] 更新检查异常，已忽略：%s" % exc, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="PubChat 本机版")
    ap.add_argument("--port", type=int, default=8765, help="对外端口（页面 + /ws），默认 8765")
    ap.add_argument("--admin-port", type=int, default=0, help="本机管理端口，默认对外端口 +1")
    ap.add_argument("--host", default="0.0.0.0", help="对外绑定地址，默认 0.0.0.0（局域网可访问）")
    ap.add_argument("--data-dir", default="", help="数据目录（默认 %%LOCALAPPDATA%%\\PubChat）")
    ap.add_argument("--web-dir", default="", help="静态目录（一般不用改）")
    ap.add_argument("--cloudflared", default="", help="cloudflared.exe 路径（默认自动查找）")
    ap.add_argument("--import", dest="import_db", default="",
                    help="把老 pubchat 数据库导进来（覆盖前自动备份，导入后直接启动）")
    ap.add_argument("--portable", action="store_true", help="数据放 exe 同级 pubchat-data/")
    ap.add_argument("--no-browser", action="store_true", help="启动时不自动开浏览器")
    ap.add_argument("--gui", dest="gui", action="store_true", default=None,
                    help="打开桌面窗口（内置聊天 + 设置，打包 exe 默认开）")
    ap.add_argument("--no-gui", dest="gui", action="store_false", default=None,
                    help="不开窗口，只跑服务（测试/命令行用）")
    ap.add_argument("--classic-gui", action="store_true",
                    help="改用旧的 tkinter 控制台窗口（内置窗口打不开时兜底）")
    ap.add_argument("--local", action="store_true",
                    help="强制本机部署模式（忽略设置里的云端部署）；桌面壳「本机部署」用")
    args = ap.parse_args(argv)

    global TUNNEL
    admin_port = args.admin_port or (args.port + 1)
    AdminHTTP.public_port = args.port
    data_dir, data_err = resolve_data_dir(args)
    if data_err:
        # 报错到 UI 措辞，别只抛 OSError
        _fatal_popup("PubChat 数据目录错误",
                     "%s\n\n请在设置里重新选择数据目录，或用 --data-dir 参数指定。" % data_err)
        return 4
    web_dir = resolve_web_dir(args)
    if not (web_dir / "index.html").is_file():
        _fatal_popup("PubChat 启动失败", "找不到静态目录：%s" % web_dir)
        return 2

    server.set_runtime(data_dir, web_dir, root_dir=data_dir)
    if args.import_db:
        try:
            backup = install_db_file(Path(args.import_db).expanduser())
        except ValueError as exc:
            _fatal_popup("导入旧数据失败", str(exc))
            return 5
        print("[pubchat] 已导入 %s%s" % (
            args.import_db, "（旧库备份：%s）" % backup if backup else ""), flush=True)
    server.boot()
    # 桌面壳「本机部署」：用户明确选了本机，这里把「云端部署」设置改回 local，
    # 否则老数据目录 settings.json 里的 mode=remote 会让本机服务不监听对外口。
    if args.local:
        cfg = load_settings()
        if cfg.get("mode") != "local" or str(cfg.get("remote_url") or "").strip():
            cfg["mode"] = "local"
            cfg["remote_url"] = ""
            save_settings(cfg)
    # 部署模式启动时就定下来：remote 是纯客户端，本机不再监听对外口，免得
    # "以为连的是服务器、其实本机又起了一个空实例" 两套并存。
    # 管理口（8766）任何模式都常驻 —— 版本、更新检查、部署设置归本机管，
    # 不能依赖别人服务器活着。
    remote_url = remote_target(load_settings())
    TUNNEL = tunnel.Tunnel(args.port, args.cloudflared or None,
                           extra_dirs=[str(data_dir)],
                           config_path=data_dir / "tunnel.json")

    try:
        start_admin(admin_port)
    except OSError as exc:
        _fatal_popup("PubChat 启动失败",
                     "管理口 %d 启动失败：%s\n\n端口可能被占用，换一个试试：--port 9000"
                     % (admin_port, exc))
        return 3
    admin_link, _ = generate_admin_link(admin_port, data_dir)

    gui_on = args.gui if args.gui is not None else bool(getattr(sys, "frozen", False))
    if args.classic_gui and args.gui is not False:
        gui_on = True
    appwin_mod = None
    gui_mod = None
    if gui_on:
        if not args.classic_gui:
            try:
                import appwin as appwin_mod
            except Exception as exc:
                print("[pubchat] 单窗口模块加载失败：%s" % exc, flush=True)
        try:
            import gui as gui_mod
        except Exception as exc:
            print("[pubchat] 控制台模块加载失败：%s" % exc, flush=True)
        if appwin_mod is None and gui_mod is None:
            _fatal_popup("图形界面加载失败", "将退回命令行模式运行。")
            gui_on = False

    print("=" * 52, flush=True)
    print(" PubChat %s" % VERSION, flush=True)
    if remote_url:
        print(" 部署模式: 服务器部署（纯客户端），连 %s" % remote_url, flush=True)
        print("           本机不监听对外口 %d，公网隧道不启动" % args.port, flush=True)
    else:
        print(" 部署模式: 本机部署，局域网 http://<本机IP>:%d/" % args.port, flush=True)
        for ip in lan_ips():
            print("          http://%s:%d/" % (ip, args.port), flush=True)
    print(" 本机管理: http://127.0.0.1:%d/admin.html" % admin_port, flush=True)
    print("           （一次性认领链接不在终端打印，已写入 %s，浏览器会自动打开）"
          % (data_dir / "admin-open.json"), flush=True)
    print("           （无凭证访问：API 给 401，页面给「粘贴管理链接」引导）", flush=True)
    print(" 数据目录: %s" % data_dir, flush=True)
    print(" 公网: %s / %s" % (TUNNEL.mode,
                              "cloudflared 已找到" if TUNNEL.binary else "未找到 cloudflared.exe"), flush=True)
    print(" 关闭窗口或按 Ctrl+C 即停止服务", flush=True)
    print("=" * 52, flush=True)

    if remote_url:
        # 纯客户端模式不把自己的端口暴露到公网：隧道转的是本机 8765，
        # 本机既然没监听，起了也只是多开一条对外通道。
        print("[pubchat] 服务器部署模式：跳过公网隧道", flush=True)
    else:
        TUNNEL.autostart()
    threading.Thread(target=_background_update_check, daemon=True,
                     name="pubchat-update").start()

    if gui_on:
        stop_event = threading.Event()
        started = threading.Event()
        served = threading.Event()
        boot_error = {}

        def _serve():
            if remote_url:
                # 纯客户端模式：不占对外端口，窗口只跑管理口 + 远端页面
                _set_serving(False)
                started.set()
                served.set()
                return
            try:
                asyncio.run(run_public(args.host, args.port, admin_port,
                                       stop_event, started))
            except BaseException as exc:      # 端口被占用等，带回 GUI 显示
                boot_error["error"] = exc
                started.set()
            finally:
                served.set()

        threading.Thread(target=_serve, daemon=True, name="pubchat-public").start()
        started.wait(10)
        if boot_error.get("error"):
            msg = "%s\n\n端口可能被占用，换一个试试：--port 9000" % boot_error["error"]
            if gui_mod is not None:
                gui_mod.show_error("PubChat 启动失败", msg)
            else:
                _fatal_popup("PubChat 启动失败", msg)
            TUNNEL.stop()
            return 4
        try:
            opened = False
            if appwin_mod is not None:
                # 只传 appwin.run 真收的参数。它以前还带着 admin_token/public_port，
                # 而 appwin.run 的签名里没有这两个——frozen exe 走到这里直接
                # TypeError，而这层 try 只有 finally，窗口打不开也不会有任何提示。
                # 一次性管理链接走 <数据目录>/admin-open.json 交接，聊天端口走 /api/state。
                opened = appwin_mod.run(admin_port=admin_port, data_dir=data_dir,
                                        version=VERSION, stop_event=stop_event,
                                        tunnel=TUNNEL)
            if not opened and gui_mod is not None:
                try:
                    # 内置窗口可能已经用掉了当前 token，回退前重新发一条：
                    # 发链接这件事本身就是目的（写进交接文件），gui.run 不接这个串。
                    generate_admin_link(admin_port, data_dir)
                    gui_mod.run(port=args.port, admin_port=admin_port, version=VERSION,
                                data_dir=data_dir, tunnel=TUNNEL, stop_event=stop_event,
                                import_db=install_db_file, lan_ips=lan_ips)
                    opened = True
                except Exception as exc:
                    print("[pubchat] 控制台窗口启动失败：%s" % exc, flush=True)
            if not opened:
                _fatal_popup("无法打开窗口",
                             "系统缺少 WebView2 运行库或图形环境。\n"
                             "可安装 Microsoft Edge WebView2 Runtime 后重试，"
                             "或用 --no-gui 在命令行运行。")
                return 6
        finally:
            stop_event.set()
            served.wait(5)                   # 先让 WebSocket 断开，再关数据库
            TUNNEL.stop()
            try:
                server.STORE.conn.close()
            except Exception:
                pass
        return 0

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(admin_link)).start()

    if remote_url:
        # 命令行 + 服务器部署：管理口在守护线程里跑，主线程只负责挂着等 Ctrl+C
        _set_serving(False)
        print("[pubchat] 服务器部署模式：本机不监听 %d，管理口见 http://127.0.0.1:%d/admin.html"
              "（一次性链接在 %s）"
              % (args.port, admin_port, data_dir / "admin-open.json"), flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        TUNNEL.stop()
        try:
            server.STORE.conn.close()
        except Exception:
            pass
        return 0

    try:
        asyncio.run(run_public(args.host, args.port, admin_port))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print("[pubchat] 对外端口 %d 启动失败：%s" % (args.port, exc), flush=True)
        print("[pubchat] 端口可能被占用，换一个试试：--port 9000", flush=True)
        return 4
    finally:
        TUNNEL.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
