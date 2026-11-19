#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PubChat 本机版冒烟测试（M0/M2）

只跑本机 127.0.0.1，不碰生产。会上手起一个真实的 desktop.py 子进程，
用临时数据目录，覆盖：首启向导、单端口静态、管理口隔离、WebSocket 协议、
未读/补消息、智能体权限、改资料、重启后数据留存、穿透接口状态。

用法：python tests/smoke.py [--exe dist\\PubChat.exe]
"""
import argparse
import asyncio
import http.client
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []
TARGET = []                       # 起服务用的命令前缀，main() 里定
LAST_LOG = ""
ADMIN_SECRETS = {}                # 管理口设备 Bearer token（一次性链接兑换）


def ok(name):
    PASS.append(name)
    print("  ok   " + name, flush=True)


def bad(name, detail=""):
    FAIL.append((name, detail))
    print("  FAIL " + name + (" :: " + str(detail) if detail else ""), flush=True)


def check(cond, name, detail=""):
    ok(name) if cond else bad(name, detail)
    return cond


def read_text_loose(path):
    """子进程日志的编码不固定：源码跑是 UTF-8，打包 exe 走控制台 GBK。
    先按 UTF-8 严格解，解不开再按 GBK 解。反过来比"哪种解出的字多"会把
    GBK 乱码当成正文（乱码字符不是替换符，字数反而更多）。"""
    try:
        raw = Path(path).read_bytes()
    except Exception:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def req(port, path, method="GET", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    if port in ADMIN_SECRETS and "Authorization" not in h:
        h["Authorization"] = "Bearer " + ADMIN_SECRETS[port]
    payload = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=payload, headers=h)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        obj = json.loads(data) if data else None
    except Exception:
        obj = None
    return r.status, obj, data


def read_admin_link(tmp, timeout=10):
    """读管理口交接文件（<数据目录>/admin-open.json），返回 (url, token)。"""
    f = Path(tmp) / "admin-open.json"
    end = time.time() + timeout
    while time.time() < end:
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            url = info.get("url") or ""
            if "#t=" in url:
                return url, url.split("#t=", 1)[1]
        except Exception:
            pass
        time.sleep(0.1)
    return "", ""


def admin_login(port, tmp, timeout=10):
    """兑换一次管理链接，把设备 secret 记进 ADMIN_SECRETS 供后续 admin 请求复用。"""
    url, token = read_admin_link(tmp, timeout)
    if not token:
        return ""
    admin_secret = ADMIN_SECRETS.pop(port, None)
    st, obj, _ = req(port, "/api/admin/token/verify", "POST", {"token": token})
    if admin_secret:
        ADMIN_SECRETS[port] = admin_secret
    if st != 200:
        return ""
    secret = (obj or {}).get("secret")
    if not secret:
        return ""
    ADMIN_SECRETS[port] = secret
    return secret


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post_raw(port, path, data, ctype="application/octet-stream"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"Content-Type": ctype}
    if port in ADMIN_SECRETS:
        headers["Authorization"] = "Bearer " + ADMIN_SECRETS[port]
    conn.request("POST", path, body=data, headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    try:
        obj = json.loads(body) if body else None
    except Exception:
        obj = None
    return r.status, obj


def wait_ready(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            st, _, _ = req(port, "/healthz")
            if st == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def stop_tree(p):
    """onefile exe 是「引导进程 + 子进程」，必须整棵树杀掉，否则端口不放。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            p.terminate()
        p.wait(timeout=10)
    except Exception:
        pass
    try:
        p._logf.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# WebSocket 小客户端
# --------------------------------------------------------------------------- #
class WS:
    def __init__(self, url, auth):
        self.url = url
        self.auth = auth
        self.ws = None

    async def open(self):
        self.ws = await websockets.connect(self.url, open_timeout=10)
        await self.ws.send(json.dumps(self.auth))
        return await self.next("authenticated")

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def next(self, want=None, timeout=6, contains=None):
        end = time.time() + timeout
        while time.time() < end:
            raw = await asyncio.wait_for(self.ws.recv(), timeout)
            msg = json.loads(raw)
            if want is not None and msg.get("type") != want:
                continue
            if contains is not None and contains not in (msg.get("content") or ""):
                continue
            return msg
        raise TimeoutError("没等到 %s" % want)

    async def close(self):
        if self.ws:
            await self.ws.close()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run():
    pub = free_port()
    adm = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-smoke-"))
    ws_url = "ws://127.0.0.1:%d/ws" % pub

    env = dict(os.environ)
    log_path = tmp / "server.log"

    def spawn():
        logf = open(log_path, "ab")
        p = subprocess.Popen(
            TARGET + ["--no-browser", "--no-gui", "--host", "127.0.0.1", "--port", str(pub),
                      "--admin-port", str(adm), "--data-dir", str(tmp)],
            cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT)
        p._logf = logf
        return p

    def stop(p):
        stop_tree(p)

    proc = spawn()
    try:
        check(wait_ready(adm), "管理口启动")
        check(wait_ready(pub), "对外口启动")
        check(bool(admin_login(adm, tmp)), "管理口兑换设备凭证")

        # ---- 首启前 ----
        st, _, body = req(pub, "/")
        check(st == 503 and b"\xe8\xbf\x98\xe6\xb2\xa1\xe5\x88\x9d\xe5\xa7\x8b\xe5\x8c\x96" in body,
              "未初始化时对外只给等待页")
        st, _, _ = req(pub, "/admin.html")
        check(st == 404, "对外口不给管理页")
        st, _, _ = req(pub, "/api/state")
        check(st == 404, "对外口不给管理 API")
        for p in ("/Admin.HTML", "/x/../admin.html", "/setup.html", "/shell.html",
                  "/qrcode.js", "/admin.js", "/instances.html", "/invite.js",
                  "/claim.html", "/device.js"):
            st, _, _ = req(pub, p)
            check(st == 404, "对外口挡住管理资源 %s" % p)
        st, _, _ = req(pub, "/healthz")
        check(st == 200, "对外口 /healthz")

        # ---- 管理口 Host/Origin ----
        st, _, _ = req(adm, "/api/state", headers={"Host": "evil.example:80"})
        check(st == 403, "管理口拒绝陌生 Host")
        st, _, _ = req(adm, "/api/state", headers={"Origin": "http://evil.example"})
        check(st == 403, "管理口拒绝陌生 Origin")
        # /api/deploy 是新增写入口，必须走同一道门
        st, _, _ = req(adm, "/api/deploy", headers={"Host": "evil.example:80"})
        check(st == 403, "/api/deploy GET 拒绝陌生 Host")
        st, _, _ = req(adm, "/api/deploy", "POST",
                       {"action": "set_manifest",
                        "url": "https://pub.example.org/pubchat-dist/latest.json"},
                       headers={"Origin": "http://evil.example"})
        check(st == 403, "/api/deploy POST 拒绝陌生 Origin")
        st, obj, _ = req(adm, "/api/deploy")
        check(st == 200 and obj.get("update", {}).get("manifest_url", "") == "",
              "被门拒掉的写入没有落盘", obj.get("update"))

        st, obj, _ = req(adm, "/api/state")
        check(st == 200 and obj["initialized"] is False, "管理口本机可读状态")
        st, _, body = req(adm, "/admin.js")
        check(st == 200 and b"renderTunnel" in body, "管理口可取静态资源")
        st, _, body = req(adm, "/shell.html")
        check(st == 200 and b"pubchat-setup-done" in body and "聊天".encode() in body,
              "管理口可取单窗口外壳")

        # ---- 实例列表与实例管理 API ----
        st, obj, _ = req(adm, "/api/instances")
        check(st == 200 and len(obj.get("instances") or []) == 1
              and obj["instances"][0]["id"] == "local",
              "实例列表恒含本机且排第一", obj)
        st, obj, _ = req(adm, "/api/state")
        check(len(obj.get("instances") or []) == 1
              and obj["instances"][0]["kind"] == "local",
              "/api/state 带实例列表")

        st, obj, _ = req(adm, "/api/instances", "POST", {"action": "add", "url": "not a url"})
        check(st == 400, "实例地址非法被拒", obj)
        st, obj, _ = req(adm, "/api/instances", "POST",
                         {"action": "add", "url": "https://example.com/pub/", "name": "示例"})
        check(st == 200 and obj["instance"]["id"].startswith("remote-")
              and len(obj["instances"]) == 2, "新增远端实例", obj)
        check(obj["instance"]["url"] == "https://example.com/pub",
              "实例地址去掉末尾斜杠", obj["instance"])
        iid = obj["instance"]["id"]
        st, obj, _ = req(adm, "/api/instances", "POST",
                         {"action": "add", "url": "https://example.com/pub"})
        check(st == 400 and "已经添加过" in (obj.get("error") or ""), "重复实例被拒", obj)
        st, obj, _ = req(adm, "/api/instances", "POST",
                         {"action": "update", "id": iid, "name": "改过名"})
        check(st == 200 and obj["instance"]["name"] == "改过名", "实例改名", obj)
        st, obj, _ = req(adm, "/api/instances", "POST",
                         {"action": "update", "id": "local", "name": "x"})
        check(st == 400, "本机实例不能改名", obj)
        st, obj, _ = req(adm, "/api/instances", "POST", {"action": "remove", "id": "local"})
        check(st == 400, "本机实例不能删除", obj)
        st, obj, _ = req(adm, "/api/instances", "POST", {"action": "nope"})
        check(st == 400, "未知实例操作被拒", obj)
        st, obj, _ = req(adm, "/api/instances")
        check(len(obj["instances"]) == 2 and obj["instances"][1]["name"] == "改过名",
              "实例列表含改名后的新增项", obj)
        st, obj, _ = req(adm, "/api/instances", "POST", {"action": "remove", "id": iid})
        check(st == 200 and len(obj["instances"]) == 1, "删除远端实例", obj)

        # ---- 前端契约：实例栏 / 全部聚合 / 首启两选项 ----
        st, _, body = req(adm, "/shell.html")
        check(st == 200 and b"railAll" in body and b"pubchat-host-state" in body
              and "未读在前".encode() in body, "外壳含实例栏与全部聚合")
        check(b"pubchat-instances-refresh" in body and b"adoptedNames" in body,
              "外壳会把远端自报名写回实例配置并通知设置页")
        check(b"frameNonce" in body, "外壳在只改 hash 时强制重载实例 iframe")
        st, _, body = req(adm, "/instances.html")
        check(st == 200 and "实例".encode() in body and b"invite.js" in body,
              "实例管理页可取")
        check(b"pubchat-instances-refresh" in body, "实例页能响应外壳的刷新通知")
        st, _, body = req(adm, "/invite.js")
        check(st == 200 and b"pubchat-join-remote" in body, "邀请加入脚本可取")
        st, _, body = req(adm, "/settings.html")
        check(st == 200 and b"/instances.html" in body and b"manageFrame" in body,
              "管理页用二级页签 + 单 iframe 承载")

        # ---- S1 桌面壳信息架构：入口唯一性（修订 v2 ③，机械可执行，不靠肉眼） ----
        # 注意：管理口把 /index.html 重写成 admin.html（local.py:677），聊天页只能从对外口取，
        # 所以 index.html 的断言排在首启之后、走 pub（见「初始化后对外给聊天页」那段）。
        shell_paths = ("/shell.html", "/settings.html", "/admin.html",
                       "/members.html", "/instances.html", "/server.html",
                       "/about.html", "/setup.html")
        shell_docs = {p: req(adm, p)[2] for p in shell_paths}
        dupmap = {}
        for p, b in shell_docs.items():
            dup = sorted(x.decode("utf-8", "replace") for x, c in
                         Counter(re.findall(rb'\bid="([^"]+)"', b)).items() if c > 1)
            if dup:
                dupmap[p] = dup[:6]
        check(not dupmap, "S1 每个桌面文档内 DOM id 不重复", dupmap)

        settxt = shell_docs["/settings.html"].decode("utf-8", "replace")
        CAPS = ("实例与运行状态", "邀请", "成员与 Key", "智能体席位",
                "服务器设置", "已授权设备", "公网地址", "手机扫码")
        for cap in CAPS:
            check(settxt.count("label: '%s'" % cap) == 1,
                  "S1 管理八项逐项唯一：%s" % cap, settxt.count("label: '%s'" % cap))
        check("本机服务" in settxt and "可选" in settxt,
              "S1 本机服务降为「（可选）」一项（修订 v2 ②）")

        # 每个页签的 src 必须真存在，src 里带的 sec= 必须被目标页认识（防假控件）
        SEC_OK = {"/admin.html": ("seats", "server", "devices", "public", "qrcode"),
                  "/members.html": ("invite", "members")}
        tabdef = re.findall(r"key:\s*'(\w+)',\s*label:\s*'([^']+)'[^{}]*?src:\s*'([^']+)'",
                            settxt)
        check(len(tabdef) == 9, "S1 管理二级页签共 9 项（八能力 + 本机服务）", len(tabdef))
        dead = []
        for _k, label, src in tabdef:
            path, _, qs = src.partition("?")
            if path not in shell_docs:
                dead.append("%s → %s 页不存在" % (label, path))
                continue
            m = re.search(r"(?:^|&)sec=([^&]+)", qs)
            if m and m.group(1) not in SEC_OK.get(path, ()):
                dead.append("%s → %s 的 sec=%s 目标页不认" % (label, path, m.group(1)))
        check(not dead, "S1 每个二级页签 src 与 sec 都真实可达", dead)
        for k in ("seats", "server", "devices", "public", "qrcode"):
            check("%s: 1" % k in shell_docs["/admin.html"].decode("utf-8", "replace"),
                  "S1 管理页 SEC_KEYS 含 %s" % k)
        for k in ("invite", "members"):
            check("%s: 1" % k in shell_docs["/members.html"].decode("utf-8", "replace"),
                  "S1 成员页 SEC_KEYS 含 %s" % k)

        sh = shell_docs["/shell.html"].decode("utf-8", "replace")
        check(sorted(set(re.findall(r'data-tab="([a-z]+)"', sh))) ==
              ["about", "chat", "manage", "me"],
              "S1 外壳顶栏恰好四项：聊天/管理/我的/关于",
              re.findall(r'data-tab="([a-z]+)"', sh))

        # ---- S2 左侧分组折叠 + 智能体开房入口（对应需求方的问题 7）----
        # 折叠状态按实例 id 存：外壳一个键记 {iid: 是否展开}；聊天帧里的分组本来就
        # 按 origin 隔离（每个实例一个 iframe），所以两边都不串状态。
        check(sh.count("pubchat.shell.railCollapsed") == 1,
              "S2 折叠状态按实例 id 存本地（外壳单一键名）",
              sh.count("pubchat.shell.railCollapsed"))
        check("function isRailOpen" in sh and "function setRailOpen" in sh,
              "S2 外壳折叠读与写成对存在")
        check("if (inst.kind === 'local') return true;" in sh
              and "return inst.id === view;" in sh,
              "S2 默认规则：本机与当前实例展开、其余实例收起")
        check("rg-chev" in sh and sh.count("aria-expanded") == 1,
              "S2 实例分组头有折叠三角与展开态语义", sh.count("aria-expanded"))
        check("} else if (open) {" in sh,
              "S2 折叠时房间列表不进 DOM（不是靠 CSS 藏起来）")
        check("setBadge(gb, instUnread(inst.id))" in sh,
              "S2 折叠态分组头仍显示该实例未读汇总")
        check("setRailOpen(iid, true)" in sh,
              "S2 从「全部」点进折叠实例里的会话会自动展开该实例")

        cfgtxt = req(adm, "/config.js")[2].decode("utf-8", "replace")
        check("id: 'agentsdm'" in cfgtxt and "label: '智能体协作'" in cfgtxt,
              "S2 桌面 H5 出「智能体协作」分组（与手机端 uni 侧同名）")
        check("rooms: ['agent_dm']" in cfgtxt and "defaultOpen: false" in cfgtxt,
              "S2 智能体协作默认收起")
        check("agentDm: true" in cfgtxt,
              "S2 智能体协作带开房入口，且空组不隐藏（否则第一次开不了房）")

        apptxt = req(adm, "/app.js")[2].decode("utf-8", "replace")
        for tok in ("agent_dm_open", "own_client_id", "agent_dm_opened",
                    "agentDmSheet", "agentDmMine", "agentDmPeer", "agentDmAdd"):
            check(apptxt.count(tok) >= 1, "S2 前端接上 %s" % tok, apptxt.count(tok))
        check(apptxt.count("add.id = 'agentDmAdd'") == 1,
              "S2 开房入口按钮在 app.js 只造一次", apptxt.count("add.id = 'agentDmAdd'"))
        check("searching ? true : !state.collapsed[g.id]" in apptxt,
              "S2 搜索态强制全展开")
        check("cfg.hideEmptyGroups !== false && !g.agentDm" in apptxt,
              "S2 空组隐藏规则给开房入口留了例外")
        check("state.selectAfterRooms" in apptxt and "state.pendingAgentDm" in apptxt,
              "S2 开房只认领自己发起的那一次，别人家的广播不抢视图")

        st, _, body = req(adm, "/setup.html")
        check(st == 200 and "我自己的服务器".encode() in body
              and "加入已有聊天室".encode() in body, "首启向导含两条真实路径")
        st, _, body = req(adm, "/app.js")
        check(b"pubchat-host-state" in body and b"pubchat-open-room" in body,
              "聊天页实现壳联动协议")

        # ---- S3 开屏动画 / 首启两条路 / 引导四步（对应需求方的问题 1、6）----
        # 判据一律读源码与 HTTP 状态，不量像素。开屏的时序在真浏览器那遍复核。
        splash_src = read_text_loose(ROOT / "web" / "splash.js")
        idx_src = read_text_loose(ROOT / "web" / "index.html")
        m_src = read_text_loose(ROOT / "web" / "m.html")
        setup_src = read_text_loose(ROOT / "web" / "setup.html")
        guide_src = read_text_loose(ROOT / "web" / "guide.js")
        local_src = read_text_loose(ROOT / "hosts" / "local.py")

        check("MAX_MS = 1200" in splash_src, "S3 开屏最长 1.2s 写死在组件里")
        check("prefers-reduced-motion" in splash_src, "S3 开屏尊重减弱动效偏好")
        check("window.top !== window.self" in splash_src, "S3 被 iframe 嵌进外壳时不放开屏")
        check("el.addEventListener('click', hide)" in splash_src, "S3 开屏点任意处跳过")
        for page, src in (("index.html", idx_src), ("m.html", m_src)):
            check(src.count('<script src="splash.js"></script>') == 1,
                  "S3 %s 同步加载 splash.js 恰好一次（无 defer/async）" % page,
                  src.count('<script src="splash.js"></script>'))
            check(src.count('id="pcSplash"') == 1,
                  "S3 %s 预留覆盖层恰好一个" % page)
        check(-1 < idx_src.find('id="pcSplash"') < idx_src.find('id="login"'),
              "S3 桌面：覆盖层排在登录框之前")
        check(-1 < m_src.find('id="pcSplash"') < m_src.find('id="screen-home"'),
              "S3 手机：覆盖层排在首页之前")
        check("· 桌面版" in idx_src and "· 手机版" in m_src,
              "S3 两端只差副标题称谓，组件与逻辑同一份")
        check("window.PubChatSplash.ready()" in apptxt, "S3 首屏就绪调 ready() 提前退场")
        check("setTimeout(splashDone, 1500)" in apptxt, "S3 连不上服务器也有 1.5s 兜底")
        check("getElementById('pcSplash')" in apptxt, "S3 组件没加载上时自己摘掉预留层")
        # 下面两条钉的是「只有真浏览器才暴露」的坑：ready() 比 DOMContentLoaded 先到，
        # 旧代码直接 return 把信号丢了，开屏每次都白撑满 1.2s（实测 1441ms）。
        check("if (!el) { pending = true; return; }" in splash_src
              and "if (reduced || gone || pending) hide();" in splash_src,
              "S3 ready() 早到也不丢信号：接线后立刻退场，不撑满 MAX_MS")
        check('.boot .mk' in sh and '<div class="mk">P</div>' in sh,
              "S3 外壳过渡页用同一套绿色 P 品牌符号")
        pub_gate = local_src.split("public_files = {")[1].split("}")[0]
        adm_gate = local_src.split("_public_static = {")[1].split("}")[0]
        for asset in ("splash.js", "guide.js"):
            check('"%s"' % asset in pub_gate, "S3 对外口白名单含 %s" % asset)
            check('"%s"' % asset in adm_gate, "S3 管理口白名单含 %s" % asset)
            check(req(pub, "/" + asset)[0] == 200, "S3 对外口实际取得到 %s" % asset)
            check(req(adm, "/" + asset)[0] == 200, "S3 管理口实际取得到 %s" % asset)

        # 首启两条互斥真实路径
        check(re.findall(r'data-go="(\w+)"', setup_src) == ["Join", "1"],
              "S3 首启恰好两张卡，各自直连一条真实路径",
              re.findall(r'data-go="(\w+)"', setup_src))
        for dead in ("btnStep0Next", "skipStep0", "skipJoin", "skipStep2"):
            check(dead not in setup_src, "S3 假出口 %s 已删除" % dead)
        check("backToStep0" in setup_src, "S3 步骤里留了回 Step 0 的返回")
        check(setup_src.count('id="joinBack"') == 1 and setup_src.count('id="localBack"') == 1
              and setup_src.count('id="remoteBack"') == 1,
              "S3 三条返回入口各只有一个")
        check(setup_src.count("showStep(3,") == 1,
              "S3 域名绑定只剩 set_remote 成功这一条真实入口",
              setup_src.count("showStep(3,"))
        check('id="toChat" hidden' in setup_src and "chat=1" in setup_src,
              "S3「直接进聊天」默认藏好，只在有机可进时亮出")
        check("'&chat=1'" in sh, "S3 外壳按 chatReady 决定要不要给向导带 chat=1")
        check(setup_src.count('role="button" tabindex="0"') == 2, "S3 两张卡键盘可达")

        # 引导四步：一份文案，三端对齐
        s3_titles = re.findall(r"t: '([^']+)'", guide_src)
        check(len(s3_titles) == 4 and len(set(s3_titles)) == 4,
              "S3 引导恰好四步且标题不重复", s3_titles)
        check("PubChatGuide.steps('mobile')" in m_src, "S3 手机首页改用同一份引导")
        check('src="guide.js"' in m_src and 'src="guide.js"' in idx_src,
              "S3 桌面与手机都载 guide.js")
        check("PubChatGuide.steps('desktop')" in apptxt, "S3 桌面弹层吃同一份步骤数据")
        check(apptxt.count("state.firstRunGuide = true") == 1,
              "S3 首启只标记一次进引导", apptxt.count("state.firstRunGuide = true"))
        check(apptxt.count("closeProfile()") >= 4
              and apptxt.count("closeSheet('profileSheet')") == 1,
              "S3 资料步的四条收尾路径（保存/稍后/✕/点背景）都接引导",
              apptxt.count("closeProfile()"))
        check(idx_src.count('id="meGuide"') == 1 and apptxt.count("'#meGuide'") == 1,
              "S3「我的 → 新手引导」重看入口唯一")
        for gid in ("guideSheet", "guideTitle", "guideBody", "guideDots",
                    "guideNext", "guideSkip", "closeGuide"):
            check(idx_src.count('id="%s"' % gid) == 1, "S3 引导容器 id 唯一：%s" % gid)
        check("'guideSheet'" in apptxt, "S3 原生返回栈能关掉引导层")
        # 本机部署的房主在 /api/setup 里就是 onboarded 的（hosts/local.py 的 mark_onboarded），
        # 只看服务端标记会让他一次也看不到四步引导。这里钉住本机那份额外账。
        check("var LS_GUIDE = 'pubchat.guide.seen.v1'" in apptxt
              and "function guideSeen()" in apptxt,
              "S3 引导看过没有另记在本机，不和服务端 onboarded 混用")
        check("if (id === 'guideSheet')" in apptxt
              and "localStorage.setItem(LS_GUIDE, '1')" in apptxt,
              "S3 关掉引导层的统一出口落本机标记（四条路都走 closeSheet）")
        check("state.me.onboarded === false" in apptxt
              and "} else if (!state.guideShown && !guideSeen()) {" in apptxt,
              "S3 已 onboarded 但没看过引导的房主，第一次进聊天补弹一次")
        check(apptxt.count("state.guideShown = true") == 2,
              "S3 自动弹引导一次会话最多一次（重连不重复弹）",
              apptxt.count("state.guideShown = true"))
        # 引导里承诺的界面必须真实存在——写错比不写更坏
        check("智能体协作" in cfgtxt and "邀请朋友" in idx_src,
              "S3 引导提到的组名与按钮在界面上存在")
        check("pubchat_agent_dm_open" in guide_src, "S3 引导点名离线收件箱的开房工具")
        for term in ("实例与运行状态", "已授权设备", "手机扫码"):
            check(term in guide_src and term in settxt, "S3 管理项与设置页同名：%s" % term)

        # ---- S4 更新公告（服务端 /api/notice + 客户端弹窗）----
        # 服务端语义由 评审上线：无文件/空 → 204，有 → 200 原样透传。
        notice_file = tmp / "notice.json"
        notice_js_src = read_text_loose(ROOT / "web" / "notice.js")
        server_pub_src = read_text_loose(ROOT / "hosts" / "server.py")

        st, obj, body = req(pub, "/api/notice")
        check(st == 204 and not body, "S4 没有 notice.json 时对外口 204 且无正文", (st, body[:40]))
        notice_file.write_text("", encoding="utf-8")
        st, obj, body = req(pub, "/api/notice")
        check(st == 204 and not body, "S4 空文件同样 204（不是 200 空对象）", (st, body[:40]))
        notice_file.write_text("{不是 json", encoding="utf-8")
        st, obj, body = req(pub, "/api/notice")
        check(st == 500 and obj and obj.get("error") == "notice_malformed",
              "S4 坏 JSON 报 500 notice_malformed，不静默兜成空公告", (st, obj))
        notice_file.write_text('["一个数组"]', encoding="utf-8")
        st, obj, body = req(pub, "/api/notice")
        check(st == 500 and obj and obj.get("error") == "notice_malformed",
              "S4 非对象同样按畸形处理（透传不等于不校验）", (st, obj))

        notice_payload = {"version": "0.8.0", "title": "维护通知",
                          "body": "今晚 22:00 停机十分钟\n期间消息会排队",
                          "published_at": "2026-11-12T10:00:00"}
        notice_file.write_text(json.dumps(notice_payload, ensure_ascii=False), encoding="utf-8")
        st, obj, body = req(pub, "/api/notice")
        check(st == 200 and obj == notice_payload,
              "S4 有文件时 200 且四字段原样透传（服务端不加工）", (st, obj))
        st, obj, body = req(adm, "/api/notice")
        check(not (st == 200 and obj == notice_payload),
              "S4 公告只在对外口，管理口不另开一条口（管理口对未知路径回落 admin.html，"
              "所以判据是「拿不到这份公告」而不是状态码）",
              (st, str(obj)[:60]))

        # 公网宿主（hosts/server.py）是同一条分支——线上跑的是它，不能只改本机这份
        check('if path == "/api/notice":' in server_pub_src
              and '"notice_malformed"' in server_pub_src,
              "S4 公网宿主也有 /api/notice 同语义分支")
        pub_gate2 = server_pub_src.split("PUBLIC_FILES = {")[1].split("}")[0]
        for asset in ("splash.js", "guide.js", "notice.js"):
            check('"%s"' % asset in pub_gate2,
                  "S4 公网静态白名单含 %s（漏了云端实例就 404）" % asset)
        for asset in ("splash.js", "guide.js", "notice.js"):
            check('"%s"' % asset in adm_gate, "S4 管理口白名单含 %s" % asset)
            check(req(pub, "/" + asset)[0] == 200, "S4 对外口实际取得到 %s" % asset)
            check(req(adm, "/" + asset)[0] == 200, "S4 管理口实际取得到 %s" % asset)

        # 客户端：一份组件，桌面与手机同吃；任何失败都静默
        check(idx_src.count('<script src="notice.js"></script>') == 1,
              "S4 聊天页同步载 notice.js 恰好一次")
        check(idx_src.find('<script src="notice.js"></script>')
              < idx_src.find('<script src="app.js"></script>'),
              "S4 notice.js 排在 app.js 之前")
        check("PubChatNotice.boot()" in apptxt, "S4 启动就拉公告")
        check("notices: hostNotices()" in apptxt,
              "S4 公告历史顺着已有的 host-state 回包带给外壳（不跨源另取）")
        check(".catch(function () { return { status: 'error' }; })" in notice_js_src,
              "S4 断网/接口失败静默，不抛给聊天页")
        check("if (r.status === 204) return { status: 'none' }" in notice_js_src,
              "S4 204 走静默而不是空弹窗")
        check("lsPut(LS_MUTE, n.version)" in notice_js_src
              and "lsRaw(LS_MUTE) === n.version" in notice_js_src
              and "lsSet(LS_MUTE" not in notice_js_src,
              "S4「以后不再弹出」读写同一套编码：版本号存纯字符串，"
              "JSON.stringify 一遍会带引号、比不中（真浏览器抓到的）")
        check("var v = text(o.version).trim();\n    if (!v) return null;" in notice_js_src,
              "S4 没有 version 就不弹（按版本静默会失去依据）")
        check("body.textContent = n.body;" in notice_js_src
              and "innerHTML" not in notice_js_src,
              "S4 公告正文只走 textContent，绝不按 HTML 解析")
        check("if (list.length > LIST_MAX) list.length = LIST_MAX" in notice_js_src,
              "S4 本机公告历史有上限，不会无限长")
        check("z-index:9000" in notice_js_src,
              "S4 公告层排在开屏之下：退场时才露出来，不用跟开屏协调")
        check("firstRunBusy()" in notice_js_src,
              "S4 首启流程（资料/引导）开着时不叠公告")

        # 「关于」页看历史：数据在实例源上，只能由外壳转交，两边接线都要钉住
        about_src = read_text_loose(ROOT / "web" / "about.html")
        shell_src2 = read_text_loose(ROOT / "web" / "shell.html")
        check("pubchat-about-notices" in about_src and "pubchat-about-ready" in about_src,
              "S4 关于页收公告并主动要一次（顺序不靠运气）")
        check("id=\"noticeList\"" in about_src and "更新公告" in about_src,
              "S4 关于页真有公告卡")
        check("box.textContent = ''" in about_src, "S4 关于页列表也不用 innerHTML")
        check("notices: Array.isArray(d.notices)" in shell_src2
              and "function pushNotices()" in shell_src2
              and "pubchat-about-ready" in shell_src2,
              "S4 外壳收下 notices 并转交给关于页")

        notice_file.unlink()
        check(req(pub, "/api/notice")[0] == 204, "S4 撤掉文件后回到 204（不留缓存）")

        # ---- 首启向导 ----
        st, obj, _ = req(adm, "/api/setup", "POST",
                         {"name": "测试员", "client_id": "tester", "key": "12345678"})
        check(st == 200 and obj["user"]["client_id"] == "tester"
              and len(obj["user"]["agents"]) == 3, "首启向导建管理员 + 3 个智能体")
        st, _, _ = req(adm, "/api/setup", "POST",
                       {"name": "别人", "client_id": "other", "key": "12345678"})
        check(st == 409, "首启只能做一次")
        key2 = obj["user"]["agents"][0]["key2"]
        agent_cid = obj["user"]["agents"][0]["client_id"]

        st, _, body = req(pub, "/")
        check(st == 200 and b"PubChat" in body, "初始化后对外给聊天页")

        # 聊天页只认对外口：管理口把 /index.html 重写成 admin.html，所以这一段必须在
        # 首启之后用 pub 取（首启前 pub 给的是等待页）。
        ixt = body.decode("utf-8", "replace")
        for gone in ("聊天设置", "公网地址", "服务器管理", "外观"):
            check(gone not in ixt, "S1 聊天页顶栏九项残留已清除：%s" % gone)
        check(ixt.count('id="newRoom"') == 1,
              "S1 创建房间入口全页只有一处（会话列表顶部＋）",
              ixt.count('id="newRoom"'))
        idup = sorted(x.decode("utf-8", "replace") for x, c in
                      Counter(re.findall(rb'\bid="([^"]+)"', body)).items() if c > 1)
        check(not idup, "S1 聊天页 DOM id 不重复", idup[:6])
        check('id="meSheet"' in ixt and 'data-tab="me"' in sh,
              "S1「我的」在外壳顶栏、聊天页只留弹层承接")
        # S2 开房弹层：控件各一处，且没有一处把 key2/token 写进 DOM
        for sid in ("agentDmSheet", "agentDmMine", "agentDmPeer", "agentDmGo",
                    "agentDmCancel", "closeAgentDm"):
            check(ixt.count('id="%s"' % sid) == 1,
                  "S2 开房弹层控件唯一：%s" % sid, ixt.count('id="%s"' % sid))
        check('id="agentDmAdd"' not in ixt,
              "S2 开房入口由 app.js 造，静态页里不预留第二个")
        dm_block = (ixt.split('id="agentDmSheet"', 1)[1].split('id="roomMenuSheet"', 1)[0]
                    if 'id="agentDmSheet"' in ixt and 'id="roomMenuSheet"' in ixt else "")
        check(bool(dm_block) and dm_block.count("<select") == 2,
              "S2 开房弹层存在且正好两个下拉", dm_block.count("<select"))
        check(bool(dm_block) and "key2" not in dm_block,
              "S2 开房弹层不出现 key2（开房只需要 client_id）")

        # ---- 第二个人 ----
        st, obj2, _ = req(adm, "/api/admin/user", "POST", {"name": "二号", "client_id": "second"})
        check(st == 200 and obj2["user"]["client_id"] == "second", "管理页新建成员")
        st, _, _ = req(adm, "/api/admin/user", "POST", {"name": "重复", "client_id": "second"})
        check(st == 400, "重复登录名被拒")

        # ---- 人登录 ----
        a = WS(ws_url, {"type": "auth", "kind": "user", "username": "tester", "key": "12345678"})
        first = await a.open()
        rooms = {r["slug"]: r for r in first["rooms"]}
        check(first["me"]["client_id"] == "tester", "人登录即可用")
        check("lobby" in rooms and "own-tester" in rooms and "dm-second-tester" in rooms,
              "房间列表含大厅/我的/私聊")
        check(all(r.get("unread") == 0 for r in first["rooms"]), "无游标时未读全 0")

        await a.send({"type": "message", "room": "lobby", "content": "第一条"})
        m = await a.next("message")
        check(m.get("content") == "第一条" and m.get("id"), "大厅发消息并回显")
        await a.next("ack")

        # ---- 第二个人带游标 ----
        b = WS(ws_url, {"type": "auth", "kind": "user", "username": "second",
                        "key": obj2["user"]["key1"], "last_read": {"lobby": 0}})
        fb = await b.open()
        rb = {r["slug"]: r for r in fb["rooms"]}
        check(rb["lobby"]["unread"] == 1, "带 last_read=0 时未读数为 1",
              rb["lobby"])
        check(rb["lobby"]["last_id"] == m["id"], "房间字段带 last_id")
        await b.send({"type": "history", "room": "lobby", "since": 0})
        h = await b.next("history")
        check(len(h["messages"]) == 1 and h["messages"][0]["content"] == "第一条",
              "history since 只补增量")

        # ---- 智能体 ----
        ag = WS(ws_url, {"type": "auth", "kind": "agent", "client_id": agent_cid, "key": key2})
        fa = await ag.open()
        check(fa["me"]["kind"] == "agent" and fa["me"]["owner"] == "tester", "智能体用二级 Key 登录")
        await ag.send({"type": "message", "room": "own-tester", "content": "智能体在主人房里"})
        check((await ag.next("message")).get("content") == "智能体在主人房里", "智能体进主人房")
        await ag.send({"type": "message", "room": "own-second", "content": "越权"})
        e = await ag.next("error")
        check(e.get("code") == "NO_PERM", "智能体进不了别人的房")

        # ---- 房间成员第二级 ----
        await b.send({"type": "message", "room": "own-tester", "content": "还没被拉进去"})
        e = await b.next("error")
        check(e.get("code") == "NO_PERM", "非房间成员被拒")

        st, obj, _ = req(adm, "/api/admin/rooms", "POST", {})
        room_ids = {r["room_id"] for r in obj.get("rooms", [])}
        check("own-tester" in room_ids and "own-second" in room_ids, "房间列表含个人房")

        st, obj, _ = req(adm, "/api/admin/room/members", "POST",
                         {"action": "add", "room": "own-tester", "client_ids": ["second"]})
        added = [m["client_id"] for r in obj.get("rooms", [])
                 if r["room_id"] == "own-tester" for m in r["members"]]
        check(st == 200 and "second" in added, "管理口把成员加入房间", obj)

        await b.send({"type": "message", "room": "own-tester", "content": "被拉进来后发言"})
        mm = await b.next("message", contains="被拉进来后发言")
        check(mm.get("room") == "own-tester", "加入后能在该房间发言")

        await b.send({"type": "rooms", "last_read": {}})
        wr = await b.next("authenticated")
        check(any(r["slug"] == "own-tester" and r["kind"] == "shared"
                  for r in wr["rooms"]), "加入后房间列表出现共享房")

        st, obj, _ = req(adm, "/api/admin/room/members", "POST",
                         {"action": "remove", "room": "own-tester", "client_id": "second"})
        check(st == 200, "管理口把成员移出房间")
        await b.send({"type": "message", "room": "own-tester", "content": "移出后再试"})
        e = await b.next("error")
        check(e.get("code") == "NO_PERM", "移出后再次被拒")

        st, obj, _ = req(adm, "/api/admin/room/members", "POST",
                         {"action": "add", "room": "lobby", "client_ids": ["second"]})
        check(st in (400, 404), "大厅不需要单独配置房间成员")
        st, obj, _ = req(adm, "/api/admin/room/members", "POST",
                         {"action": "add", "room": "own-tester", "client_ids": ["nonexistent"]})
        check(st == 404, "给不存在成员加房被拒")

        # ---- @ 唤醒 ----
        await a.send({"type": "message", "room": "lobby", "content": "@%s 帮忙看看" % agent_cid})
        mm = await a.next("message", contains="帮忙看看")
        check(agent_cid in (mm.get("mentions") or []), "@client_id 能解析成 mentions")

        # ---- 改名与人改资料 ----
        await a.send({"type": "agent_rename", "client_id": agent_cid, "name": "小助手"})
        r = await a.next("agent_renamed")
        check(r.get("name") == "小助手", "本人可改智能体显示名")

        # ---- 接入方式（mode）----
        modes = {m["id"]: m["label"] for m in (first.get("agent_modes") or [])}
        check(modes.get("online", "").startswith("值守在线")
              and modes.get("inbox", "").startswith("离线收件"),
              "登录下发两种接入方式", modes)
        agent_row = [x for x in (first.get("agents") or [])
                     if x["client_id"] == agent_cid][0]
        check(agent_row.get("mode") == "online"
              and agent_row.get("mode_label", "").startswith("值守在线"),
              "已有智能体默认模式一", agent_row.get("mode"))
        prompts = agent_row.get("prompts") or {}
        check(set(prompts) == {"online", "inbox"}
              and prompts.get("online") != prompts.get("inbox")
              and "pubchat_mcp.py" in prompts.get("inbox", "")
              and "断线重连" in prompts.get("online", ""),
              "服务端按模式下发两份可粘贴指令")

        await ag.send({"type": "agent_set_mode", "client_id": agent_cid, "mode": "inbox"})
        e = await ag.next("error")
        check(e.get("code") == "NO_PERM", "智能体自己不能切接入方式")
        await b.send({"type": "agent_set_mode", "client_id": agent_cid, "mode": "inbox"})
        e = await b.next("error")
        check(e.get("code") == "NO_PERM", "非主人不能切接入方式")
        await a.send({"type": "agent_set_mode", "client_id": agent_cid, "mode": "bogus"})
        e = await a.next("error")
        check(e.get("code") == "BAD_MODE", "非法接入方式被拒")

        await a.send({"type": "agent_set_mode", "client_id": agent_cid, "mode": "inbox"})
        am = await a.next("agent_mode")
        check(am.get("mode") == "inbox" and am.get("mode_label", "").startswith("离线收件"),
              "本人把接入方式切到模式二", am.get("mode_label"))
        check((am.get("prompts") or {}).get("inbox", "") == prompts.get("inbox"),
              "切换回帧带回该智能体的模式二指令")

        # 只改标签：不踢线、不影响发言
        await ag.send({"type": "message", "room": "own-tester", "content": "切模式后还在线"})
        check((await ag.next("message")).get("content") == "切模式后还在线",
              "切模式不会把智能体踢下线")

        await a.send({"type": "rooms"})
        wr = await a.next("authenticated")
        again = [x for x in wr.get("agents") or [] if x["client_id"] == agent_cid][0]
        check(again.get("mode") == "inbox", "切模式后重新取状态仍是模式二")

        st, obj, _ = req(adm, "/api/state")
        tester = [u for u in obj["users"] if u["client_id"] == "tester"][0]
        admin_agent = [x for x in tester["agents"] if x["client_id"] == agent_cid][0]
        check(admin_agent.get("mode") == "inbox", "管理口 users_state 带 mode")
        check({m["id"] for m in obj.get("agent_modes") or []} == {"online", "inbox"},
              "/api/state 下发接入方式列表")

        st, obj, _ = req(adm, "/api/admin/agent/mode", "POST",
                         {"client_id": agent_cid, "mode": "online"})
        check(st == 200 and obj.get("mode") == "online", "管理口能切接入方式")
        st, obj, _ = req(adm, "/api/state")
        admin_agent = [x for u in obj["users"] if u["client_id"] == "tester"
                       for x in u["agents"] if x["client_id"] == agent_cid][0]
        check(admin_agent.get("mode") == "online", "管理口切换已落库")
        st, _, _ = req(adm, "/api/admin/agent/mode", "POST",
                       {"client_id": agent_cid, "mode": "bogus"})
        check(st == 400, "管理口拒绝非法接入方式")
        st, _, _ = req(adm, "/api/admin/agent/mode", "POST",
                       {"client_id": "nobody", "mode": "inbox"})
        check(st == 404, "管理口拒绝不存在的智能体")

        await a.send({"type": "agent_set_mode", "client_id": agent_cid, "mode": "inbox"})
        await a.next("agent_mode")
        await a.send({"type": "presence"})
        end = time.time() + 6
        seen = None
        while time.time() < end and seen is None:
            msg = await a.next("presence")
            row = [m for m in msg.get("members") or [] if m["client_id"] == agent_cid]
            if row and row[0].get("mode") == "inbox":
                seen = row[0]
        check(seen is not None, "presence 名单带接入方式")

        await a.send({"type": "agent_set_mode", "client_id": agent_cid, "mode": "online"})
        am = await a.next("agent_mode")
        check(am.get("mode") == "online", "可以切回模式一")

        # 0.8 起新用户席位默认上限 3（首启送 3 个），先由主人抬高自己再新建
        await a.send({"type": "member_agent_limit", "client_id": "tester", "limit": 5})
        ml = await a.next("member_agent_limit_set")
        check(ml.get("limit") == 5 and ml.get("agent_limit") == 5
              and ml.get("client_id") == "tester", "主人可抬高自己的席位上限", ml)

        await a.send({"type": "agent_create", "name": "新助手"})
        ac = await a.next("agent_created")
        check(ac["agent"].get("mode") == "online" and ac["agent"].get("prompts"),
              "新建智能体默认模式一并带指令")
        await a.send({"type": "agent_remove", "client_id": ac["agent"]["client_id"]})
        await a.next("agent_removed")

        await a.send({"type": "agent_create", "name": "收件助手", "mode": "inbox"})
        ac2 = await a.next("agent_created")
        check(ac2["agent"].get("mode") == "inbox"
              and ac2["agent"].get("mode_label", "").startswith("离线收件"),
              "新建智能体可按所选模式二创建", ac2["agent"].get("mode"))
        check("pubchat_mcp.py" in (ac2["agent"].get("prompts") or {}).get("inbox", ""),
              "新建的模式二席位带 MCP 指令")
        await a.send({"type": "agent_remove", "client_id": ac2["agent"]["client_id"]})
        await a.next("agent_removed")

        await a.send({"type": "agent_create", "name": "非法模式", "mode": "bogus"})
        e = await a.next("error")
        check(e.get("code") == "BAD_MODE", "新建智能体拒绝非法接入方式")

        await a.send({"type": "profile", "name": "测试员甲", "username": "tester",
                      "key": "87654321"})
        acct = await a.next("account")
        check(acct.get("relogin") is True and acct.get("name") == "测试员甲", "本人可改显示名与密码")
        st, _, _ = req(adm, "/api/state")
        await ag.close()
        await b.close()
        await a.close()

        # ---- 穿透接口（只读状态，不真的开公网） ----
        st, obj, _ = req(adm, "/api/admin/tunnel", "POST", {"action": "status"})
        check(st == 200 and obj["tunnel"]["state"] in ("off", "error", "starting"),
              "穿透接口返回状态", obj)
        check("available" in obj["tunnel"], "穿透状态带 available")

        # ---- 固定公网地址（手动模式不联网） ----
        st, obj, _ = req(adm, "/api/admin/tunnel", "POST",
                         {"action": "config", "mode": "manual",
                          "public_url": "https://chat.example.com"})
        check(st == 200 and obj["tunnel"]["state"] == "on"
              and obj["tunnel"]["url"] == "https://chat.example.com",
              "固定地址：手动模式立即生效", obj)
        st, obj, _ = req(adm, "/api/state")
        check(obj["tunnel"].get("url") == "https://chat.example.com", "固定地址进入状态")
        check("token" not in obj["tunnel"], "状态不回传隧道 Token")
        st, obj, _ = req(adm, "/api/admin/tunnel", "POST",
                         {"action": "config", "mode": "named",
                          "public_url": "https://chat.example.com"})
        check(st == 400, "固定域名缺 Token 被拒", obj)
        st, obj, _ = req(adm, "/api/admin/tunnel", "POST",
                         {"action": "config", "mode": "quick"})
        check(st == 200 and obj["tunnel"]["mode"] == "quick", "能切回临时模式")
        st, _, body = req(adm, "/admin.html")
        check(st == 200 and b"scanUser" in body, "管理页带扫码登录入口")

        # ---- 实例身份 ----
        st, obj, _ = req(adm, "/api/state")
        check("instance" in obj and obj["instance"].get("instance_id"),
              "状态含实例 identity")
        check(obj["instance"].get("fingerprint") and len(obj["instance"]["fingerprint"]) == 12,
              "实例指纹 12 位")
        inst_file = tmp / "instance.json"
        check(inst_file.is_file(), "instance.json 已写入数据目录")
        st, obj2, _ = req(pub, "/api/instance")
        check(st == 200 and obj2.get("fingerprint"), "对外口 /api/instance 返回指纹")

        # ---- 成员管理页 ----
        st, _, body = req(adm, "/members.html")
        check(st == 200 and b"memberList" in body, "管理口可取成员管理页")
        st, _, body = req(adm, "/members.js")
        check(st == 200 and b"renderMembers" in body, "管理口可取成员管理脚本")
        st, _, body = req(adm, "/shell.html")
        check(st == 200 and "成员".encode() in body, "单窗口外壳含成员页签")

        # ---- 可达性自测 ----
        st, obj, _ = req(adm, "/api/admin/reachability", "POST", {})
        check(st == 200 and "has_public" in obj and "url" in obj, "可达性自测返回结果")

        # ---- 邀请码生成（批量 + 严格一次性） ----
        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "member", "hours": 24, "count": 3})
        check(st == 200 and len(obj.get("invites") or []) == 3, "批量生成 3 个邀请码", obj)
        invites = obj.get("invites") or []
        check(all(i.get("token") for i in invites), "每个邀请码都有 token")
        check(len({i["token"] for i in invites}) == 3, "批量码互不相同")
        check(all("max_uses" not in i for i in invites), "邀请 JSON 不再含 max_uses")
        inv_token = invites[0]["token"]
        inv_url = invites[0]["url"]
        check(inv_url and "join.html" in inv_url, "邀请 URL 指向 join.html")
        check("token=" not in inv_url.split("#")[-1] or inv_token not in inv_url,
              "邀请 URL 不含明文 token（token 在 hash 里）")

        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "readonly", "hours": 1, "count": 1})
        check(st == 200 and obj["invites"][0]["role"] == "readonly", "可生成只读邀请码")
        ro_token = obj["invites"][0]["token"]

        for bad in ({"role": "badrole"}, {"count": 0}, {"count": 21}):
            body = {"action": "create"}
            body.update(bad)
            st, obj, _ = req(adm, "/api/admin/invite", "POST", body)
            check(st == 400, "非法邀请参数被拒 %s" % bad)

        st, obj, _ = req(adm, "/api/state")
        check(len(obj.get("invites", [])) >= 4, "邀请列表含已生成的邀请")
        check(all("max_uses" not in i for i in obj.get("invites", [])),
              "状态列表不含 max_uses")

        # ---- 有效期档位（单机本地宿主必须与云端同口径：0＝永久） ----
        # 浏览器实走时抓到过：本机 /api/admin/invite 用 `hours or 24`，
        # 点「永久」被静默降级成 24 小时。
        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "member", "hours": 0, "count": 1})
        perm_inv = (obj or {}).get("invites") or [{}]
        check(st == 200 and perm_inv[0].get("expires_at") is None,
              "单机 hours=0 生成永久邀请（expires_at 为 null）", perm_inv[0])
        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "member", "hours": 48, "count": 1})
        exp48 = ((obj or {}).get("invites") or [{}])[0].get("expires_at") or ""
        check(st == 200 and "T" in exp48, "单机 hours=48 带到期时间", exp48)
        for bad_h in ("abc", -1, 721):
            st, obj, _ = req(adm, "/api/admin/invite", "POST",
                             {"action": "create", "role": "member", "hours": bad_h, "count": 1})
            check(st == 400, "单机非法 hours 被拒 %r" % (bad_h,), obj)
        st, obj, _ = req(adm, "/api/state")
        perm_rows = [i for i in (obj or {}).get("invites", []) if not i.get("expires_at")]
        check(st == 200 and len(perm_rows) >= 1, "状态列表里永久邀请 expires_at 为空")

        # ---- 加入页可访问 ----
        st, _, body = req(pub, "/join.html")
        check(st == 200 and b"PubChat" in body, "对外口可访问加入页")

        # ---- 通过 WebSocket 加入（批量码各用一次） ----
        batch = [("newbie", "新成员", invites[0]["token"]),
                 ("batchb", "批二", invites[1]["token"]),
                 ("batchc", "批三", invites[2]["token"])]
        jmsgs = []
        for cid, name, tok in batch:
            jws = await websockets.connect(ws_url, open_timeout=10)
            await jws.send(json.dumps({"type": "join", "token": tok,
                                        "name": name, "client_id": cid}))
            jmsgs.append(json.loads(await asyncio.wait_for(jws.recv(), 10)))
            await jws.close()
        check(all(m.get("type") == "joined" and m.get("ok") for m in jmsgs),
              "批量码各用一次都能加入", jmsgs)
        check(jmsgs[0].get("client_id") == "newbie" and jmsgs[0].get("key1"),
              "加入返回 client_id 和 key1")
        newbie_key = jmsgs[0]["key1"]

        # 验证新成员在列表中
        st, obj, _ = req(adm, "/api/state")
        newbie = [u for u in obj["users"] if u["client_id"] == "newbie"]
        check(len(newbie) == 1 and newbie[0]["role"] == "member",
              "新成员出现在列表且角色为 member")
        check(newbie[0].get("source") == "public", "新成员来源标记为 public")

        # ---- 邀请码一次性（用过即废，库里标记 used） ----
        jws2 = await websockets.connect(ws_url, open_timeout=10)
        await jws2.send(json.dumps({"type": "join", "token": inv_token,
                                     "name": "重复", "client_id": "dup"}))
        raw2 = await asyncio.wait_for(jws2.recv(), 10)
        jmsg2 = json.loads(raw2)
        await jws2.close()
        check(jmsg2.get("type") == "error" and jmsg2.get("code") in ("BAD_INVITE", "USED"),
              "一次性邀请码用过即废", jmsg2)
        import server as srv
        inv_db = sqlite3.connect(str(tmp / "pubchat.db"))
        used_row = inv_db.execute(
            "SELECT used, max_uses FROM invites WHERE token_hash=?",
            (srv._hash_token(inv_token),)).fetchone()
        inv_db.close()
        check(used_row is not None and used_row[0] == 1 and used_row[1] == 1,
              "用过的邀请码在库里标记 used=1", used_row)

        # ---- 只读成员服务端拒收消息 ----
        ro_jws = await websockets.connect(ws_url, open_timeout=10)
        await ro_jws.send(json.dumps({"type": "join", "token": ro_token,
                                       "name": "只读者", "client_id": "reader"}))
        ro_raw = await asyncio.wait_for(ro_jws.recv(), 10)
        ro_msg = json.loads(ro_raw)
        check(ro_msg.get("type") == "joined" and ro_msg.get("role") == "readonly",
              "只读邀请码创建只读成员")
        reader_key = ro_msg["key1"]
        ro_jws2 = await websockets.connect(ws_url, open_timeout=10)
        await ro_jws2.send(json.dumps({"type": "auth", "kind": "user",
                                        "username": "reader", "key": reader_key}))
        auth_raw = await asyncio.wait_for(ro_jws2.recv(), 10)
        auth_msg = json.loads(auth_raw)
        check(auth_msg.get("type") == "authenticated", "只读成员可登录")
        pres_raw = await asyncio.wait_for(ro_jws2.recv(), 10)
        pres_msg = json.loads(pres_raw)
        check(pres_msg.get("type") == "presence", "登录后收到 presence")
        await ro_jws2.send(json.dumps({"type": "message", "room": "lobby", "content": "我想说话"}))
        err_raw = await asyncio.wait_for(ro_jws2.recv(), 10)
        err_msg = json.loads(err_raw)
        await ro_jws.close()
        await ro_jws2.close()
        check(err_msg.get("type") == "error" and err_msg.get("code") == "READONLY",
              "只读成员发消息被服务端拒绝", err_msg)

        # ---- 需房主确认：开启 ----
        st, obj, _ = req(adm, "/api/admin/instance", "POST", {"require_approval": True})
        check(st == 200 and obj["instance"]["require_approval"] is True, "开启加入审批")
        st, obj, _ = req(adm, "/api/state")
        check(obj["instance"]["require_approval"] is True, "状态显示审批开关")

        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "member", "hours": 24, "count": 1})
        p_token = obj["invites"][0]["token"]
        pj = await websockets.connect(ws_url, open_timeout=10)
        await pj.send(json.dumps({"type": "join", "token": p_token,
                                  "name": "待审者", "client_id": "pending1"}))
        pm = json.loads(await asyncio.wait_for(pj.recv(), 10))
        await pj.close()
        check(pm.get("type") == "pending" and pm.get("key1"),
              "审批开启时加入进入待审", pm)
        pending_key = pm["key1"]

        pj2 = await websockets.connect(ws_url, open_timeout=10)
        await pj2.send(json.dumps({"type": "auth", "kind": "user",
                                   "username": "pending1", "key": pending_key}))
        em = json.loads(await asyncio.wait_for(pj2.recv(), 10))
        await pj2.close()
        check(em.get("type") == "error" and em.get("code") == "PENDING",
              "待审账号不能登录", em)

        st, obj, _ = req(adm, "/api/state")
        pu = [u for u in obj["users"] if u["client_id"] == "pending1"]
        check(len(pu) == 1 and pu[0]["status"] == "pending", "状态列表含待审成员")

        st, obj, _ = req(adm, "/api/admin/room/members", "POST",
                         {"action": "add", "room": "own-tester", "client_ids": ["pending1"]})
        check(st == 404, "待审成员不能先被拉进房间")

        st, obj, _ = req(adm, "/api/admin/member/approve", "POST",
                         {"client_id": "pending1"})
        check(st == 200, "批准待审成员")
        st, obj, _ = req(adm, "/api/state")
        pu = [u for u in obj["users"] if u["client_id"] == "pending1"]
        check(pu and pu[0]["status"] == "active", "批准后状态变为 active")

        pj3 = await websockets.connect(ws_url, open_timeout=10)
        await pj3.send(json.dumps({"type": "auth", "kind": "user",
                                   "username": "pending1", "key": pending_key}))
        am = json.loads(await asyncio.wait_for(pj3.recv(), 10))
        await pj3.close()
        check(am.get("type") == "authenticated", "批准后可以登录")

        # ---- 需房主确认：拒绝 ----
        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "member", "hours": 24, "count": 1})
        rj_token = obj["invites"][0]["token"]
        rj = await websockets.connect(ws_url, open_timeout=10)
        await rj.send(json.dumps({"type": "join", "token": rj_token,
                                  "name": "被拒者", "client_id": "pending2"}))
        rm = json.loads(await asyncio.wait_for(rj.recv(), 10))
        await rj.close()
        check(rm.get("type") == "pending", "第二个申请进入待审", rm)
        rj_key = rm["key1"]

        st, obj, _ = req(adm, "/api/admin/member/reject", "POST",
                         {"client_id": "pending2"})
        check(st == 200, "拒绝待审成员")
        st, obj, _ = req(adm, "/api/state")
        check(not [u for u in obj["users"] if u["client_id"] == "pending2"],
              "拒绝后成员不在列表")

        rj2 = await websockets.connect(ws_url, open_timeout=10)
        await rj2.send(json.dumps({"type": "auth", "kind": "user",
                                   "username": "pending2", "key": rj_key}))
        rjm = json.loads(await asyncio.wait_for(rj2.recv(), 10))
        await rj2.close()
        check(rjm.get("code") == "BAD_LOGIN", "被拒账号不能登录", rjm)

        # ---- 需房主确认：关闭后直接加入 ----
        st, obj, _ = req(adm, "/api/admin/instance", "POST", {"require_approval": False})
        check(st == 200 and obj["instance"]["require_approval"] is False, "关闭加入审批")
        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "member", "hours": 24, "count": 1})
        d_token = obj["invites"][0]["token"]
        dj = await websockets.connect(ws_url, open_timeout=10)
        await dj.send(json.dumps({"type": "join", "token": d_token,
                                  "name": "直入者", "client_id": "direct1"}))
        dm = json.loads(await asyncio.wait_for(dj.recv(), 10))
        await dj.close()
        check(dm.get("type") == "joined" and dm.get("ok"), "关闭审批后直接加入", dm)

        # ---- 改角色 ----
        st, obj, _ = req(adm, "/api/admin/member/role", "POST",
                         {"client_id": "newbie", "role": "readonly"})
        check(st == 200, "改成员角色为只读")
        st, obj, _ = req(adm, "/api/state")
        newbie2 = [u for u in obj["users"] if u["client_id"] == "newbie"]
        check(newbie2[0]["role"] == "readonly", "角色变更已生效")

        st, obj, _ = req(adm, "/api/admin/member/role", "POST",
                         {"client_id": "newbie", "role": "badrole"})
        check(st == 400, "非法角色被拒")

        # ---- 重置一级 Key ----
        st, obj, _ = req(adm, "/api/admin/member/reset-key", "POST",
                         {"client_id": "newbie"})
        check(st == 200 and obj.get("key1"), "重置一级 Key 返回新 Key")
        new_key1 = obj["key1"]
        check(new_key1 != newbie_key, "新 Key 与旧 Key 不同")

        # 旧 Key 登不上
        jws3 = await websockets.connect(ws_url, open_timeout=10)
        await jws3.send(json.dumps({"type": "auth", "kind": "user",
                                     "username": "newbie", "key": newbie_key}))
        raw3 = await asyncio.wait_for(jws3.recv(), 10)
        msg3 = json.loads(raw3)
        await jws3.close()
        check(msg3.get("code") == "BAD_LOGIN", "旧 Key 登录被拒")

        # 新 Key 能登上
        jws4 = await websockets.connect(ws_url, open_timeout=10)
        await jws4.send(json.dumps({"type": "auth", "kind": "user",
                                     "username": "newbie", "key": new_key1}))
        raw4 = await asyncio.wait_for(jws4.recv(), 10)
        msg4 = json.loads(raw4)
        await jws4.close()
        check(msg4.get("type") == "authenticated", "新 Key 可登录")

        # ---- 重置智能体二级 Key ----
        st, obj, _ = req(adm, "/api/state")
        newbie3 = [u for u in obj["users"] if u["client_id"] == "newbie"][0]
        agent1_cid = newbie3["agents"][0]["client_id"]
        old_k2 = newbie3["agents"][0]["key2"]
        st, obj, _ = req(adm, "/api/admin/agent/reset-key", "POST",
                         {"client_id": agent1_cid})
        check(st == 200 and obj.get("key2"), "重置智能体二级 Key")
        check(obj["key2"] != old_k2, "新二级 Key 与旧的不同")

        # ---- 踢人 ----
        kick_ws = await websockets.connect(ws_url, open_timeout=10)
        await kick_ws.send(json.dumps({"type": "auth", "kind": "user",
                                       "username": "reader", "key": reader_key}))
        await asyncio.wait_for(kick_ws.recv(), 10)
        st, obj, _ = req(adm, "/api/admin/member/remove", "POST",
                         {"client_id": "reader"})
        check(st == 200, "踢出成员")
        kicked = False
        try:
            while True:
                await asyncio.wait_for(kick_ws.recv(), 5)
        except websockets.ConnectionClosed:
            kicked = True
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                await kick_ws.close()
            except Exception:
                pass
        check(kicked, "在线成员被踢后连接立即断开")
        st, obj, _ = req(adm, "/api/state")
        readers = [u for u in obj["users"] if u["client_id"] == "reader"]
        check(len(readers) == 0, "被踢成员不在列表中")

        st, obj, _ = req(adm, "/api/admin/member/remove", "POST",
                         {"client_id": "nonexistent"})
        check(st == 404, "踢不存在的成员返回 404")

        # ---- 删除邀请 ----
        st, obj, _ = req(adm, "/api/state")
        inv_count_before = len(obj.get("invites", []))
        if inv_count_before > 0:
            inv_id = obj["invites"][0]["id"]
            st, obj2, _ = req(adm, "/api/admin/invite", "POST",
                              {"action": "delete", "id": inv_id})
            check(st == 200, "删除邀请码")
            st, obj3, _ = req(adm, "/api/state")
            check(len(obj3.get("invites", [])) == inv_count_before - 1, "删除后列表减少")

        # ---- 数据模型验证：messages 表结构未变 ----
        db_path = tmp / "pubchat.db"
        conn = sqlite3.connect(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        conn.close()
        check(cols == {"id", "room", "sender", "sender_name", "body", "mentions", "created_at"},
              "messages 表结构未变", cols)

        # ---- C期：部署模式与三段连通测试 ----
        st, obj, _ = req(adm, "/api/state")
        check("mode" in obj and obj["mode"] == "local", "/api/state 返回 mode=local", obj.get("mode"))
        check("remote_url" in obj and obj["remote_url"] == "", "/api/state 返回 remote_url 为空")

        st, obj, _ = req(adm, "/api/deploy")
        check(obj.get("ok") and obj.get("mode") == "local", "GET /api/deploy 返回当前模式")

        # test_remote 三段式：DNS 失败
        st, obj, _ = req(adm, "/api/deploy", "POST",
                         {"action": "test_remote", "url": "http://this-host-does-not-exist-xyz123.invalid"})
        check(st == 200 and obj.get("result", {}).get("reachable") is False,
              "test_remote DNS 失败时 reachable=false")
        check("stages" in obj.get("result", {}) and "dns" in obj["result"]["stages"],
              "test_remote 返回 stages.dns")
        check(obj["result"]["stages"]["dns"].get("ok") is False, "DNS 段报错")

        # test_remote 三段式：TCP 失败（用一个不可达的 IP）
        st, obj, _ = req(adm, "/api/deploy", "POST",
                         {"action": "test_remote", "url": "http://192.0.2.1:19999"})
        check(st == 200 and obj.get("result", {}).get("reachable") is False,
              "test_remote TCP 不可达时 reachable=false")
        check("stages" in obj.get("result", {}) and "tcp" in obj["result"]["stages"],
              "test_remote 返回 stages.tcp")
        check(obj["result"]["stages"].get("dns", {}).get("ok") is True, "DNS 段通过（IP 能解析）")
        check(obj["result"]["stages"].get("tcp", {}).get("ok") is False, "TCP 段报错")

        # test_remote 三段式：对本机成功
        st, obj, _ = req(adm, "/api/deploy", "POST",
                         {"action": "test_remote", "url": "http://127.0.0.1:%d" % pub})
        check(st == 200 and obj.get("result", {}).get("reachable") is True,
              "test_remote 本机三段全通过", obj.get("result"))
        stages = obj.get("result", {}).get("stages", {})
        check(stages.get("dns", {}).get("ok") is True, "本机 DNS 段通过")
        check(stages.get("tcp", {}).get("ok") is True, "本机 TCP 段通过")
        check(stages.get("ws", {}).get("ok") is True, "本机 WS 段通过")

        # 切换到 remote 模式
        st, obj, _ = req(adm, "/api/deploy", "POST",
                         {"action": "set_remote", "url": "http://127.0.0.1:%d" % pub})
        check(st == 200 and obj.get("mode") == "remote", "set_remote 切到 remote 模式")
        st, obj, _ = req(adm, "/api/state")
        check(obj.get("mode") == "remote" and obj.get("remote_url") == "http://127.0.0.1:%d" % pub,
              "/api/state 反映 remote 模式")

        # 切回 local 模式
        st, obj, _ = req(adm, "/api/deploy", "POST",
                         {"action": "set_remote", "url": ""})
        check(st == 200 and obj.get("mode") == "local", "set_remote url=空 切回 local")
        st, obj, _ = req(adm, "/api/state")
        check(obj.get("mode") == "local", "/api/state 反映 local 模式")

        # ---- 重启后数据还在 ----
        stop(proc)
        proc = spawn()
        check(wait_ready(adm), "重启后管理口可用")
        st, obj, _ = req(adm, "/api/state")
        names = {u["client_id"]: u["name"] for u in obj["users"]}
        check(names.get("tester") == "测试员甲" and "second" in names, "重启后账号与资料留存")
        st, _, _ = req(adm, "/")
        check(st == 200, "重启后直接进管理页而不是向导")

        a2 = WS(ws_url, {"type": "auth", "kind": "user", "username": "tester", "key": "87654321"})
        f2 = await a2.open()
        check(f2["me"]["name"] == "测试员甲", "改了密码后新密码能登录")
        await a2.close()
    finally:
        stop(proc)
        out = ""
        try:
            out = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        global LAST_LOG
        LAST_LOG = out

    print("  —— 主流程结束，%d 通过，%d 失败" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


async def run_import():
    """老库导入：未初始化时从管理页上传 pubchat.db，账号和历史一起接管。"""
    import server

    pub, adm = free_port(), free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-import-"))
    legacy = tmp / "legacy.db"
    st = server.Store(legacy)
    st.add_user("olduser", "老用户", "12345678", 1)
    st.add_agent("olduser_a1", "老用户-1", "olduser", "abcdefgh12345")
    st.add_message("lobby", "olduser", "老用户", "老消息", [])
    st.conn.close()

    log_path = tmp / "server.log"

    def spawn():
        logf = open(log_path, "ab")
        p = subprocess.Popen(
            TARGET + ["--no-browser", "--no-gui", "--host", "127.0.0.1", "--port", str(pub),
                      "--admin-port", str(adm), "--data-dir", str(tmp)],
            cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT)
        p._logf = logf
        return p

    proc = spawn()
    try:
        check(wait_ready(adm), "导入：实例启动")
        check(bool(admin_login(adm, tmp)), "导入：兑换设备凭证")
        st_code, obj = post_raw(adm, "/api/import", b"not a sqlite file")
        check(st_code == 400 and "SQLite" in (obj or {}).get("error", ""),
              "导入：非 SQLite 文件被拒", obj)
        st_code, obj = post_raw(adm, "/api/import", legacy.read_bytes())
        check(st_code == 200 and obj.get("users") == 1, "导入：老库接管成功", obj)
        st, _, body = req(pub, "/")
        check(st == 200 and b"PubChat" in body, "导入：对外口直接进聊天页")

        w = WS("ws://127.0.0.1:%d/ws" % pub,
               {"type": "auth", "kind": "user", "username": "olduser", "key": "12345678"})
        f = await w.open()
        check(f["me"]["client_id"] == "olduser", "导入：老账号能登录")
        await w.send({"type": "history", "room": "lobby", "limit": 5})
        h = await w.next("history")
        check(any(m["content"] == "老消息" for m in h["messages"]), "导入：历史消息还在")
        await w.close()

        st_code, obj = post_raw(adm, "/api/import", legacy.read_bytes())
        check(st_code == 409, "导入：已有数据时拒绝覆盖", obj)
    finally:
        stop_tree(proc)
        try:
            out = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            out = ""
        global LAST_LOG
        LAST_LOG = (LAST_LOG + "\n" + out)[-6000:]
    return 1 if FAIL else 0



def unit_role_migration():
    """role 迁移：老库最早创建的账号是 owner，其余 member；新库不重复迁移。"""
    from core.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="pubchat-role-"))
    legacy = tmp / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript(
        "CREATE TABLE users(client_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "key1 TEXT NOT NULL, is_admin INTEGER DEFAULT 0, created_at TEXT);")
    conn.executemany("INSERT INTO users VALUES(?,?,?,?,?)", [
        ("first", "一", "k1", 1, "2026-01-01T00:00:00+08:00"),
        ("second", "二", "k2", 0, "2026-01-02T00:00:00+08:00"),
        ("third", "三", "k3", 0, "2026-01-03T00:00:00+08:00"),
    ])
    conn.commit()
    conn.close()
    st = Store(legacy)
    roles = {u["client_id"]: u["role"] for u in st.users_with_role()}
    st.conn.close()
    check(roles == {"first": "owner", "second": "member", "third": "member"},
          "老库迁移：最早 owner 其余 member", roles)

    broken = tmp / "broken.db"
    conn = sqlite3.connect(str(broken))
    conn.executescript(
        "CREATE TABLE users(client_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "key1 TEXT NOT NULL, is_admin INTEGER DEFAULT 0, created_at TEXT, "
        "role TEXT DEFAULT 'owner');")
    conn.executemany(
        "INSERT INTO users(client_id,name,key1,is_admin,created_at,role) "
        "VALUES(?,?,?,?,?,?)", [
            ("a", "甲", "k1", 1, "2026-01-01T00:00:00+08:00", "owner"),
            ("b", "乙", "k2", 0, "2026-01-02T00:00:00+08:00", "owner"),
        ])
    conn.commit()
    conn.close()
    st = Store(broken)
    roles = {u["client_id"]: u["role"] for u in st.users_with_role()}
    st.conn.close()
    check(roles == {"a": "owner", "b": "member"}, "全是 owner 的旧库被纠正", roles)

    fresh = tmp / "fresh.db"
    st = Store(fresh)
    st.add_user("x", "X", "12345678", 0, role="member")
    st.add_user("y", "Y", "12345678", 0, role="owner")
    st.conn.close()
    st = Store(fresh)
    roles = {u["client_id"]: u["role"] for u in st.users_with_role()}
    st.conn.close()
    check(roles == {"x": "member", "y": "owner"}, "新库重开不覆盖已有角色", roles)


def unit_paths():
    """数据目录选择规则：默认 LOCALAPPDATA；exe 同级已有可写 pubchat-data/ 才便携。"""
    import hosts.local as local_host

    class A:
        def __init__(self, **kw):
            self.data_dir = kw.get("data_dir", "")
            self.portable = kw.get("portable", False)

    saved = {k: os.environ.get(k) for k in ("LOCALAPPDATA",)}

    fake = Path(tempfile.mkdtemp(prefix="pubchat-env-"))
    os.environ["LOCALAPPDATA"] = str(fake)
    orig_app = local_host.app_dir
    try:
        local_host.app_dir = lambda: fake / "app"
        (fake / "app").mkdir()
        p, e = local_host.resolve_data_dir(A())
        check(p == fake / "PubChat" and e is None, "默认数据目录落在 %LOCALAPPDATA%\\PubChat", (p, e))
        p, e = local_host.resolve_data_dir(A(portable=True))
        check(p == fake / "app" / "pubchat-data" and e is None, "便携模式放 exe 同级", (p, e))
        (fake / "app" / "pubchat-data").mkdir()
        p, e = local_host.resolve_data_dir(A())
        check(p == fake / "app" / "pubchat-data" and e is None, "同级已有 pubchat-data 时自动便携", (p, e))
        p, e = local_host.resolve_data_dir(A(data_dir=str(fake / "x")))
        check(p == (fake / "x").resolve() and e is None, "--data-dir 最优先", (p, e))
    finally:
        local_host.app_dir = orig_app
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def unit_settings_defaults():
    """settings.json 默认值包含 autostart: false。"""
    import hosts.local as local_host

    fake = Path(tempfile.mkdtemp(prefix="pubchat-settings-"))
    orig_data = local_host.server.DATA if hasattr(local_host, 'server') else None
    try:
        # 模拟 server.DATA
        class FakeServer:
            DATA = fake
        local_host.server = FakeServer()

        # 测试新文件默认值
        cfg = local_host.load_settings()
        check("autostart" in cfg and cfg["autostart"] is False,
              "新配置默认 autostart=false", cfg.get("autostart"))
        check("shortcut" in cfg and cfg["shortcut"] is False,
              "新配置默认 shortcut=false", cfg.get("shortcut"))
        check("domain_prompted" in cfg and cfg["domain_prompted"] is False,
              "新配置默认 domain_prompted=false", cfg.get("domain_prompted"))

        # 测试老配置兼容（没有 autostart 键）
        old_cfg = {"mode": "local", "remote_url": ""}
        with open(fake / "settings.json", "w", encoding="utf-8") as f:
            json.dump(old_cfg, f)
        cfg = local_host.load_settings()
        check(cfg.get("autostart") is False,
              "老配置补上 autostart=false", cfg.get("autostart"))
        check(cfg.get("shortcut") is False,
              "老配置补上 shortcut=false", cfg.get("shortcut"))
    finally:
        if orig_data is not None:
            local_host.server.DATA = orig_data


def unit_instances():
    """实例列表：老 remote_url 迁移、地址规范化、上限、去重、本机保护。"""
    import hosts.local as local_host

    items, migrated = local_host.normalize_instances({"remote_url": "https://x.example/pub/"})
    check(migrated is True and len(items) == 2 and items[0]["id"] == "local",
          "老 remote_url 迁成实例且本机在首位", items)
    check(items[1]["id"] == "remote-legacy"
          and items[1]["url"] == "https://x.example/pub",
          "迁移项去掉末尾斜杠", items[1])

    items, migrated = local_host.normalize_instances({"instances": []})
    check(migrated is False and len(items) == 1,
          "显式空实例列表不再迁回 remote_url", items)
    items, _ = local_host.normalize_instances(
        {"instances": [], "remote_url": "https://x.example"})
    check(len(items) == 1, "用户删光实例后不会被 remote_url 复活", items)

    cfg = {"instances": []}
    for i in range(local_host.MAX_INSTANCES - 1):
        local_host.add_instance(cfg, "https://h%d.example" % i)
    check(len(local_host.instances_state(cfg)) == local_host.MAX_INSTANCES,
          "实例上限 %d（含本机）" % local_host.MAX_INSTANCES)
    try:
        local_host.add_instance(cfg, "https://overflow.example")
        check(False, "超过实例上限被拒")
    except ValueError as exc:
        check("最多" in str(exc), "超过实例上限被拒", str(exc))

    cfg2 = {"instances": []}
    a = local_host.ensure_instance(cfg2, "https://dup.example/pub")
    b = local_host.ensure_instance(cfg2, "https://dup.example/pub/")
    check(a and b and a["id"] == b["id"]
          and len(local_host.instances_state(cfg2)) == 2,
          "ensure_instance 按地址去重", (a, b))
    check(local_host.ensure_instance(cfg2, "bad url") is None,
          "ensure_instance 拒坏地址")


def unit_pointer_errors():
    """指针损坏/目标不可用时报错到 UI 措辞。"""
    from hosts.windows_setup import read_pointer, write_pointer, _pointer_path
    import hosts.local as local_host

    fake = Path(tempfile.mkdtemp(prefix="pubchat-pointer-"))
    saved_localappdata = os.environ.get("LOCALAPPDATA")
    try:
        os.environ["LOCALAPPDATA"] = str(fake)

        # 测试 1：指针不存在
        target, err, path = read_pointer()
        check(target is None and err is not None and "不存在" in err,
              "指针不存在时返回错误信息", (target, err))

        # 测试 2：指针损坏（JSON 格式错误）
        ptr = _pointer_path()
        ptr.parent.mkdir(parents=True, exist_ok=True)
        with open(ptr, "w", encoding="utf-8") as f:
            f.write("{bad json")
        target, err, path = read_pointer()
        check(target is None and err is not None and "损坏" in err,
              "指针损坏时返回错误信息", (target, err))

        # 测试 3：指针目标不存在
        write_pointer(fake / "nonexistent" / "dir")
        target, err, path = read_pointer()
        check(err is not None and "不存在" in err and path is not None,
              "指针目标不存在时返回错误且带 path", (target, err, path))

        # 测试 4：resolve_data_dir 返回错误
        class A:
            data_dir = ""
            portable = False

        p, e = local_host.resolve_data_dir(A())
        check(p is None and e is not None,
              "resolve_data_dir 指针目标不可用时返回错误", (p, e))
    finally:
        if saved_localappdata is not None:
            os.environ["LOCALAPPDATA"] = saved_localappdata
        else:
            os.environ.pop("LOCALAPPDATA", None)


# --------------------------------------------------------------------------- #
# D期 / C期：版本比较、发布源清单、三段自检、前端硬约束、纯客户端模式
# --------------------------------------------------------------------------- #
def unit_version_compare():
    """版本号比较：预发布号必须排在正式版后面，畸形串宁可报无更新。"""
    from hosts import update_check as uc

    check(uc.is_newer("0.4.0", "0.3.0") is True, "0.4.0 比 0.3.0 新")
    check(uc.is_newer("0.4.0", "0.4.0") is False, "同版本不算新")
    check(uc.is_newer("0.3.0", "0.3.0-rc1") is True,
          "0.3.0 正式版比 0.3.0-rc1 新", (uc.version_key("0.3.0"), uc.version_key("0.3.0-rc1")))
    check(uc.is_newer("0.3.0-rc2", "0.3.0-rc1") is True, "rc2 比 rc1 新")
    check(uc.is_newer("0.3.0-rc1", "0.3.0-rc1") is False, "同 rc 不算新")
    check(uc.is_newer("0.10.0", "0.9.0") is True, "0.10.0 比 0.9.0 新（不是字符串序）")
    check(uc.is_newer("", "0.3.0") is False, "空版本号不算新")
    check(uc.is_newer("abc", "0.3.0") is False, "畸形版本号不算新")


def _fake_manifest(**over):
    m = {
        "api": 1,
        "released_at": "2026-11-08T21:00:00+08:00",
        "channel": "stable",
        "note": "本轮更新说明",
        "ends": {
            "desktop": {"version": "0.4.0", "size": 44829036, "sha256": "a" * 64,
                        "url": "https://pub.example.org/pubchat-dist/PubChat-0.4.0.exe",
                        "min": "Windows 10", "junk_key": "不该透传"},
            "android": {"version": "0.2.0", "size": 287195, "sha256": "b" * 64,
                        "url": "https://pub.example.org/pubchat-dist/PubChat-0.2.0.apk"},
            "server": {"version": "0.4.0", "size": 130949, "sha256": "c" * 64,
                       "url": "https://pub.example.org/pubchat-dist/PubChat-server-0.4.0.zip",
                       "schema": 1},
        },
    }
    m.update(over)
    return m


def unit_update_check():
    """发布源检查的每条分支都要能被注入 reader 复现，不靠真实网络。"""
    import urllib.error
    from hosts import update_check as uc

    URL = "https://pub.example.org/pubchat-dist/latest.json"

    def reader_for(obj):
        body = obj if isinstance(obj, (bytes, str)) else json.dumps(obj)
        return lambda u, t: body

    def code_of(res):
        return res.get("code")

    # —— 配置类失败：不发网络 —— #
    r = uc.check("", "0.3.0-rc1")
    check(r["ok"] is False and r["code"] == "no_url", "未配置清单地址报 no_url", r)
    r = uc.check("http://pub.example.org/latest.json", "0.3.0-rc1")
    check(r["code"] == "not_https" and r["ok"] is False, "http:// 清单一律拒收", r)
    r = uc.check("https:///no-host", "0.3.0-rc1")
    check(r["code"] == "bad_url", "取不到域名的地址报 bad_url", r)

    # —— TOFU：首次与换域名都要确认 —— #
    hit = []

    def counting_reader(u, t):
        hit.append(u)
        return json.dumps(_fake_manifest())

    r = uc.check(URL, "0.3.0-rc1", trusted_host="", reader=counting_reader)
    check(r["ok"] is False and r["need_confirm"] and r["code"] == "confirm_first",
          "首次配置发布源要先确认域名", r)
    check(hit == [], "未确认时一次网络请求都不发", hit)
    check("pub.example.org" in r["error"], "确认提示里带完整域名", r["error"])

    r = uc.check(URL, "0.3.0-rc1", trusted_host="", confirm_host="pub.example.org",
                 reader=counting_reader)
    check(r["ok"] is True and r["has_update"] is True, "确认后清单生效且发现新版本", r)
    check(r["latest"] == "0.4.0" and r["current"] == "0.3.0-rc1", "回显最新与当前版本", r)
    check(r["manifest"]["ends"]["desktop"]["version"] == "0.4.0", "清单 ends 交给前端展示")
    check("junk_key" not in r["manifest"]["ends"]["desktop"],
          "清单未知键不透传", r["manifest"]["ends"]["desktop"])

    before = len(hit)
    r = uc.check(URL, "0.3.0-rc1", trusted_host="other.example.com", reader=counting_reader)
    check(r["need_confirm"] and r["code"] == "host_changed" and len(hit) == before,
          "换发布源域名要再确认一次，且不发请求", r)

    # —— 已忽略的版本不再提醒 —— #
    r = uc.check(URL, "0.3.0-rc1", trusted_host="pub.example.org",
                 reader=counting_reader, dismissed_version="0.4.0")
    check(r["ok"] is True and r["has_update"] is False and r["dismissed"] == "0.4.0",
          "忽略过的版本 has_update=false", r)

    # —— 不比当前新 —— #
    r = uc.check(URL, "0.4.0", trusted_host="pub.example.org", reader=counting_reader)
    check(r["ok"] is True and r["has_update"] is False, "当前已是清单版本时不算有更新", r)

    # —— 网络与解析失败 —— #
    def boom(u, t):
        raise urllib.error.HTTPError(u, 404, "Not Found", {}, None)

    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=boom)
    check(r["code"] == "http_error" and "404" in r["error"], "HTTP 错误码原样报", r)

    def slow(u, t):
        raise socket.timeout("timed out")

    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=slow)
    check(r["code"] == "timeout", "超时报 timeout", r)

    def offline(u, t):
        raise urllib.error.URLError("名字未解析")

    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=offline)
    check(r["code"] == "network", "连不上报 network", r)

    def moved(u, t):
        raise urllib.error.URLError("发布源跨域重定向，已拒绝")

    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=moved)
    check(r["code"] == "cross_host", "跨域重定向单独报 cross_host", r)

    def oversized(u, t):
        raise ValueError("清单过大")

    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=oversized)
    check(r["code"] == "too_large", "超过 64KB 的清单报 too_large", r)

    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=lambda u, t: "{oops")
    check(r["code"] == "bad_json", "非法 JSON 报 bad_json", r)
    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=lambda u, t: "[1,2]")
    check(r["code"] == "bad_json", "顶层不是对象也报 bad_json", r)
    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org",
                 reader=lambda u, t: b"\xff\xfe bad bytes")
    check(r["code"] == "bad_json", "非 UTF-8 报 bad_json", r)

    # —— 清单结构 —— #
    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org",
                 reader=reader_for(_fake_manifest(api=2)))
    check(r["code"] == "newer_api", "api 不认识时当不可用，不误报有更新", r)
    check(r["has_update"] is False, "api 不认识时不触发提醒", r)

    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org",
                 reader=reader_for(_fake_manifest(ends={})))
    check(r["code"] == "incomplete", "缺 ends 报 incomplete", r)

    nover = _fake_manifest()
    nover["ends"]["desktop"].pop("version")
    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=reader_for(nover))
    check(r["code"] == "incomplete", "ends 缺 version 报 incomplete", r)

    # —— 外链： 硬要求 —— #
    outside = _fake_manifest()
    outside["ends"]["android"]["url"] = "https://evil.example.net/a.apk"
    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=reader_for(outside))
    check(r["ok"] is False and r["code"] == "outside_url" and "外链" in r["error"],
          "清单含外链时整体拒绝", r)

    plain = _fake_manifest()
    plain["ends"]["server"]["url"] = "http://pub.example.org/a.zip"
    r = uc.check(URL, "0.3.0", trusted_host="pub.example.org", reader=reader_for(plain))
    check(r["code"] == "outside_url" and "HTTPS" in r["error"],
          "清单含非 HTTPS 下载地址也整体拒绝", r)

    # —— 落盘字段（apply_check） —— #
    cfg = {"update": {"manifest_url": URL, "trusted_host": "pub.example.org",
                      "last_checked": "", "last_result": "", "dismissed_version": ""}}
    cfg, res = uc.apply_check(cfg, "0.3.0-rc1", reader=counting_reader)
    up = cfg["update"]
    check(res["ok"] and up["last_result"] == "ok" and up["last_checked"],
          "检查成功写 last_result=ok 与时间", up)
    check(up["has_update"] is True and up["latest_version"] == "0.4.0",
          "结论落盘 has_update/latest_version", up)
    check(up["trusted_host"] == "pub.example.org", "信任域名保持")

    cfg2 = {"update": {"manifest_url": URL, "trusted_host": "", "last_result": ""}}
    cfg2, res2 = uc.apply_check(cfg2, "0.3.0", reader=counting_reader)
    check(res2["need_confirm"] and cfg2["update"]["trusted_host"] == ""
          and "manifest" not in cfg2["update"],
          "未确认时不写信任域名、不落清单", cfg2["update"])


def unit_probe_stages():
    """三段自检：HTTP 200 但没有 /ws 握手，绝不能算「能连」。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from hosts import remote_probe as rp

    class Plain(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html>hello"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Plain)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.15)
    try:
        res = rp.probe("http://127.0.0.1:%d" % port)
        check(res["dns"]["ok"] and res["tcp"]["ok"], "假远端 DNS/TCP 段通过", res)
        check(res["ws"]["ok"] is False and res["reachable"] is False,
              "只回 HTTP 200 不算能连", res)
        check(res["stage"] == "ws", "失败点定位在 ws 段", res["stage"])

        dead = free_port()
        res = rp.probe("http://127.0.0.1:%d" % dead)
        check(res["dns"]["ok"] and res["tcp"]["ok"] is False and res["reachable"] is False,
              "端口没人监听时 TCP 段失败", res)
        check(res["stage"] == "tcp", "失败点定位在 tcp 段", res["stage"])

        def bad_resolver(*a, **kw):
            raise socket.gaierror(11001, "name not resolved")

        res = rp.probe("https://this-host-does-not-exist-xyz.invalid", resolver=bad_resolver)
        check(res["dns"]["ok"] is False and res["stage"] == "dns" and res["reachable"] is False,
              "DNS 解析失败单独报", res)

        res = rp.probe("not a url")
        check(res["reachable"] is False and res["stage"] == "url", "地址非法时报 url 段", res)
    finally:
        srv.shutdown()
        srv.server_close()

    url, err = rp.normalize_url("pubchat.example.org")
    check(url == "https://pubchat.example.org" and not err, "缺协议时默认补 https", (url, err))
    url, err = rp.normalize_url("http://127.0.0.1:8765")
    check(url == "http://127.0.0.1:8765" and not err, "显式 http 保留（本机测试用）", (url, err))
    url, err = rp.normalize_url("ftp://example.org")
    check(url == "" and bool(err), "非 http/https 协议直接拒", (url, err))
    url, err = rp.normalize_url("https://pub chat.example.org")
    check(url == "" and "空格" in (err or ""), "地址里有空格直接拒", (url, err))
    url, err = rp.normalize_url("https://例.网络")
    check(url == "" and "写法" in (err or ""), "非法主机名在保存时就拒", (url, err))
    url, err = rp.normalize_url("http://[::1]:8765")
    check(url == "http://[::1]:8765" and not err, "IPv6 地址放行", (url, err))
    check(rp.ws_path_for("https://chat.example.org") == "/ws",
          "根路径的 WS 地址", rp.ws_path_for("https://chat.example.org"))
    check(rp.ws_path_for("https://example.org/pub/") == "/pub/ws",
          "子路径的 WS 地址与 config.js 同规则", rp.ws_path_for("https://example.org/pub/"))
    check(rp.ws_path_for("https://example.org/pub/chat.html") == "/pub/ws",
          "带页面名的子路径也取目录", rp.ws_path_for("https://example.org/pub/chat.html"))


def unit_frontend_locks():
    """iframe 硬点：不加 X-Frame-Options/CSP，不用 https-only 能力，不搞顶层跳转。"""
    def read(*parts):
        return (ROOT / Path(*parts)).read_text(encoding="utf-8", errors="replace")

    http_src = read("core", "http.py") + read("hosts", "local.py") + read("server.py")
    for needle in ("X-Frame-Options", "frame-ancestors"):
        check(needle not in http_src, "服务端不下发 %s" % needle)

    for page in ("about.html", "server.html"):
        src = read("web", page)
        for needle in ("crypto.subtle", "navigator.serviceWorker", "caches.open"):
            check(needle not in src, "%s 不用 %s" % (page, needle))

    for js in ("app.js", "setup.html", "shell.html", "settings.html", "about.html"):
        src = read("web", js)
        for needle in ("top.location", "'_top'", '"_top"'):
            check(needle not in src, "%s 里没有 %s" % (js, needle))

    # 开机自启：文案必须写「会常驻后台、开机自动运行」，默认不勾
    setup_src = read("web", "setup.html")
    check("会常驻后台" in setup_src and "开机自动运行" in setup_src,
          "首启向导的自启文案写明常驻后台")
    check('id="cbAutostart">' in setup_src and 'id="cbAutostart" checked' not in setup_src,
          "自启复选框默认不勾")
    ws_src = read("hosts", "windows_setup.py")
    check("常驻后台" in ws_src, "Startup 快捷方式描述也写明常驻后台")
    check("HKEY" not in ws_src and "registry" not in ws_src.lower(),
          "开机自启不写注册表")


def unit_windows_shortcuts():
    """快捷方式/开机自启：创建 → 磁盘存在 → 删除 → 再删不报错，且不碰注册表。
    APPDATA / USERPROFILE 指到临时目录，绝不往需求方的真实桌面和启动目录写东西。"""
    from hosts import windows_setup as ws

    saved = {k: os.environ.get(k) for k in ("APPDATA", "USERPROFILE")}
    fake = Path(tempfile.mkdtemp(prefix="pubchat-lnk-"))
    try:
        os.environ["APPDATA"] = str(fake / "Roaming")
        os.environ["USERPROFILE"] = str(fake / "Profile")

        check(ws.autostart_enabled() is False, "默认没有开机自启快捷方式（磁盘为准）")
        check(ws.shortcut_exists() is False, "默认没有桌面快捷方式")

        # 没东西时删除也要幂等成功
        ok_, err_ = ws.set_autostart(False)
        check(ok_ is True and not err_, "未创建时关闭开机自启不报错", err_)
        ok_, err_ = ws.set_shortcut(False)
        check(ok_ is True and not err_, "未创建时删除桌面快捷方式不报错", err_)

        ok_, err_ = ws.set_autostart(True, data_dir=str(fake))
        check(ok_ is True, "创建开机自启快捷方式", err_)
        check(ws.autostart_enabled() is True, "开机自启在磁盘上确实存在")
        lnk = ws._startup_dir() / "PubChat.lnk"
        check(lnk.is_file() and lnk.stat().st_size > 0, "Startup 目录里能看到 .lnk 文件")

        ok_, err_ = ws.set_shortcut(True, data_dir=str(fake))
        check(ok_ is True and ws.shortcut_exists(), "创建桌面快捷方式", err_)

        ok_, err_ = ws.set_autostart(False)
        check(ok_ is True and ws.autostart_enabled() is False, "关闭后磁盘上不再有 .lnk", err_)
        ok_, err_ = ws.set_autostart(False)
        check(ok_ is True and ws.autostart_enabled() is False, "重复关闭仍然幂等", err_)
        ok_, err_ = ws.set_shortcut(False)
        check(ok_ is True and ws.shortcut_exists() is False, "删除桌面快捷方式", err_)
        ok_, err_ = ws.set_shortcut(False)
        check(ok_ is True, "重复删除仍然幂等", err_)

        # 全程只碰 Startup / Desktop 两个目录
        created = sorted(str(p.relative_to(fake)) for p in fake.rglob("*") if p.is_file())
        check(created == [], "收尾后临时目录里不留任何文件（都删干净了）", created)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def unit_data_migration():
    """数据目录迁移：WAL 未落盘也要带走、行数校验、失败回滚不删源、目标校验。"""
    import server
    from hosts import windows_setup as ws

    saved = {k: os.environ.get(k) for k in ("LOCALAPPDATA",)}
    fake = Path(tempfile.mkdtemp(prefix="pubchat-move-"))
    os.environ["LOCALAPPDATA"] = str(fake / "Local")
    src = fake / "src"
    src.mkdir()
    db = src / "pubchat.db"

    def rows(path):
        c = sqlite3.connect(str(path))
        try:
            return [c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                    for t in ("users", "agents", "messages")]
        finally:
            c.close()

    try:
        # 造一个 WAL 模式、有未 checkpoint 页的库（裸拷必丢最后一段的那种）
        c = sqlite3.connect(str(db))
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        for i in range(5):
            c.execute("INSERT INTO users(name) VALUES (?)", ("u%d" % i,))
        c.execute("INSERT INTO messages(body) VALUES ('最后一条还没落盘')")
        c.commit()
        wal = src / "pubchat.db-wal"
        check(wal.is_file() and wal.stat().st_size > 0,
              "构造出未 checkpoint 的 -wal（裸拷会丢数据的前提）")
        c.close()

        # server 模块在失败路径里会 boot，先把它的 DATA 钉在临时目录
        old_data, old_store = server.DATA, getattr(server, "STORE", None)
        server.DATA = src
        server.STORE = None

        before = rows(db)

        # —— 目标校验 —— #
        ok_, err_, _ = ws.pick_data_dir(str(src), str(db))
        check(ok_ is False and "相同" in err_, "目标等于源目录直接拒", err_)
        ok_, err_, _ = ws.pick_data_dir(str(src / "inner"), str(db))
        check(ok_ is False and "子目录" in err_, "目标在源里面直接拒", err_)
        busy = fake / "busy"
        busy.mkdir()
        (busy / "keep.txt").write_text("x", encoding="utf-8")
        ok_, err_, _ = ws.pick_data_dir(str(busy), str(db))
        check(ok_ is False and "不为空" in err_, "目标非空直接拒", err_)
        check(db.is_file() and rows(db) == before, "被拒的三次都没动源库")

        # —— 成功迁移：WAL 里的最后一条必须带走 —— #
        dst = fake / "dst"
        ok_, err_, new_db = ws.pick_data_dir(str(dst), str(db))
        check(ok_ is True, "迁移成功", err_)
        check(new_db and Path(new_db).is_file(), "新位置有 pubchat.db", new_db)
        check(rows(new_db) == before, "三张表行数一致", (before, rows(new_db)))
        c = sqlite3.connect(str(new_db))
        try:
            last = c.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]
        finally:
            c.close()
        check(last == "最后一条还没落盘",
              "未 checkpoint 的 WAL 尾部数据也迁走了（不是裸拷）", last)
        check(not db.is_file(), "迁移成功后删掉源库")
        target, perr, _ = ws.read_pointer()
        check(target == dst.resolve() and perr is None, "指针已指向新目录", (target, perr))

        # —— 失败回滚：坏库不许删源、不许改指针 —— #
        src2 = fake / "src2"
        src2.mkdir()
        bad = src2 / "pubchat.db"
        bad.write_bytes(b"this is not a database at all")
        server.DATA = src2
        ok_, err_, new_db = ws.pick_data_dir(str(fake / "dst2"), str(bad))
        check(ok_ is False and "迁移失败" in (err_ or ""), "迁移失败时明确报失败", err_)
        check(bad.is_file(), "失败后源文件还在（没删源）")
        check(not (fake / "dst2" / "pubchat.db").exists(), "失败后不留半截目标库")
        target2, err2, _ = ws.read_pointer()
        check(target2 == dst.resolve(), "失败后指针仍指上一次成功的新目录", (target2, err2))
        server.DATA = old_data
        server.STORE = old_store
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def run_remote_mode():
    """模式矩阵：remote 不监听对外口，管理口与部署接口照常（设计稿 §9）。"""
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-remote-"))
    (tmp / "settings.json").write_text(
        json.dumps({"mode": "remote", "remote_url": "http://127.0.0.1:19999"}),
        encoding="utf-8")

    pub, adm = free_port(), free_port()
    log_path = tmp / "server.log"
    logf = open(log_path, "ab")
    proc = subprocess.Popen(
        TARGET + ["--no-browser", "--no-gui", "--host", "127.0.0.1", "--port", str(pub),
                  "--admin-port", str(adm), "--data-dir", str(tmp)],
        cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT)
    proc._logf = logf
    try:
        check(wait_ready(adm), "remote 模式下管理口照常起")
        check(bool(admin_login(adm, tmp)), "remote 模式兑换设备凭证")

        def open_(p):
            s = socket.socket()
            s.settimeout(0.6)
            try:
                s.connect(("127.0.0.1", p))
                return True
            except Exception:
                return False
            finally:
                s.close()

        time.sleep(0.6)
        check(open_(adm) is True and open_(pub) is False,
              "remote 模式不监听对外端口 %d" % pub,
              {"admin": open_(adm), "public": open_(pub)})
        st, obj, _ = req(adm, "/api/deploy")
        check(st == 200 and obj.get("mode") == "remote" and obj.get("serving") is False,
              "/api/deploy 回显 mode=remote 且 serving=false", obj)
        check(obj.get("restart_needed") is False, "模式与运行时一致时不需要重启", obj)
        check(obj.get("remote_url") == "http://127.0.0.1:19999", "/api/deploy 带远端地址", obj)
        st, obj, _ = req(adm, "/api/state")
        check(obj.get("mode") == "remote" and obj.get("serving") is False,
              "/api/state 同样反映纯客户端状态", obj)
        check(len(obj.get("instances") or []) == 2
              and obj["instances"][1]["id"] == "remote-legacy",
              "老配置的 remote_url 迁成实例并在列表里", obj.get("instances"))

        # 远端不可达时仍然允许切回本机（不能被锁死在坏地址上）
        st, obj, _ = req(adm, "/api/deploy", "POST", {"action": "set_remote", "url": ""})
        check(st == 200 and obj.get("mode") == "local" and obj.get("restart_needed") is True,
              "remote 运行中切回 local 报 restart_needed", obj)
        st, obj, _ = req(adm, "/api/deploy")
        check(obj.get("mode") == "local" and obj.get("serving") is False
              and obj.get("restart_needed") is True,
              "切回后仍未监听，直到重启才生效", obj)
        check(open_(pub) is False, "切回 local 但当次运行不补开对外口")

        # 发布源接口：未确认域名 → need_confirm，确认后仍走同一入口
        url = "https://pub.example.org/pubchat-dist/latest.json"
        st, obj, _ = req(adm, "/api/deploy", "POST", {"action": "set_manifest", "url": url})
        check(st == 200 and obj.get("need_confirm") is True
              and obj.get("host") == "pub.example.org", "set_manifest 首次要确认", obj)
        st, obj, _ = req(adm, "/api/deploy", "POST", {"action": "check_update"})
        check(st == 200 and obj.get("ok") is False and obj.get("need_confirm") is True,
              "未确认时 check_update 不生效", obj)
        st, obj, _ = req(adm, "/api/deploy", "POST",
                         {"action": "set_manifest", "url": "http://pub.example.org/latest.json"})
        check(st == 400 and "HTTPS" in (obj or {}).get("error", ""),
              "set_manifest 拒收 http://", obj)
        st, obj, _ = req(adm, "/api/deploy", "POST", {"action": "set_manifest", "url": ""})
        check(st == 200 and obj.get("host") == "", "清单地址可以清空", obj)

        # 忽略版本：写盘并清掉提醒
        st, obj, _ = req(adm, "/api/deploy", "POST",
                         {"action": "dismiss_update", "version": "0.4.0"})
        check(st == 200 and obj.get("dismissed_version") == "0.4.0", "忽略指定版本", obj)
        cfg = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
        check(cfg["update"]["dismissed_version"] == "0.4.0", "忽略结果落盘", cfg["update"])
    finally:
        stop_tree(proc)
    # 打包 exe 往日志里写的是控制台编码（GBK），源码跑是 UTF-8，
    # 断言中文必须两种都认，否则测的是编码不是行为。
    check("跳过公网隧道" in read_text_loose(log_path),
          "remote 模式启动日志声明跳过隧道")


# --------------------------------------------------------------------------- #
# Stage 2 安全单元测试
# --------------------------------------------------------------------------- #
def unit_rooms_v2():
    import tempfile
    from core.store import Store
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-rooms-"))
    st = Store(tmp / "t.db")
    st.add_user("alice", "Alice", "k12345678")
    st.create_room("g-test", "测试房", "alice")
    check(st.room("g-test")["title"] == "测试房", "建房写入标题")
    check(st.is_room_member("g-test", "alice"), "房主自动进房")
    check(st.count_owned_rooms("alice") == 1, "房主房间计数")
    st.rename_room("g-test", "改名房")
    check(st.room("g-test")["title"] == "改名房", "房间改名")
    from core.rooms import rooms_for, can_access
    ident = {"client_id": "alice", "kind": "human", "role": "member"}
    rs = rooms_for(st, ident, st.users())
    check(any(r["slug"] == "g-test" and r["title"] == "改名房" for r in rs),
          "共享房用真实标题")
    check(can_access(ident, "g-test", st), "房主可进共享房")
    st.archive_room("g-test")
    check(st.room("g-test")["archived"] == 1, "房间归档")
    check(len(st.rooms_all()) == 0, "归档房间不出现在列表")
    check(not any(r["slug"] == "g-test" for r in rooms_for(st, ident, st.users())),
          "归档房不出现在房间列表")
    check(can_access(ident, "g-test", st) is False, "归档房不可进")
    st.conn.close()


def unit_rooms_ws():
    import tempfile
    from core.store import Store, _hash_token
    from core.rooms_ws import (create_room, invite_to_room, archive_room,
                               leave_room, rename_room)
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-rooms-ws-"))
    st = Store(tmp / "t.db")
    st.add_user("alice", "Alice", "k12345678")
    st.add_user("bob", "Bob", "k87654321")
    alice = {"client_id": "alice", "kind": "human", "role": "member"}
    bob = {"client_id": "bob", "kind": "human", "role": "member"}
    ro = {"client_id": "alice", "kind": "human", "role": "owner"}

    p, code, _ = create_room(st, alice, "甲房")
    check(code is None and p["room"]["title"] == "甲房", "成员建房成功", code)
    rid = p["room"]["slug"]
    check(st.room(rid)["owner"] == "alice", "房主写入")
    for name in ("乙房", "丙房"):
        _, code, _ = create_room(st, alice, name)
        check(code is None, "成员第 %s 个房间成功" % name, code)
    _, code, _ = create_room(st, alice, "丁房")
    check(code == "ROOM_LIMIT", "第 4 个房间被限", code)
    _, code, _ = create_room(st, alice, "关闭房", allow=False)
    check(code == "ROOM_CREATE_OFF", "管理员关建房后成员被拒", code)
    _, code, _ = create_room(st, ro, "管理房", allow=False)
    check(code is None, "实例 owner 不受限", code)
    _, code, _ = create_room(st, {"client_id": "x", "kind": "agent", "owner": "alice"}, "智能体房")
    check(code == "NO_PERM", "智能体不能建房", code)

    _, code, _ = invite_to_room(st, bob, rid, 1)
    check(code == "NO_PERM", "非房主不能邀请", code)
    p, code, _ = invite_to_room(st, alice, rid, 2)
    check(code is None and len(p["invites"]) == 2, "房主批量邀请", code)
    inv = st.find_invite_by_hash(_hash_token(p["invites"][0]["token"]))
    check(inv and inv.get("room_id") == rid, "房间邀请带 room_id")

    _, code, _ = rename_room(st, bob, rid, "改不了")
    check(code == "NO_PERM", "非房主不能改名", code)
    _, code, _ = archive_room(st, bob, rid)
    check(code == "NO_PERM", "非房主不能归档", code)

    st.add_room_member(rid, "bob", "member")
    p, code, _ = leave_room(st, bob, rid)
    check(code is None and not st.is_room_member(rid, "bob"), "成员可退房", code)
    _, code, _ = leave_room(st, alice, rid)
    check(code == "NO_PERM", "房主不能退房", code)

    p, code, _ = archive_room(st, alice, rid)
    check(code is None and st.room(rid)["archived"] == 1, "房主归档", code)
    _, code, _ = invite_to_room(st, alice, rid, 1)
    check(code == "NO_ROOM", "归档房不能邀请", code)
    st.conn.close()


def unit_room_join():
    import asyncio
    import tempfile
    from datetime import datetime, timedelta
    from core.store import Store, CST, _hash_token
    from core.members import handle_join_ws
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-room-join-"))
    st = Store(tmp / "t.db")
    st.add_user("alice", "Alice", "k12345678")
    st.create_room("g-join", "加入房", "alice")
    exp = (datetime.now(CST) + timedelta(hours=1)).isoformat(timespec="seconds")

    class Hub:
        def __init__(self):
            self.sent = []

        async def send(self, ws, obj):
            self.sent.append(obj)

    hub = Hub()
    tok = "tok-room-join"
    st.add_invite(_hash_token(tok), "member", exp, "alice", room_id="g-join")
    asyncio.run(handle_join_ws(None, st, hub, {
        "token": tok, "name": "Carol", "client_id": "carol"}, lambda *a: None))
    check(st.is_room_member("g-join", "carol"), "房间邀请加入后自动进房")
    check(hub.sent and hub.sent[0].get("type") == "joined", "房间邀请返回 joined", hub.sent)

    tok2 = "tok-room-pending"
    st.add_invite(_hash_token(tok2), "member", exp, "alice", room_id="g-join")
    asyncio.run(handle_join_ws(None, st, hub, {
        "token": tok2, "name": "Dave", "client_id": "dave"}, lambda *a: None,
        settings={"require_approval": True}))
    check(st.user("dave")["status"] == "pending", "审批模式先 pending")
    check(st.is_room_member("g-join", "dave"), "审批中也先留房籍")
    st.conn.close()


def unit_rooms_admin():
    import tempfile
    from core.store import Store
    from hosts.server_admin import rooms_state
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-rooms-admin-"))
    st = Store(tmp / "t.db")
    st.add_user("alice", "Alice", "k12345678")
    st.create_room("g-admin", "管理房", "alice")
    row = [r for r in rooms_state(st) if r["room_id"] == "g-admin"][0]
    check(row["title"] == "管理房" and row["owner"] == "alice"
          and row["archived"] == 0, "管理端房间元数据", row)
    st.archive_room("g-admin")
    row = [r for r in rooms_state(st) if r["room_id"] == "g-admin"][0]
    check(row["archived"] == 1, "管理端看到归档标记", row)
    st.conn.close()


class _FakeWS:
    """in-process 用例里的假连接：收帧、记录 close，驱动真实 Hub/handler。"""

    def __init__(self, name="ws"):
        self.name = name
        self.frames = []
        self.closed = None

    async def send(self, text):
        self.frames.append(text)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def objs(self):
        out = []
        for f in self.frames:
            try:
                out.append(json.loads(f))
            except Exception:
                pass
        return out

    def last(self, want=None):
        for o in reversed(self.objs()):
            if want is None or o.get("type") == want:
                return o
        return {}


def unit_member_admin():
    """0.8 批1：member_list / member_remove / member_agent_limit 与席位上限生效点。"""
    import asyncio
    from core.store import Store
    from core.ws import Hub, handle_agent_create
    from core.member_admin import (handle_member_agent_limit, handle_member_list,
                                   handle_member_remove, list_members, member_rows,
                                   remove_member, seat_full, seat_limit_of,
                                   set_agent_limit)
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-members-"))
    st = Store(tmp / "t.db")
    hub = Hub(store=st)
    noop = lambda *a: None
    st.add_user("alice", "Alice", "k12345678", 0, role="owner")
    st.add_user("bob", "Bob", "k87654321", 0, role="member")
    for i in (1, 2, 3):
        st.add_agent("bob_a%d" % i, "Bob-%d" % i, "bob", "k2-x-%d" % i)
    OW = {"client_id": "alice", "kind": "human", "role": "owner"}
    ME = {"client_id": "bob", "kind": "human", "role": "member"}

    check(st.user("bob")["agent_limit"] == 3, "建库即带 agent_limit 默认 3")
    check(st.user("alice")["agent_limit"] == 3, "owner 同样默认 3")
    check(seat_limit_of(st, "bob") == 3, "读上限＝3")
    check(seat_limit_of(st, "ghost") == 0, "查不到的账号按不限额兜底")

    p, code, _ = list_members(st, hub, ME)
    check(code == "NO_PERM" and p is None, "普通成员无权看成员名单", code)
    p, code, _ = list_members(st, hub, {"client_id": "bob_a1", "kind": "agent",
                                        "owner": "bob"})
    check(code == "NO_PERM", "智能体身份也无权看成员名单", code)
    ow = _FakeWS()
    asyncio.run(handle_member_list(ow, st, hub, ME))
    check(ow.last("error").get("code") == "NO_PERM", "member_list 帧对非 owner 报 NO_PERM")
    asyncio.run(handle_member_list(ow, st, hub, OW))
    m = ow.last("member_list")
    check(bool(m.get("members")), "owner 拿到 member_list", m)
    rows = {x["client_id"]: x for x in m.get("members", [])}
    check(set(rows) == {"alice", "bob"}, "成员名单含两个人", set(rows))
    check(rows["bob"]["agent_limit"] == 3 and rows["bob"]["role"] == "member",
          "名单带 agent_limit 与 role", rows["bob"])
    check(len(rows["bob"]["agents"]) == 3, "名单带名下三个智能体", rows["bob"]["agents"])
    check(all(a["online"] is False for a in rows["bob"]["agents"]),
          "未连接的智能体 online=False")
    check(rows["bob"]["online"] is False, "未连接成员 online=False")
    bws = _FakeWS()
    hub.add(bws, ME)
    rows2 = {x["client_id"]: x for x in member_rows(st, hub)}
    check(rows2["bob"]["online"] is True, "已连接成员 online=True")
    hub.remove(bws)

    blocked, cur, lim = seat_full(st, "bob")
    check(blocked and cur == 3 and lim == 3, "3/3 时判定席位已满", (blocked, cur, lim))
    bws = _FakeWS()
    asyncio.run(handle_agent_create(bws, st, hub, ME, {"name": "再加一个"}, noop))
    err = bws.last("error")
    check(err.get("code") == "AGENT_LIMIT", "到上限时 agent_create 报 AGENT_LIMIT", err)
    check("3/3" in (err.get("message") or ""), "上限文案带真实数字", err)
    check(len(st.agents_of("bob")) == 3, "被拦时不落库")

    p, code, _ = set_agent_limit(st, ME, "bob", 5)
    check(code == "NO_PERM", "非 owner 不能改上限", code)
    p, code, _ = set_agent_limit(st, OW, "ghost", 5)
    check(code == "NOT_FOUND", "改不存在成员的上限报 NOT_FOUND", code)
    for bad in (-1, 21, "abc", None):
        p, code, _ = set_agent_limit(st, OW, "bob", bad)
        check(code == "BAD_LIMIT", "非法上限 %r 被拒" % (bad,), code)
    p, code, _ = set_agent_limit(st, OW, "bob", 0)
    check(code is None and p["agent_limit"] == 0, "设为 0＝不限额", (p, code))
    check(st.user("bob")["agent_limit"] == 0, "0 已落库")
    asyncio.run(handle_agent_create(bws, st, hub, ME, {"name": "第四个"}, noop))
    check(bws.last("agent_created").get("agent", {}).get("client_id") == "bob_a4",
          "不限额后可建第 4 个", bws.last())
    check(len(st.agents_of("bob")) == 4, "名下现有 4 席")

    lim_ws = _FakeWS()
    asyncio.run(handle_member_agent_limit(lim_ws, st, hub, OW,
                                          {"client_id": "bob", "limit": 1}, noop))
    ack = lim_ws.last("member_agent_limit_set")
    check(ack.get("client_id") == "bob" and ack.get("limit") == 1,
          "member_agent_limit 回 member_agent_limit_set", ack)
    check(st.user("bob")["agent_limit"] == 1, "回包与库一致")
    blocked, cur, lim = seat_full(st, "bob")
    check(blocked and cur == 4 and lim == 1, "存量 4 超上限 1 时只拦新增")
    check(len(st.agents_of("bob")) == 4, "存量超限不追溯删除")
    bws.frames = []
    asyncio.run(handle_agent_create(bws, st, hub, ME, {}, noop))
    check(bws.last("error").get("code") == "AGENT_LIMIT", "超限后新建仍被拦", bws.last())

    st.create_room("g-mem", "管理房", "alice")
    st.add_room_member("g-mem", "bob", "member")
    st.add_room_member("g-mem", "bob_a1", "member")
    cws = _FakeWS()
    hub.add(cws, ME)
    rm_ws = _FakeWS()
    hub.add(rm_ws, OW)
    asyncio.run(handle_member_remove(rm_ws, st, hub, OW, {"client_id": "bob"}, noop))
    check(cws.closed and cws.closed[0] == 1011, "被删者连接以 1011 断开", cws.closed)
    check(cws.last("kicked").get("reason") == "已被移出服务器",
          "断开前先收到 kicked", cws.last())
    check(rm_ws.last("member_removed").get("client_id") == "bob",
          "member_remove 回 member_removed", rm_ws.last())
    check(sorted(rm_ws.last("member_removed").get("agents", [])) ==
          ["bob_a1", "bob_a2", "bob_a3", "bob_a4"], "回执点名连带删除的智能体", rm_ws.last())
    check(any(o.get("type") == "rooms-changed" for o in rm_ws.objs()),
          "移出后广播 rooms-changed")
    check(st.user("bob") is None, "成员账号已删")
    check(st.agents_of("bob") == [], "名下智能体全删")
    left = [r["user_id"] for r in st.q("SELECT * FROM room_members")]
    check("bob" not in left and "bob_a1" not in left, "智能体的房籍连带清干净", left)
    asyncio.run(hub.presence(st))
    pl = [o for o in rm_ws.objs() if o.get("type") == "presence"]
    check(pl and not any(x["client_id"] == "bob" for x in pl[-1]["members"]),
          "presence 里不再出现被删成员", pl[-1]["members"] if pl else None)
    check(all(x["client_id"] != "bob" for x in member_rows(st, hub)), "成员名单同步瘦身")
    p, code, _ = remove_member(st, OW, "alice")
    check(code == "NO_PERM", "不能把自己移出服务器", code)
    p, code, _ = remove_member(st, OW, "ghost")
    check(code == "NOT_FOUND", "删不存在的成员报 NOT_FOUND", code)
    p, code, _ = remove_member(st, OW, "")
    check(code == "NOT_FOUND", "空 client_id 报 NOT_FOUND", code)
    p, code, _ = remove_member(st, ME, "alice")
    check(code == "NO_PERM", "普通成员删人报 NO_PERM", code)
    st.conn.close()


def unit_invite_hours():
    """0.8 批1：邀请有效期 24 / 48 / 永久，非法值回落 24。"""
    from datetime import datetime, timedelta
    from core.store import Store, CST, _hash_token
    from core.members import validate_join_request
    from core.rooms_ws import (INVITE_HOURS_CHOICES, _expires_at, invite_to_instance,
                               invite_to_room)
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-hours-"))
    st = Store(tmp / "t.db")
    st.add_user("alice", "Alice", "k12345678", 0, role="owner")
    st.create_room("g-hour", "有效期房", "alice")
    OW = {"client_id": "alice", "kind": "human", "role": "owner"}
    now = datetime.now(CST)

    def hours_of(iso):
        return round((datetime.fromisoformat(iso) - now).total_seconds() / 3600.0)

    check(INVITE_HOURS_CHOICES == (24, 48, 0), "有效期档位是 24/48/0")
    check(hours_of(_expires_at(24)) == 24, "hours=24 → 24 小时后过期")
    check(hours_of(_expires_at(48)) == 48, "hours=48 → 48 小时后过期")
    check(_expires_at(0) is None, "hours=0 → 永久（NULL）")
    check(hours_of(_expires_at(None)) == 24, "不传 hours 回落 24")
    check(hours_of(_expires_at(7)) == 24, "非法档位 7 回落 24")
    check(hours_of(_expires_at("abc")) == 24, "非数字回落 24")
    check(hours_of(_expires_at("48")) == 48, "字符串 48 可用")

    p, code, _ = invite_to_instance(st, OW, 2, 48)
    check(code is None and len(p["invites"]) == 2, "实例邀请批量生成", code)
    check(all(i["expires_at"] and hours_of(i["expires_at"]) == 48 for i in p["invites"]),
          "实例邀请回包带 48 小时过期时间", p)
    tok48 = p["invites"][0]["token"]
    row = st.find_invite_by_hash(_hash_token(tok48))
    check(row["expires_at"] == p["invites"][0]["expires_at"], "落库过期时间与回包一致")

    p, code, _ = invite_to_instance(st, OW, 1, 0)
    check(code is None and p["invites"][0]["expires_at"] is None,
          "永久邀请回包 expires_at=null", p)
    tok0 = p["invites"][0]["token"]
    row = st.find_invite_by_hash(_hash_token(tok0))
    check(row["expires_at"] is None, "永久邀请落库为 NULL", row)
    inv, err, msg = validate_join_request(st, tok0, "新人", "newguy1")
    check(err is None and inv, "永久邀请不过期可直接加入", (err, msg))

    p, code, _ = invite_to_room(st, OW, "g-hour", 1, 7)
    check(code is None and hours_of(p["invites"][0]["expires_at"]) == 24,
          "房间邀请非法档位回落 24", p)
    p, code, _ = invite_to_room(st, OW, "g-hour", 1, 48)
    check(code is None and hours_of(p["invites"][0]["expires_at"]) == 48,
          "房间邀请支持 48 小时", p)
    past = (datetime.now(CST) - timedelta(minutes=5)).isoformat(timespec="seconds")
    st.add_invite(_hash_token("tok-past"), "member", past, "alice", room_id="g-hour")
    inv, err, msg = validate_join_request(st, "tok-past", "迟到", "latecomer")
    check(err == "EXPIRED", "过期邀请仍被拒", (err, msg))
    st.conn.close()


def unit_agent_dm():
    """0.8 批2：adm- 智能体私聊房——开房、权限、分组、级联。"""
    import shutil
    from core.store import Store
    from core import agent_dm
    from core.agent_dm import can_access, entries_for, open_dm, slug_for, split_slug
    from core.agent_modes import MODES, prompt_for
    from core.rooms import can_access as room_can_access, rooms_for
    from core.rooms_ws import RESERVED
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-agentdm-"))
    st = Store(tmp / "t.db")
    st.add_user("alice", "Alice", "k12345678", 0, role="owner")
    st.add_user("bob", "Bob", "k87654321", 0, role="member")
    st.add_agent("alice_a1", "小喵", "alice", "ka1")
    st.add_agent("alice_a2", "小柴", "alice", "ka2")
    st.add_agent("bob_a1", "鲍勃助手", "bob", "kb1")
    A1 = {"client_id": "alice_a1", "kind": "agent", "owner": "alice"}
    A2 = {"client_id": "alice_a2", "kind": "agent", "owner": "alice"}
    B1 = {"client_id": "bob_a1", "kind": "agent", "owner": "bob"}
    AL = {"client_id": "alice", "kind": "human", "role": "owner"}
    BO = {"client_id": "bob", "kind": "human", "role": "member"}
    RO = {"client_id": "bob", "kind": "human", "role": "readonly"}
    SY = {"client_id": "sys", "kind": "system"}

    check(slug_for("bob_a1", "alice_a1") == "adm-alice_a1-bob_a1", "slug 按两个 agent id 字典序")
    check(split_slug("adm-alice_a1-bob_a1") == ("alice_a1", "bob_a1"), "slug 能解析回配对")
    check(split_slug("adm-onlyone") is None and split_slug("dm-a-b") is None
          and split_slug("adm-") is None, "畸形 adm slug 一律拒")
    check("adm-" in RESERVED, "RESERVED 里占住 adm- 前缀")

    p, c, m = open_dm(st, A1, "alice_a2")
    check(c is None and p["room"] == "adm-alice_a1-alice_a2" and p["created"],
          "智能体自己开一条私聊", (p, c, m))
    check("「小喵」×「小柴」" == p["title"], "标题是「A」×「B」", p["title"])
    p2, _, _ = open_dm(st, A2, "alice_a1")
    check(p2["room"] == p["room"] and not p2["created"] and len(st.agent_dm_rows()) == 1,
          "对面再开一次不重复建行、created 转 False", p2)
    check(open_dm(st, A1, "alice_a1")[1] == "AGENT_DM_SELF", "不能和自己私聊")
    check(open_dm(st, A1, "ghost_a9")[1] == "NO_AGENT", "对方不存在报 NO_AGENT")
    check(open_dm(st, AL, "bob_a1")[1] == "AGENT_DM_AMBIGUOUS",
          "主人名下多个席位又不指明，回 AGENT_DM_AMBIGUOUS")
    p3, c3, _ = open_dm(st, AL, "bob_a1", "alice_a1")
    check(c3 is None and (p3["a"], p3["b"]) == ("alice_a1", "bob_a1"),
          "主人可代自己某个席位开房", (p3, c3))
    check(open_dm(st, BO, "alice_a2", "alice_a1")[1] == "NO_PERM", "主人不能拿别人的席位开房")
    check(open_dm(st, RO, "alice_a1")[1] == "NO_PERM", "只读成员不能开智能体私聊")
    check(open_dm(st, SY, "alice_a1")[1] == "NO_PERM", "system 身份不能开房")

    slug = p["room"]
    check(can_access(A1, "alice_a1", "alice_a2", st) and can_access(A2, "alice_a1", "alice_a2", st),
          "两个参与者都能进")
    check(room_can_access(A1, slug, st) and room_can_access(A2, slug, st),
          "can_access 走 adm- 分支同样放行参与者")
    check(not can_access(B1, "alice_a1", "alice_a2", st), "别人家的智能体进不来")
    check(can_access(AL, "alice_a1", "alice_a2", st)
          and not can_access(BO, "alice_a1", "alice_a2", st),
          "只有参与者各自的主人能进")
    check(not room_can_access(SY, slug, st), "system 身份进不了 adm 房")
    st.add_agent("alice_a3", "小三", "alice", "ka3")
    check(not can_access({"client_id": "alice_a3", "kind": "agent", "owner": "alice"},
                         "alice_a1", "alice_a2", st), "同主人的兄弟席位也不是参与者")
    check(not can_access(AL, "ghost_a1", "alice_a1", st), "配对行不存在时不留幽灵房间")

    rows = entries_for(st, A1)
    check({r["slug"] for r in rows} == {slug, "adm-alice_a1-bob_a1"}
          and all(r["kind"] == "agent_dm" for r in rows), "发起方看到自己名下两条协作房", rows)
    check([r["slug"] for r in entries_for(st, B1)] == ["adm-alice_a1-bob_a1"],
          "别人只看到与自己相关的那条")
    listed = {r["slug"]: r for r in rooms_for(st, A2, st.users())}
    hit = listed.get(slug) or {}
    check(hit.get("kind") == "agent_dm" and "last_id" in hit and "unread" in hit,
          "rooms_for 里带出 agent_dm 分组与未读字段", hit)
    check("adm-alice_a1-bob_a1" not in listed, "A2 看不到不属于它的协作房")
    st.add_message(slug, "alice_a1", "小喵", "在吗", [])
    after = [r for r in rooms_for(st, A2, st.users()) if r["slug"] == slug][0]
    check(int(after["last_id"]) >= 1, "私聊消息抬高了房间游标", after)

    from core.tasks_ws import room_participants
    pool = {x["client_id"] for x in room_participants(st, A1, slug)}
    check(pool == {"alice_a1", "alice_a2", "alice"},
          "adm 房 @全体成员 只含两个智能体与各自主人", pool)
    st.add_user("carol", "Carol", "kc12345678", 0, role="member")
    st.add_agent("carol_a1", "卡罗助手", "carol", "kc1")
    away, c_open, _ = open_dm(st, BO, "carol_a1")
    check(c_open is None and away["room"] == "adm-bob_a1-carol_a1",
          "两个普通成员的智能体也能开一条", away)
    check(not room_can_access(AL, away["room"], st),
          "实例 owner 进不了别人家两个智能体的私聊房")
    check("alice" not in {x["client_id"] for x in
                          room_participants(st, B1, away["room"])},
          "实例 owner 不会被算进智能体私聊的 @名单", away["room"])

    st.conn.execute("UPDATE agent_dm SET archived=1 WHERE a=? AND b=?",
                    ("alice_a1", "alice_a2"))
    st.conn.commit()
    check(not can_access(A1, "alice_a1", "alice_a2", st)
          and not [r for r in entries_for(st, A1) if r["slug"] == slug],
          "归档的配对既进不去也不上列表")
    p4, _, _ = open_dm(st, A1, "alice_a2")
    check(p4["created"] and st.agent_dm("alice_a1", "alice_a2")["archived"] == 0,
          "重新 open 会把归档配对复活")

    st.remove_agent("bob_a1")
    check(not [r for r in st.agent_dm_rows() if "bob_a1" in (r["a"], r["b"])],
          "删席位连带清掉它的配对（不留幽灵房）")
    st.add_agent("bob_a2", "鲍勃二号", "bob", "kb2")
    open_dm(st, A1, "bob_a2")
    st.remove_user("bob")
    check(all("bob_" not in (r["a"], r["b"]) for r in st.agent_dm_rows()),
          "移出成员同样清掉其智能体的配对")

    for mode in MODES:
        text = prompt_for(mode, "alice_a1", "ka1")
        check("智能体私聊" in text or ("私聊" in text and "各自主人" in text),
              "%s 模式指令里有智能体私聊那一行" % mode, text[-160:])
        check("agent_dm" in text, "%s 模式指令点明 agent_dm 这条通道" % mode)
        # 口径翻转：批2 时 pubchat_agent_dm_open 还不存在，所以当时禁的是「承诺一个没有的入口」；
        # 已把该工具落进 pubchat_mcp.py，现在要求指令给出完整帧 JSON，模式二还要点名工具。
        check('"type":"agent_dm_open"' in text and '"type":"agent_dm_opened"' in text,
              "%s 模式指令给完整帧 JSON，不只报帧名" % mode, text[-200:])
        if mode == "inbox":
            check("pubchat_agent_dm_open" in text,
                  "模式二工具清单点名 pubchat_agent_dm_open")
        check("{client_id}" not in text and "{key2}" not in text,
              "%s 模式指令渲染后不留身份占位符" % mode)

    uni = Path(os.environ.get("PUBCHAT_UNI_DIR") or (ROOT / "uni-client"))
    js = uni / "utils" / "pubchat.js"
    if js.exists():
        text = js.read_text(encoding="utf-8")
        check("agent_dm_opened" in text and "openAgentDm" in text,
              "uni 数据层登记了 agent_dm_opened 与 openAgentDm")
        home = (uni / "pages" / "home" / "home.vue").read_text(encoding="utf-8")
        check("agentsOpen: false" in home and "智能体协作" in home
              and "LS_AGENTS" in home and "agent_dm" in home,
              "uni 消息列表有默认收起的智能体协作分组")
        check("'kicked'" in text, "uni 仍保留 kicked 断连处理（批1 修复不退化）")
    else:
        print("  (跳过 uni 静态项：未找到 %s)" % js)
    shutil.rmtree(tmp, ignore_errors=True)


def unit_mobile_native():
    main_java = read_text_loose(ROOT / "android" / "java" / "dev" / "pubchat" / "client" / "MainActivity.java")
    manifest = read_text_loose(ROOT / "android" / "AndroidManifest.xml")
    onboarding = read_text_loose(ROOT / "web" / "onboarding.html")
    build = read_text_loose(ROOT / "android" / "build.ps1")
    v31 = read_text_loose(ROOT / "android" / "res" / "values-v31" / "styles.xml")
    check("PubChatNative" in main_java, "APK: 有 JS bridge")
    check("pubchatBack" in main_java and "onBackPressed" in main_java,
          "APK: 返回先走应用内 pubchatBack")
    check("clearBlankHistory" in main_java, "APK: 清掉 about:blank 历史")
    check("onboarding_done" in main_java and "onboarding.html" in main_java,
          "APK: 首启引导标记与本地资源")
    check('android:versionCode="5"' in manifest and 'android:versionName="0.3.0"' in manifest,
          "APK: 版本 0.3.0 / versionCode 5")
    check('android:scheme="pubchat"' in manifest, "APK: 自定义深链 scheme")
    # 引导四步标题只有一份（web/guide.js）。APK 那份是手抄的 assets，
    # 这里逐字对标题，改一边忘另一边就会红。
    guide_js = read_text_loose(ROOT / "web" / "guide.js")
    guide_titles = re.findall(r"t: '([^']+)'", guide_js)
    check(len(guide_titles) == 4, "引导恰好四步（guide.js）")
    for gi, gt in enumerate(guide_titles):
        check(gt in onboarding, "APK 引导第 %d 步与 guide.js 同标题：%s" % (gi + 1, gt))
    check("-A $assets" in build and "onboarding.html" in build,
          "APK 构建: 打包 assets/onboarding.html")
    check("windowSplashScreenBackground" in v31 and "#ededed" in v31,
          "APK: Android 12+ Splash 主题")


def unit_stage2_security():
    """Stage 2 安全红线单元测试：transport_ok / 限流 / bootstrap / 审计 / 认证。"""
    import hashlib
    import hmac as hmac_mod
    import hosts.server_admin as sa
    from core.store import Store

    # ---- Red Line #1: transport_ok 默认不信任 X-Forwarded-Proto ---- #
    old_trust = sa.TRUST_PROXY
    old_server = sa.SERVER_MODE
    try:
        sa.TRUST_PROXY = False
        sa.SERVER_MODE = False
        check(sa.transport_ok("127.0.0.1", {}) is True,
              "transport_ok: local 模式回环直连始终放行")
        check(sa.transport_ok("::1", {}) is True,
              "transport_ok: IPv6 回环放行")
        check(sa.transport_ok("10.0.0.1", {"X-Forwarded-Proto": "https"}) is False,
              "transport_ok: 默认不信任 XFP，即使写了 https 也拒")
        check(sa.transport_ok("10.0.0.1", {}) is False,
              "transport_ok: 非回环且无 trust-proxy 直接拒")

        sa.TRUST_PROXY = True
        check(sa.transport_ok("10.0.0.1", {"X-Forwarded-Proto": "https"}) is True,
              "transport_ok: trust-proxy 开启 + HTTPS 放行")
        check(sa.transport_ok("10.0.0.1", {"X-Forwarded-Proto": "http"}) is False,
              "transport_ok: trust-proxy 开启但 HTTP 仍拒")
        check(sa.transport_ok("10.0.0.1", {}) is False,
              "transport_ok: trust-proxy 开启但没 XFP 头也拒")

        # SERVER_MODE: server 宿主回环也需要 X-Forwarded-Proto: https
        sa.SERVER_MODE = True
        sa.TRUST_PROXY = True
        check(sa.transport_ok("127.0.0.1", {"X-Forwarded-Proto": "https"}) is True,
              "transport_ok: server 模式回环 + HTTPS 放行")
        check(sa.transport_ok("127.0.0.1", {}) is False,
              "transport_ok: server 模式回环无 XFP 拒绝（nginx 场景修复）")
        check(sa.transport_ok("127.0.0.1", {"X-Forwarded-Proto": "http"}) is False,
              "transport_ok: server 模式回环 + HTTP 拒绝")
        check(sa.transport_ok("10.0.0.1", {"X-Forwarded-Proto": "https"}) is True,
              "transport_ok: server 模式非回环 + trust-proxy + HTTPS 放行")
    finally:
        sa.TRUST_PROXY = old_trust
        sa.SERVER_MODE = old_server

    # ---- Red Line #4: hash_secret 用 SHA-256，authenticate 用 Bearer ---- #
    h1 = sa.hash_secret("test-secret")
    h2 = sa.hash_secret("test-secret")
    h3 = sa.hash_secret("other-secret")
    check(h1 == h2, "hash_secret: 同输入同输出")
    check(h1 != h3, "hash_secret: 不同输入不同输出")
    check(len(h1) == 64, "hash_secret: SHA-256 .hexdigest() 64 字符")

    # ---- Red Line #7: 三层限流 ---- #
    sa.reset_rate_limits()
    for i in range(10):
        ok_ = sa.rate_ok("test_bucket", "1.2.3.4", 10, 60)
    check(ok_ is True, "rate_ok: 第 10 次仍在限额内")
    ok_ = sa.rate_ok("test_bucket", "1.2.3.4", 10, 60)
    check(ok_ is False, "rate_ok: 第 11 次超限拒绝")
    check(sa.rate_ok("test_bucket", "5.6.7.8", 10, 60) is True,
          "rate_ok: 不同 IP 独立计数")
    check(sa.rate_ok("other_bucket", "1.2.3.4", 10, 60) is True,
          "rate_ok: 不同 bucket 独立计数")
    sa.reset_rate_limits()

    # ---- is_loopback / real_ip ---- #
    check(sa.is_loopback("127.0.0.1") is True, "is_loopback: 127.0.0.1")
    check(sa.is_loopback("::1") is True, "is_loopback: ::1")
    check(sa.is_loopback("localhost") is True, "is_loopback: localhost")
    check(sa.is_loopback("127.0.0.2") is True, "is_loopback: 127.0.0.0/8")
    check(sa.is_loopback("10.0.0.1") is False, "is_loopback: 外网 IP")
    check(sa.real_ip("127.0.0.1", {"X-Real-IP": "203.0.113.5"}) == "203.0.113.5",
          "real_ip: 回环来源看 X-Real-IP")
    check(sa.real_ip("10.0.0.1", {"X-Real-IP": "203.0.113.5"}) == "10.0.0.1",
          "real_ip: 非回环直接用 peer IP")

    # ---- Red Line #5: 审计文件写入 + 每次 chmod ---- #
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-audit-"))
    try:
        sa.audit(tmp, "#1 test", "127.0.0.1", "test-action", "ok", "detail", store=None)
        sa.audit(tmp, "#1 test", "127.0.0.1", "test-action2", "ok", "detail2", store=None)
        from datetime import datetime as dt
        from core.store import CST as _CST
        fname = "admin-audit-%s.jsonl" % dt.now(_CST).strftime("%Y%m")
        fpath = tmp / fname
        check(fpath.is_file(), "审计文件已创建")
        lines = fpath.read_text(encoding="utf-8").strip().split("\n")
        check(len(lines) == 2, "审计文件两条记录", "got %d" % len(lines))
        rec = json.loads(lines[0])
        check(rec["device"] == "#1 test" and rec["action"] == "test-action",
              "审计记录字段正确")
        # Red Line #5: 审计里不出现 token/secret/code
        check("token" not in lines[0].lower() and "secret" not in lines[0].lower(),
              "审计记录不含 token/secret")
        if os.name == "posix":
            mode = oct(fpath.stat().st_mode & 0o777)
            check(mode == "0o600", "审计文件权限 600", "got %s" % mode)
            # 修复验证：即使权限被改成 644，下次写入也会纠正回 600
            os.chmod(str(fpath), 0o644)
            check(oct(fpath.stat().st_mode & 0o777) == "0o644", "预设：权限已改为 644")
            sa.audit(tmp, "#1 test", "127.0.0.1", "test-action3", "ok", "detail3",
                     store=None)
            mode2 = oct(fpath.stat().st_mode & 0o777)
            check(mode2 == "0o600", "审计 chmod 每次写入都纠正", "got %s" % mode2)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- Red Line #2/#3: bootstrap 生命周期（用内存 DB） ---- #
    db_path = Path(tempfile.mkdtemp(prefix="pubchat-bs-")) / "test.db"
    try:
        store = Store(db_path)
        # generate_bootstrap: 生成 token + code，code 只存进程内存
        token, code, expires = sa.generate_bootstrap(store)
        check(len(token) > 20, "bootstrap token 足够长")
        check(len(code) == 6 and code.isdigit(), "bootstrap code 是 6 位数字")
        # token 哈希在 DB 里，code 不在 DB 里（只在内存）
        row = store.find_bootstrap_by_hash(sa.hash_secret(token))
        check(row is not None, "按 token 哈希找到 bootstrap 行")
        # 建议1 修复：code 只存内存，DB code_hash 列为空
        check(row["code_hash"] == "", "code 不存 DB 哈希（只存进程内存）")
        # code 在进程内存里
        with sa._code_lock:
            check(sa._code_mem.get(sa.hash_secret(token)) == code,
                  "code 存在进程内存 _code_mem 中")
        # DB 里不含 token/code 明文
        db_bytes = db_path.read_bytes()
        check(token.encode() not in db_bytes, "DB 里不含 token 明文")
        check(code.encode() not in db_bytes, "DB 里不含 code 明文")

        # bootstrap_state: 查状态
        bid = row["id"]
        st = store.bootstrap_state(bid)
        check(st["used"] == 0 and st["attempts"] == 0, "初始状态: used=0, attempts=0")

        # bump_bootstrap_attempts: 错码计数
        info = store.bump_bootstrap_attempts(bid)
        check(int(info["attempts"]) == 1, "错码一次 attempts=1")
        for _ in range(3):
            store.bump_bootstrap_attempts(bid)
        info = store.bump_bootstrap_attempts(bid)
        check(int(info["attempts"]) == 5, "错码五次 attempts=5")

        # burn_bootstrap: 作废
        store.burn_bootstrap(bid)
        row2 = store.find_bootstrap_by_hash(sa.hash_secret(token))
        check(row2["used"] == 1, "burn 后 used=1")

        # handle_claim: 已用 token 返回 403 + 模糊提示
        sa.reset_rate_limits()
        status, obj = sa.handle_claim(store, str(db_path.parent),
                                      {"token": token, "code": code, "name": "test"},
                                      "127.0.0.1")
        check(status == 403, "已用 bootstrap 再认领返回 403")
        check("expired" not in obj and "burned" not in obj,
              "已用 bootstrap 不泄露状态（无 expired/burned 键）")

        # handle_claim: 错码五次后作废（新 bootstrap）+ 模糊提示
        sa.reset_rate_limits()
        token2, code2, _ = sa.generate_bootstrap(store)
        row2 = store.find_bootstrap_by_hash(sa.hash_secret(token2))
        bid2 = row2["id"]
        for i in range(5):
            status, obj = sa.handle_claim(
                store, str(db_path.parent),
                {"token": token2, "code": "000000", "name": "test"},
                "127.0.0.1")
            check(status == 403, "第 %d 次错码返回 403" % (i + 1))
        # 建议2 修复：五次错码后返回模糊提示，不带 burned/expires 状态
        check("burned" not in obj, "五次错码不泄露 burned 状态")
        check("expired" not in obj, "五次错码不泄露 expired 状态")
        st2 = store.bootstrap_state(bid2)
        check(st2["used"] == 1, "五次错码后 bootstrap 已作废")

        # handle_claim: 正确码认领成功
        sa.reset_rate_limits()
        token3, code3, _ = sa.generate_bootstrap(store)
        status, obj = sa.handle_claim(
            store, str(db_path.parent),
            {"token": token3, "code": code3, "name": "TestDevice"},
            "127.0.0.1", "Mozilla/5.0 (iPhone)")
        check(status == 200 and obj.get("ok"), "正确码认领返回 200")
        check("secret" in obj and len(obj["secret"]) > 20, "认领返回含 device_secret")
        check("device" in obj and obj["device"]["name"] == "TestDevice",
              "设备名用 payload 里的 name（TestDevice）")
        # 认领成功后 code 从内存清除
        with sa._code_lock:
            check(sa._code_mem.get(sa.hash_secret(token3)) is None,
                  "认领成功后 code 从 _code_mem 清除")
        # device_name_from_ua: name 为空时从 UA 推导
        check(sa.device_name_from_ua("Mozilla/5.0 (iPhone)") == "iPhone",
              "device_name_from_ua: iPhone UA → iPhone")
        check(sa.device_name_from_ua("Mozilla/5.0 (Android 14)") == "Android 设备",
              "device_name_from_ua: Android UA → Android 设备")
        check(sa.device_name_from_ua("") == "新设备",
              "device_name_from_ua: 空 UA → 新设备")
        # Red Line #4: secret 只在返回值里，DB 里只存哈希
        sec = obj["secret"]
        dev = store.device_by_hash(sa.hash_secret(sec))
        check(dev is not None, "DB 里按 secret 哈希找到设备")
        check(dev["secret_hash"] == sa.hash_secret(sec), "DB 存的是哈希不是明文")

        # authenticate: Bearer token 认证
        from websockets.datastructures import Headers
        fake_headers = Headers([("Authorization", "Bearer " + sec)])
        authed = sa.authenticate(store, fake_headers)
        check(authed is not None and authed["id"] == dev["id"],
              "authenticate: 正确 Bearer 通过")
        bad_headers = Headers([("Authorization", "Bearer wrong-secret")])
        check(sa.authenticate(store, bad_headers) is None,
              "authenticate: 错误 Bearer 返回 None")
        no_headers = Headers([])
        check(sa.authenticate(store, no_headers) is None,
              "authenticate: 无 Authorization 返回 None")
        cookie_headers = Headers([("Cookie", "session=abc")])
        check(sa.authenticate(store, cookie_headers) is None,
              "authenticate: Cookie 认证不支持（Red Line #4）")

        # Red Line #10: schema 兼容——老表结构不变
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            check("admin_bootstrap" in tables, "schema: admin_bootstrap 表存在")
            check("admin_device" in tables, "schema: admin_device 表存在")
            check("admin_audit" in tables, "schema: admin_audit 表存在")
            check("users" in tables, "schema: users 表仍在")
            check("messages" in tables, "schema: messages 表仍在")
        finally:
            conn.close()

        # clear_expired_bootstraps: 启动清理
        sa.reset_rate_limits()
        token4, code4, _ = sa.generate_bootstrap(store)
        store.clear_expired_bootstraps()
        row4 = store.find_bootstrap_by_hash(sa.hash_secret(token4))
        check(row4 is not None and row4["used"] == 0,
              "clear_expired_bootstraps: 未过期的不被清")

        store.conn.close()
    finally:
        import shutil
        shutil.rmtree(db_path.parent, ignore_errors=True)

    # ---- Red Line #6: rollback 不含密钥 ---- #
    tmp2 = Path(tempfile.mkdtemp(prefix="pubchat-rb-"))
    try:
        inst = {"instance_id": "test-id", "name": "TestChat", "require_approval": True}
        rec = sa.snapshot_rollback(tmp2, inst, "instance")
        rb_text = (tmp2 / "admin-rollback.json").read_text(encoding="utf-8")
        check("secret" not in rb_text.lower(), "rollback 文件不含 secret")
        check("key" not in rb_text.lower() or "keyboard" in rb_text.lower(),
              "rollback 文件不含 key")
        check(rec["action"] == "instance", "rollback 记录 action")
        rb2 = sa.read_rollback(tmp2)
        check(rb2 is not None and rb2["instance"]["name"] == "TestChat",
              "read_rollback 读回快照")
        sa.clear_rollback(tmp2)
        check(sa.read_rollback(tmp2) is None, "clear_rollback 后读回 None")
    finally:
        import shutil
        shutil.rmtree(tmp2, ignore_errors=True)

    # ---- Red Line #8: public_meta 不含敏感信息 ---- #
    meta = sa.public_meta({"instance_id": "abc123def456", "name": "Test"}, "0.5.0")
    check(meta["ok"] is True, "public_meta: ok=True")
    check(meta["fingerprint"] == "abc123def456", "public_meta: fingerprint 是 id 前 12 位")
    check("secret" not in json.dumps(meta), "public_meta: 不含 secret")
    check("token" not in json.dumps(meta), "public_meta: 不含 token")

    # ---- 设备凭证前端接线：认领成功后管理页必须能带 Bearer ---- #
    web = ROOT / "web"
    dev_js = (web / "device.js").read_text(encoding="utf-8")
    check("PubChatDevice" in dev_js and "Authorization" in dev_js
          and "Bearer" in dev_js, "device.js: 注入 Bearer 头")
    check("/api/admin/" in dev_js and "localStorage" in dev_js,
          "device.js: 只覆盖管理 API，凭证存 localStorage")
    for page in ("admin.html", "members.html", "claim.html"):
        text = (web / page).read_text(encoding="utf-8")
        check('src="device.js"' in text, "%s: 加载 device.js" % page)
    claim = (web / "claim.html").read_text(encoding="utf-8")
    check("location.hash" in claim and "replaceState" in claim,
          "claim.html: token 只从 fragment 取并立刻清掉")
    check("PubChatDevice.set" in claim, "claim.html: 认领成功后保存设备凭证")

    # ----  三：对外口 /api/claim 与 /api/admin/* 必须 404 ---- #
    server_py = (ROOT / "hosts" / "server.py").read_text(encoding="utf-8")
    # 对外口的 process_request 函数里，只允许 /api/meta 和 /api/instance
    # 不允许 /api/claim 或 /api/admin/
    in_public_handler = False
    for line in server_py.split("\n"):
        if "def process_request" in line:
            in_public_handler = True
        if in_public_handler:
            if line.strip().startswith("return process_request"):
                break
            if "/api/claim" in line and "return" in line:
                check(False, "对外口不应直接路由 /api/claim")
            if "/api/admin/" in line and "return" in line and "json_response" in line:
                check(False, "对外口不应直接路由 /api/admin/*")
    # 正面断言：/api/meta 在对外口存在
    check("/api/meta" in server_py or "/api/instance" in server_py,
          "对外口有 /api/meta 或 /api/instance")
    # 正面断言：/api/claim 只在回环管理口的 do_POST 里
    check('path == "/api/claim"' in server_py,
          "/api/claim 只在回环管理口 AdminRequestHandler.do_POST")

    # ----  四：users_state 读 Key 时写审计 ---- #
    db_path2 = Path(tempfile.mkdtemp(prefix="pubchat-ka-")) / "test.db"
    try:
        store2 = Store(db_path2)
        store2.add_user("testuser", "Test", "fake-key-1", is_admin=1)
        audit_dir = Path(tempfile.mkdtemp(prefix="pubchat-ka-audit-"))
        # 调 users_state 带审计参数
        class FakeHub:
            by_name = {}
            clients = {}
        sa.users_state(store2, FakeHub(), audit_dir, "#1 dev", "127.0.0.1")
        # 检查审计文件
        from datetime import datetime as dt2
        from core.store import CST as _CST2
        audit_file = audit_dir / ("admin-audit-%s.jsonl" % dt2.now(_CST2).strftime("%Y%m"))
        check(audit_file.is_file(), "users_state 产生审计文件")
        audit_lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        check(any("key_list_read" in l for l in audit_lines),
              "审计记录含 key_list_read 动作")
        store2.conn.close()
        import shutil as _sh
        _sh.rmtree(audit_dir, ignore_errors=True)
    finally:
        import shutil as _sh2
        _sh2.rmtree(db_path2.parent, ignore_errors=True)

    # ----  四：前端不把 Key 拼进 URL ---- #
    admin_js = (web / "admin.js").read_text(encoding="utf-8")
    check("scanKey" not in admin_js, "admin.js: 已移除 scanKey 接线（Key 不进 URL）")
    check("&k=" not in admin_js and "key1" not in admin_js.split("renderScan")[0]
          if "renderScan" in admin_js else True,
          "admin.js: renderScan 不拼 key 到 URL")

    print("  Stage 2 安全红线单元测试全部通过")


# --------------------------------------------------------------------------- #
# Stage 3 UI 单元测试
# --------------------------------------------------------------------------- #
def unit_stage3_ui():
    """Stage 3 UI 单元测试：设备条 / 逐条邀请二维码 / 危险操作确认 / 回滚 UI。"""
    web = ROOT / "web"

    # ---- admin.html: 设备条 + 回滚横幅 + 确认弹窗 ---- #
    admin_html = (web / "admin.html").read_text(encoding="utf-8")
    check('id="deviceSection"' in admin_html, "admin.html: 有设备管理区块")
    check('id="devices"' in admin_html, "admin.html: 有设备列表容器")
    check('id="newClaimBtn"' in admin_html, "admin.html: 有生成认领链接按钮")
    check('id="claimResult"' in admin_html, "admin.html: 有认领结果展示区")
    check('id="rollbackBox"' in admin_html, "admin.html: 有回滚横幅容器")
    check('id="confirmOverlay"' in admin_html, "admin.html: 有确认弹窗遮罩")
    check('id="confirmTitle"' in admin_html, "admin.html: 有确认弹窗标题")
    check('id="confirmMsg"' in admin_html, "admin.html: 有确认弹窗内容")
    check('id="confirmOk"' in admin_html, "admin.html: 有确认弹窗确认按钮")
    check('id="confirmCancel"' in admin_html, "admin.html: 有确认弹窗取消按钮")
    check('.device' in admin_html, "admin.html: 有设备卡片样式")
    check('.rollback-banner' in admin_html, "admin.html: 有回滚横幅样式")
    check('.confirm-overlay' in admin_html, "admin.html: 有确认弹窗样式")

    # ---- admin.js: 设备渲染 + 回滚 + 确认函数 ---- #
    admin_js = (web / "admin.js").read_text(encoding="utf-8")
    check('function renderDevices()' in admin_js, "admin.js: 有 renderDevices 函数")
    check('function renderRollback()' in admin_js, "admin.js: 有 renderRollback 函数")
    check('function confirmAction(' in admin_js, "admin.js: 有 confirmAction 函数")
    check('data-revoke=' in admin_js, "admin.js: 有吊销设备按钮处理")
    check('action: \'revoke\'' in admin_js, "admin.js: 调用吊销 API")
    check('action: \'invite\'' in admin_js, "admin.js: 调用生成认领链接 API")
    check('renderDevices()' in admin_js, "admin.js: render() 调用 renderDevices()")
    check('renderRollback()' in admin_js, "admin.js: render() 调用 renderRollback()")
    check('.countdown' in admin_js, "admin.js: 回滚倒计时显示")
    check('setInterval(updateCountdown' in admin_js, "admin.js: 回滚倒计时定时器")
    check('PubChatDevice' in admin_js and 'clear()' in admin_js,
          "admin.js: 吊销自己时清理本地凭证")

    # ---- members.html: 确认弹窗 ---- #
    members_html = (web / "members.html").read_text(encoding="utf-8")
    check('id="confirmOverlay"' in members_html, "members.html: 有确认弹窗遮罩")
    check('.confirm-overlay' in members_html, "members.html: 有确认弹窗样式")

    # ---- members.js: 逐条邀请二维码 + 确认函数 ---- #
    members_js = (web / "members.js").read_text(encoding="utf-8")
    check('function confirmAction(' in members_js, "members.js: 有 confirmAction 函数")
    check('makeQr(inv.url)' in members_js, "members.js: 为每个邀请生成二维码")
    check('data-show-qr=' not in members_js,
          "members.js: 历史邀请不含二维码按钮（token 不存 DB）")
    check('confirmAction(\'删除邀请码\'' in members_js,
          "members.js: 删除邀请码用确认弹窗")
    check('confirmAction(\'拒绝加入\'' in members_js,
          "members.js: 拒绝加入用确认弹窗")
    check('confirmAction(\'重置 Key\'' in members_js,
          "members.js: 重置 Key 用确认弹窗")
    check('confirmAction(\'踢出成员\'' in members_js,
          "members.js: 踢出成员用确认弹窗")
    # 确保不再用原生 confirm()
    check('if (!confirm(' not in members_js,
          "members.js: 不再用原生 confirm()")

    # ---- 批1 H5：席位上限三选一 + 邀请有效期三选一 ---- #
    check('id="invHours"' in members_html and 'data-hours="0"' in members_html
          and 'data-hours="48"' in members_html,
          "members.html: 邀请有效期改成 24/48/永久三选一")
    check('type="number" value="24" min="1" max="720"' not in members_html,
          "members.html: 旧的手填小时数输入框已移除")
    check('.chip.on{' in members_html and '.member .lim{' in members_html,
          "members.html: 自带 chip 与席位计数样式")
    check('var LIMIT_OPTS = [3, 10, 0]' in members_js,
          "members.js: 席位上限档位 3/10/不限")
    check('data-limit-for=' in members_js and "api('/api/admin/member/limit'" in members_js,
          "members.js: 每人一行席位上限 chip 并调管理口")
    check('u.agent_limit' in members_js, "members.js: 读取 users_state 的 agent_limit")
    check("'长期有效'" in members_js, "members.js: 永久邀请显示长期有效")
    idx_html = (web / "index.html").read_text(encoding="utf-8")
    app_js = (web / "app.js").read_text(encoding="utf-8")
    css = (web / "style.css").read_text(encoding="utf-8")
    check('id="roomInviteHours"' in idx_html and 'data-hours="48"' in idx_html
          and 'id="roomInviteTip"' in idx_html,
          "index.html: 房间邀请弹层有有效期三选一与动态文案")
    check('hours: inviteHours' in app_js and 'function setInviteHours(' in app_js,
          "app.js: room_invite 帧带上所选 hours")
    check("type: 'room_invite', room: r.slug, count: n }" not in app_js,
          "app.js: 不再发不带 hours 的 room_invite")
    check('#roomInviteHours .chip' in app_js, "app.js: 绑定有效期 chip 点击")
    check('.chip.on{border-color:var(--accent)' in css,
          "style.css: chip 选中态走 WeUI 品牌绿 token")

    # ---- 服务器模式适配：设备条 / 无管理权 / 全套设置 ---- #
    check('id="devBar"' in admin_html and 'id="devCurrent"' in admin_html,
          "admin.html: 顶部当前设备条")
    check('当前设备不在这里吊销自己' in admin_html,
          "admin.html: 设备条说明不提供自吊销（防自锁）")
    check('id="denyBox"' in admin_html and 'id="denyDetail"' in admin_html,
          "admin.html: 无管理权提示区")
    check('这是别人服务器上的聊天室，你没有管理权' in admin_html
          or '这台浏览器还没有认领管理权限' in admin_html,
          "admin.html: 未认领时显示没有管理权")
    check('id="serverSection"' in admin_html and 'id="srvName"' in admin_html,
          "admin.html: 服务器模式设置区（实例名）")
    check('id="srvRename"' in admin_html and 'id="srvApproval"' in admin_html,
          "admin.html: 改名与加入审批开关")
    check('id="srvMembersLink"' in admin_html and 'href="/members.html?sec=members"' in admin_html,
          "admin.html: 成员管理入口（S1 后带 sec=members，直开对应段）")
    check('data-local-only' in admin_html and 'html.mode-server' in admin_html,
          "admin.html: 本机专属区块在服务器模式隐藏")

    check('function renderServer()' in admin_js,
          "admin.js: 有 renderServer 函数")
    check('renderServer();' in admin_js,
          "admin.js: render() 调用 renderServer()")
    check('function showDeny(' in admin_js and 'function showDeniedOff()' in admin_js,
          "admin.js: 有无管理权显隐函数")
    check("classList.toggle('mode-server'" in admin_js,
          "admin.js: 服务器模式切换页面样式")
    check("res.status === 401" in admin_js and "res.status === 403" in admin_js,
          "admin.js: 401/403 进入无管理权提示")
    check("'/api/admin/instance'" in admin_js,
          "admin.js: 改实例名/审批走 /api/admin/instance")
    check("'/api/admin/agent/mode'" in admin_js and 'class="mode-sel"' in admin_js,
          "admin.js: 每个智能体有接入方式下拉")
    check("state.agent_modes" in admin_js,
          "admin.js: 下拉选项取自 /api/state 的 agent_modes")
    check('.mode-sel' in admin_html and "接入方式只是给智能体看的标签" in admin_html,
          "admin.html: 接入方式样式与释义")
    check("confirmAction('修改实例名'" in admin_js,
          "admin.js: 改名二次确认")
    check("confirmAction('加入审批'" in admin_js,
          "admin.js: 审批开关二次确认")

    check('id="devBar"' in members_html and 'id="devCurrent"' in members_html,
          "members.html: 顶部当前设备条")
    check('id="denyBox"' in members_html and 'id="denyDetail"' in members_html,
          "members.html: 无管理权提示区")
    check('id="rollbackBar"' in members_html and 'id="rollbackBtn"' in members_html,
          "members.html: 回滚横幅与一键回滚")
    check('function renderChrome()' in members_js,
          "members.js: 有 renderChrome 函数")
    check('renderChrome();' in members_js,
          "members.js: render() 调用 renderChrome()")
    check('function showDeny(' in members_js and 'res.status === 401' in members_js,
          "members.js: 401 进入无管理权提示")
    check("'/api/admin/rollback'" in members_js,
          "members.js: 回滚走 /api/admin/rollback")

    claim_html = (web / "claim.html").read_text(encoding="utf-8")
    check('id="goAdmin"' in claim_html and 'href="/admin.html"' in claim_html,
          "claim.html: 认领成功后引导进入管理面板")

    instances_html = (web / "instances.html").read_text(encoding="utf-8")
    check('data-manage' in instances_html and "'/admin.html'" in instances_html,
          "instances.html: 远端实例卡片有管理入口")

    # ---- 房间 v2 UI ---- #
    index_html = (web / "index.html").read_text(encoding="utf-8")
    app_js = (web / "app.js").read_text(encoding="utf-8")
    check('id="newRoom"' in index_html, "index.html: 有创建房间按钮")
    check('id="roomMenuBtn"' in index_html, "index.html: 有房间菜单按钮")
    check('id="roomSheet"' in index_html and 'id="roomInviteSheet"' in index_html,
          "index.html: 有建房/邀请弹窗")
    check('id="roomName"' in index_html and 'id="roomInviteList"' in index_html,
          "index.html: 有房名输入与邀请结果区")
    check('qrcode.js' in index_html, "index.html: 加载二维码脚本")
    check("'room_create'" in app_js or '"room_create"' in app_js,
          "app.js: 发送 room_create")
    check('room_invites' in app_js and 'room_archived' in app_js,
          "app.js: 处理房间邀请/归档回包")
    check('function openRoomMenu(' in app_js and 'function renderRoomInvites(' in app_js,
          "app.js: 有房间菜单与邀请渲染")

    # ---- 微信风主题 ---- #
    tokens_css = (web / "weui-tokens.css").read_text(encoding="utf-8")
    check("--weui-BRAND: #07c160" in tokens_css, "weui-tokens.css: 官方品牌绿")
    check("--pc-bubble-self: #95ec69" in tokens_css, "weui-tokens.css: 自己气泡绿")
    check("--pc-bubble-other: #ffffff" in tokens_css, "weui-tokens.css: 对方气泡白")
    style_css = (web / "style.css").read_text(encoding="utf-8")
    check("var(--weui-BG-0)" in style_css, "style.css: 页面底用 WeUI token")
    check("var(--pc-bubble-self)" in style_css and "var(--pc-bubble-other)" in style_css,
          "style.css: 气泡用聊天 token")
    check("border-bottom-right-radius:var(--pc-bubble-tail)" in style_css,
          "style.css: 自己气泡右下切角")
    check(".new-msg{" in style_css and ".msg-menu{" in style_css,
          "style.css: 新消息胶囊与长按菜单样式")
    check("weui-tokens.css" in index_html, "index.html: 加载 WeUI token")
    check('id="newMsg"' in index_html and 'id="msgMenu"' in index_html,
          "index.html: 新消息胶囊与长按菜单")
    check("function setNewMsg(" in app_js and "function bindMsgMenu(" in app_js,
          "app.js: 智能滚动与长按菜单")
    check("function openMsgMenu(" in app_js and "copyText(" in app_js,
          "app.js: 长按复制/@TA")

    # ---- 手机壳 / 首启引导 ---- #
    m_html = (web / "m.html").read_text(encoding="utf-8")
    check('id="onboarding"' in m_html and 'id="obTitle"' in m_html,
          "m.html: 首启引导弹层")
    check("pubchat.instances.v1" in m_html and "localStorage" in m_html,
          "m.html: 实例存 localStorage")
    check("pubchat-host-state" in m_html and "全部" in m_html,
          "m.html: 跨实例未读聚合")
    check("join.html#t=" in m_html and "claim#b=" in m_html,
          "m.html: 加入与认领入口")
    check("weui-tokens.css" in m_html, "m.html: 使用 WeUI token")
    # 引导正文收进共享的 guide.js（桌面/手机同一份），这里只钉接线与内容口径
    guide_js = (web / "guide.js").read_text(encoding="utf-8")
    check('src="guide.js"' in m_html and "PubChatGuide.steps('mobile')" in m_html,
          "m.html: 引导吃共享文案")
    for gt in ("加入聊天室", "房间与邀请", "智能体接入", "管理这台服务器"):
        check(gt in guide_js, "guide.js: 四步标题含 %s" % gt)
    check("房间默认仅邀请" in guide_js and "3 个未归档房间" in guide_js,
          "guide.js: 建房口径与房间上限")
    check("admin-open.json" in guide_js and "127.0.0.1" in guide_js,
          "guide.js: 认领与管理口口径写对")
    check("值守在线" in guide_js and "离线收件" in guide_js,
          "guide.js: 两种接入模式都点名，不偏一端")

    print("  Stage 3 UI 单元测试全部通过")


# --------------------------------------------------------------------------- #
# Stage 3 真实进程 E2E
# --------------------------------------------------------------------------- #
def parse_bootstrap_from_log(log_text):
    """从服务器启动日志里解析认领链接和 6 位码。"""
    import re
    token = None
    code = None
    for line in log_text.split("\n"):
        # 匹配：管理认领链接（5 分钟内有效，仅一次）：/claim#b=<token>
        m = re.search(r"/claim#b=([A-Za-z0-9_-]+)", line)
        if m:
            token = m.group(1)
        # 匹配：6 位码：123456
        m = re.search(r"6 位码[：:]\s*(\d{6})", line)
        if m:
            code = m.group(1)
    return token, code


async def e2e_stage3_flows():
    """Stage 3 真实进程 E2E：3 条核心流程（临时 DB、不碰生产）。

     补充要求：Stage 3 的 34 条新测试是静态字符串断言，不证明流程能用。
    补一个最小 E2E，三条就够：
    1. 管理端生成新认领链接 → 旧链立即失效（用旧 token 认领必须 403）；
    2. 吊销一台设备 → 它的 Bearer 立即 401，另一台不受影响；
    3. 改房名 → 回滚横幅出现 → 回滚生效且快照被清掉。
    """
    pub = free_port()
    adm = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-s3e2e-"))
    log_path = tmp / "server.log"

    env = dict(os.environ)
    # 日志里混有中文，重定向到文件时默认编码随控制台；统一 UTF-8 便于解析
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    logf = open(log_path, "ab")
    # 用 server.py 而不是 desktop.py，因为只有 server 模式有设备认领流程
    server_target = [sys.executable, "-u", str(ROOT / "hosts" / "server.py")]
    proc = subprocess.Popen(
        server_target + ["--host", "127.0.0.1", "--port", str(pub),
                         "--admin-port", str(adm), "--data-dir", str(tmp),
                         "--web-dir", str(ROOT / "web")],
        cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT)
    proc._logf = logf

    # server 模式需要 X-Forwarded-Proto: https 头（即使是从回环）
    server_headers = {"X-Forwarded-Proto": "https"}

    try:
        check(wait_ready(adm), "Stage3 E2E: 管理口启动")
        st, _, body = req(pub, "/m.html")
        check(st == 200 and b"pubchat.instances.v1" in body,
              "Stage3 E2E: 手机壳 m.html 可访问")
        st, _, body = req(pub, "/weui-tokens.css")
        check(st == 200 and b"--weui-BRAND" in body,
              "Stage3 E2E: WeUI token 可访问")

        # 等日志写出 bootstrap 信息
        time.sleep(1.5)
        log_text = read_text_loose(log_path)
        token1, code1 = parse_bootstrap_from_log(log_text)
        check(token1 is not None and code1 is not None,
              "Stage3 E2E: 从日志解析到初始认领链接和码",
              {"token": token1 is not None, "code": code1 is not None})

        if token1 is None or code1 is None:
            # 无法继续测试
            return

        # ==== 测试 1: 生成新认领链接 → 旧链立即失效 ====
        # 用初始 token 认领第一台设备（限流在子进程里，次数远低于阈值）
        st, obj, _ = req(adm, "/api/claim", "POST",
                         {"token": token1, "code": code1, "name": "Device1"},
                         headers=server_headers)
        check(st == 200 and obj.get("ok"), "Stage3 E2E: 用初始 token 认领成功", obj)
        secret1 = obj.get("secret")
        check(secret1 is not None and len(secret1) > 20, "Stage3 E2E: 拿到 device_secret")

        if secret1 is None:
            return

        # 用 device1 的 Bearer 生成新认领链接
        auth_headers = {**server_headers, "Authorization": "Bearer " + secret1}
        st, obj, _ = req(adm, "/api/admin/devices", "POST",
                         {"action": "invite"},
                         headers=auth_headers)
        check(st == 200 and obj.get("link"), "Stage3 E2E: 生成新认领链接", obj)
        new_link = obj.get("link", "")
        token2 = new_link.split("#b=")[-1] if "#b=" in new_link else None
        code2 = obj.get("code")
        check(token2 is not None and code2 is not None,
              "Stage3 E2E: 解析到新 token 和 code")

        # 再生成一个新链接（这会使 token2 失效）
        st, obj, _ = req(adm, "/api/admin/devices", "POST",
                         {"action": "invite"},
                         headers=auth_headers)
        check(st == 200 and obj.get("link"), "Stage3 E2E: 生成第三个认领链接")
        token3 = obj.get("link", "").split("#b=")[-1]
        code3 = obj.get("code")

        # token2 应该已经失效（因为 generate_bootstrap 会 clear_bootstraps）
        st, obj, _ = req(adm, "/api/claim", "POST",
                         {"token": token2, "code": code2, "name": "Device2"},
                         headers=server_headers)
        check(st == 403, "Stage3 E2E: 旧 token 认领返回 403（链接已失效）", obj)
        check("链接无效" in (obj.get("error") or ""),
              "Stage3 E2E: 403 返回模糊提示", obj)

        # token3 应该还能用
        st, obj, _ = req(adm, "/api/claim", "POST",
                         {"token": token3, "code": code3, "name": "Device2"},
                         headers=server_headers)
        check(st == 200 and obj.get("ok"), "Stage3 E2E: 新 token 认领成功", obj)
        secret2 = obj.get("secret")

        if secret2 is None:
            return

        # ==== 测试 2: 吊销一台设备 → 它的 Bearer 立即 401，另一台不受影响 ====
        auth1_headers = {**server_headers, "Authorization": "Bearer " + secret1}
        auth2_headers = {**server_headers, "Authorization": "Bearer " + secret2}

        # 先验证两台设备都能用 Bearer 访问管理 API
        st, obj, _ = req(adm, "/api/state", headers=auth1_headers)
        check(st == 200, "Stage3 E2E: Device1 Bearer 有效")
        st, obj, _ = req(adm, "/api/state", headers=auth2_headers)
        check(st == 200, "Stage3 E2E: Device2 Bearer 有效")

        # 获取设备列表，找到 Device2 的 id
        st, obj, _ = req(adm, "/api/admin/devices", "POST",
                         {"action": "list"},
                         headers=auth1_headers)
        check(st == 200 and "devices" in obj, "Stage3 E2E: 获取设备列表", obj)
        devices = obj.get("devices", [])
        device2_id = None
        for d in devices:
            if d.get("name") == "Device2":
                device2_id = d.get("id")
                break
        check(device2_id is not None, "Stage3 E2E: 找到 Device2 的 id")

        if device2_id is None:
            return

        # 吊销 Device2
        st, obj, _ = req(adm, "/api/admin/devices", "POST",
                         {"action": "revoke", "id": device2_id},
                         headers=auth1_headers)
        check(st == 200 and obj.get("ok"), "Stage3 E2E: 吊销 Device2", obj)

        # Device2 的 Bearer 应该立即 401
        st, obj, _ = req(adm, "/api/state", headers=auth2_headers)
        check(st == 401, "Stage3 E2E: 吊销后 Device2 Bearer 立即 401", obj)

        # Device1 的 Bearer 应该仍然有效
        st, obj, _ = req(adm, "/api/state", headers=auth1_headers)
        check(st == 200, "Stage3 E2E: Device1 Bearer 不受影响")

        # ==== 测试 3: 改房名 → 回滚横幅出现 → 回滚生效且快照被清掉 ====
        # 先获取当前实例名
        st, obj, _ = req(adm, "/api/state", headers=auth1_headers)
        check(st == 200, "Stage3 E2E: 获取当前状态")
        old_name = obj.get("instance", {}).get("name", "")

        # 改房名
        new_name = "改名测试_" + str(int(time.time()))
        st, obj, _ = req(adm, "/api/admin/instance", "POST",
                         {"name": new_name},
                         headers=auth1_headers)
        check(st == 200 and obj.get("instance", {}).get("name") == new_name,
              "Stage3 E2E: 改房名成功", obj)

        # 回滚横幅应该出现（rollback 有内容）
        st, obj, _ = req(adm, "/api/admin/rollback", "GET",
                         headers=auth1_headers)
        check(st == 200 and obj.get("rollback") is not None,
              "Stage3 E2E: 改名后回滚快照存在", obj)
        rollback_data = obj.get("rollback", {})
        check(rollback_data.get("instance", {}).get("name") == old_name,
              "Stage3 E2E: 回滚快照保存了旧房名", rollback_data)

        # 执行回滚
        st, obj, _ = req(adm, "/api/admin/rollback", "POST",
                         {},
                         headers=auth1_headers)
        check(st == 200 and obj.get("ok"), "Stage3 E2E: 回滚成功", obj)

        # 验证房名已恢复
        st, obj, _ = req(adm, "/api/state", headers=auth1_headers)
        check(st == 200 and obj.get("instance", {}).get("name") == old_name,
              "Stage3 E2E: 回滚后房名恢复", obj)

        # 验证快照已被清掉
        st, obj, _ = req(adm, "/api/admin/rollback", "GET",
                         headers=auth1_headers)
        check(st == 200 and obj.get("rollback") is None,
              "Stage3 E2E: 回滚后快照被清掉", obj)

    finally:
        stop_tree(proc)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("  —— Stage 3 E2E 结束，3 条核心流程全部通过")


async def e2e_rooms_v2():
    """房间 v2 真实进程 E2E：建房 / 邀请进房 / 权限 / 归档 / 限额。"""
    pub = free_port()
    adm = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-rooms-e2e-"))
    log_path = tmp / "server.log"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    logf = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "hosts" / "server.py"),
         "--host", "127.0.0.1", "--port", str(pub), "--admin-port", str(adm),
         "--data-dir", str(tmp), "--web-dir", str(ROOT / "web")],
        cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT)
    proc._logf = logf
    H = {"X-Forwarded-Proto": "https"}
    ws_url = "ws://127.0.0.1:%d/ws" % pub
    A = B = None
    try:
        check(wait_ready(adm), "房间 E2E: 管理口启动")
        time.sleep(1.5)
        token, code = parse_bootstrap_from_log(read_text_loose(log_path))
        st, obj, _ = req(adm, "/api/claim", "POST",
                         {"token": token, "code": code, "name": "Dev"}, headers=H)
        check(st == 200 and obj.get("secret"), "房间 E2E: 认领管理设备", obj)
        secret = obj.get("secret")
        auth = {**H, "Authorization": "Bearer " + secret}
        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "member", "hours": 2},
                         headers=auth)
        check(st == 200 and obj.get("invites"), "房间 E2E: 生成实例邀请", obj)
        inv = obj["invites"][0]["token"]

        async def join(token, name, cid):
            ws = await websockets.connect(ws_url, open_timeout=10)
            await ws.send(json.dumps({"type": "join", "token": token,
                                      "name": name, "client_id": cid}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), 8))
            await ws.close()
            return msg

        j = await join(inv, "Alice", "usera")
        check(j.get("type") == "joined" and j.get("key1"), "房间 E2E: A 加入实例", j)
        key_a = j.get("key1")
        A = WS(ws_url, {"type": "auth", "kind": "user",
                        "username": "usera", "key": key_a})
        await A.open()

        await A.send({"type": "room_create", "title": "周末局"})
        m = await A.next("room_created")
        rid = m.get("room", {}).get("slug")
        check(bool(rid) and m["room"]["title"] == "周末局",
              "房间 E2E: A 建房", m)
        check(any(r["slug"] == rid for r in m.get("rooms", [])),
              "房间 E2E: 建房回包带房间列表")

        await A.send({"type": "room_invite", "room": rid, "count": 1})
        m = await A.next("room_invites")
        room_inv = m["invites"][0]["token"]
        check(m.get("room") == rid, "房间 E2E: 房主生成房间邀请", m)

        j = await join(room_inv, "Bob", "userb")
        check(j.get("type") == "joined" and j.get("key1"),
              "房间 E2E: B 用房间邀请加入", j)
        B = WS(ws_url, {"type": "auth", "kind": "user",
                        "username": "userb", "key": j.get("key1")})
        await B.open()
        await B.send({"type": "rooms"})
        m = await B.next("authenticated")
        check(any(r["slug"] == rid for r in m.get("rooms", [])),
              "房间 E2E: B 自动进房", m)

        await B.send({"type": "room_invite", "room": rid, "count": 1})
        m = await B.next("error")
        check(m.get("code") == "NO_PERM", "房间 E2E: 非房主不能邀请", m)

        await B.send({"type": "message", "room": rid, "content": "hi room"})
        m = await A.next("message", contains="hi room")
        check(m.get("room") == rid, "房间 E2E: 房间内消息可达", m)

        await A.send({"type": "room_archive", "room": rid})
        m = await A.next("room_archived")
        check(m.get("room") == rid, "房间 E2E: 房主归档", m)
        await B.send({"type": "message", "room": rid, "content": "after"})
        m = await B.next("error")
        check(m.get("code") == "NO_PERM", "房间 E2E: 归档后不能发", m)

        # 限额：A 当前 0 个活跃房（刚归档）；再建 3 个，第 4 个拒绝
        for i in range(3):
            await A.send({"type": "room_create", "title": "限额房%d" % i})
            m = await A.next("room_created")
            check(m.get("room"), "房间 E2E: 限额内建房 %d" % i, m)
        await A.send({"type": "room_create", "title": "超额房"})
        m = await A.next("error")
        check(m.get("code") == "ROOM_LIMIT", "房间 E2E: 第 4 个房间被限", m)
    finally:
        if A:
            await A.close()
        if B:
            await B.close()
        stop_tree(proc)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("  —— 房间 v2 E2E 结束，全部通过")


async def e2e_members_08():
    """0.8 批1 真实进程 E2E：成员名单 / 席位上限生效点 / 移出服务器的真断开行为。"""
    from datetime import datetime
    from core.store import CST
    pub = free_port()
    adm = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-members-e2e-"))
    log_path = tmp / "server.log"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    logf = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "hosts" / "server.py"),
         "--host", "127.0.0.1", "--port", str(pub), "--admin-port", str(adm),
         "--data-dir", str(tmp), "--web-dir", str(ROOT / "web")],
        cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT)
    proc._logf = logf
    H = {"X-Forwarded-Proto": "https"}
    ws_url = "ws://127.0.0.1:%d/ws" % pub
    boss = None
    mike_ws = agent_ws = None
    try:
        check(wait_ready(adm), "成员 E2E: 管理口启动")
        time.sleep(1.5)
        token, code = parse_bootstrap_from_log(read_text_loose(log_path))
        st, obj, _ = req(adm, "/api/claim", "POST",
                         {"token": token, "code": code, "name": "Dev"}, headers=H)
        check(st == 200 and obj.get("secret"), "成员 E2E: 认领管理设备", obj)
        auth = {**H, "Authorization": "Bearer " + obj["secret"]}

        st, obj, _ = req(adm, "/api/admin/user", "POST",
                         {"name": "总管", "client_id": "boss"}, headers=auth)
        check(st == 200 and obj["user"]["role"] == "owner",
              "成员 E2E: 首个账号是 owner", obj)
        key_boss = obj["user"]["key1"]

        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "role": "member", "hours": 0},
                         headers=auth)
        check(st == 200 and obj["invites"][0]["expires_at"] is None,
              "成员 E2E: 管理端 hours=0 出永久邀请", obj)
        inv = obj["invites"][0]["token"]
        st, obj, _ = req(adm, "/api/admin/invite", "POST",
                         {"action": "create", "hours": -3}, headers=auth)
        check(st == 400, "成员 E2E: 管理端非法 hours 被拒", obj)

        async def join(token, name, cid):
            ws = await websockets.connect(ws_url, open_timeout=10)
            await ws.send(json.dumps({"type": "join", "token": token,
                                      "name": name, "client_id": cid}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), 8))
            await ws.close()
            return msg

        async def until(ws, want, timeout=8):
            end = time.time() + timeout
            while time.time() < end:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if msg.get("type") == want:
                    return msg
            raise TimeoutError("没等到 %s" % want)

        async def connect_auth(frame):
            ws = await websockets.connect(ws_url, open_timeout=10)
            await ws.send(json.dumps(frame))
            return await until(ws, "authenticated"), ws

        j = await join(inv, "Mike", "mike")
        check(j.get("type") == "joined" and j.get("key1"), "成员 E2E: 永久邀请可加入", j)

        boss = WS(ws_url, {"type": "auth", "kind": "user",
                           "username": "boss", "key": key_boss})
        await boss.open()
        _a, mike_ws = await connect_auth({"type": "auth", "kind": "user",
                                          "username": "mike", "key": j["key1"]})

        st, obj, _ = req(adm, "/api/state", headers=auth)
        keys = {a["client_id"]: a["key2"] for u in obj["users"] for a in u["agents"]}
        _a2, agent_ws = await connect_auth({"type": "auth", "kind": "agent",
                                            "client_id": "mike_a1",
                                            "key": keys["mike_a1"]})

        await boss.send({"type": "member_list"})
        mm = await boss.next("member_list")
        rows = {x["client_id"]: x for x in mm.get("members", [])}
        check(set(rows) == {"boss", "mike"}, "member_list 覆盖两名成员", set(rows))
        check(rows["mike"]["agent_limit"] == 3, "新成员默认上限 3", rows["mike"])
        check(len(rows["mike"]["agents"]) == 3, "名单点名名下三个智能体", rows["mike"])
        check(rows["mike"]["online"] is True, "在线成员标记为真")
        check(any(a["client_id"] == "mike_a1" and a["online"] is True
                  for a in rows["mike"]["agents"]), "已连线的智能体 online=True", rows["mike"])
        check(rows["boss"]["role"] == "owner", "名单带角色")

        await mike_ws.send(json.dumps({"type": "member_list"}))
        e = await until(mike_ws, "error")
        check(e.get("code") == "NO_PERM", "非 owner 的成员名单被拒", e)

        await boss.send({"type": "member_agent_limit", "client_id": "mike", "limit": 99})
        e = await boss.next("error")
        check(e.get("code") == "BAD_LIMIT", "超范围上限被拒", e)
        await boss.send({"type": "member_agent_limit", "client_id": "mike", "limit": 1})
        ack = await boss.next("member_agent_limit_set")
        check(ack.get("limit") == 1, "改上限回 member_agent_limit_set", ack)
        st, obj, _ = req(adm, "/api/state", headers=auth)
        lim = {u["client_id"]: u.get("agent_limit") for u in obj["users"]}
        check(lim["mike"] == 1, "管理端状态里同步上限", lim)

        await mike_ws.send(json.dumps({"type": "agent_create", "name": "第四个"}))
        e = await until(mike_ws, "error")
        check(e.get("code") == "AGENT_LIMIT" and "3/1" in (e.get("message") or ""),
              "到上限后新建席位报 AGENT_LIMIT", e)
        await boss.send({"type": "member_agent_limit", "client_id": "mike", "limit": 0})
        await boss.next("member_agent_limit_set")
        await mike_ws.send(json.dumps({"type": "agent_create", "name": "第四个"}))
        cr = await until(mike_ws, "agent_created")
        check(cr.get("agent", {}).get("client_id") == "mike_a4",
              "0＝不限额后可建第 4 席", cr)

        async def closed_code(ws, timeout=8):
            """读到连接真的断开为止，返回关闭码（中间还可能有几帧在途广播）。"""
            end = time.time() + timeout
            while time.time() < end:
                try:
                    await asyncio.wait_for(ws.recv(), timeout)
                except websockets.exceptions.ConnectionClosed as exc:
                    return exc.code
                except asyncio.TimeoutError:
                    return None
            return None

        await boss.send({"type": "member_remove", "client_id": "mike"})
        k = await until(mike_ws, "kicked")
        check(k.get("reason") == "已被移出服务器", "被删者先收到 kicked", k)
        closed = await closed_code(mike_ws)
        check(closed == 1011, "被删者连接以 1011 断开", closed)
        aclosed = await closed_code(agent_ws)
        check(aclosed == 1011, "其智能体连接一并断开", aclosed)
        rm = await boss.next("member_removed")
        check(rm.get("client_id") == "mike" and len(rm.get("agents", [])) == 4,
              "member_removed 点名连带删除的四个智能体", rm)
        await boss.send({"type": "member_list"})
        mm2 = await boss.next("member_list")
        check(all(x["client_id"] != "mike" for x in mm2["members"]),
              "移出后成员名单瘦身", mm2["members"])
        st, obj, _ = req(adm, "/api/state", headers=auth)
        check(not any(u["client_id"] == "mike" for u in obj["users"]),
              "管理端不再出现被删成员")
        await boss.send({"type": "presence"})
        pl = await boss.next("presence")
        check(not any(x["client_id"].startswith("mike") for x in pl["members"]),
              "presence 里成员与其智能体一并消失", pl["members"])

        await boss.send({"type": "member_remove", "client_id": "boss"})
        e = await boss.next("error")
        check(e.get("code") == "NO_PERM", "owner 不能把自己移出服务器", e)
        await boss.send({"type": "member_remove", "client_id": "ghost"})
        e = await boss.next("error")
        check(e.get("code") == "NOT_FOUND", "删不存在的成员报 NOT_FOUND", e)

        await boss.send({"type": "instance_invite", "count": 1, "hours": 48})
        iv = await boss.next("instance_invites")
        exp = iv["invites"][0]["expires_at"]
        delta = (datetime.fromisoformat(exp) - datetime.now(CST)).total_seconds() / 3600.0
        check(47.0 < delta < 49.0, "WS 邀请 hours=48 落对时间窗", delta)
        await boss.send({"type": "instance_invite", "count": 1, "hours": 7})
        iv2 = await boss.next("instance_invites")
        d2 = (datetime.fromisoformat(iv2["invites"][0]["expires_at"])
              - datetime.now(CST)).total_seconds() / 3600.0
        check(23.0 < d2 < 25.0, "非法档位回落 24 小时", d2)
        j2 = await join(iv2["invites"][0]["token"], "Nina", "nina")
        check(j2.get("type") == "joined", "回落 24 小时的邀请仍可加入", j2)

        st, obj, _ = req(adm, "/api/admin/member/limit", "POST",
                         {"client_id": "nina", "limit": 10}, headers=auth)
        check(st == 200 and obj.get("agent_limit") == 10, "管理口能设成员席位上限", obj)
        st, obj, _ = req(adm, "/api/state", headers=auth)
        nina = [u for u in obj["users"] if u["client_id"] == "nina"][0]
        check(nina.get("agent_limit") == 10, "users_state 把上限透给管理页", nina)
        st, obj, _ = req(adm, "/api/admin/member/limit", "POST",
                         {"client_id": "nina", "limit": 99}, headers=auth)
        check(st == 400 and "0–20" in (obj.get("error") or ""), "上限越界被管理口拒", obj)
        st, _, _ = req(adm, "/api/admin/member/limit", "POST",
                       {"client_id": "ghost", "limit": 3}, headers=auth)
        check(st == 404, "上限改到不存在的成员报 404")
        st, obj, _ = req(adm, "/api/admin/member/limit", "POST",
                         {"client_id": "nina", "limit": "abc"}, headers=auth)
        check(st == 400, "上限非整数被管理口拒")
        st, obj, _ = req(adm, "/api/admin/member/limit", "POST",
                         {"client_id": "nina", "limit": 0}, headers=auth)
        check(st == 200 and obj.get("agent_limit") == 0, "管理口能设 0＝不限", obj)
        await boss.send({"type": "member_list"})
        mm3 = await boss.next("member_list")
        nina_row = [x for x in mm3["members"] if x["client_id"] == "nina"][0]
        check(nina_row["agent_limit"] == 0, "管理口与 WS 成员名单看到同一个上限", nina_row)
    finally:
        if boss:
            await boss.close()
        for ws in (mike_ws, agent_ws):
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
        stop_tree(proc)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("  —— 成员管理 0.8 E2E 结束，全部通过")


async def e2e_agent_dm_08():
    """0.8 批2 真实进程 E2E：adm- 智能体私聊房的开房 / 双端可见 / 越权 / 级联。"""
    pub = free_port()
    adm = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="pubchat-agentdm-e2e-"))
    log_path = tmp / "server.log"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    logf = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "hosts" / "server.py"),
         "--host", "127.0.0.1", "--port", str(pub), "--admin-port", str(adm),
         "--data-dir", str(tmp), "--web-dir", str(ROOT / "web")],
        cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT)
    proc._logf = logf
    H = {"X-Forwarded-Proto": "https"}
    ws_url = "ws://127.0.0.1:%d/ws" % pub

    async def join(inv, name, cid):
        ws = await websockets.connect(ws_url, open_timeout=10)
        await ws.send(json.dumps({"type": "join", "token": inv,
                                  "name": name, "client_id": cid}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), 8))
        await ws.close()
        return msg

    async def closed_code(ws, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            try:
                await asyncio.wait_for(ws.recv(), timeout)
            except websockets.exceptions.ConnectionClosed as exc:
                return exc.code
            except asyncio.TimeoutError:
                return None
        return None

    boss = mike = nina = ba = ma = None
    try:
        check(wait_ready(adm), "智能体私聊 E2E: 管理口启动")
        time.sleep(1.5)
        token, code = parse_bootstrap_from_log(read_text_loose(log_path))
        st, obj, _ = req(adm, "/api/claim", "POST",
                         {"token": token, "code": code, "name": "Dev"}, headers=H)
        check(st == 200 and obj.get("secret"), "智能体私聊 E2E: 认领管理设备", obj)
        auth = {**H, "Authorization": "Bearer " + obj["secret"]}

        st, obj, _ = req(adm, "/api/admin/user", "POST",
                         {"name": "总管", "client_id": "boss"}, headers=auth)
        check(st == 200 and obj["user"]["role"] == "owner",
              "智能体私聊 E2E: 建出 owner 账号", obj)
        key_boss = obj["user"]["key1"]
        boss_a = obj["user"]["agents"][0]["client_id"]
        boss_k = obj["user"]["agents"][0]["key2"]

        invites = []
        for _ in range(2):
            st, obj, _ = req(adm, "/api/admin/invite", "POST",
                             {"action": "create", "role": "member", "hours": 0},
                             headers=auth)
            invites.append(obj["invites"][0]["token"])
        jm = await join(invites[0], "Mike", "mike")
        jn = await join(invites[1], "Nina", "nina")
        check(jm.get("type") == "joined" and jn.get("type") == "joined",
              "智能体私聊 E2E: 两名成员各带席位入网")
        st, obj, _ = req(adm, "/api/state", headers=auth)
        akey = {a["client_id"]: a["key2"]
                for u in obj["users"] for a in u["agents"]}
        mike_a = sorted(c for c in akey if c.startswith("mike_a"))[0]

        boss = WS(ws_url, {"type": "auth", "kind": "user",
                           "username": "boss", "key": key_boss})
        mike = WS(ws_url, {"type": "auth", "kind": "user",
                           "username": "mike", "key": jm["key1"]})
        nina = WS(ws_url, {"type": "auth", "kind": "user",
                           "username": "nina", "key": jn["key1"]})
        ba = WS(ws_url, {"type": "auth", "kind": "agent",
                         "client_id": boss_a, "key": boss_k})
        ma = WS(ws_url, {"type": "auth", "kind": "agent",
                         "client_id": mike_a, "key": akey[mike_a]})
        for w in (boss, mike, nina, ba, ma):
            await w.open()

        room = "adm-%s" % "-".join(sorted([boss_a, mike_a]))

        await ba.send({"type": "agent_dm_open", "client_id": boss_a})
        e = await ba.next("error")
        check(e.get("code") == "AGENT_DM_SELF", "E2E: 不能和自己开私聊", e)
        await ba.send({"type": "agent_dm_open", "client_id": "ghost_a9"})
        e = await ba.next("error")
        check(e.get("code") == "NO_AGENT", "E2E: 对方不存在报 NO_AGENT", e)

        await ba.send({"type": "agent_dm_open", "client_id": mike_a})
        got = await ba.next("agent_dm_opened")
        check(got.get("room") == room and got.get("created") is True
              and set([got.get("a"), got.get("b")]) == {boss_a, mike_a},
              "E2E: 智能体自开房回 agent_dm_opened", got)
        peer = await ma.next("agent_dm_opened")
        check(peer.get("room") == room and "「" in (peer.get("title") or ""),
              "E2E: 对面智能体同步收到开房回执", peer)

        await ba.send({"type": "rooms"})
        mine = await ba.next("authenticated")
        row = [r for r in mine["rooms"] if r["slug"] == room]
        check(len(row) == 1 and row[0]["kind"] == "agent_dm"
              and "×" in row[0]["title"], "E2E: 房间列表出现 agent_dm 分组", row)
        await boss.send({"type": "rooms"})
        his = await boss.next("authenticated")
        check([r for r in his["rooms"] if r["slug"] == room],
              "E2E: 主人也能在列表里看到自己智能体这条协作房")

        await ma.send({"type": "message", "room": room, "content": "批2 私信：收到请回"})
        acked = await ma.next("ack")
        live = await ba.next("message")
        check(acked.get("status") == "ok" and live.get("room") == room
              and live.get("from") == mike_a, "E2E: 私聊消息实时送达对面", live)
        await ba.send({"type": "history", "room": room, "limit": 20})
        h = await ba.next("history")
        check(h.get("room") == room and any(m["content"].startswith("批2 私信")
                                            for m in h["messages"]),
              "E2E: 私聊历史可回读", [m["content"] for m in h.get("messages", [])])

        await ba.send({"type": "room_members", "room": room})
        rm = await ba.next("room_members")
        got_ids = {m["client_id"] for m in rm["members"]}
        check(got_ids == {boss_a, mike_a, "boss", "mike"},
              "E2E: 房内可 @ 名单只有双方与各自主人", got_ids)

        await nina.send({"type": "history", "room": room})
        e = await nina.next("error")
        check(e.get("code") == "NO_PERM", "E2E: 别人家成员读不到私聊历史", e)
        await nina.send({"type": "message", "room": room, "content": "路过的窥探"})
        e = await nina.next("error")
        check(e.get("code") == "NO_PERM", "E2E: 别人家成员发不进私聊房", e)

        await mike.send({"type": "agent_dm_open", "client_id": boss_a})
        e = await mike.next("error")
        check(e.get("code") == "AGENT_DM_AMBIGUOUS",
              "E2E: 主人名下多席位不指明回报歧义", e)
        await mike.send({"type": "agent_dm_open", "client_id": boss_a,
                         "own_client_id": mike_a})
        again = await mike.next("agent_dm_opened")
        check(again.get("room") == room and again.get("created") is False,
              "E2E: 主人代席位开同一间房是幂等的", again)

        await boss.send({"type": "member_remove", "client_id": "mike"})
        k = await mike.next("kicked")
        check(k.get("reason") == "已被移出服务器", "E2E: kicked 断连处理不退化", k)
        check(await closed_code(mike.ws) == 1011, "E2E: 被删成员连接以 1011 断开")
        check(await closed_code(ma.ws) == 1011, "E2E: 其智能体连接一并断开")
        await boss.next("member_removed")
        await ba.send({"type": "rooms"})
        after = await ba.next("authenticated")
        check(not [r for r in after["rooms"] if r["slug"] == room],
              "E2E: 成员被移出后私聊房一并消失",
              [r["slug"] for r in after["rooms"]])
        await ba.send({"type": "history", "room": room})
        e = await ba.next("error")
        check(e.get("code") == "NO_PERM", "E2E: 级联后旧配对不再可达", e)
        await ba.send({"type": "agent_dm_open", "client_id": mike_a})
        e = await ba.next("error")
        check(e.get("code") == "NO_AGENT", "E2E: 被移出成员的智能体不可再开房", e)
    finally:
        for w in (boss, mike, nina, ba, ma):
            if w:
                try:
                    await w.close()
                except Exception:
                    pass
        stop_tree(proc)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("  —— 智能体私聊 0.8 E2E 结束，全部通过")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default="", help="测打包产物，比如 dist\\PubChat.exe")
    a = ap.parse_args()
    if a.exe:
        exe = Path(a.exe)
        if not exe.is_absolute():
            exe = ROOT / exe
        TARGET = [str(exe)]
        print("目标: 打包 exe %s" % exe)
    else:
        TARGET = [sys.executable, str(ROOT / "desktop.py")]
        print("目标: 源码 desktop.py")
    print("== 路径规则 ==")
    unit_paths()
    print("== 设置默认值 ==")
    unit_settings_defaults()
    print("== 实例列表 ==")
    unit_instances()
    print("== 指针错误处理 ==")
    unit_pointer_errors()
    print("== 角色迁移 ==")
    unit_role_migration()
    print("== 房间 v2 ==")
    unit_rooms_v2()
    print("== 房间 v2 WS ==")
    unit_rooms_ws()
    print("== 房间邀请加入 ==")
    unit_room_join()
    print("== 房间管理端 ==")
    unit_rooms_admin()
    print("== 成员管理与席位上限 ==")
    unit_member_admin()
    print("== 邀请有效期 ==")
    unit_invite_hours()
    print("== 智能体私聊 ==")
    unit_agent_dm()
    print("== 手机原生 ==")
    unit_mobile_native()
    print("== 版本号比较 ==")
    unit_version_compare()
    print("== 发布源清单 ==")
    unit_update_check()
    print("== 远端三段自检 ==")
    unit_probe_stages()
    print("== 前端硬约束 ==")
    unit_frontend_locks()
    print("== 快捷方式与开机自启 ==")
    unit_windows_shortcuts()
    print("== 数据目录迁移 ==")
    unit_data_migration()
    print("== Stage 2 安全红线 ==")
    unit_stage2_security()
    print("== Stage 3 UI ==")
    unit_stage3_ui()
    print("== 端到端 ==")
    try:
        asyncio.run(run())
        print("== 数据库导入 ==")
        asyncio.run(run_import())
        print("== 服务器部署模式 ==")
        asyncio.run(run_remote_mode())
        print("== Stage 3 E2E ==")
        asyncio.run(e2e_stage3_flows())
        print("== 房间 v2 E2E ==")
        asyncio.run(e2e_rooms_v2())
        print("== 成员管理 0.8 E2E ==")
        asyncio.run(e2e_members_08())
        print("== 智能体私聊 0.8 E2E ==")
        asyncio.run(e2e_agent_dm_08())
    except Exception:
        traceback.print_exc()
        code = 1
    else:
        code = 0

    print("\n%d 通过，%d 失败" % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print(" - %s %s" % (name, detail))
    if FAIL and LAST_LOG:
        print("\n--- 服务端输出 ---")
        print(LAST_LOG[-3000:].encode("gbk", "replace").decode("gbk"))
    sys.exit(code or (1 if FAIL else 0))
