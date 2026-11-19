#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PubChat 本机宿主的更新通道：拉取式静态版本清单（latest.json）。

设计稿 §7：docs/superpowers/specs/2026-11-08-deploy-modes-about-update-channel-design.md

五条红线（需求方）：
  1. HTTPS-only —— http:// 的发布源一律拒；
  2. TOFU —— 首次配置发布源要确认完整域名，**换域名必须再确认一次**，防 DNS 劫持换包；
  3. 清单里任何一条 url 的域名必须与 manifest 自身域名一致，不一致 **整体拒收**；
     TOFU 只管「换域名」，管不了「同一份清单塞外链」，所以这条落在解析之后；
  4. 只展示、不自动下载、绝不自动执行；
  5. sha256 本轮只作展示与自证（页面显示前 12 位给用户核对），不是软件自己去校验安装。

本模块不写任何状态、不起线程：settings.json 的 update 字段由 hosts/local.py 存取，
定时检查由宿主安排。这样模块可以纯函数式测完所有分支。
"""
import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlsplit

API_VERSION = 1                  # 本客户端认识的清单格式版本
MAX_BODY = 64 * 1024             # 清单上限 64KB，超了当不可用
DEFAULT_TIMEOUT = 8.0
KNOWN_ENDS = ("desktop", "android", "server")
_PRE_RANK = {"alpha": 0, "beta": 1, "rc": 2}


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #
def version_key(v):
    """
    把版本号折成一个可比的排序键，大的更新。

      '0.4.0'      → ((0, 4, 0), 1, 0, 0)      正式版
      '0.4.0-rc1'  → ((0, 4, 0), 0, 2, 1)      预发布排在同版本正式版之前
      ''           → ((0, 0, 0), -1, 0, 0)     空当最旧

    非数字段按数字部分取，取不到算 0，绝不抛异常——清单是外部输入。
    """
    s = str(v or "").strip().lower().lstrip("v")
    if not s:
        return ((), -1, 0, 0)
    base, _, pre = s.partition("-")
    parts = []
    for chunk in base.split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    if not pre:
        return (tuple(parts), 1, 0, 0)
    m = re.match(r"([a-z]+)[-_]?(\d*)", pre)
    kind = m.group(1) if m else pre
    num = int(m.group(2)) if (m and m.group(2)) else 0
    return (tuple(parts), 0, _PRE_RANK.get(kind, 0), num)


def is_newer(latest, current):
    """latest 是否比 current 新。比不了（畸形串）就当不新，宁可提示也不误报。"""
    try:
        return version_key(latest) > version_key(current)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 取清单
# --------------------------------------------------------------------------- #
def host_of(url):
    try:
        return (urlsplit(str(url or "")).netloc or "").lower()
    except ValueError:
        return ""


def is_https(url):
    return (urlsplit(str(url or "")).scheme or "").lower() == "https"


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """只允许同域名重定向。跨域跳转能把 TOFU 绕成空文，所以直接拒。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if host_of(newurl) != host_of(req.get_full_url()):
            raise urllib.error.URLError("发布源跨域重定向，已拒绝")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_reader(url, timeout):
    """真实取一次。https 由 check() 在调用前卡死，这里只负责搬运。"""
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _SameHostRedirect(),
    )
    req = urllib.request.Request(
        str(url), headers={"User-Agent": "PubChat-update-check/1", "Accept": "application/json"}
    )
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("清单过大")
    return body


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def _fail(code, msg, **kw):
    out = {"ok": False, "code": code, "error": msg, "need_confirm": False,
           "has_update": False, "latest": "", "note": "", "manifest": None}
    out.update(kw)
    return out


def _ends_view(manifest):
    """只留展示要用的字段，别把清单里的未知键原样搬给前端。"""
    ends = manifest.get("ends")
    if not isinstance(ends, dict):
        return {}
    view = {}
    for name, item in ends.items():
        if not isinstance(item, dict):
            continue
        row = {}
        for k in ("version", "url", "size", "sha256", "min", "schema"):
            if item.get(k) not in (None, ""):
                row[k] = item[k]
        view[str(name)] = row
    return view


def check(url, current_version, trusted_host="", confirm_host="",
          reader=None, timeout=DEFAULT_TIMEOUT, dismissed_version=""):
    """
    检查一次发布源。返回 dict：

      ok          取到并通过全部校验
      code/error  失败原因（error 是给人看的中文，可直接上 UI）
      need_confirm 需要用户确认域名（首次配置或换域名），此时清单不生效
      host        发布源域名
      manifest    通过校验的清单（含 ends 展示视图、note、released_at、channel）
      has_update  桌面端版本比当前新，且用户没忽略过
      latest      清单上的桌面端版本号

    reader 为测试注入口，签名 reader(url, timeout) -> bytes/str。
    注入口只替换「搬运」，HTTPS-only / TOFU / 外链一致性这些校验全在本函数里，
    测试改不了判定规则。
    """
    url = str(url or "").strip()
    host = host_of(url)
    read = reader or default_reader

    if not url:
        return _fail("no_url", "未配置版本清单地址", host="")
    if not is_https(url):
        return _fail("not_https", "发布源必须使用 HTTPS（http:// 一律拒收）", host=host)
    if not host or "." not in host:
        return _fail("bad_url", "发布源地址不合法，取不到域名", host=host)

    # TOFU：没信任过任何域名 → 要先确认；信任过又换了 → 还要再确认一次
    trusted_host = str(trusted_host or "").strip().lower()
    confirm_host = str(confirm_host or "").strip().lower()
    if trusted_host != host:
        if confirm_host == host:
            pass                          # 用户已经确认过这个域名，放行
        elif not trusted_host:
            return _fail("confirm_first",
                         "首次配置发布源，请确认域名后再生效：%s" % host,
                         host=host, need_confirm=True)
        else:
            return _fail("host_changed",
                         "发布源域名已从 %s 变成 %s，需重新确认后才生效" % (trusted_host, host),
                         host=host, need_confirm=True)

    try:
        body = read(url, timeout)
    except (socket.timeout, TimeoutError):
        return _fail("timeout", "检查超时，发布源没有响应", host=host)
    except urllib.error.HTTPError as exc:
        return _fail("http_error", "发布源返回错误：HTTP %s" % getattr(exc, "code", "?"), host=host)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        text = str(getattr(exc, "reason", exc)) or "连不上发布源"
        if "跨域重定向" in text:
            return _fail("cross_host", "发布源跨域重定向到别的域名，已拒绝", host=host)
        if "过大" in text:
            return _fail("too_large", "清单过大（超过 64KB），已拒绝", host=host)
        return _fail("network", "连不上发布源：%s" % text, host=host)

    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return _fail("bad_json", "清单不是 UTF-8 文本", host=host)
    try:
        manifest = json.loads(body)
    except (ValueError, TypeError):
        return _fail("bad_json", "清单不是合法 JSON", host=host)
    if not isinstance(manifest, dict):
        return _fail("bad_json", "清单顶层不是对象", host=host)

    try:
        api = int(manifest.get("api", 0))
    except (TypeError, ValueError):
        api = 0
    if api != API_VERSION:
        return _fail("newer_api",
                     "发布源格式比本软件新（api=%s），已跳过自动比对，请手动查看发布源"
                     % manifest.get("api"),
                     host=host)

    ends = manifest.get("ends")
    if not isinstance(ends, dict) or not ends:
        return _fail("incomplete", "清单缺 ends 字段，无法使用", host=host)

    # 硬要求：清单里任何一条 url 的域名必须等于 manifest 自身域名
    for name, item in ends.items():
        if not isinstance(item, dict):
            continue
        u = str(item.get("url") or "").strip()
        if not u:
            continue
        if not is_https(u):
            return _fail("outside_url",
                         "清单中含非 HTTPS 下载链接（%s），已整体拒绝" % name, host=host)
        if host_of(u) != host:
            return _fail("outside_url",
                         "清单中含外链（%s 指向 %s，与发布源域名 %s 不一致），已整体拒绝"
                         % (name, host_of(u), host), host=host)

    for name, item in ends.items():
        if isinstance(item, dict) and not str(item.get("version") or "").strip():
            return _fail("incomplete", "清单字段不完整：%s 缺 version" % name, host=host)

    latest = str(((ends.get("desktop") or {}) if isinstance(ends.get("desktop"), dict)
                  else {}).get("version") or "").strip()
    if not latest:
        return _fail("incomplete", "清单缺桌面端版本号", host=host)

    dismissed = str(dismissed_version or "").strip()
    has_update = is_newer(latest, current_version) and dismissed != latest
    return {
        "ok": True,
        "code": "",
        "error": "",
        "need_confirm": False,
        "host": host,
        "has_update": has_update,
        "latest": latest,
        "current": str(current_version or ""),
        "dismissed": dismissed,
        "note": str(manifest.get("note") or "")[:2000],
        "channel": str(manifest.get("channel") or "stable"),
        "released_at": str(manifest.get("released_at") or ""),
        "manifest": {
            "api": api,
            "channel": str(manifest.get("channel") or "stable"),
            "released_at": str(manifest.get("released_at") or ""),
            "note": str(manifest.get("note") or "")[:2000],
            "ends": _ends_view(manifest),
        },
    }


def apply_check(cfg, current_version, confirm_host="", reader=None,
                timeout=DEFAULT_TIMEOUT):
    """
    跑一次检查，并把结论写进 cfg["update"]（就地把要存的字段补齐），返回
    (cfg, 检查结果)。local.py 只管调它再 save_settings()。

    信任域名的落盘规则：只有「本次已带 confirm 并且通过」或
    「原本就信任同一域名」才会写 trusted_host；未确认时清单不生效、也不污染信任值。
    """
    update = dict(cfg.get("update") or {})
    update.setdefault("manifest_url", "")
    update.setdefault("trusted_host", "")
    update.setdefault("last_checked", "")
    update.setdefault("last_result", "")
    update.setdefault("dismissed_version", "")

    url = str(payload_url(update, cfg) or "").strip()
    res = check(url, current_version,
                trusted_host=update.get("trusted_host", ""),
                confirm_host=confirm_host,
                reader=reader, timeout=timeout,
                dismissed_version=update.get("dismissed_version", ""))

    host = res.get("host") or ""
    if res["ok"]:
        update["last_result"] = "ok"
        update["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if host and update.get("trusted_host", "").lower() != host:
            update["trusted_host"] = host          # 只有确认过才会走到这里
        update["manifest"] = res.get("manifest")
        update["has_update"] = bool(res.get("has_update"))
        update["latest_version"] = res.get("latest", "")
    else:
        update["last_result"] = "%s：%s" % (res.get("code") or "error", res.get("error") or "")
        update["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if res.get("need_confirm"):
            update.pop("manifest", None)
            update["has_update"] = False

    cfg["update"] = update
    return cfg, res


def payload_url(update, cfg):
    """清单地址优先取 update.manifest_url，兼容早期写在顶层的情况。"""
    return (update or {}).get("manifest_url") or (cfg or {}).get("manifest_url") or ""


def start_check(cfg, current_version):
    """每日一次的判据：距上次成功检查是否已超过 24 小时。"""
    last = str((cfg.get("update") or {}).get("last_checked") or "").strip()
    if not last:
        return True
    try:
        then = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (datetime.now() - then).total_seconds() >= 24 * 3600
