#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公网地址：临时快速隧道 / 固定域名隧道 / 已有公网地址。

三种模式（配置存在数据目录 tunnel.json，Token 只落在本机，不回传浏览器）：

  quick   Cloudflare Quick Tunnel：零账号，https://xxxx.trycloudflare.com，
          重启就换地址，只适合临时给外面的人用。
  named   Cloudflare Named Tunnel：用自己的域名 + Zero Trust 里创建的隧道 Token，
          地址固定（例如 https://chat.example.com），重启不变。
          本程序只负责运行 cloudflared，不碰你的 DNS/证书。
  manual  你已经有反向代理或公网地址：只填地址，程序负责展示和出二维码。

cloudflared 不随 exe 打包（本体 55MB）。查找顺序：
  1. --cloudflared 指定的路径
  2. exe 同级 cloudflared.exe
  3. 数据目录下的 cloudflared.exe
  4. PATH
  5. Windows 常见安装目录

官方下载页：
  https://github.com/cloudflare/cloudflared/releases/latest
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
READY_RE = re.compile(r"Registered tunnel connection|Connection .* registered|Registered tunnel")
CANDIDATES = [
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\cloudflared\cloudflared.exe"),
]
MODES = ("quick", "named", "manual")


def find_cloudflared(explicit=None, extra_dirs=()):
    if explicit and os.path.isfile(explicit):
        return explicit
    here = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__)
    local = os.path.join(here, "cloudflared.exe")
    if os.path.isfile(local):
        return local
    # 单文件 exe 把 cloudflared 一起打进来时，会解压到 _MEIPASS
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass and os.path.isfile(os.path.join(meipass, "cloudflared.exe")):
        return os.path.join(meipass, "cloudflared.exe")
    for d in extra_dirs:
        if d and os.path.isfile(os.path.join(d, "cloudflared.exe")):
            return os.path.join(d, "cloudflared.exe")
    which = shutil.which("cloudflared")
    if which:
        return which
    for p in CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def clean_url(value):
    """固定地址统一成不带结尾斜杠的 http(s)://...；空串表示未填。"""
    s = (value or "").strip()
    if not s:
        return ""
    if not re.match(r"(?i)^https?://", s):
        s = "https://" + s
    return s.rstrip("/")


class Tunnel:
    def __init__(self, port, cloudflared=None, extra_dirs=(), config_path=None):
        self.port = port
        self.extra_dirs = list(extra_dirs)
        self.binary = find_cloudflared(cloudflared, self.extra_dirs)
        self.config_path = str(config_path) if config_path else None
        self.proc = None
        self.url = None            # quick 隧道拿到的地址
        self.ready = False         # named 隧道是否已连上
        self.error = None
        self.mode = "quick"
        self.token = ""
        self.public_url = ""
        self.auto = False
        self._thread = None
        self._lock = threading.RLock()
        self._load()
        atexit.register(self.stop)

    # ---------------------------------------------------------------- 配置 --
    def _load(self):
        if not self.config_path:
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            return
        except Exception:
            return
        if not isinstance(cfg, dict):
            return
        mode = str(cfg.get("mode") or "quick")
        self.mode = mode if mode in MODES else "quick"
        self.token = str(cfg.get("token") or "")
        self.public_url = clean_url(cfg.get("public_url"))
        self.auto = bool(cfg.get("auto"))

    def _save(self):
        if not self.config_path:
            return
        data = {"mode": self.mode, "token": self.token,
                "public_url": self.public_url, "auto": self.auto}
        folder = os.path.dirname(self.config_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="tunnel-", suffix=".json", dir=folder or None)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.config_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def config(self):
        return {"mode": self.mode, "public_url": self.public_url,
                "has_token": bool(self.token), "auto": self.auto}

    def save_config(self, mode=None, token=None, public_url=None, auto=None,
                    clear_token=False):
        """改配置并落盘。token 为 None 表示沿用；clear_token=True 清空。"""
        with self._lock:
            mode = (mode or self.mode).strip().lower()
            if mode not in MODES:
                raise ValueError("未知的公网模式")
            new_token = "" if clear_token else (
                token.strip() if token is not None and token.strip() else self.token)
            new_url = clean_url(public_url) if public_url is not None else self.public_url
            new_auto = bool(auto) if auto is not None else self.auto
            if mode == "named" and not new_token:
                raise ValueError("固定域名需要 Cloudflare 隧道 Token")
            if mode == "manual" and not new_url:
                raise ValueError("已有公网地址模式需要填地址，例如 https://chat.example.com")
            if self.mode != mode:
                self.stop()
            self.mode = mode
            self.token = new_token
            self.public_url = new_url
            self.auto = new_auto
            self._save()
            return self.config()

    # ---------------------------------------------------------------- 状态 --
    def effective_url(self):
        if self.mode == "quick":
            return self.url
        return self.public_url or None

    def status(self):
        available = bool(self.binary)
        base = {"available": available, "mode": self.mode,
                "public_url": self.public_url, "has_token": bool(self.token),
                "auto": self.auto}
        if self.mode == "manual":
            if self.public_url:
                base.update(state="on", url=self.public_url,
                            detail="由你自己的反向代理/域名提供，本程序不管理连接")
            else:
                base.update(state="off", url=None, detail="还没填公网地址")
            return base
        if self.url:
            base.update(state="on", url=self.url,
                        detail="临时公网已开启，关掉程序即失效")
            return base
        if self.proc and self.proc.poll() is None:
            if self.mode == "named" and self.ready:
                if self.public_url:
                    base.update(state="on", url=self.public_url,
                                detail="固定域名隧道已连上，重启后地址不变")
                else:
                    base.update(state="starting", url=None,
                                detail="隧道已连上，但还没填固定域名（在设置里填你的网址）")
            else:
                base.update(state="starting", url=None,
                            detail="正在向 Cloudflare 申请地址…")
            return base
        if self.error:
            base.update(state="error", url=None, detail=self.error)
            return base
        if not available:
            base.update(state="off", url=None,
                        detail="没找到 cloudflared.exe：下载后放到程序同目录，再点开启。")
            return base
        if self.mode == "named":
            base.update(state="off", url=None, detail="固定域名未开启")
        else:
            base.update(state="off", url=None, detail="未开启")
        return base

    def refresh_binary(self):
        if not self.binary or not os.path.isfile(self.binary):
            self.binary = find_cloudflared(None, self.extra_dirs)
        return self.binary

    # ---------------------------------------------------------------- 启停 --
    def start(self):
        with self._lock:
            if self.mode == "manual":
                return self.status()
            if self.proc and self.proc.poll() is None:
                return self.status()
            self.error = None
            self.url = None
            self.ready = False
            if not self.refresh_binary():
                self.error = "未找到 cloudflared"
                return self.status()
            if self.mode == "named" and not self.token:
                self.error = "还没配置隧道 Token"
                return self.status()
            env = dict(os.environ)
            if self.mode == "named":
                cmd = [self.binary, "tunnel", "--no-autoupdate", "run"]
                env["TUNNEL_TOKEN"] = self.token     # 不走命令行，避免出现在进程列表
            else:
                cmd = [self.binary, "tunnel", "--url",
                       "http://127.0.0.1:%d" % self.port, "--no-autoupdate"]
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=flags, env=env,
                )
            except Exception as exc:
                self.error = "启动失败：%s" % exc
                return self.status()
            self._thread = threading.Thread(target=self._read, daemon=True)
            self._thread.start()
            return self.status()

    def _read(self):
        proc = self.proc
        try:
            for line in proc.stderr:
                if self.mode == "quick":
                    m = QUICK_URL_RE.search(line)
                    if m:
                        self.url = m.group(0)
                elif READY_RE.search(line):
                    self.ready = True
        except Exception:
            pass
        code = proc.poll()
        if code not in (0, None):
            self.url = None
            self.ready = False
            self.error = "cloudflared 退出（code %s），检查网络、Token 或代理" % code

    def stop(self):
        with self._lock:
            proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.url = None
        self.ready = False
        return self.status()

    def autostart(self):
        """启动时按配置自动拉起固定域名隧道（quick 不自动开）。"""
        if self.mode == "named" and self.auto and self.token:
            return self.start()
        return self.status()
