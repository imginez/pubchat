"""
PubChat HTTP 层。

web_asset()、static_response()、HTTP base class。宿主无关。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from core.store import now_iso, jd

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


def web_asset(path, web_dir: Path):
    """把 URL 路径映射到 web/ 下的真实文件；越界返回 None。"""
    rel = (path or "/").lstrip("/")
    if rel in ("", "index.html"):
        rel = "index.html"
    base = web_dir.resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def static_response(path, web_dir: Path):
    """静态文件响应：返回 (code, headers, body)。"""
    target = web_asset(path, web_dir)
    if target is None:
        return 403, {"Content-Type": CTYPES["json"]}, b'{"error":"forbidden"}'
    if not target.is_file():
        return 404, {"Content-Type": CTYPES["json"]}, b'{"error":"not found"}'
    body = target.read_bytes()
    ctype = CTYPES.get(target.suffix.lstrip(".").lower(), "application/octet-stream")
    return 200, {"Content-Type": ctype, "Cache-Control": "no-store"}, body


class HTTP(BaseHTTPRequestHandler):
    """HTTP 基类，宿主可继承并扩展。"""
    server_version = "pubchat/1.0"
    web_dir = Path("web")
    hub = None

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
            rel = p.resolve().relative_to(self.web_dir.resolve())
        except Exception:
            rel = Path(str(path).lstrip("/"))
        code, headers, body = static_response("/" + str(rel).replace("\\", "/"), self.web_dir)
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            online = len(self.hub.clients) if self.hub else 0
            return self._json(200, {"ok": True, "ts": now_iso(), "online": online})
        if path in ("/", "/index.html"):
            return self._file(self.web_dir / "index.html")
        target = web_asset(path, self.web_dir)
        if target is None:
            return self._json(403, {"error": "forbidden"})
        return self._file(target)

    def do_POST(self):
        return self._json(404, {"error": "not found"})
