#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PubChat 公网多实例宿主。

一个对外口：页面 + /ws（反代后面）
一个回环口：认领页 + /api/claim + /api/state + /api/admin/*（只绑 127.0.0.1）

部署口径：
- 首次启动（数据目录里没有可用管理设备）终端打印 5 分钟一次性认领链接 + 6 位码；
- 手机/电脑打开链接、输码，换长期设备凭证；之后管理只认 Bearer 设备凭证；
- 反代把 /api/ 与 /claim 指到回环管理口，把其余页面与 /ws 指到对外口。
- 管理 API 在服务器模式强制要求 X-Forwarded-Proto: https（nginx 的请求也来自
  回环，只认回环不足以区分直连与反代）；反代不设这个头会一律 403。
"""
import argparse
import asyncio
import json
import os
import re
import socket
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from websockets.asyncio.server import serve as ws_serve
except Exception:  # pragma: no cover
    from websockets.server import serve as ws_serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from core.http import static_response, web_asset
from core.store import Store, jd, now_iso
from core.ws import Hub, handler as ws_handler
from hosts import server_admin

REASON = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
          404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
          413: "Payload Too Large", 429: "Too Many Requests",
          500: "Internal Server Error", 503: "Service Unavailable"}

PUBLIC_FILES = {
    "index.html", "app.js", "config.js", "style.css", "weui-tokens.css",
    "join.html", "claim.html", "device.js", "m.html",
    "admin.html", "admin.js", "members.html", "members.js", "qrcode.js",
    # 0.8.17 管理页恢复成 0.8.10 那套（settings.html + 六个子页），
    # 权限改由 owner 账号的一级 Key 授权，所以这些页面放公网口是安全的
    "settings.html", "instances.html", "server.html",
    "manage.html", "manage.js",
    # S3/S4 的三份共用组件：漏一个，云端实例上开屏与四步引导就静默消失
    # （app.js 里有兜底不会白屏，但功能等于没上）。
    "splash.js", "guide.js", "notice.js",
}


def read_version():
    """版本号只维护在 hosts/local.py 一处，这里按源码取值，避免两份漂移。"""
    p = Path(__file__).resolve().parent.parent / "hosts" / "local.py"
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', p.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


VERSION = read_version()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def public_base(admin_port, env_public=""):
    """终端链接的基地址：优先 PUBCHAT_PUBLIC_URL（反代域名），否则回环管理口。"""
    base = (env_public or os.environ.get("PUBCHAT_PUBLIC_URL") or "").strip()
    if base:
        return base.rstrip("/")
    return "http://127.0.0.1:%d" % admin_port


# --------------------------------------------------------------------------- #
# 回环管理面（认领 + 管理 API）
# --------------------------------------------------------------------------- #
class AdminHTTP(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class AdminRequestHandler(BaseHTTPRequestHandler):
    server_version = "pubchat-admin/1.0"
    store = None
    hub = None
    data_dir = Path("data")
    web_dir = Path("web")
    public_port = 0
    admin_port = 0
    instance_getter = None
    instance_saver = None

    def log_message(self, *a):
        pass

    def _send(self, code, headers, body):
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, {"Content-Type": "application/json; charset=utf-8"},
                   jd(obj).encode("utf-8"))

    def _page(self, name):
        code, headers, body = static_response("/" + name, self.web_dir)
        return self._send(code, headers, body)

    def _peer(self):
        return self.client_address[0] if self.client_address else "unknown"

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, (400, {"error": "Content-Length 不合法"})
        if n <= 0:
            return {}, None
        if n > server_admin.BODY_LIMIT:
            return None, (413, {"error": "请求体太大"})
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw or b"{}")
        except ValueError:
            return None, (400, {"error": "请求体不是合法 JSON"})
        if not isinstance(obj, dict):
            return None, (400, {"error": "请求体必须是对象"})
        return obj, None

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/claim", "/claim/"):
            return self._page("claim.html")
        if path == "/healthz":
            return self._json(200, {"ok": True, "ts": now_iso(),
                                    "online": len(self.hub.clients),
                                    "role": "admin"})
        if path in ("/api/state",) or path.startswith("/api/admin/"):
            code, obj = server_admin.handle_admin(
                self.store, self.hub, self.data_dir, self.web_dir,
                self.instance_getter, self.instance_saver,
                "GET", path, {}, self.headers, self._peer(),
                VERSION, self.public_port, self.admin_port)
            return self._json(code, obj)
        if path in ("", "/", "/index.html", "/admin.html"):
            return self._page("admin.html")
        target = web_asset(path, self.web_dir)
        if target is None or not target.is_file():
            return self._json(404, {"error": "not found"})
        return self._page(str(target.relative_to(self.web_dir.resolve())).replace("\\", "/"))

    def do_POST(self):
        path = urlparse(self.path).path
        payload, err = self._read_json()
        if err:
            return self._json(*err)
        if path == "/api/claim":
            ip = server_admin.real_ip(self._peer(), self.headers)
            code, obj = server_admin.handle_claim(
                self.store, self.data_dir, payload, ip,
                self.headers.get("User-Agent") or "")
            return self._json(code, obj)
        if path == "/api/state" or path.startswith("/api/admin/"):
            code, obj = server_admin.handle_admin(
                self.store, self.hub, self.data_dir, self.web_dir,
                self.instance_getter, self.instance_saver,
                "POST", path, payload, self.headers, self._peer(),
                VERSION, self.public_port, self.admin_port)
            return self._json(code, obj)
        return self._json(404, {"error": "not found"})


def start_admin(store, hub, data_dir, web_dir, admin_port, public_port,
                instance_getter, instance_saver):
    AdminRequestHandler.store = store
    AdminRequestHandler.hub = hub
    AdminRequestHandler.data_dir = Path(data_dir)
    AdminRequestHandler.web_dir = Path(web_dir)
    AdminRequestHandler.public_port = public_port
    AdminRequestHandler.admin_port = admin_port
    AdminRequestHandler.instance_getter = staticmethod(instance_getter)
    AdminRequestHandler.instance_saver = staticmethod(instance_saver)
    srv = AdminHTTP(("127.0.0.1", admin_port), AdminRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="pubchat-admin").start()
    return srv


# --------------------------------------------------------------------------- #
# 对外单端口
# --------------------------------------------------------------------------- #
def make_process_request(hub, web_dir, inst_getter):
    web_dir = Path(web_dir)

    def json_response(code, obj):
        body = jd(obj).encode("utf-8")
        return Response(code, REASON.get(code, "OK"),
                        Headers([("Content-Type", "application/json; charset=utf-8"),
                                 ("Content-Length", str(len(body))),
                                 ("Connection", "close")]),
                        body)

    def static_reply(path):
        code, headers, body = static_response(path, web_dir)
        headers = dict(headers)
        headers["Content-Length"] = str(len(body))
        headers["Connection"] = "close"
        return Response(code, REASON.get(code, "OK"),
                        Headers(list(headers.items())), body)

    def process_request(connection, request):
        path = urlparse(request.path).path
        if path in ("/ws", "/pub/ws"):
            return None                      # 交给 websockets 升级
        if path == "/healthz":
            return json_response(200, {"ok": True, "online": len(hub.clients)})
        if path in ("/api/instance", "/api/meta"):
            return json_response(200, server_admin.public_meta(inst_getter(), VERSION))
        if path in ("/claim", "/claim/"):
            return static_reply("/claim.html")
        if path == "/api/notice":
            notice = Path(web_dir).parent / "data" / "notice.json"
            raw = None
            try:
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
        target = web_asset(path, web_dir)
        if target is None or not target.is_file() or target.name.lower() not in PUBLIC_FILES:
            return json_response(404, {"error": "not found"})
        return static_reply(path)

    return process_request


async def run_public(host, port, store, hub, web_dir, inst_getter, inst_saver=None):
    async def handler(ws):
        inst = inst_getter()
        settings = {"require_approval": bool(inst.get("require_approval")),
                    "allow_room_create": bool(inst.get("allow_room_create", True))}
        # 0.8.24：settings_get/settings_set 帧需要读写 instance.json，从这里注入。
        await ws_handler(ws, store, hub, print, settings,
                         inst_getter=inst_getter, inst_saver=inst_saver)

    return await ws_serve(handler, host, port,
                          process_request=make_process_request(hub, web_dir, inst_getter))



# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="PubChat 公网宿主")
    ap.add_argument("--host", default="0.0.0.0", help="对外绑定地址，默认 0.0.0.0")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PUBCHAT_PORT") or 8865),
                    help="对外端口（默认 PUBCHAT_PORT 或 8865）")
    ap.add_argument("--admin-port", type=int,
                    default=int(os.environ.get("PUBCHAT_ADMIN_PORT") or 0),
                    help="回环管理口（默认对外端口 +1，只绑 127.0.0.1）")
    ap.add_argument("--data-dir", default=os.environ.get("PUBCHAT_DATA") or "data")
    ap.add_argument("--web-dir", default=os.environ.get("PUBCHAT_WEB") or "web")
    ap.add_argument("--new-claim", action="store_true",
                    help="强制生成新的认领链接并继续运行（救援用；先停掉旧进程）")
    ap.add_argument("--pub-url", default=os.environ.get("PUBCHAT_PUBLIC_URL") or "",
                    help="服务器已有公网地址（如 https://chat.example.com）。"
                         "首次启动会询问；认领链接用它拼出来")
    ap.add_argument("--trust-proxy", action="store_true",
                    help="信任 X-Forwarded-Proto 头（仅当反代已校验 HTTPS 时开启）")
    args = ap.parse_args(argv)

    if args.trust_proxy:
        server_admin.TRUST_PROXY = True
    server_admin.SERVER_MODE = True

    data_dir = Path(args.data_dir).expanduser().resolve()
    web_dir = Path(args.web_dir).expanduser().resolve()
    admin_port = args.admin_port or (args.port + 1)
    if admin_port == args.port:
        print("[pubchat] 管理口不能和对外口相同", flush=True)
        return 2
    if not (web_dir / "index.html").is_file() and args.web_dir == "web":
        # 源码布局把 web/ 放在 src/ 下：从仓库根目录（或任意目录）启动时按脚本位置兜底
        alt = Path(__file__).resolve().parent.parent / "web"
        if (alt / "index.html").is_file():
            web_dir = alt
    if not (web_dir / "index.html").is_file():
        print("[pubchat] 找不到静态目录：%s" % web_dir, flush=True)
        return 2
    data_dir.mkdir(parents=True, exist_ok=True)

    store = Store(data_dir / "pubchat.db")
    hub = Hub(store)
    server_admin.load_instance(data_dir)

    def get_inst():
        return server_admin.load_instance(data_dir)

    def save_inst(obj):
        server_admin.save_instance(data_dir, obj)

    # 服务器已有公网地址（反代域名）。首次启动交互问一次，填过就记进 instance.json。
    pub_url = (args.pub_url or os.environ.get("PUBCHAT_PUBLIC_URL") or "").strip()
    _inst = server_admin.load_instance(data_dir)
    if not pub_url and _inst.get("public_url"):
        pub_url = _inst["public_url"]
    if not pub_url and sys.stdin.isatty():
        print("[pubchat] 首次启动：请填写服务器已有的公网地址（反代域名）。", flush=True)
        print("[pubchat] 例如 https://chat.example.com；直接回车可跳过，之后启动会再问。", flush=True)
        try:
            _ans = input("[pubchat] 公网地址：").strip()
        except (EOFError, KeyboardInterrupt):
            _ans = ""
        if _ans:
            print("[pubchat] 确认绑定到这个地址：%s ？ [y/N]" % _ans, flush=True)
            try:
                _ok = input().strip().lower() in ("y", "yes", "是", "确认")
            except (EOFError, KeyboardInterrupt):
                _ok = False
            if _ok:
                pub_url = _ans
                _inst = server_admin.load_instance(data_dir)
                _inst["public_url"] = pub_url
                server_admin.save_instance(data_dir, _inst)
                os.environ["PUBCHAT_PUBLIC_URL"] = pub_url
                print("[pubchat] 已绑定公网地址：%s" % pub_url, flush=True)
            else:
                print("[pubchat] 已取消，本次用回环地址生成认领链接", flush=True)
    base = public_base(admin_port, pub_url)

    # 先占住端口再生成 bootstrap：6 位码只存在本进程内存里，旧的
    # `--new-claim 生成后退出` 做法会让正在跑的进程拿不到码（认领必然失败）。
    try:
        start_admin(store, hub, data_dir, web_dir, admin_port, args.port,
                    get_inst, save_inst)
    except OSError as exc:
        print("[pubchat] 回环管理口 %d 启动失败：%s（先停掉占用端口的旧进程）"
              % (admin_port, exc), flush=True)
        store.conn.close()
        return 4
    server_admin.startup_bootstrap(store, print, base, force=args.new_claim)
    if args.new_claim:
        print("[pubchat] --new-claim：旧链接已作废，新链接和码只在本进程有效",
              flush=True)

    print("[pubchat] 对外口 %s:%d  /ws + 页面" % (args.host, args.port), flush=True)
    print("[pubchat] 回环管理口 127.0.0.1:%d  /claim + /api/claim + /api/admin/*"
          % admin_port, flush=True)
    print("[pubchat] 反代请把 /api/ 与 /claim 指到回环管理口，其余页面与 /ws 指到对外口",
          flush=True)
    print("[pubchat] 反代必须设 X-Forwarded-Proto: https，否则管理 API 一律 403"
          "（直连 127.0.0.1 也一样）", flush=True)
    print("[pubchat] 局域网直连地址 http://%s:%d/" % (lan_ip(), args.port), flush=True)

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_public(args.host, args.port, store, hub,
                                           web_dir, get_inst, save_inst))
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print("[pubchat] 对外端口 %d 启动失败：%s" % (args.port, exc), flush=True)
        return 4
    finally:
        try:
            store.conn.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
