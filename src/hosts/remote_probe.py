#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「服务器部署」的连通性自检：DNS / TCP / WS 三段分开报。

为什么不能只发一个 HEAD：HEAD 200 只说明有台 web 服务器在
答话，不代表 /ws 能握手成功。反代配错、路径前缀不对、后端进程没起来，HEAD 照样
200，用户在客户端里看到的就是「说能连结果连不上」。所以三段各自判，报对那一段。

只用 stdlib（socket / ssl / http.client），不起事件循环、不依赖 websockets 包，
这样管理口线程里直接调，打包也不用多带东西。

安全口径：远端地址是用户自己填的，本模块只对它发起只读探测，不发送任何 key、
不接收可执行内容，也不会因为探测成功就信任该站点。
"""
import base64
import json
import os
import re
import socket
import ssl
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 6.0
_STAGES = ("dns", "tcp", "ws")
# 域名 / IPv4 / IPv6（urlsplit 已去掉方括号）三种主机名的合法字符
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$|^[0-9A-Fa-f:]+$")


def _err(text):
    return {"ok": False, "reachable": False, "stage": "", "note": "",
            "detail": str(text), "dns": {}, "tcp": {}, "ws": {}, "instance": None}


def ws_path_for(url):
    """
    和 web/config.js 同一套推导：prefix = 路径去掉最后一段，WS 在 prefix + /ws。
    根路径 → /ws；反代子路径 /pub/ → /pub/ws。前端能跑，这里就必须能跑。
    """
    parts = urlsplit(str(url or ""))
    path = parts.path or "/"
    prefix = path[:path.rfind("/")] if "/" in path else ""
    return (prefix or "") + "/ws"


def base_path_for(url):
    """反代子路径前缀，用于拼 /api/state。"""
    path = urlsplit(str(url or "")).path or "/"
    return path[: path.rfind("/")] if "/" in path else ""


def _port_of(parts):
    if parts.port:
        return int(parts.port)
    return 443 if (parts.scheme or "").lower() == "https" else 80


def _resolve(host, port, resolver):
    try:
        rows = resolver(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return [], "DNS 解析失败：%s" % (exc.strerror or exc)
    except OSError as exc:
        return [], "DNS 解析失败：%s" % exc
    ips = []
    for row in rows:
        ip = (row[4] or ("",))[0]
        if ip and ip not in ips:
            ips.append(ip)
    if not ips:
        return [], "DNS 有应答但没返回可用地址"
    return ips, "解析到 %s（%s）" % (host, "、".join(ips[:3]))


def _connect(ip, port, timeout, transport):
    try:
        sock = transport(ip, port, timeout)
    except socket.timeout:
        return None, "端口无响应（%d 秒超时）" % int(timeout)
    except OSError as exc:
        return None, "端口不通：%s" % (exc.strerror or exc)
    return sock, "TCP 已连上 %s:%d" % (ip, port)


def _handshake(sock, host, port, scheme, path, timeout):
    """裸 socket 发一次 WebSocket Upgrade，只看响应头，收不到就断。"""
    # Sec-WebSocket-Key 必须是 16 随机字节的 base64，长度不对服务端直接 400
    # （第一版写死一个字符串就被真实例拒了，别再犯）
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    hostline = host if port in (80, 443) else "%s:%d" % (host, port)
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ) % (path, hostline, key)
    try:
        sock.settimeout(timeout)
        sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
            if len(head) > 65536:
                break
    except socket.timeout:
        return False, "WS 握手超时，对端没响应"
    except OSError as exc:
        return False, "WS 握手失败：%s" % (exc.strerror or exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not head:
        return False, "WS 握手没有任何响应"
    text = head.decode("latin-1", "replace")
    lines = text.split("\r\n")
    status = lines[0] if lines else ""
    code = ""
    try:
        code = status.split()[1]
    except IndexError:
        pass
    lowered = text.lower()
    if code == "101" and "upgrade: websocket" in lowered:
        return True, "WS 握手成功（101 Switching Protocols）"
    if code and code not in ("101",):
        return False, ("WS 握手被拒：HTTP %s —— 该地址不是 PubChat 服务，"
                       "或 /ws 路径没被反代转发") % code
    return False, "WS 握手响应无法识别（%s）" % (status or "空状态行")


def _instance_info(url, timeout):
    """探测成功后尽量回显对面是哪个实例；拿不到不影响判定。"""
    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https"):
        return None
    prefix = base_path_for(url)
    target = "%s://%s%s/api/state" % (parts.scheme, parts.netloc, prefix)
    try:
        from urllib.request import Request, urlopen
        resp = urlopen(Request(target, headers={"Accept": "application/json"}),
                      timeout=max(1.5, timeout / 2.0))
        data = json.loads(resp.read(65536).decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {"version": str(data.get("version") or ""),
            "users": data.get("users"),
            "instance": str(((data.get("instance") or {}) or {}).get("name") or "")[:60]
            if isinstance(data.get("instance"), dict) else ""}


def normalize_url(raw):
    """补齐 scheme，返回 (url, error)。只接受 http/https，且地址形状要合法。"""
    raw = str(raw or "").strip()
    if not raw:
        return "", "地址不能为空"
    if any(c.isspace() for c in raw):
        return "", "地址里有空格，请检查是否误粘贴"
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "", "只支持 http:// 或 https://，当前是 %s" % (scheme or "空")
    host = parts.hostname or ""
    if not host:
        return "", "地址里取不到域名"
    # IPv4 / IPv6 / 域名三种都放行；其它形状（带引号、带中文、缺分隔符）在保存时
    # 就拒掉，别等自检报「DNS 解析失败」让人以为是对面服务器坏了。
    if not _HOST_RE.match(host):
        return "", "域名或 IP 写法不对：%s" % host
    return parts._replace(query="", fragment="").geturl().rstrip("/"), ""


def probe(raw_url, timeout=DEFAULT_TIMEOUT, resolver=socket.getaddrinfo,
          transport=None):
    """
    三段自检。返回 dict：

      reachable   只有 WS 真的握手成功才为 True
      stage       失败发生在哪一段（dns / tcp / ws），成功时为空
      dns/tcp/ws  各段 {ok, detail}
      note        给人看的一句话
      instance    对面实例的版本/人数（拿不到就 None，不影响判定）

    resolver / transport 是给冒烟留的注入口，默认真实解析与真实连接。
    """
    url, bad = normalize_url(raw_url)
    if bad:
        out = _err(bad)
        out["stage"] = "url"
        out["note"] = bad
        return out

    if transport is None:
        def transport(ip, port, to):
            sock = socket.create_connection((ip, port), timeout=to)
            return sock

    parts = urlsplit(url)
    host = parts.hostname
    scheme = parts.scheme.lower()
    port = _port_of(parts)
    out = {"ok": False, "reachable": False, "stage": "", "note": "", "detail": "",
           "dns": {}, "tcp": {}, "ws": {}, "instance": None, "url": url}

    ips, dns_detail = _resolve(host, port, resolver)
    out["dns"] = {"ok": bool(ips), "detail": dns_detail}
    if not ips:
        out["stage"] = "dns"
        out["detail"] = dns_detail
        out["note"] = "DNS 这一段就没过：%s" % dns_detail
        return out

    ip = ips[0]
    try:
        sock, tcp_detail = _connect(ip, port, timeout, transport)
    except Exception as exc:                     # 注入的 transport 乱抛也别炸
        sock, tcp_detail = None, "连接异常：%s" % exc
    out["tcp"] = {"ok": sock is not None, "detail": tcp_detail}
    if sock is None:
        out["stage"] = "tcp"
        out["detail"] = tcp_detail
        out["note"] = "TCP 这一段就没过：%s" % tcp_detail
        return out

    if scheme == "https":
        try:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        except ssl.SSLError as exc:
            out["tcp"]["detail"] += "；TLS 握手失败：%s" % exc
            out["stage"] = "ws"
            out["ws"] = {"ok": False, "detail": "TLS 证书握手失败：%s" % exc}
            out["detail"] = str(exc)
            out["note"] = "TLS 证书有问题，WS 没握手：%s" % exc
            try:
                sock.close()
            except OSError:
                pass
            return out
        except OSError as exc:
            out["stage"] = "ws"
            out["ws"] = {"ok": False, "detail": "TLS 建立失败：%s" % exc}
            out["note"] = "TLS 建立失败：%s" % exc
            return out
        tcp_ver = getattr(sock, "version", "")
        out["tcp"]["detail"] += "（TLS %s）" % tcp_ver if tcp_ver else ""

    path = ws_path_for(url)
    ws_ok, ws_detail = _handshake(sock, host, port, scheme, path, timeout)
    out["ws"] = {"ok": ws_ok, "detail": ws_detail, "path": path}
    if not ws_ok:
        out["stage"] = "ws"
        out["detail"] = ws_detail
        out["note"] = "前两段过了，WS 握手没过：%s" % ws_detail
        return out

    out["reachable"] = True
    out["ok"] = True
    out["instance"] = _instance_info(url, timeout)
    inst = out["instance"] or {}
    ver = inst.get("version")
    # 公网口不暴露 /api/state（管理口才给），所以拿不到版本是正常情况，
    # 不能因此报失败，也不要在文案里假装知道对面是什么版本。
    out["note"] = ("WS 握手成功（PubChat v%s），可以收发消息" % ver) if ver \
        else "WS 握手成功（101），对面能接受 WebSocket 连接"
    return out
